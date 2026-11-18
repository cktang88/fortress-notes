import hashlib
import tempfile
import unittest
from pathlib import Path

from app import block_store, corpus, textutil
from app.models import BlockOperation


class TextRulesTests(unittest.TestCase):
    def test_fold_keeps_offsets_and_ignores_case_and_accents(self) -> None:
        text = "Crème Brûlée at Café ÅNGSTRÖM"
        folded = textutil.fold(text)
        self.assertEqual(len(folded), len(text))
        self.assertEqual(folded, "creme brulee at cafe angstrom")
        self.assertEqual(textutil.words("Café-Crème_2024!"), ["cafe", "creme", "2024"])

    def test_fold_accents_leaves_ascii_regex_syntax_alone(self) -> None:
        self.assertEqual(textutil.fold_accents(r"\D\W[Éé]+ Café"), r"\D\W[ee]+ Cafe")

    def test_content_words_drop_stopwords_and_short_ascii_words(self) -> None:
        self.assertEqual(
            textutil.content_words("What did Miguel say on 14 March about the café? 東京 ok"),
            ["miguel", "say", "14", "march", "cafe", "東京"],
        )
        self.assertEqual(textutil.content_words("Room 14 in 2024", numbers=False), ["room"])

    def test_window_is_a_slice_around_the_match_starting_on_a_word(self) -> None:
        text = ("lorem ipsum " * 50) + "TARGET" + (" dolor sit" * 50)
        first = text.index("TARGET")
        start, stop = textutil.window(text, first, 100)
        self.assertEqual(stop - start, 100)
        self.assertLess(start, first)
        self.assertLess(first, stop)
        self.assertEqual(text[start - 1], " ")
        self.assertEqual(textutil.window("short", 3, 100), (0, 5))
        self.assertEqual(textutil.first_match(textutil.fold("x Café y"), ["nope", "cafe"]), 2)

    def test_searchable_text_joins_extracted_text_after_the_block_text(self) -> None:
        self.assertEqual(textutil.searchable_text("Body", ["one", " ", "two"]), "Body\n\none\ntwo")
        self.assertEqual(textutil.searchable_text("Body", "one\ntwo"), "Body\n\none\ntwo")
        self.assertEqual(textutil.searchable_text("", ["one"]), "one")
        self.assertEqual(textutil.searchable_text("Body"), "Body")


class OneSearchableTextTests(unittest.TestCase):
    """Full-text search, embeddings/related notes and Ask index exactly the same text."""

    def test_fts_row_equals_corpus_row_including_picture_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / ".fortress.sqlite3"
            document = block_store.create_block_document(database, "Trip", "rough", "Tickets below")
            names = []
            for name, data in (("01JBBBBBBBBBBBBBBBBBBBBBBB.png", b"b"), ("01JAAAAAAAAAAAAAAAAAAAAAAA.png", b"a")):
                path = Path(directory) / name
                path.write_bytes(data)
                block_store.register_asset(database, path, hashlib.sha256(data).hexdigest(), "image/png")
                names.append(name)
            block_store.apply_transaction(database, document["id"], [
                BlockOperation(operation="insert", type="image", attrs={"url": f"/media/{name}"})
                for name in names
            ] + [BlockOperation(operation="insert", type="paragraph", content=[{"type": "text", "text": "Hi"}])], 0)
            block_store.store_asset_text(database, hashlib.sha256(b"a").hexdigest(), "Gate A4")
            block_store.store_asset_text(database, hashlib.sha256(b"b").hexdigest(), "Seat 12B")
            with block_store.connection_scope(database) as connection:
                fts = {row[0]: row[1] for row in connection.execute("SELECT block_id, text FROM blocks_fts")}
            rows = {block_id: text for block_id, _d, _v, text in corpus.snapshot(database)}
            self.assertTrue(any("Gate A4" in text for text in rows.values()))
            for block_id, text in rows.items():
                self.assertEqual(fts[block_id], text)


if __name__ == "__main__":
    unittest.main()
