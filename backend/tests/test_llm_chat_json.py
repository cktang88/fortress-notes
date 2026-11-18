import unittest

from app import llm


class ParseJsonObjectTests(unittest.TestCase):
    """The model's reply text is turned into a dict (what Ask's stream parses)."""

    def reply(self, content) -> dict:
        return llm._parse_json_object(content)

    def test_plain_json_object(self) -> None:
        self.assertEqual(self.reply('{"action": "answer"}'), {"action": "answer"})

    def test_json_inside_a_markdown_code_fence(self) -> None:
        content = '```json\n{"action": "grep", "pattern": "nata"}\n```'
        self.assertEqual(self.reply(content), {"action": "grep", "pattern": "nata"})

    def test_json_after_a_sentence_of_prose(self) -> None:
        content = 'Here you go:\n{"action": "read", "note": "Lisbon {2024}"}\nHope that helps.'
        self.assertEqual(self.reply(content), {"action": "read", "note": "Lisbon {2024}"})

    def test_non_object_json_and_empty_content_come_back_raw(self) -> None:
        self.assertEqual(self.reply("[1, 2]"), {"_raw": "[1, 2]"})
        self.assertEqual(self.reply('"just a string"'), {"_raw": '"just a string"'})
        self.assertEqual(self.reply(None), {"_raw": ""})
        self.assertEqual(self.reply("not json at all"), {"_raw": "not json at all"})


if __name__ == "__main__":
    unittest.main()
