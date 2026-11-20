"""The real ColBERT model end to end, on the device ``EMBEDDINGS_DEVICE=auto`` picks here.

Two backends over the same notes, one pinned to the CPU (``EMBEDDINGS_DEVICE=cpu``) and
one on ``auto`` (on Apple Silicon: the MLX GPU encoder, app/colbert_mlx.py), each run
through the API like the app does: index, related notes and paragraphs, unified search
and Ask's prefetch (grep + semantic search; the LLM is a fake that records what it was
sent). The ``auto`` backend must reach the CPU's rankings. Then a real error inside the
GPU encoder must move it back to the CPU, still with the CPU's results.

Slow (the model loads twice) and needs the model (lightonai/Agent-ModernColBERT) in
the Hugging Face cache, so it runs only with ``FORTRESS_REAL_MODEL=1`` (the macOS CI
job). ``FORTRESS_EXPECT_EMBEDDINGS_DEVICE`` names the device ``auto`` must choose for
both queries and documents (``mlx`` on GitHub's arm64 runners); ``cpu`` elsewhere.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from support import make_settings, use_settings

ENABLED = os.environ.get("FORTRESS_REAL_MODEL") == "1"
EXPECTED_DEVICE = os.environ.get("FORTRESS_EXPECT_EMBEDDINGS_DEVICE", "").strip().lower()
MODEL = "lightonai/Agent-ModernColBERT"

NOTES = {
    "Kyoto trip": [
        "Flight and hotel for the Kyoto conference are booked; the ryokan is near Gion.",
        "Visit Fushimi Inari early in the morning before the crowds arrive.",
    ],
    "Lake Constance": [
        "The Zeppelin NT leaves Friedrichshafen at nine and circles over the water for an hour.",
        "Ferry tickets to Meersburg are sold at the harbour kiosk.",
    ],
    "Q3 budget": [
        "The quarterly budget review is on Friday; finance wants the cloud spend broken down by team.",
        "Marketing overspent on trade shows by twelve percent.",
    ],
    "Garden": [
        "Tomatoes need staking in June and watering every other evening.",
        "Turn the compost pile weekly so it stays warm.",
    ],
    "Search latency": [
        "Related paragraphs took 400 ms on a cold cache after the index change.",
        "Precomputing paragraph queries fixed most of the slowdown.",
    ],
    "Invoices": [
        "Invoice 2231 is still pending; payment terms are thirty days.",
        "The customer asked for a summary of shipping costs by order.",
    ],
    "Contractors": [
        "Miguel quoted 12k for the deck and can start in May.",
    ],
    "Running": [
        "Long run on Sunday: eighteen kilometres along the river at an easy pace.",
        "Replace the shoes after about eight hundred kilometres.",
    ],
    "Japan itinerary": [
        "Two days in Kyoto for the temples, then the train to Osaka.",
        "Book the shinkansen seats a week ahead.",
    ],
    "Cloud costs": [
        "Our server bill doubled after the storage migration; finance wants it split by team.",
    ],
    "Marathon training": [
        "Increase the weekly distance by ten percent and keep the long run slow.",
    ],
    "Lentil soup": [
        "Simmer the lentils with cumin and garlic for twenty minutes, then add lemon.",
    ],
}
# Typed searches (most share no word with the paragraph they mean).
QUERIES = [
    "airship over the lake",
    "money spent on servers",
    "vegetable garden chores",
    "slow search performance",
    "unpaid bill",
    "jogging plan",
    "temple visit in Japan",
]
# Ask: one question only meaning can answer (no paragraph holds "airship" or "leave"
# spelled that way), one that grep answers by name.
QUESTIONS = ["When does the airship take off?", "What did Miguel quote?"]
# Added after the first round of checks (and after the GPU is broken, on ``auto``).
LATE_NOTE = ("Bike repair", "The rear derailleur needs a new cable before the hill ride.")
LATE_QUERIES = ["bicycle maintenance", "aircraft over the lake"]

# Encodings of the same text on the GPU and the CPU (the bar _accelerate sets itself).
MIN_COSINE = 0.999


def _tolerance(score: float) -> float:
    """How far a GPU score may sit from the CPU's: MaxSim sums ~32 token cosines
    (scores ~5-30) and float16 moves each by up to ~1e-3; related scores are means
    (0-1). Scores closer than this are ties: either order is right."""

    return max(0.01, 0.005 * abs(score))


def _say(message: str) -> None:
    print(f"REALMODEL {message}", file=sys.stderr, flush=True)


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _run_backend(device: str, *, break_gpu: bool = False) -> dict:
    """Start a backend with ``EMBEDDINGS_DEVICE=device`` over NOTES; what it answers."""

    import torch
    from fastapi.testclient import TestClient

    from app import embeddings, llm, main

    out: dict = {"device_setting": device}
    prompts: list[str] = []

    async def fake_llm(_system, prompt, **_kwargs):
        prompts.append(prompt)
        yield json.dumps({"action": "answer", "found": False, "sentences": []})

    records = _Records()
    logger = logging.getLogger("fortress")
    with tempfile.TemporaryDirectory() as directory, use_settings(
        make_settings(Path(directory), embeddings_enabled=True, colbert_model=MODEL,
                      embeddings_device=device)
    ), patch.object(embeddings, "_cache", embeddings._Cache()), patch.object(
        llm, "chat_json_stream", fake_llm
    ), TestClient(main.app) as client:
        logger.addHandler(records)
        try:
            started = time.perf_counter()
            ids: dict[str, str] = {}
            for title, paragraphs in NOTES.items():
                response = client.post(
                    "/api/block-documents", json={"title": title, "body": "\n\n".join(paragraphs)}
                )
                assert response.status_code == 201, response.text
                ids[title] = response.json()["id"]
            if embeddings.load_model() is None:
                # FORTRESS_REAL_MODEL=1 promises the model: a missing or broken download
                # (e.g. a CI cache without its files) must fail, not quietly skip.
                raise AssertionError("the ColBERT model could not be loaded")
            out["load_s"] = time.perf_counter() - started
            started = time.perf_counter()
            while embeddings.warm_index():
                pass
            out["index_s"] = time.perf_counter() - started
            out["health"] = client.get("/api/health").json()
            out["cpu_model_device"] = next(embeddings._cache.model.parameters()).device.type
            routes = embeddings._cache.routes
            out["gpu_encoder"] = type(routes.documents).__name__ if routes is not None else None
            out["mps_available"] = bool(torch.backends.mps.is_available())
            out["first"] = _answers(client, ids, QUERIES, prompts)
            out["encodings"] = _encodings()

            if break_gpu:
                _break(routes)
            response = client.post(
                "/api/block-documents", json={"title": LATE_NOTE[0], "body": LATE_NOTE[1]}
            )
            assert response.status_code == 201, response.text
            while embeddings.warm_index():
                pass
            out["late"] = _answers(client, ids, LATE_QUERIES, prompts, related=False)
            out["late_health"] = client.get("/api/health").json()
            out["late_encodings"] = _encodings()
            out["warnings"] = list(records.messages)
        finally:
            logger.removeHandler(records)
    _say(
        f"device={device} chosen={out['health']['embeddings_device']} "
        f"gpu_encoder={out['gpu_encoder']} cpu_model_on={out['cpu_model_device']} "
        f"mps_available={out['mps_available']} load+accelerate={out['load_s']:.1f}s "
        f"index={out['index_s']:.1f}s search_p50={out['first']['search_ms']:.0f}ms "
        f"after={out['late_health']['embeddings_device']}"
    )
    return out


def _break(routes) -> None:
    """Make the real GPU encoder fail: a weight of the wrong shape, so MLX itself raises
    (a shape error from the first layer's matmul) on the next encode."""

    if routes is None or not hasattr(routes.documents, "w"):
        raise AssertionError(f"no MLX encoder to break (routes: {routes})")
    encoder = routes.documents
    mx = encoder.mx
    encoder.w["layers.0.attn.Wqkv.weight"] = mx.zeros((7, 5), dtype=encoder.dense.dtype)


def _answers(client, ids: dict[str, str], queries: list[str], prompts: list[str], *,
             related: bool = True) -> dict:
    """Related lists, unified search, raw MaxSim scores and Ask's sources, by text."""

    from app import corpus, embeddings
    from app.config import get_settings

    out: dict = {"related": {}, "block_related": {}, "search": {}, "meaning": {}, "ask": {}}
    timings = []
    for query in queries:
        started = time.perf_counter()
        response = client.get("/api/unified-search", params={"q": query})
        timings.append(time.perf_counter() - started)
        assert response.status_code == 200, response.text
        out["search"][query] = [hit["text"] for hit in response.json()]
    out["search_ms"] = sorted(timings)[len(timings) // 2] * 1000
    blocks = corpus.snapshot(get_settings().block_db_path)
    docs = [(block_id, version, text) for block_id, _d, version, text in blocks]
    texts = {block_id: text for block_id, _v, text in docs}
    for query in [*queries, *(QUESTIONS if related else [])]:
        scores = embeddings.score_documents(query, docs)
        out["meaning"][query] = {texts[block_id]: score for block_id, score in scores.items()}
    if not related:
        return out
    for title, note_id in ids.items():
        response = client.get(f"/api/block-documents/{note_id}/related", params={"k": 5})
        assert response.status_code == 200, response.text
        out["related"][title] = [
            (hit["note"]["title"], hit["score"], hit["matched_block_text"]) for hit in response.json()
        ]
        tree = client.get(f"/api/block-documents/{note_id}").json()
        first = next(
            (block for block in tree["children"] if block.get("text", "").strip()), None
        )
        if first is not None:
            response = client.get(
                f"/api/block-documents/{note_id}/related", params={"k": 5, "block_id": first["id"]}
            )
            assert response.status_code == 200, response.text
            out["block_related"][title] = [
                (hit["note"]["title"], hit["score"]) for hit in response.json()
            ]
    for question in QUESTIONS:
        before = len(prompts)
        response = client.post("/api/ask", json={"question": question})
        assert response.status_code == 200, response.text
        assert len(prompts) > before, f"Ask found nothing to send for {question!r}"
        payload = prompts[before].split("Sources:\n", 1)[1]
        sources, _end = json.JSONDecoder().raw_decode(payload)
        out["ask"][question] = [source["text"] for source in sources]
    return out


def _encodings() -> dict:
    """Encodings of a few texts through the backend's own routes (GPU or CPU)."""

    from app import embeddings

    model = embeddings._cache.model
    texts = [paragraph for paragraphs in NOTES.values() for paragraph in paragraphs][:6]
    return {
        "documents": embeddings._encode(model, texts, is_query=False, background=False),
        "queries": embeddings._encode(model, QUERIES, is_query=True),
    }


@unittest.skipUnless(ENABLED, "set FORTRESS_REAL_MODEL=1 (needs the ColBERT model downloaded)")
class RealModelTests(unittest.TestCase):
    cpu: dict
    auto: dict

    @classmethod
    def setUpClass(cls) -> None:
        cls.cpu = _run_backend("cpu")
        cls.auto = _run_backend("auto", break_gpu=EXPECTED_DEVICE not in ("", "cpu"))
        # How far apart the devices are (to keep the tolerances honest).
        for phase in ("first", "late"):
            worst = max(
                abs(cls.auto[phase]["meaning"][query][text] - score)
                for query, scores in cls.cpu[phase]["meaning"].items()
                for text, score in scores.items()
            )
            _say(f"{phase}: largest MaxSim difference auto vs cpu {worst:.5f}")

    # Helpers --------------------------------------------------------------------------

    def _same_order(self, label: str, cpu: list[str], got: list[str], scores: dict[str, float]) -> None:
        """Same list, except where two items tie on the CPU's ``scores``."""

        self.assertEqual(len(got), len(cpu), f"{label}: {got} vs {cpu}")
        for rank, (want, have) in enumerate(zip(cpu, got)):
            if want != have:
                self.assertAlmostEqual(
                    scores.get(have, 0.0), scores.get(want, 0.0),
                    delta=_tolerance(scores.get(want, 0.0)),
                    msg=f"{label} rank {rank}: {have!r} instead of {want!r}",
                )

    # The device ---------------------------------------------------------------------

    def test_auto_picks_the_expected_device(self) -> None:
        health = self.auto["health"]
        self.assertTrue(health["model_loaded"])
        expected = EXPECTED_DEVICE or "cpu"
        self.assertEqual(health["embeddings_device"], {"query": expected, "documents": expected})
        if expected == "mlx":
            self.assertEqual(self.auto["gpu_encoder"], "MlxColBERT")

    def test_the_cpu_setting_and_the_cpu_model_stay_on_the_cpu(self) -> None:
        # EMBEDDINGS_DEVICE=cpu: no GPU encoder at all.
        self.assertEqual(self.cpu["health"]["embeddings_device"], {"query": "cpu", "documents": "cpu"})
        self.assertIsNone(self.cpu["gpu_encoder"])
        # The CPU model is pinned to the CPU whatever PyLate would pick (MPS when the
        # Mac has it), also beside a GPU encoder.
        self.assertEqual(self.cpu["cpu_model_device"], "cpu")
        self.assertEqual(self.auto["cpu_model_device"], "cpu")

    # Same answers as the CPU ----------------------------------------------------------

    def test_encodings_match_the_cpu(self) -> None:
        import torch

        for phase in ("encodings", "late_encodings"):
            for role in ("documents", "queries"):
                for cpu, got in zip(self.cpu[phase][role], self.auto[phase][role], strict=True):
                    self.assertEqual(cpu.shape, got.shape)
                    cosine = torch.nn.functional.cosine_similarity(cpu.float(), got.float(), dim=-1)
                    self.assertGreaterEqual(float(cosine.min()), MIN_COSINE, f"{phase} {role}")

    def test_semantic_scores_and_rankings_match_the_cpu(self) -> None:
        for phase in ("first", "late"):
            for query, cpu in self.cpu[phase]["meaning"].items():
                got = self.auto[phase]["meaning"][query]
                self.assertEqual(set(got), set(cpu))
                for text, score in cpu.items():
                    self.assertAlmostEqual(got[text], score, delta=_tolerance(score),
                                           msg=f"{query!r}: {text!r}")
                self._same_order(query, sorted(cpu, key=cpu.get, reverse=True),
                                 sorted(got, key=got.get, reverse=True), cpu)

    def test_the_semantic_matches_are_the_right_paragraphs(self) -> None:
        # The model works at all (not the same garbage on both devices).
        for run in (self.cpu, self.auto):
            meaning = run["first"]["meaning"]
            best = {query: max(scores, key=scores.get) for query, scores in meaning.items()}
            self.assertIn("Zeppelin", best["airship over the lake"])
            self.assertIn(best["vegetable garden chores"], NOTES["Garden"])
            self.assertIn(best["temple visit in Japan"], NOTES["Kyoto trip"] + NOTES["Japan itinerary"])
            self.assertIn(best["jogging plan"], NOTES["Running"] + NOTES["Marathon training"])
            late = {query: max(scores, key=scores.get) for query, scores in run["late"]["meaning"].items()}
            self.assertIn("derailleur", late["bicycle maintenance"])
            self.assertIn("Zeppelin", late["aircraft over the lake"])

    def test_unified_search_matches_the_cpu(self) -> None:
        for phase in ("first", "late"):
            for query, cpu in self.cpu[phase]["search"].items():
                self._same_order(query, cpu, self.auto[phase]["search"][query],
                                 self.cpu[phase]["meaning"][query])
        self.assertIn("Zeppelin", self.auto["first"]["search"]["airship over the lake"][0])

    def test_related_notes_and_paragraphs_match_the_cpu(self) -> None:
        found = 0
        for kind in ("related", "block_related"):
            cpu, got = self.cpu["first"][kind], self.auto["first"][kind]
            self.assertEqual(set(got), set(cpu))
            for title in cpu:
                scores = {hit[0]: hit[1] for hit in cpu[title]}
                self._same_order(f"{kind} of {title!r}", [hit[0] for hit in cpu[title]],
                                 [hit[0] for hit in got[title]], scores)
                for want, have in zip(cpu[title], got[title]):
                    if want[0] == have[0]:
                        self.assertAlmostEqual(have[1], want[1], delta=_tolerance(want[1]))
                found += len(cpu[title])
        self.assertGreater(found, 0, "no related notes at all: nothing was compared")
        related = self.auto["first"]["related"]
        self.assertEqual(related["Japan itinerary"][0][0], "Kyoto trip", related)

    def test_ask_prefetches_the_same_grep_and_semantic_sources(self) -> None:
        for question, cpu in self.cpu["first"]["ask"].items():
            self._same_order(question, cpu, self.auto["first"]["ask"][question],
                             self.cpu["first"]["meaning"][question])
        airship = self.auto["first"]["ask"][QUESTIONS[0]]
        # Nothing to grep for: the paragraph came from the model.
        self.assertIn("Zeppelin", airship[0])
        self.assertIn("Miguel", self.auto["first"]["ask"][QUESTIONS[1]][0])

    # A GPU error -------------------------------------------------------------------

    @unittest.skipIf(EXPECTED_DEVICE in ("", "cpu"), "no GPU encoder here")
    def test_a_real_gpu_error_moves_the_backend_to_the_cpu(self) -> None:
        self.assertEqual(self.auto["late_health"]["embeddings_device"],
                         {"query": "cpu", "documents": "cpu"})
        self.assertTrue(
            any("ColBERT on the GPU failed" in message for message in self.auto["warnings"]),
            self.auto["warnings"],
        )
        # The paragraph added after the failure was indexed (on the CPU) and found.
        self.assertIn("derailleur", self.auto["late"]["search"]["bicycle maintenance"][0])

    def test_no_gpu_failure_without_a_broken_encoder(self) -> None:
        self.assertFalse(
            any("ColBERT on the GPU failed" in message for message in self.cpu["warnings"])
        )
        self.assertEqual(self.cpu["late_health"]["embeddings_device"], {"query": "cpu", "documents": "cpu"})


if __name__ == "__main__":
    unittest.main()
