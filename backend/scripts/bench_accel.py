"""Benchmark the local models on Apple Silicon accelerators against the CPU.

Compares, each configuration in its own process (clean first-call and memory numbers):

* OCR (RapidOCR, PP-OCRv6 small det/rec + cls, ONNX): onnxruntime's CPU provider vs
  its CoreML provider (MLProgram / NeuralNetwork; ALL, CPU_AND_GPU, CPU_AND_NE) on
  generated screenshots and printouts. Reports init, first call (CoreML compiles),
  median per image, how many graph nodes CoreML took, and text equality / word
  recall against the ground truth and the CPU's output.
* ColBERT (lightonai/Agent-ModernColBERT via PyLate): CPU vs torch MPS
  (fp32/fp16/bf16) vs MLX (the app's encoder, app/colbert_mlx.py), for
  one query encode, one paragraph-as-query encode and a 64-paragraph batch. Reports
  token cosine and ranking agreement against CPU float32.

Usage (from ``backend/``; downloads the ColBERT model from the Hugging Face hub)::

    uv run --frozen python scripts/bench_accel.py all --out /tmp/accel
    uv run --frozen python scripts/bench_accel.py ocr cpu        # one configuration
    uv run --frozen python scripts/bench_accel.py colbert mps_fp16

Writes ``results.json`` and ``summary.md`` into ``--out``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import re
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import COLBERT_MODEL, COLBERT_REVISION  # noqa: E402

# --- shared helpers -----------------------------------------------------------------------


def _peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # bytes on macOS, KiB on Linux
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 2) if values else float("nan")


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 2)


WORDS = (
    "invoice meeting project budget review deadline quarter revenue customer design "
    "report schedule update release version server database network backup storage "
    "account payment shipping order total amount balance transfer summary agenda "
    "notes action owner status pending complete draft final approve reject comment "
    "search index paragraph related question answer model latency memory thread "
    "window button settings profile message channel calendar event reminder travel "
    "flight hotel booking receipt tax refund contract policy security password login"
).split()


def _rng_words(rng: random.Random, n: int) -> list[str]:
    return [rng.choice(WORDS) for _ in range(n)]


# --- test images --------------------------------------------------------------------------


def make_images() -> list[tuple[str, object, list[str]]]:
    """Deterministic screenshots and printouts: (name, PIL image, ground-truth words)."""

    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    rng = random.Random(1234)

    def font(size: int):
        return ImageFont.load_default(size=size)

    images = []

    # 1. Light UI screenshot: sidebar, title, paragraphs, buttons.
    img = Image.new("RGB", (1440, 900), (250, 250, 252))
    d = ImageDraw.Draw(img)
    truth: list[str] = []
    d.rectangle((0, 0, 260, 900), fill=(236, 238, 242))
    for i in range(14):
        words = _rng_words(rng, 2)
        d.text((24, 30 + i * 44), " ".join(words).title(), fill=(40, 40, 50), font=font(20))
        truth += words
    title = _rng_words(rng, 4)
    d.text((300, 30), " ".join(title).title(), fill=(10, 10, 20), font=font(40))
    truth += title
    for i in range(16):
        words = _rng_words(rng, 12)
        d.text((300, 110 + i * 34), " ".join(words), fill=(30, 30, 30), font=font(22))
        truth += words
    for i, label in enumerate(["Cancel", "Save", "Share"]):
        d.rounded_rectangle((300 + i * 160, 820, 440 + i * 160, 864), 10, fill=(0, 110, 230))
        d.text((330 + i * 160, 830), label, fill=(255, 255, 255), font=font(22))
        truth.append(label.lower())
    images.append(("ui_light_1440x900", img, truth))

    # 2. Dark screenshot, smaller text.
    img = Image.new("RGB", (1280, 800), (30, 30, 34))
    d = ImageDraw.Draw(img)
    truth = []
    for i in range(24):
        words = _rng_words(rng, 10)
        d.text((40, 24 + i * 31), f"{i + 1:02d}  " + " ".join(words), fill=(220, 220, 225),
               font=font(19))
        truth += words
    images.append(("ui_dark_1280x800", img, truth))

    # 3. Scanned printout (A4 at ~200 dpi), slight blur, noise and tilt.
    img = Image.new("RGB", (1654, 2339), (255, 255, 255))
    d = ImageDraw.Draw(img)
    truth = []
    for i in range(44):
        words = _rng_words(rng, 11)
        d.text((140, 160 + i * 46), " ".join(words), fill=(20, 20, 20), font=font(34))
        truth += words
    img = img.rotate(0.4, resample=Image.BICUBIC, fillcolor=(255, 255, 255))
    img = img.filter(ImageFilter.GaussianBlur(0.4))
    noise = Image.effect_noise(img.size, 10).convert("RGB")
    img = Image.blend(img, noise, 0.03)
    images.append(("printout_a4_scan", img, truth))

    # 4. Receipt / table with numbers.
    img = Image.new("RGB", (900, 1300), (252, 250, 244))
    d = ImageDraw.Draw(img)
    truth = []
    for i in range(28):
        words = _rng_words(rng, 2)
        amount = f"{rng.randint(1, 999)}.{rng.randint(0, 99):02d}"
        d.text((60, 60 + i * 42), " ".join(words), fill=(25, 25, 25), font=font(26))
        d.text((700, 60 + i * 42), amount, fill=(25, 25, 25), font=font(26))
        truth += words + [amount]
    images.append(("receipt_table", img, truth))

    # 5. Phone screenshot: tall, chat bubbles.
    img = Image.new("RGB", (1170, 2532), (255, 255, 255))
    d = ImageDraw.Draw(img)
    truth = []
    y = 200
    for i in range(18):
        words = _rng_words(rng, 6)
        x = 60 if i % 2 == 0 else 420
        fill = (233, 233, 235) if i % 2 == 0 else (10, 132, 255)
        color = (0, 0, 0) if i % 2 == 0 else (255, 255, 255)
        d.rounded_rectangle((x, y, x + 690, y + 90), 30, fill=fill)
        d.text((x + 30, y + 25), " ".join(words), fill=color, font=font(36))
        truth += words
        y += 125
    images.append(("phone_chat_1170x2532", img, truth))

    # 6. A small single-line crop.
    img = Image.new("RGB", (640, 110), (255, 255, 255))
    d = ImageDraw.Draw(img)
    words = _rng_words(rng, 5)
    d.text((20, 30), " ".join(words), fill=(0, 0, 0), font=font(40))
    images.append(("snippet_640x110", img, words))
    return images


def _norm_tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text.lower())


def _recall(text: str, truth: list[str]) -> float:
    from collections import Counter

    got = Counter(_norm_tokens(text))
    want = Counter(_norm_tokens(" ".join(truth)))
    hit = sum(min(count, got[word]) for word, count in want.items())
    return round(hit / max(1, sum(want.values())), 4)


# --- OCR ----------------------------------------------------------------------------------

OCR_CONFIGS = {
    "cpu": None,
    "cpu_allcores": None,
    "coreml_mlprogram_all": {"ModelFormat": "MLProgram", "MLComputeUnits": "ALL"},
    "coreml_mlprogram_gpu": {"ModelFormat": "MLProgram", "MLComputeUnits": "CPUAndGPU"},
    "coreml_mlprogram_ne": {"ModelFormat": "MLProgram", "MLComputeUnits": "CPUAndNeuralEngine"},
    "coreml_nn_all": {"ModelFormat": "NeuralNetwork", "MLComputeUnits": "ALL"},
    "coreml_mlprogram_all_static": {
        "ModelFormat": "MLProgram", "MLComputeUnits": "ALL", "RequireStaticInputShapes": "1",
    },
}
# "<config>+cached": the same configuration in a new process reusing the first one's
# compiled-model cache (what every app start after the first sees).


def _coreml_options(options: dict, cache_dir: str) -> dict:
    full = {
        "ModelFormat": "MLProgram",
        "MLComputeUnits": "ALL",
        "RequireStaticInputShapes": "0",
        "EnableOnSubgraphs": "0",
        "SpecializationStrategy": "FastPrediction",
        "ModelCacheDirectory": cache_dir,
    }
    full.update(options)
    return full


def _capture_fd2(fn):
    """Run fn() with the C-level stderr captured (onnxruntime logs there)."""

    with tempfile.TemporaryFile(mode="w+b") as sink:
        sys.stderr.flush()
        saved = os.dup(2)
        os.dup2(sink.fileno(), 2)
        try:
            result = fn()
        finally:
            sys.stderr.flush()
            os.dup2(saved, 2)
            os.close(saved)
        sink.seek(0)
        return result, sink.read().decode("utf-8", "replace")


def _model_files() -> dict[str, Path]:
    import rapidocr

    models = Path(rapidocr.__file__).parent / "models"
    return {
        "det": models / "PP-OCRv6_det_small.onnx",
        "rec": models / "PP-OCRv6_rec_small.onnx",
        "cls": models / "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    }


def _raw_sessions(options: dict | None, cache_dir: str, threads: int) -> dict:
    """Per-model sessions: partitioning, compile time and fixed-shape latencies,
    including how much each *new* input shape costs (dynamic shapes)."""

    import numpy as np
    import onnxruntime as ort

    out = {}
    shapes = {
        "det": [(1, 3, 960, 736), (1, 3, 1280, 1280), (1, 3, 640, 1280), (1, 3, 960, 736)],
        "rec": [(6, 3, 48, 320), (6, 3, 48, 480), (1, 3, 48, 1024), (6, 3, 48, 320)],
        "cls": [(6, 3, 48, 192), (6, 3, 48, 192)],
    }
    for name, path in _model_files().items():
        so = ort.SessionOptions()
        so.log_severity_level = 1
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        providers = (["CPUExecutionProvider"] if options is None else
                     [("CoreMLExecutionProvider", _coreml_options(options, cache_dir)),
                      "CPUExecutionProvider"])
        started = time.perf_counter()
        try:
            session, log = _capture_fd2(
                lambda: ort.InferenceSession(str(path), sess_options=so, providers=providers))
        except Exception as error:  # noqa: BLE001
            out[name] = {"error": f"{type(error).__name__}: {error}"[:400]}
            continue
        init = time.perf_counter() - started
        partition = [line.strip() for line in log.splitlines()
                     if "number of nodes" in line or "partitions supported" in line]
        input_name = session.get_inputs()[0].name
        runs = []
        for shape in shapes[name]:
            x = np.random.default_rng(0).random(shape, dtype=np.float32)
            times = []
            for _ in range(4):
                t = time.perf_counter()
                try:
                    session.run(None, {input_name: x})
                except Exception as error:  # noqa: BLE001
                    times = [float("nan")]
                    runs.append({"shape": shape, "error": str(error)[:300]})
                    break
                times.append(time.perf_counter() - t)
            else:
                runs.append({"shape": "x".join(map(str, shape)), "first_ms": _ms(times[0]),
                             "median_ms": _ms(statistics.median(times[1:]))})
        out[name] = {"providers": session.get_providers(), "init_ms": _ms(init),
                     "partition": partition[-2:], "runs": runs}
    return out


def run_ocr(config: str, reps: int, out_dir: Path) -> dict:
    from app.config import Settings
    from rapidocr import ModelType, OCRVersion, RapidOCR

    import numpy as np
    import onnxruntime as ort

    base = config.split("+")[0]
    options = OCR_CONFIGS[base]
    threads = os.cpu_count() if base == "cpu_allcores" else Settings.default_threads(0)
    cache_root = (out_dir / "coreml-cache" / base).resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_dir = str(cache_root / "raw")
    result: dict = {"config": config, "threads": threads,
                    "available_providers": ort.get_available_providers()}
    if options is not None and "CoreMLExecutionProvider" not in ort.get_available_providers():
        result["error"] = "CoreMLExecutionProvider not available"
        return result
    result["raw"] = _raw_sessions(options, cache_dir, threads)

    params = {
        "Global.log_level": "error",
        "Det.ocr_version": OCRVersion.PPOCRV6,
        "Det.model_type": ModelType.SMALL,
        "Rec.ocr_version": OCRVersion.PPOCRV6,
        "Rec.model_type": ModelType.SMALL,
        "Det.limit_type": "max",
        "Det.limit_side_len": 1280,
        "EngineConfig.onnxruntime.intra_op_num_threads": threads,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
    }
    if options is not None:
        params["EngineConfig.onnxruntime.use_coreml"] = True
        for key, value in _coreml_options(options, str(cache_root / "e2e")).items():
            params[f"EngineConfig.onnxruntime.coreml_ep_cfg.{key}"] = value
    from app import ocr as app_ocr

    started = time.perf_counter()
    engine = RapidOCR(params=params)
    result["init_ms"] = _ms(time.perf_counter() - started)
    images = make_images()
    per_image = {}
    for index, (name, image, truth) in enumerate(images):
        array = np.asarray(image.convert("RGB"))[:, :, ::-1]
        times, det, rec = [], [], []
        text = ""
        for rep in range(reps + 1):
            t = time.perf_counter()
            output = engine(array)
            elapsed = time.perf_counter() - t
            text = (app_ocr._lines(output.boxes, output.txts)
                    if output.boxes is not None and output.txts else "")
            if rep == 0:
                first = elapsed
                continue
            times.append(elapsed)
            el = list(output.elapse_list or [])
            if len(el) >= 3:
                det.append(el[0] or 0.0)
                rec.append(el[2] or 0.0)
        per_image[name] = {
            "first_ms": _ms(first),
            "median_ms": _ms(statistics.median(times)),
            "det_ms": _ms(statistics.median(det)) if det else None,
            "rec_ms": _ms(statistics.median(rec)) if rec else None,
            "recall": _recall(text, truth),
            "text_sha": hashlib.sha1(text.encode()).hexdigest()[:12],
            "text": text,
            "size": "x".join(map(str, image.size)),
        }
        if index == 0:
            result["first_call_ms"] = _ms(first)
    result["images"] = per_image
    result["total_median_ms"] = round(sum(v["median_ms"] for v in per_image.values()), 1)
    result["mean_recall"] = round(statistics.mean(v["recall"] for v in per_image.values()), 4)
    try:
        engine_providers = {
            part: getattr(engine, attr).session.session.get_providers()
            for part, attr in (("det", "text_det"), ("cls", "text_cls"), ("rec", "text_rec"))
            if getattr(engine, attr, None) is not None
        }
        result["engine_providers"] = engine_providers
    except Exception as error:  # noqa: BLE001
        result["engine_providers"] = f"{type(error).__name__}: {error}"
    result["peak_rss_mb"] = round(_peak_rss_mb(), 1)
    return result


# --- ColBERT ------------------------------------------------------------------------------

COLBERT_CONFIGS = [
    "cpu_fp32", "cpu_fp32_allcores", "mps_fp32", "mps_fp16", "mps_bf16",
    "mlx_fp32", "mlx_fp16", "mlx_bf16",
]

QUERIES = [
    "quarterly budget review",
    "how do I reset my password",
    "flight and hotel booking for the conference",
    "notes from the design meeting about search latency",
    "invoice payment pending",
    "database backup schedule",
    "who owns the release checklist",
    "related paragraphs about memory usage",
    "tax refund receipt",
    "security policy for login",
]


def make_paragraphs(count: int = 64) -> list[str]:
    rng = random.Random(42)
    out = []
    for i in range(count):
        n = rng.choice([12, 25, 40, 60, 90, 140, 220])
        words = _rng_words(rng, n)
        sentences, step = [], rng.randint(7, 14)
        for start in range(0, n, step):
            chunk = words[start:start + step]
            sentences.append(" ".join(chunk).capitalize() + ".")
        out.append(f"Paragraph {i}: " + " ".join(sentences))
    return out


def _sync(device: str) -> None:
    import torch

    if device == "mps":
        torch.mps.synchronize()


def run_colbert(config: str, reps: int, out_dir: Path) -> dict:
    import numpy as np
    import torch

    from app.config import Settings

    backend, precision = config.split("_", 1)
    result: dict = {"config": config, "torch": torch.__version__}
    threads = os.cpu_count() if precision.endswith("allcores") else Settings.default_threads(0)
    precision = precision.replace("_allcores", "")
    torch.set_num_threads(threads)
    result["threads"] = threads
    dtype = {"fp32": torch.float32, "fp16": torch.float16, "bf16": torch.bfloat16}[precision]
    paragraphs = make_paragraphs()
    para_query = paragraphs[5]

    from pylate import models

    if backend == "mlx":
        return _run_mlx(result, precision, paragraphs, para_query, reps, out_dir)

    device = backend
    if device == "mps" and not torch.backends.mps.is_available():
        result["error"] = "MPS not available"
        return result
    started = time.perf_counter()
    try:
        model = models.ColBERT(
            model_name_or_path=COLBERT_MODEL, revision=COLBERT_REVISION, device=device
        )
        model.eval()
        if dtype != torch.float32:
            model = model.to(dtype)
    except Exception as error:  # noqa: BLE001
        result["error"] = f"load: {type(error).__name__}: {error}"[:500]
        return result
    result["load_ms"] = _ms(time.perf_counter() - started)
    result["device"] = str(next(model.parameters()).device)

    def encode(texts, is_query, batch_size=16):
        with torch.inference_mode():
            embs = model.encode(texts, is_query=is_query, batch_size=batch_size,
                                show_progress_bar=False, convert_to_numpy=False)
            out = [e.detach().to("cpu", torch.float32) for e in embs]
        _sync(device)
        return out

    docs, queries = _time_encoder(encode, result, paragraphs, para_query, reps)
    if docs is None:
        return result
    if device == "mps":
        result["mps_driver_mb"] = round(torch.mps.driver_allocated_memory() / 2**20, 1)
    result["peak_rss_mb"] = round(_peak_rss_mb(), 1)
    _save_embeddings(out_dir / f"colbert_{config}.npz", queries, docs)
    return result


def _time_encoder(encode, result, paragraphs, para_query, reps):
    """First-call, query, paragraph-as-query and 64-paragraph timings into ``result``;
    returns (document encodings, query encodings) or (None, None) on an error."""

    try:
        t = time.perf_counter()
        encode(["warm up"], True)
        result["first_query_ms"] = _ms(time.perf_counter() - t)
        t = time.perf_counter()
        encode(["warm up the documents"], False)
        result["first_doc_ms"] = _ms(time.perf_counter() - t)
        times = []
        for _ in range(max(3, reps)):
            for query in QUERIES:
                t = time.perf_counter()
                encode([query], True)
                times.append(time.perf_counter() - t)
        result["query_ms"] = _ms(statistics.median(times))
        times = []
        for _ in range(max(5, reps * 2)):
            t = time.perf_counter()
            encode([para_query], True)
            times.append(time.perf_counter() - t)
        result["paragraph_query_ms"] = _ms(statistics.median(times))
        docs = None
        for batch_size in (16, 64):
            times = []
            for rep in range(reps + 1):
                t = time.perf_counter()
                docs = encode(paragraphs, False, batch_size)
                if rep:
                    times.append(time.perf_counter() - t)
                else:
                    result[f"batch64_bs{batch_size}_first_ms"] = _ms(time.perf_counter() - t)
            result[f"batch64_bs{batch_size}_ms"] = _ms(statistics.median(times))
        return docs, encode(QUERIES + [para_query], True)
    except Exception as error:  # noqa: BLE001
        result["error"] = f"encode: {type(error).__name__}: {error}"[:500]
        return None, None


def _save_embeddings(path: Path, queries, docs) -> None:
    import numpy as np

    arrays = {f"q{i}": np.asarray(q, dtype=np.float32) for i, q in enumerate(queries)}
    arrays.update({f"d{i}": np.asarray(d, dtype=np.float32) for i, d in enumerate(docs)})
    np.savez(path, **arrays)


def _run_mlx(result, precision, paragraphs, para_query, reps, out_dir) -> dict:
    """The app's own MLX encoder (app/colbert_mlx.py)."""

    from pylate import models

    from app import colbert_mlx

    try:
        import mlx.core as mx

        result["mlx_device"] = str(mx.default_device())
        result["metal"] = bool(mx.metal.is_available())
    except Exception as error:  # noqa: BLE001
        result["error"] = f"mlx import: {type(error).__name__}: {error}"[:300]
        return result
    started = time.perf_counter()
    try:
        ref = models.ColBERT(
            model_name_or_path=COLBERT_MODEL, revision=COLBERT_REVISION, device="cpu"
        )
        ref.eval()
        name = {"fp32": "float32", "fp16": "float16", "bf16": "bfloat16"}[precision]
        model = colbert_mlx.load(ref, COLBERT_MODEL, name, COLBERT_REVISION)
    except Exception as error:  # noqa: BLE001
        result["error"] = f"mlx load: {type(error).__name__}: {error}"[:500]
        return result
    result["load_ms"] = _ms(time.perf_counter() - started)

    def encode(texts, is_query, batch_size=16):
        return model.encode(texts, is_query=is_query, batch_size=batch_size)

    docs, queries = _time_encoder(encode, result, paragraphs, para_query, reps)
    if docs is None:
        return result
    try:
        result["mlx_peak_mb"] = round(mx.get_peak_memory() / 2**20, 1)
    except Exception:  # noqa: BLE001
        pass
    result["peak_rss_mb"] = round(_peak_rss_mb(), 1)
    _save_embeddings(out_dir / f"colbert_{result['config']}.npz", queries, docs)
    return result


def compare_embeddings(out_dir: Path, config: str, reference: str = "cpu_fp32") -> dict | None:
    """Token cosine and ranking agreement of ``config`` against ``reference``."""

    import numpy as np

    ref_path, path = out_dir / f"colbert_{reference}.npz", out_dir / f"colbert_{config}.npz"
    if not ref_path.exists() or not path.exists():
        return None
    ref, got = np.load(ref_path), np.load(path)
    cosines, mismatched = [], 0
    for key in ref.files:
        a, b = ref[key], got[key]
        if a.shape != b.shape:
            mismatched += 1
            continue
        num = (a * b).sum(-1)
        den = np.linalg.norm(a, axis=-1) * np.linalg.norm(b, axis=-1) + 1e-12
        cosines.extend((num / den).tolist())
    q_keys = sorted((k for k in ref.files if k.startswith("q")), key=lambda k: int(k[1:]))
    d_keys = sorted((k for k in ref.files if k.startswith("d")), key=lambda k: int(k[1:]))

    def maxsim(store, q):
        return [float((store[q] @ store[d].T).max(axis=1).sum()) for d in d_keys]

    top1 = top10 = 0
    max_score_rel = 0.0
    for q in q_keys:
        a, b = maxsim(ref, q), maxsim(got, q)
        order_a = sorted(range(len(a)), key=lambda i: -a[i])
        order_b = sorted(range(len(b)), key=lambda i: -b[i])
        top1 += order_a[0] == order_b[0]
        top10 += order_a[:10] == order_b[:10]
        max_score_rel = max(max_score_rel, max(abs(x - y) / max(abs(x), 1e-6) for x, y in zip(a, b)))
    return {
        "cos_mean": round(float(np.mean(cosines)), 5) if cosines else None,
        "cos_min": round(float(np.min(cosines)), 5) if cosines else None,
        "shape_mismatch": mismatched,
        "top1_same": f"{top1}/{len(q_keys)}",
        "top10_order_same": f"{top10}/{len(q_keys)}",
        "max_score_rel_diff": round(max_score_rel, 5),
    }


# --- environment and app probe ------------------------------------------------------------


def environment() -> dict:
    info: dict = {"platform": platform.platform(), "machine": platform.machine(),
                  "python": sys.version.split()[0], "cpu_count": os.cpu_count()}
    if sys.platform == "darwin":
        for key in ("machdep.cpu.brand_string", "hw.memsize", "hw.perflevel0.physicalcpu",
                    "hw.perflevel1.physicalcpu", "kern.hv_vmm_present"):
            try:
                info[key] = subprocess.run(["sysctl", "-n", key], capture_output=True,
                                           text=True, timeout=10).stdout.strip()
            except Exception:  # noqa: BLE001
                pass
        try:
            gpu = subprocess.run(["system_profiler", "SPDisplaysDataType"], capture_output=True,
                                 text=True, timeout=60).stdout
            info["gpu"] = " | ".join(line.strip() for line in gpu.splitlines()
                                     if any(k in line for k in ("Chipset", "Metal", "Cores", "Vendor")))
        except Exception:  # noqa: BLE001
            pass
    try:
        import onnxruntime as ort

        info["onnxruntime"] = ort.__version__
        info["ort_providers"] = ort.get_available_providers()
    except Exception as error:  # noqa: BLE001
        info["onnxruntime"] = str(error)
    try:
        import torch

        info["torch"] = torch.__version__
        info["mps_built"] = torch.backends.mps.is_built()
        info["mps_available"] = torch.backends.mps.is_available()
        if info["mps_available"]:
            # A real check: VMs have reported MPS "available" while kernels fail.
            a = torch.randn(2048, 2048)
            t = time.perf_counter()
            for _ in range(10):
                (a @ a).sum().item()
            info["cpu_matmul_2048_ms"] = _ms((time.perf_counter() - t) / 10)
            try:
                b = a.to("mps")
                (b @ b).sum().item()
                t = time.perf_counter()
                for _ in range(10):
                    (b @ b).sum().item()
                info["mps_matmul_2048_ms"] = _ms((time.perf_counter() - t) / 10)
                info["mps_bf16_ok"] = bool((b.bfloat16() @ b.bfloat16()).float().isfinite().all())
            except Exception as error:  # noqa: BLE001
                info["mps_error"] = f"{type(error).__name__}: {error}"[:300]
    except Exception as error:  # noqa: BLE001
        info["torch"] = str(error)
    try:
        import mlx.core as mx

        info["mlx_device"] = str(mx.default_device())
        info["mlx_metal"] = bool(mx.metal.is_available())
    except Exception as error:  # noqa: BLE001
        info["mlx"] = f"{type(error).__name__}: {error}"[:200]
    return info


def app_probe() -> dict:
    """What the app's own embeddings module does on this machine with default settings."""

    import torch

    import io
    import logging

    out: dict = {}
    log = io.StringIO()
    handler = logging.StreamHandler(log)
    logging.getLogger("fortress").addHandler(handler)
    logging.getLogger("fortress").setLevel(logging.INFO)
    tmp = tempfile.mkdtemp(prefix="probe-notes-")
    os.environ.setdefault("NOTES_DIR", tmp)
    from app import embeddings

    started = time.perf_counter()
    model = embeddings.load_model()
    out["load_ms"] = _ms(time.perf_counter() - started)
    if model is None:
        return {"error": "model failed to load", "log": log.getvalue()[-2000:]}
    out["model_device"] = str(next(model.parameters()).device)
    out["dtype"] = str(embeddings._cache.dtype)
    out["devices"] = embeddings.device_info()
    try:
        q = embeddings._encode(model, ["probe query"], is_query=True)[0]
        out["query_tensor_device"] = str(q.device)
        d = embeddings._encode(model, ["a probe paragraph about invoices"], is_query=False)[0]
        index = embeddings._Index()
        index.put("b1", "h", "v", d)
        ids, scores = index.maxsim_all(q)
        out["maxsim_ok"] = bool(len(ids) == 1)
        embeddings._to_bytes(d)
        out["persist_ok"] = True
        times = []
        for query in QUERIES:
            t = time.perf_counter()
            embeddings._encode(model, [query], is_query=True)
            times.append(time.perf_counter() - t)
        out["app_query_ms"] = _ms(statistics.median(times))
        t = time.perf_counter()
        embeddings._encode(model, make_paragraphs(), is_query=False)
        out["app_index_64_paragraphs_ms"] = _ms(time.perf_counter() - t)
    except Exception as error:  # noqa: BLE001
        out["error"] = f"{type(error).__name__}: {error}"[:300]
    out["log"] = log.getvalue()[-2000:]
    return out


# --- orchestration ------------------------------------------------------------------------


def _child(args: list[str], timeout: float = 1800) -> dict:
    proc = subprocess.run([sys.executable, __file__, *args], capture_output=True, text=True,
                          timeout=timeout)
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
    if not lines:
        return {"error": f"exit {proc.returncode}: {proc.stderr[-1500:]}"}
    return json.loads(lines[-1][7:])


def _fmt(value) -> str:
    return "-" if value is None else str(value)


def summarize(results: dict) -> str:
    lines = ["# Apple Silicon acceleration benchmark", "", "## Environment", "", "```",
             json.dumps(results["env"], indent=2), "```", ""]
    lines += ["## App probe (default settings)", "", "```", json.dumps(results.get("probe"), indent=2),
              "```", ""]
    lines += ["## OCR (RapidOCR PP-OCRv6 small)", "",
              "| config | init ms | first call ms | sum of per-image medians ms | mean recall | "
              "same text as CPU | det/rec providers |", "|---|---|---|---|---|---|---|"]
    cpu = results["ocr"].get("cpu", {})
    for name, r in results["ocr"].items():
        if "error" in r and "images" not in r:
            lines.append(f"| {name} | error: {r['error'][:120]} | | | | | |")
            continue
        same = sum(r["images"][k]["text_sha"] == cpu.get("images", {}).get(k, {}).get("text_sha")
                   for k in r["images"])
        provs = r.get("engine_providers")
        if isinstance(provs, dict):
            provs = "/".join(p[0].replace("ExecutionProvider", "") for p in provs.values())
        lines.append(f"| {name} | {r.get('init_ms')} | {r.get('first_call_ms')} | "
                     f"{r.get('total_median_ms')} | {r.get('mean_recall')} | "
                     f"{same}/{len(r['images'])} | {provs} |")
    lines += ["", "Per image median ms (det / rec):", "", "| config | " +
              " | ".join(cpu.get("images", {}).keys()) + " |"]
    lines.append("|---" * (1 + len(cpu.get("images", {}))) + "|")
    for name, r in results["ocr"].items():
        if "images" not in r:
            continue
        cells = [f"{v['median_ms']} ({v['det_ms']}/{v['rec_ms']}) r={v['recall']}"
                 for v in r["images"].values()]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += ["", "Raw sessions (fixed shapes; first = first run at that shape):", ""]
    for name, r in results["ocr"].items():
        raw = r.get("raw")
        if not raw:
            continue
        for model, info in raw.items():
            if "error" in info:
                lines.append(f"- {name} {model}: error {info['error']}")
                continue
            runs = "; ".join(f"{run.get('shape')}: {run.get('first_ms')}->{run.get('median_ms')}"
                             if "error" not in run else f"{run['shape']}: ERR"
                             for run in info["runs"])
            lines.append(f"- {name} {model}: init {info['init_ms']} ms; "
                         f"{' '.join(info['partition'])}; {runs}")
    lines += ["", "## ColBERT (Agent-ModernColBERT)", "",
              "| config | threads | load ms | first query ms | query ms | paragraph-as-query ms | "
              "64 paras bs16 ms | 64 paras bs64 ms | peak RSS MB | cos mean / min | top1 | top10 order |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, r in results["colbert"].items():
        if "error" in r:
            lines.append(f"| {name} | error: {r['error'][:160]} |||||||||||")
            continue
        c = r.get("compare") or {}
        lines.append(
            f"| {name} | {r.get('threads')} | {r.get('load_ms')} | {r.get('first_query_ms')} | "
            f"{r.get('query_ms')} | {r.get('paragraph_query_ms')} | {r.get('batch64_bs16_ms')} | "
            f"{r.get('batch64_bs64_ms')} | {r.get('peak_rss_mb')} | {_fmt(c.get('cos_mean'))} / "
            f"{_fmt(c.get('cos_min'))} | {_fmt(c.get('top1_same'))} | {_fmt(c.get('top10_order_same'))} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=["all", "ocr", "colbert", "env", "probe"])
    parser.add_argument("config", nargs="?")
    parser.add_argument("--out", default="accel-results")
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--tag", default="", help="suffix for results/summary file names")
    parser.add_argument("--ocr-configs", default=",".join(OCR_CONFIGS))
    parser.add_argument("--colbert-configs", default=",".join(COLBERT_CONFIGS))
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.command == "env":
        print("RESULT " + json.dumps(environment()))
        return
    if args.command == "probe":
        print("RESULT " + json.dumps(app_probe()))
        return
    if args.command == "ocr":
        print("RESULT " + json.dumps(run_ocr(args.config, args.reps, out_dir)))
        return
    if args.command == "colbert":
        print("RESULT " + json.dumps(run_colbert(args.config, args.reps, out_dir)))
        return

    results: dict = {"env": _child(["env"]), "ocr": {}, "colbert": {}}
    print(json.dumps(results["env"], indent=2), flush=True)
    results["probe"] = _child(["probe", "--out", str(out_dir)])
    print("probe", json.dumps(results["probe"]), flush=True)
    for config in filter(None, args.colbert_configs.split(",")):
        r = _child(["colbert", config, "--out", str(out_dir), "--reps", str(args.reps)])
        r["compare"] = compare_embeddings(out_dir, config) if config != "cpu_fp32" else None
        results["colbert"][config] = r
        print("colbert", config, json.dumps(r), flush=True)
    for config in filter(None, args.ocr_configs.split(",")):
        r = _child(["ocr", config, "--out", str(out_dir), "--reps", str(args.reps)])
        results["ocr"][config] = r
        brief = {k: v for k, v in r.items() if k not in ("images", "raw")}
        print("ocr", config, json.dumps(brief), flush=True)
    suffix = f"-{args.tag}" if args.tag else ""
    (out_dir / f"results{suffix}.json").write_text(json.dumps(results, indent=2))
    summary = summarize(results)
    (out_dir / f"summary{suffix}.md").write_text(summary)
    print(summary)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a") as handle:
            handle.write(summary)


if __name__ == "__main__":
    main()
