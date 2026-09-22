import tempfile
import unittest
from pathlib import Path

from app.block_query import BlockSearchFilters, build_fts_match, search_blocks
from app.block_store import bootstrap_markdown, connection_scope, delete_document


class BlockQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / ".fortress.sqlite3"
        self._write_note(
            "alpha",
            "Alpha",
            "rough",
            "[work, shared]",
            "# Matching alpha heading\n\nmatching alpha paragraph",
        )
        self._write_note(
            "beta",
            "Beta",
            "polished",
            "[personal, shared]",
            "matching beta paragraph",
        )
        self._write_note(
            "deleted",
            "Deleted",
            "rough",
            "[work]",
            "matching deleted paragraph",
        )
        self._write_note(
            "prefix_only",
            "Prefix only",
            "rough",
            "[work]",
            "matchingonly paragraph",
        )
        bootstrap_markdown(self.root, self.database)
        with connection_scope(self.database) as connection:
            connection.execute(
                "UPDATE blocks SET updated_at = ? WHERE document_id = ?",
                ("2026-01-10T00:00:00+00:00", "alpha"),
            )
            connection.execute(
                "UPDATE blocks SET updated_at = ? WHERE document_id = ?",
                ("2026-02-10T00:00:00+00:00", "beta"),
            )
        delete_document(self.database, "deleted")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_filters_by_document_and_block_type(self) -> None:
        document_hits = search_blocks(
            self.database, "matching", BlockSearchFilters(document_id="alpha")
        )
        heading_hits = search_blocks(
            self.database, "matching", BlockSearchFilters(block_type="heading")
        )
        self.assertEqual({hit["document_id"] for hit in document_hits}, {"alpha"})
        self.assertEqual([hit["block_type"] for hit in heading_hits], ["heading"])

    def test_filters_by_status_and_exact_tag(self) -> None:
        polished_hits = search_blocks(
            self.database, "matching", BlockSearchFilters(status="polished")
        )
        work_hits = search_blocks(self.database, "matching", BlockSearchFilters(tag="work"))
        self.assertEqual({hit["document_id"] for hit in polished_hits}, {"beta"})
        self.assertEqual({hit["document_id"] for hit in work_hits}, {"alpha"})

    def test_filters_by_updated_range(self) -> None:
        after_hits = search_blocks(
            self.database,
            "matching",
            BlockSearchFilters(updated_after="2026-02-01T00:00:00Z"),
        )
        before_hits = search_blocks(
            self.database,
            "matching",
            BlockSearchFilters(updated_before="2026-02-01T00:00:00+00:00"),
        )
        self.assertEqual({hit["document_id"] for hit in after_hits}, {"beta"})
        self.assertEqual({hit["document_id"] for hit in before_hits}, {"alpha"})

    def test_date_only_bounds_cover_the_selected_calendar_day(self) -> None:
        same_day = search_blocks(
            self.database,
            "matching",
            BlockSearchFilters(
                updated_after="2026-01-10",
                updated_before="2026-01-10",
            ),
        )
        self.assertEqual({hit["document_id"] for hit in same_day}, {"alpha"})

    def test_timestamp_filters_compare_instants_not_timestamp_text(self) -> None:
        with connection_scope(self.database) as connection:
            connection.execute(
                "UPDATE blocks SET updated_at = ? WHERE document_id = ?",
                ("2026-01-10T00:30:00+02:00", "alpha"),
            )
        hits = search_blocks(
            self.database,
            "matching",
            BlockSearchFilters(updated_after="2026-01-09T23:00:00Z"),
        )
        self.assertNotIn("alpha", {hit["document_id"] for hit in hits})

    def test_combines_filters_at_block_level(self) -> None:
        hits = search_blocks(
            self.database,
            "matching",
            BlockSearchFilters(
                document_id="alpha",
                block_type="paragraph",
                status="rough",
                tag="shared",
            ),
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["document_id"], "alpha")
        self.assertEqual(hits[0]["block_type"], "paragraph")
        self.assertEqual(hits[0]["text"], "matching alpha paragraph")

    def test_excludes_soft_deleted_documents(self) -> None:
        hits = search_blocks(self.database, "matching")
        self.assertNotIn("deleted", {hit["document_id"] for hit in hits})

    def test_invalid_filters_and_wildcards_are_safe(self) -> None:
        self.assertEqual(
            search_blocks(self.database, "matching", BlockSearchFilters(status="other")), []
        )
        self.assertEqual(search_blocks(self.database, "matching", BlockSearchFilters(tag="")), [])
        self.assertEqual(
            search_blocks(
                self.database,
                "matching",
                BlockSearchFilters(updated_after="tomorrow"),
            ),
            [],
        )
        self.assertIsNone(build_fts_match("*"))
        self.assertEqual(search_blocks(self.database, "*"), [])
        self.assertEqual(
            build_fts_match("matching OR deleted"), '"matching" AND "OR" AND "deleted"'
        )
        self.assertEqual(search_blocks(self.database, "matching OR deleted"), [])
        self.assertEqual(build_fts_match("matching*"), '"matching"')
        wildcard_hits = search_blocks(self.database, "matching*")
        self.assertNotIn("prefix_only", {hit["document_id"] for hit in wildcard_hits})

    def _write_note(self, note_id: str, title: str, status: str, tags: str, body: str) -> None:
        (self.root / f"{note_id}.md").write_text(
            f"---\ntitle: {title}\nstatus: {status}\ntags: {tags}\n---\n{body}\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
