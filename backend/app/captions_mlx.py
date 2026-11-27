"""The picture describer (SmolVLM, app/captions.py) on Apple Silicon's GPU, with MLX.

The same prompt and pixels as the torch model (transformers' processor makes both),
the same forward pass: SigLIP vision encoder, pixel-shuffle connector, Llama text
model, greedy decoding with a key/value cache. Only the arithmetic runs in MLX, on
the GPU (Metal), in float16. MLX is already a dependency there (app/colbert_mlx.py);
mlx-vlm, which does the same, would add a dozen packages.

The architecture follows transformers' ``modeling_idefics3`` (v5) for one 512 px
tile (no image splitting): 32x32 patches of 16 px, all real (the processor stretches
the picture to the square), pre-norm SigLIP layers with biases and tanh-GELU; the
connector folds 4x4 patches into one token (64 per picture) and projects them into
the text model; the text model is pre-norm with RMSNorm, rotate-half RoPE, grouped
query attention and a SiLU-gated MLP.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

DTYPE = "float16"


class MlxSmolVLM:
    def __init__(self, path: Path, dtype: str = DTYPE) -> None:
        import mlx.core as mx

        config = json.loads((path / "config.json").read_text())
        if config.get("model_type") != "idefics3":
            raise ValueError(f"MLX describer supports Idefics3/SmolVLM, not {config.get('model_type')}")
        text, vision = config["text_config"], config["vision_config"]
        self.image_token = int(config["image_token_id"])
        self.scale_factor = int(config["scale_factor"])
        self.v_heads = int(vision["num_attention_heads"])
        self.v_layers = int(vision["num_hidden_layers"])
        self.v_eps = float(vision.get("layer_norm_eps", 1e-6))
        self.patch = int(vision["patch_size"])
        self.heads = int(text["num_attention_heads"])
        self.kv_heads = int(text["num_key_value_heads"])
        self.head_dim = int(text.get("head_dim") or text["hidden_size"] // self.heads)
        self.layers = int(text["num_hidden_layers"])
        self.eps = float(text["rms_norm_eps"])
        self.theta = float(text.get("rope_theta", 10000.0))
        if text.get("rope_scaling") or vision.get("hidden_act") != "gelu_pytorch_tanh":
            raise ValueError("MLX describer expects plain RoPE and tanh-GELU")
        dtype = getattr(mx, dtype)
        weights = mx.load(str(path / "model.safetensors"))
        self.w = {key.removeprefix("model."): value.astype(dtype) for key, value in weights.items()}
        mx.eval(self.w)
        self.mx = mx

    # Vision -------------------------------------------------------------------------

    def _vision(self, pixels):
        """[3, H, W] pixels in [-1, 1] -> [tokens, text hidden] picture embeddings."""

        mx = self.mx
        w, p = self.w, self.patch
        prefix = "vision_model."
        channels, height, width = pixels.shape
        rows, cols = height // p, width // p
        patches = pixels.reshape(channels, rows, p, cols, p).transpose(1, 3, 0, 2, 4)
        patches = patches.reshape(rows * cols, channels * p * p)
        kernel = w[prefix + "embeddings.patch_embedding.weight"]
        h = patches @ kernel.reshape(kernel.shape[0], -1).T + w[prefix + "embeddings.patch_embedding.bias"]
        h = h + w[prefix + "embeddings.position_embedding.weight"][: rows * cols]
        h = h[None]
        hidden = h.shape[-1]
        head_dim = hidden // self.v_heads
        for layer in range(self.v_layers):
            n = f"{prefix}encoder.layers.{layer}."
            x = mx.fast.layer_norm(h, w[n + "layer_norm1.weight"], w[n + "layer_norm1.bias"], self.v_eps)
            q, k, v = (
                (x @ w[f"{n}self_attn.{name}_proj.weight"].T + w[f"{n}self_attn.{name}_proj.bias"])
                .reshape(1, -1, self.v_heads, head_dim).transpose(0, 2, 1, 3)
                for name in ("q", "k", "v")
            )
            attended = mx.fast.scaled_dot_product_attention(q, k, v, scale=head_dim ** -0.5)
            attended = attended.transpose(0, 2, 1, 3).reshape(1, -1, hidden)
            h = h + attended @ w[n + "self_attn.out_proj.weight"].T + w[n + "self_attn.out_proj.bias"]
            x = mx.fast.layer_norm(h, w[n + "layer_norm2.weight"], w[n + "layer_norm2.bias"], self.v_eps)
            x = _gelu_tanh(mx, x @ w[n + "mlp.fc1.weight"].T + w[n + "mlp.fc1.bias"])
            h = h + x @ w[n + "mlp.fc2.weight"].T + w[n + "mlp.fc2.bias"]
        h = mx.fast.layer_norm(h, w[prefix + "post_layernorm.weight"], w[prefix + "post_layernorm.bias"],
                               self.v_eps)[0]
        # Pixel shuffle (Idefics3Connector.pixel_shuffle): s x s patches -> one token.
        s = self.scale_factor
        h = h.reshape(rows, cols // s, hidden * s).transpose(1, 0, 2)
        h = h.reshape(cols // s, rows // s, hidden * s * s).transpose(1, 0, 2)
        h = h.reshape(-1, hidden * s * s)
        return h @ w["connector.modality_projection.proj.weight"].T

    # Text ---------------------------------------------------------------------------

    def _step(self, h, cache: list, offset: int):
        """Run the text model over ``h`` [1, n, hidden] after ``offset`` cached tokens;
        returns the last token's logits."""

        mx = self.mx
        w = self.w
        n = h.shape[1]
        mask = "causal" if n > 1 else None
        for layer in range(self.layers):
            prefix = f"text_model.layers.{layer}."
            x = mx.fast.rms_norm(h, w[prefix + "input_layernorm.weight"], self.eps)
            q = (x @ w[prefix + "self_attn.q_proj.weight"].T).reshape(1, n, self.heads, self.head_dim)
            k = (x @ w[prefix + "self_attn.k_proj.weight"].T).reshape(1, n, self.kv_heads, self.head_dim)
            v = (x @ w[prefix + "self_attn.v_proj.weight"].T).reshape(1, n, self.kv_heads, self.head_dim)
            q, k, v = (t.transpose(0, 2, 1, 3) for t in (q, k, v))
            q = mx.fast.rope(q, self.head_dim, traditional=False, base=self.theta, scale=1.0, offset=offset)
            k = mx.fast.rope(k, self.head_dim, traditional=False, base=self.theta, scale=1.0, offset=offset)
            if cache[layer] is not None:
                k = mx.concatenate([cache[layer][0], k], axis=2)
                v = mx.concatenate([cache[layer][1], v], axis=2)
            cache[layer] = (k, v)
            attended = mx.fast.scaled_dot_product_attention(
                q, k, v, scale=self.head_dim ** -0.5, mask=mask)
            attended = attended.transpose(0, 2, 1, 3).reshape(1, n, -1)
            h = h + attended @ w[prefix + "self_attn.o_proj.weight"].T
            x = mx.fast.rms_norm(h, w[prefix + "post_attention_layernorm.weight"], self.eps)
            gate = x @ w[prefix + "mlp.gate_proj.weight"].T
            up = x @ w[prefix + "mlp.up_proj.weight"].T
            h = h + ((gate * mx.sigmoid(gate)) * up) @ w[prefix + "mlp.down_proj.weight"].T
        last = mx.fast.rms_norm(h[:, -1], w["text_model.norm.weight"], self.eps)
        return last @ w["lm_head.weight"].T

    def generate(self, input_ids, pixel_values, max_new_tokens: int, eos: set[int]) -> list[int]:
        """Greedy tokens after the prompt (transformers' processor output, numpy)."""

        import numpy as np

        mx = self.mx
        ids = np.asarray(input_ids).reshape(-1)
        dtype = self.w["text_model.embed_tokens.weight"].dtype
        pixels = mx.array(np.asarray(pixel_values, dtype=np.float32).reshape(-1, *pixel_values.shape[-3:])[0])
        picture = self._vision(pixels.astype(dtype))
        h = self.w["text_model.embed_tokens.weight"][mx.array(ids.astype(np.int32))]
        slots = np.flatnonzero(ids == self.image_token)
        if len(slots) != picture.shape[0]:
            raise ValueError(f"{len(slots)} image tokens for {picture.shape[0]} picture embeddings")
        h[mx.array(slots.astype(np.int32))] = picture
        cache: list = [None] * self.layers
        logits = self._step(h[None], cache, 0)
        offset = len(ids)
        out: list[int] = []
        for _ in range(max_new_tokens):
            token = int(mx.argmax(logits, axis=-1).item())
            if token in eos:
                break
            out.append(token)
            h = self.w["text_model.embed_tokens.weight"][mx.array([[token]])]
            logits = self._step(h, cache, offset)
            offset += 1
        return out


def _gelu_tanh(mx, x):
    return 0.5 * x * (1 + mx.tanh(math.sqrt(2 / math.pi) * (x + 0.044715 * x * x * x)))
