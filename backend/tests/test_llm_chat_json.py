import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app import llm


class ChatJsonParsingTests(unittest.IsolatedAsyncioTestCase):
    """The model's reply text is turned into a dict; no real network calls."""

    async def reply(self, content) -> dict:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient
        settings = SimpleNamespace(openrouter_api_key="test-key", openrouter_model="test/model")
        with (
            patch.object(llm, "get_settings", lambda: settings),
            patch.object(
                llm.httpx,
                "AsyncClient",
                lambda **kwargs: real_client(transport=transport, **kwargs),
            ),
        ):
            return await llm.chat_json("system", "user")

    async def test_plain_json_object(self) -> None:
        self.assertEqual(await self.reply('{"action": "answer"}'), {"action": "answer"})

    async def test_json_inside_a_markdown_code_fence(self) -> None:
        content = '```json\n{"action": "grep", "pattern": "nata"}\n```'
        self.assertEqual(await self.reply(content), {"action": "grep", "pattern": "nata"})

    async def test_json_after_a_sentence_of_prose(self) -> None:
        content = 'Here you go:\n{"action": "read", "note": "Lisbon {2024}"}\nHope that helps.'
        self.assertEqual(await self.reply(content), {"action": "read", "note": "Lisbon {2024}"})

    async def test_non_object_json_and_empty_content_come_back_raw(self) -> None:
        self.assertEqual(await self.reply("[1, 2]"), {"_raw": "[1, 2]"})
        self.assertEqual(await self.reply('"just a string"'), {"_raw": '"just a string"'})
        self.assertEqual(await self.reply(None), {"_raw": ""})
        self.assertEqual(await self.reply("not json at all"), {"_raw": "not json at all"})


if __name__ == "__main__":
    unittest.main()
