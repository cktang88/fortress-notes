"""The ColBERT encoder on Apple Silicon's GPU, with MLX.

A drop-in for PyLate's ``ColBERT.encode`` (only what app/embeddings.py uses): the
same tokenization (PyLate's own tokenizer, [Q]/[D] prefixes, lengths), the same
ModernBERT forward pass and linear projection, the same masks (padding; punctuation
skiplist for documents) and normalization, returning CPU float32 torch tensors.
Only the transformer runs in MLX, on the GPU (Metal), by default in float16.

Measured on GitHub's macOS arm64 runners (virtual M1 with a paravirtualized GPU,
where torch MPS is unavailable; scripts/bench_accel.py; CPU = torch, 1 thread as the
app uses there): a query 11-15 ms vs 49-51 ms, a paragraph as query 47-54 ms vs
229-456 ms, 64 paragraphs 1.1-1.3 s vs 6.4-13.5 s (float16); token cosine to the CPU
float32 encodings >= 0.9999, same top-10 ranking for every probe query.

ModernBERT here follows transformers' ``modeling_modernbert`` (v5): pre-norm layers
without biases, GLU MLP with exact GELU, RoPE (rotate-half) with theta 160000 on
global layers (every third, starting with the first) and 10000 on local ones, which
attend within ``sliding_window`` tokens on either side.
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_DTYPE = "float16"


def available() -> bool:
    """MLX is installed and has a Metal GPU (not on Linux, nor macOS without Metal)."""

    try:
        import mlx.core as mx

        return bool(mx.metal.is_available())
    except Exception:  # noqa: BLE001 - not installed, or no GPU
        return False


def model_dir(name: str, revision: str | None = None) -> Path:
    """The local folder holding a model's config and transformer weights at
    ``revision`` (a Hugging Face model is in its cache already: PyLate loaded it)."""

    path = Path(name).expanduser()
    if path.is_dir():
        return path
    from huggingface_hub import hf_hub_download

    hf_hub_download(name, "config.json", revision=revision)
    return Path(hf_hub_download(name, "model.safetensors", revision=revision)).parent


class MlxColBERT:
    """ModernBERT-ColBERT in MLX; tokenizes with ``reference`` (the PyLate model)."""

    def __init__(self, reference, path: Path, dtype: str = DEFAULT_DTYPE) -> None:
        import mlx.core as mx

        self.reference = reference
        self.dtype_name = dtype
        dtype = getattr(mx, dtype)
        config = json.loads((path / "config.json").read_text())
        if config.get("model_type") != "modernbert":
            raise ValueError(f"MLX encoder supports ModernBERT only, not {config.get('model_type')}")
        self.heads = int(config["num_attention_heads"])
        self.hidden = int(config["hidden_size"])
        self.head_dim = self.hidden // self.heads
        self.layers = int(config["num_hidden_layers"])
        self.eps = float(config.get("norm_eps", 1e-5))
        self.global_every = int(config.get("global_attn_every_n_layers", 3))
        self.window = int(config.get("local_attention", 128)) // 2
        self.global_theta = float(config.get("global_rope_theta", 160000.0))
        self.local_theta = float(config.get("local_rope_theta", 10000.0))
        if config.get("attention_bias") or config.get("mlp_bias") or config.get("norm_bias"):
            raise ValueError("MLX encoder expects ModernBERT without biases")
        if config.get("hidden_activation", "gelu") != "gelu":
            raise ValueError("MLX encoder expects GELU")
        weights = mx.load(str(path / "model.safetensors"))
        self.w = {key: value.astype(dtype) for key, value in weights.items()}
        dense = _dense_weight(reference)
        self.dense = mx.array(dense).astype(dtype)
        mx.eval(self.w, self.dense)
        self.mx = mx

    # PyLate's interface (what app/embeddings.py calls) ------------------------------

    def encode(self, texts, *, is_query: bool, batch_size: int = 16, **_kwargs) -> list:
        import torch

        if isinstance(texts, str):
            texts = [texts]
        order = sorted(range(len(texts)), key=lambda i: -len(texts[i]))
        out: list = [None] * len(texts)
        for start in range(0, len(order), batch_size):
            batch = [texts[i] for i in order[start:start + batch_size]]
            for index, embedding in zip(order[start:start + batch_size], self._batch(batch, is_query)):
                out[index] = embedding
        return out

    def tokenize(self, texts, is_query: bool = True, pad: bool = False):
        return self.reference.tokenize(texts, is_query=is_query, pad=pad)

    def eval(self):
        return self

    # ---------------------------------------------------------------------------------

    def _batch(self, texts: list[str], is_query: bool) -> list:
        import numpy as np
        import torch

        features = self.reference.tokenize(texts, is_query=is_query)
        ids = features["input_ids"]
        attention = features["attention_mask"].bool()
        projected = self._forward(ids.numpy().astype(np.int32), attention.numpy())
        if is_query:
            keep = attention
        else:
            keep = torch.logical_and(
                self.reference.skiplist_mask(input_ids=ids, skiplist=self.reference.skiplist),
                attention,
            )
        out = []
        for row in range(len(texts)):
            tokens = torch.from_numpy(projected[row][keep[row].numpy()])
            out.append(torch.nn.functional.normalize(tokens, p=2, dim=1))
        return out

    def _forward(self, ids, attention):
        """Projected token embeddings [batch, length, dim] as float32 numpy."""

        import numpy as np

        mx = self.mx
        import mlx.nn as nn

        w = self.w
        batch, length = ids.shape
        dtype = self.dense.dtype
        h = w["embeddings.tok_embeddings.weight"][mx.array(ids)]
        h = mx.fast.layer_norm(h, w["embeddings.norm.weight"], None, self.eps)
        keys = mx.array(attention)[:, None, None, :]
        negative = mx.array(-np.inf, dtype=dtype)
        zero = mx.array(0.0, dtype=dtype)
        full_mask = mx.where(keys, zero, negative)
        positions = mx.arange(length)
        distance = mx.abs(positions[:, None] - positions[None, :])
        # Padding further than the window from every real token would see no key at all
        # (a NaN row that then poisons everything through later layers): it sees itself.
        local = mx.logical_or(mx.logical_and(keys, (distance <= self.window)[None, None]),
                              (distance == 0)[None, None])
        local_mask = mx.where(local, zero, negative)
        scale = self.head_dim ** -0.5
        for layer in range(self.layers):
            prefix = f"layers.{layer}."
            is_global = layer % self.global_every == 0
            x = h if layer == 0 else mx.fast.layer_norm(h, w[prefix + "attn_norm.weight"], None, self.eps)
            qkv = (x @ w[prefix + "attn.Wqkv.weight"].T).reshape(
                batch, length, 3, self.heads, self.head_dim
            ).transpose(2, 0, 3, 1, 4)
            theta = self.global_theta if is_global else self.local_theta
            q = mx.fast.rope(qkv[0], self.head_dim, traditional=False, base=theta, scale=1.0, offset=0)
            k = mx.fast.rope(qkv[1], self.head_dim, traditional=False, base=theta, scale=1.0, offset=0)
            attended = mx.fast.scaled_dot_product_attention(
                q, k, qkv[2], scale=scale, mask=full_mask if is_global else local_mask
            )
            attended = attended.transpose(0, 2, 1, 3).reshape(batch, length, self.hidden)
            h = h + attended @ w[prefix + "attn.Wo.weight"].T
            x = mx.fast.layer_norm(h, w[prefix + "mlp_norm.weight"], None, self.eps)
            gate_input, gate = mx.split(x @ w[prefix + "mlp.Wi.weight"].T, 2, axis=-1)
            h = h + (nn.gelu(gate_input) * gate) @ w[prefix + "mlp.Wo.weight"].T
        h = mx.fast.layer_norm(h, w["final_norm.weight"], None, self.eps)
        projected = (h @ self.dense.T).astype(mx.float32)
        mx.eval(projected)
        return np.array(projected)


def _dense_weight(reference):
    """The ColBERT projection [dim, hidden] from the PyLate model (module 1, no bias)."""

    dense = reference[1]
    linear = dense.linear
    if linear.bias is not None or getattr(dense, "use_residual", False):
        raise ValueError("MLX encoder expects a bias-free ColBERT projection")
    return linear.weight.detach().float().cpu().numpy()


def load(
    reference, model_name: str, dtype: str = DEFAULT_DTYPE, revision: str | None = None
) -> MlxColBERT:
    return MlxColBERT(reference, model_dir(model_name, revision), dtype)
