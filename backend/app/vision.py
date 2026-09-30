"""Tiny VLM caption + mini OCR for images, so image content is searchable.

Both models are lazy-loaded and optional: if unavailable, image upload still works,
the extracted text is just empty. Caption uses SmolVLM-256M (smallest VLM, <1GB);
OCR uses RapidOCR (ONNX, no extra torch).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from .config import get_settings


@dataclass
class _Cache:
    vlm = None
    processor = None
    ocr = None
    vlm_failed: bool = False
    ocr_failed: bool = False


_cache = _Cache()
_vlm_load_lock = Lock()


def _get_vlm():
    settings = get_settings()
    if not settings.vision_enabled or not settings.vlm_caption_enabled or _cache.vlm_failed:
        return None, None
    if _cache.vlm is not None:
        return _cache.vlm, _cache.processor
    with _vlm_load_lock:
        if _cache.vlm is not None:
            return _cache.vlm, _cache.processor
        if _cache.vlm_failed:
            return None, None
        try:
            import torch
            from transformers import AutoModelForImageTextToText, AutoProcessor

            processor = AutoProcessor.from_pretrained(settings.vlm_model)
            model = AutoModelForImageTextToText.from_pretrained(settings.vlm_model)
            if torch.cuda.is_available():
                device = "cuda"
            elif torch.backends.mps.is_available():
                device = "mps"
            else:
                device = "cpu"
            model.to(device)
            _cache.processor, _cache.vlm = processor, model
            return model, processor
        except Exception:
            _cache.vlm_failed = True
            return None, None


def _get_ocr():
    if not get_settings().vision_enabled or _cache.ocr_failed:
        return None
    if _cache.ocr is not None:
        return _cache.ocr
    try:
        from rapidocr_onnxruntime import RapidOCR

        _cache.ocr = RapidOCR()
        return _cache.ocr
    except Exception:
        _cache.ocr_failed = True
        return None


def _caption(path: Path) -> str:
    model, processor = _get_vlm()
    if model is None:
        return ""
    try:
        from PIL import Image

        image = Image.open(path).convert("RGB")
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image"},
                    {"type": "text", "text": "Describe this image in one short sentence."},
                ],
            }
        ]
        prompt = processor.apply_chat_template(messages, add_generation_prompt=True)
        inputs = processor(text=prompt, images=[image], return_tensors="pt").to(model.device)
        out = model.generate(**inputs, max_new_tokens=64)
        generated = out if model.config.is_encoder_decoder else out[:, inputs["input_ids"].shape[1]:]
        return processor.batch_decode(generated, skip_special_tokens=True)[0].strip()
    except Exception:
        return ""


def _ocr(path: Path) -> str:
    engine = _get_ocr()
    if engine is None:
        return ""
    try:
        result, _ = engine(str(path))
        if not result:
            return ""
        return "\n".join(line[1] for line in result)
    except Exception:
        return ""


def loaded() -> bool:
    return _cache.ocr is not None or _cache.vlm is not None


def warm() -> None:
    """Load the VLM + OCR models (blocking) — call in a background thread at startup
    so the first image upload isn't a cold start."""
    _get_vlm()
    _get_ocr()


def describe_image(path: Path) -> str:
    """Return combined caption + OCR text for an image (may be empty)."""
    caption = _caption(path)
    ocr = _ocr(path)
    parts = []
    if caption:
        parts.append(caption)
    if ocr:
        parts.append(f"Text in image: {ocr}")
    return "\n".join(parts)
