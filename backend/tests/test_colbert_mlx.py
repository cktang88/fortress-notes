"""The MLX ColBERT encoder matches PyLate's (needs mlx and the model in the HF cache:
on a Mac, or on Linux with ``mlx[cpu]``; skipped otherwise, e.g. in CI)."""

import unittest


def _reference():
    try:
        import mlx.core  # noqa: F401
        from pylate import models

        from app.config import get_settings

        model = models.ColBERT(model_name_or_path=get_settings().colbert_model, device="cpu")
    except Exception as error:  # noqa: BLE001 - no mlx, no cached model, offline
        raise unittest.SkipTest(f"MLX or the ColBERT model unavailable: {error}")
    model.eval()
    return model


class MlxColBERTTests(unittest.TestCase):
    def test_encodings_match_pylate_in_float32(self) -> None:
        import torch

        from app import colbert_mlx
        from app.config import get_settings

        reference = _reference()
        encoder = colbert_mlx.load(reference, get_settings().colbert_model, "float32")
        # A long paragraph (local attention windows) batched with short ones (padding
        # far from any real token), and queries.
        documents = [
            "Invoice 2231 is still pending; payment terms are thirty days.",
            "short",
            " ".join(f"Step {i}: check the budget, then the schedule." for i in range(14)),
        ]
        queries = ["quarterly budget review", "who owns the release checklist?"]
        for is_query, texts in ((True, queries), (False, documents)):
            with torch.inference_mode():
                expected = reference.encode(texts, is_query=is_query, convert_to_numpy=False,
                                            show_progress_bar=False)
            got = encoder.encode(texts, is_query=is_query)
            for a, b in zip(expected, got):
                self.assertEqual(a.shape, b.shape)
                cosine = torch.nn.functional.cosine_similarity(a.float(), b, dim=-1)
                self.assertGreater(float(cosine.min()), 0.9999)


if __name__ == "__main__":
    unittest.main()
