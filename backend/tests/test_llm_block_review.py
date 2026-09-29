import unittest
from unittest.mock import AsyncMock, patch

from app import llm


class BlockReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_reviews_one_block_and_discards_quotes_not_in_source(self) -> None:
        source = "The moon is made of cheese."
        response = {
            "summary": "One claim needs checking.",
            "items": [
                {"label": "claim", "detail": "Check this.", "quote": "made of cheese", "severity": "high"},
                {"label": "invented", "detail": "No source.", "quote": "moon rocks", "severity": "low"},
            ],
        }
        with patch.object(llm, "chat_json", new=AsyncMock(return_value=response)) as chat:
            result = await llm.review_block(
                "factcheck", "stable-block-id", source, {"type": "paragraph"}
            )

        self.assertEqual(result.block_id, "stable-block-id")
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].block_id, "stable-block-id")
        self.assertEqual(result.items[0].quote, "made of cheese")
        prompt = chat.await_args.args[1]
        self.assertIn("stable-block-id", prompt)
        self.assertIn(source, prompt)
        self.assertIn('"type": "paragraph"', prompt)

    async def test_unconfigured_llm_returns_block_scoped_empty_response(self) -> None:
        with patch.object(llm, "chat_json", new=AsyncMock(side_effect=llm.LLMNotConfigured)):
            result = await llm.review_block("clarify", "b-1", "text", {})

        self.assertEqual(result.block_id, "b-1")
        self.assertEqual(result.items, [])

    async def test_context_review_accepts_only_exact_quotes_from_canonical_targets(self) -> None:
        targets = [
            {"id": "b-1", "text": "Canonical claim one", "content": {"type": "paragraph"}},
            {"id": "b-2", "text": "Canonical claim two", "content": {"type": "paragraph"}},
        ]
        linked = [
            {
                "id": "b-linked",
                "document_id": "other-doc",
                "type": "paragraph",
                "text": "Context only text",
                "content": {},
            }
        ]
        response = {
            "summary": "Two useful findings.",
            "items": [
                {"block_id": "b-1", "detail": "First", "quote": "claim one"},
                {"block_id": "b-2", "detail": "Second", "quote": "claim two"},
                {"block_id": "b-1", "detail": "Context quote", "quote": "Context only text"},
                {"block_id": "not-a-target", "detail": "Unknown", "quote": "claim one"},
            ],
        }
        with patch.object(llm, "chat_json", new=AsyncMock(return_value=response)) as chat:
            result = await llm.review_blocks("factcheck", "linked", targets, linked)

        self.assertEqual(result.context, "linked")
        self.assertEqual([item.block_id for item in result.items], ["b-1", "b-2"])
        prompt = chat.await_args.args[1]
        self.assertIn("Canonical claim one", prompt)
        self.assertIn("Context only text", prompt)
        self.assertIn("never for linked context blocks", prompt)

    async def test_context_review_without_api_key_returns_empty_context_response(self) -> None:
        with patch.object(llm, "chat_json", new=AsyncMock(side_effect=llm.LLMNotConfigured)):
            result = await llm.review_blocks("clarify", "document", [], [])

        self.assertEqual(result.context, "document")
        self.assertEqual(result.items, [])
