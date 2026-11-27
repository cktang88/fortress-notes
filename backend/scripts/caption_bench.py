"""Benchmark small vision-language models as picture describers (docs/picture-descriptions.md).

    uv run --frozen python scripts/caption_bench.py MODEL [--device cpu|mps] [--runtime torch|mlx]
        [--prompt one --prompt short ...] [--images tests/fixtures/captions] [--out result.json]

Describes every picture of the labelled set with each prompt (greedy, one 512 px tile),
then prints seconds per picture, peak memory and the quality, hallucination scores of
tests/caption_eval.py. ``--runtime mlx`` needs mlx-vlm installed (not a dependency:
measured in a separate environment). Models whose processors need torchvision (SmolVLM2,
Qwen3.5) get an equivalent one built from their image processor and tokenizer.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
import caption_eval  # noqa: E402

PROMPTS = {
    "one": ("Describe this image in one sentence.", 40),
    "short": ("Caption this image in a few words.", 24),
    "objects": ("What is in this picture? Answer in one short sentence.", 40),
    "cap": ("<CAPTION>", 40),  # Florence-2's tasks
    "dcap": ("<DETAILED_CAPTION>", 60),
}
SIDE = 512


def peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


class TorchCaptioner:
    def __init__(self, repo: str, device: str) -> None:
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor, AutoTokenizer

        self.torch, self.device, self.repo = torch, device, repo
        self.kind = "florence" if "Florence" in repo else "qwen" if "Qwen" in repo else "smol"
        if self.kind == "florence":
            from transformers import Florence2ForConditionalGeneration as Model
        else:
            Model = AutoModelForImageTextToText
        self.model = Model.from_pretrained(repo, dtype=torch.float32).to(device).eval()
        if "SmolVLM2" in repo:
            # SmolVLM2's processor wants torchvision (video) and num2words: SmolVLM's
            # Idefics3 processor makes the same image prompt.
            from huggingface_hub import hf_hub_download
            from transformers import Idefics3ImageProcessor, Idefics3Processor

            template = json.loads(Path(hf_hub_download(repo, "chat_template.json")).read_text())
            self.processor = Idefics3Processor(
                image_processor=Idefics3ImageProcessor.from_pretrained(repo),
                tokenizer=AutoTokenizer.from_pretrained(repo),
                chat_template=template["chat_template"], image_seq_len=64)
        elif self.kind == "qwen":
            self.processor = _QwenProcessor(repo)
        else:
            self.processor = AutoProcessor.from_pretrained(repo)
        if self.kind == "smol":
            self.processor.image_processor.do_image_splitting = False
            self.processor.image_processor.size = {"longest_edge": SIDE}

    def __call__(self, image, prompt: str, max_new: int) -> str:
        torch = self.torch
        if self.kind == "florence":
            inputs = self.processor(text=prompt, images=image, return_tensors="pt")
        else:
            messages = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": prompt}]}]
            extra = {"enable_thinking": False} if self.kind == "qwen" else {}
            text = self.processor.apply_chat_template(messages, add_generation_prompt=True, **extra)
            inputs = self.processor(text=text, images=[image], return_tensors="pt")
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        with torch.inference_mode():
            out = self.model.generate(**inputs, max_new_tokens=max_new, do_sample=False)
        if self.device == "mps":
            torch.mps.synchronize()
        new = out[0] if self.kind == "florence" else out[0][inputs["input_ids"].shape[1]:]
        return self.processor.decode(new, skip_special_tokens=True).strip()


class _QwenProcessor:
    """Qwen3-VL's image prompt without its processor (which wants torchvision)."""

    def __init__(self, repo: str) -> None:
        from huggingface_hub import hf_hub_download
        from transformers import AutoTokenizer, Qwen2VLImageProcessorPil

        self.tokenizer = AutoTokenizer.from_pretrained(repo)
        self.tokenizer.chat_template = Path(hf_hub_download(repo, "chat_template.jinja")).read_text()
        self.images = Qwen2VLImageProcessorPil.from_pretrained(repo)
        self.images.size = {"longest_edge": SIDE * SIDE, "shortest_edge": 128 * 128}

    def apply_chat_template(self, messages, **kwargs):
        return self.tokenizer.apply_chat_template(messages, tokenize=False, **kwargs)

    def __call__(self, text, images, return_tensors):
        pixels = self.images(images=images, return_tensors="pt")
        tokens = int(pixels["image_grid_thw"][0].prod()) // 4
        encoded = self.tokenizer(text.replace("<|image_pad|>", "<|image_pad|>" * tokens),
                                 return_tensors="pt")
        return {**encoded, **pixels}

    def decode(self, ids, **kwargs):
        return self.tokenizer.decode(ids, **kwargs)


class MlxCaptioner:
    def __init__(self, repo: str) -> None:
        from mlx_vlm import load
        from mlx_vlm.prompt_utils import apply_chat_template

        self.model, self.processor = load(repo)
        image_processor = getattr(self.processor, "image_processor", None)
        if image_processor is not None and hasattr(image_processor, "do_image_splitting"):
            image_processor.do_image_splitting = False
            image_processor.size = {"longest_edge": SIDE}
        self.template = apply_chat_template

    def __call__(self, image, prompt: str, max_new: int) -> str:
        from mlx_vlm import generate

        text = self.template(self.processor, self.model.config, prompt, num_images=1)
        out = generate(self.model, self.processor, text, [image], max_tokens=max_new,
                       temperature=0.0, verbose=False)
        return str(getattr(out, "text", out)).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("model")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--runtime", default="torch", choices=["torch", "mlx"])
    parser.add_argument("--prompt", action="append")
    parser.add_argument("--images", default=str(caption_eval.FIXTURES))
    parser.add_argument("--out")
    parser.add_argument("--threads", type=int, default=0)
    args = parser.parse_args()

    from PIL import Image

    if args.threads:
        import torch

        torch.set_num_threads(args.threads)
    started = time.perf_counter()
    captioner = MlxCaptioner(args.model) if args.runtime == "mlx" else TorchCaptioner(args.model, args.device)
    load_s = time.perf_counter() - started
    paths = sorted(p for p in Path(args.images).iterdir() if p.suffix in {".jpg", ".png"})
    pictures = {}
    for path in paths:
        image = Image.open(path).convert("RGB")
        image.thumbnail((SIDE, SIDE))
        pictures[path.name] = image
    keys = args.prompt or ["one"]
    first = next(iter(pictures.values()))
    captioner(first, *PROMPTS[keys[0]])  # warm-up
    result = {"model": args.model, "runtime": args.runtime, "device": args.device,
              "load_s": round(load_s, 2), "runs": {}}
    for key in keys:
        rows = {}
        for name, image in pictures.items():
            if min(image.size) < 64:
                continue  # app/captions.py never describes these
            t = time.perf_counter()
            try:
                caption = captioner(image, *PROMPTS[key])
            except Exception as exc:  # noqa: BLE001 - a broken model must not stop the table
                caption = f"ERROR {type(exc).__name__}: {exc}"[:300]
            rows[name] = {"caption": caption, "s": round(time.perf_counter() - t, 3)}
            print(f"{key} {name} {rows[name]['s']}s {caption}", flush=True)
        score = caption_eval.score({name: row["caption"] for name, row in rows.items()})
        seconds = [row["s"] for row in rows.values()]
        result["runs"][key] = {
            "median_s": round(statistics.median(seconds), 3),
            "subject_rate": round(score["subject_rate"], 3),
            "hallucination_rate": round(score["hallucination_rate"], 3),
            "rows": rows,
        }
        print(f"RESULT {args.model} {args.runtime}/{args.device} {key}: median "
              f"{statistics.median(seconds):.2f}s subject {score['subject_rate']:.2f} "
              f"hallucination {score['hallucination_rate']:.2f}", flush=True)
    result["peak_rss_mb"] = round(peak_rss_mb())
    print(f"RESULT {args.model} {args.runtime}/{args.device} load {load_s:.1f}s peak {result['peak_rss_mb']} MB")
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=1))


if __name__ == "__main__":
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    main()
