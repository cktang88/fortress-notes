"""Graph output HTML -> manifest blocks (fixtures written from Microsoft's documented
output HTML: absolute-positioned outlines, note tags, nested tables, printouts,
MathML, onenote: links)."""

from __future__ import annotations

import unittest

from fortress_onenote_graph.htmlconv import PageConverter, onenote_link, parse_tags, resource_urls
from fortress_onenote_graph.layout import compute

from .fakes import FIXTURES


def resolver(calls):
    def resolve(url, kind, filename):
        calls.append((url, kind, filename))
        if "aaa222" in url or "bbb111" in url:
            return {"asset": f"{'0' * 64}.{'png' if kind == 'image' else 'pdf'}", "data_status": "available",
                    "mime": "application/pdf", "size": 17}
        return {"asset": None, "data_status": "missing"}
    return resolve


def convert(name):
    calls = []
    result = PageConverter(resolver(calls)).convert((FIXTURES / name).read_text(encoding="utf-8"))
    return result, calls


def text_of(mark, text):
    return text[mark["start"]:mark["end"]]


class LayoutPageTests(unittest.TestCase):
    def setUp(self):
        self.result, self.calls = convert("page_layout.html")
        self.items = self.result.items
        self.blocks = self.items[0]["blocks"]

    def by_text(self, text):
        return next(block for block in self.blocks if block.get("text") == text)

    def test_outlines_become_positioned_items(self):
        self.assertEqual([item["kind"] for item in self.items], ["outline", "outline", "outline", "image"])
        self.assertEqual([item["id"] for item in self.items], ["i0", "i1", "i2", "i3"])
        first = self.items[0]
        self.assertEqual((first["x"], first["y"], first["width"]), (1.0, 2.396, 13.0))

    def test_side_by_side_outlines_become_columns(self):
        rows, scattered = compute(self.items)
        self.assertEqual(scattered, 0)
        self.assertEqual(rows[0], {"kind": "item", "item": "i0"})
        self.assertEqual(rows[1]["kind"], "columns")
        self.assertEqual([column["rows"] for column in rows[1]["columns"]],
                         [[{"kind": "item", "item": "i1"}], [{"kind": "item", "item": "i2"}]])
        self.assertAlmostEqual(sum(column["width"] for column in rows[1]["columns"]), 1.0, places=2)
        self.assertEqual(rows[2], {"kind": "item", "item": "i3"})

    def test_heading_ignores_its_style_and_fonts(self):
        heading = self.blocks[0]
        self.assertEqual((heading["type"], heading["level"], heading["text"]), ("heading", 1, "Goals v2"))
        self.assertNotIn("marks", heading)
        self.assertEqual(heading["object_id"], "{0C8F1B2A-3D4E-4F50-8A61-72B3C4D5E6F7}&2")

    def test_marks(self):
        block = self.blocks[1]
        text = block["text"]
        self.assertEqual(text, "Bold italic under strike x2 H2O red marked web Standup that paragraph big")
        found = {(mark["type"], text_of(mark, text)) for mark in block["marks"]}
        for expected in [("bold", "Bold"), ("italic", "italic"), ("underline", "under"),
                         ("strikethrough", "strike"), ("superscript", "2"), ("subscript", "2"),
                         ("color", "red"), ("highlight", "marked"), ("link", "web"), ("link", "Standup"),
                         ("link", "that paragraph"), ("font", "big"), ("font_size", "big")]:
            self.assertIn(expected, found)
        marks = {mark["type"] + ":" + text_of(mark, text): mark for mark in block["marks"]}
        self.assertEqual(marks["color:red"]["color"], "#C00000")
        self.assertEqual(marks["highlight:marked"]["color"], "#FFFF00")
        self.assertEqual(marks["link:web"]["href"], "https://example.com/a?b=1&c=2")
        self.assertNotIn("onenote", marks["link:web"])
        self.assertEqual(marks["link:Standup"]["onenote"]["page_id"], "{3F2504E0-4F89-11D3-9A0C-0305E82C3301}")
        self.assertEqual(marks["link:that paragraph"]["onenote"]["object_ref"],
                         "{11112222-3333-4444-5555-666677778888}&3C")
        self.assertEqual(marks["font:big"]["family"], "Segoe UI")
        self.assertEqual(marks["font_size:big"]["pt"], 14.0)
        self.assertEqual(block["marks"], sorted(block["marks"], key=lambda m: (m["start"], m["end"], m["type"])))

    def test_tags_and_check_items(self):
        todo = self.by_text("Write the spec")
        self.assertEqual((todo["type"], todo["checked"], todo["list"]), ("check_item", False, None))
        done = self.by_text("Book the room")
        self.assertTrue(done["checked"])
        self.assertEqual([tag["label"] for tag in done["tags"]], ["To Do", "Important"])
        question = self.by_text("Who owns QA?")
        self.assertEqual(question["type"], "paragraph")
        self.assertEqual(question["align"], "center")
        self.assertEqual(question["tags"][0]["label"], "Question")
        self.assertFalse(question["tags"][0]["checkable"])

    def test_lists_with_levels_and_tag_on_span(self):
        tagged = self.by_text("Bullet with tag")
        self.assertEqual(tagged["type"], "check_item")
        self.assertEqual(tagged["list"]["style"], "bullet")
        parent = self.by_text("Parent bullet")
        self.assertEqual((parent["type"], parent["indent"]), ("list_item", 0))
        child = self.by_text("Child b")
        self.assertEqual(child["indent"], 1)
        self.assertEqual(child["list"], {"ordered": True, "style": "lower_alpha", "marker": "%.", "restart": None})
        self.assertEqual(child["marks"], [{"start": 0, "end": 7, "type": "bold"}])
        third = self.by_text("Third step")
        self.assertEqual(third["list"]["restart"], 3)

    def test_consolas_paragraphs_merge_into_code(self):
        code = next(block for block in self.blocks if block["type"] == "code" and "main" in block["text"])
        self.assertEqual(code["text"], "def main():\n    return 1")

    def test_source_citation(self):
        citation = next(block for block in self.blocks if block.get("is_source_citation"))
        self.assertEqual(citation["type"], "paragraph")
        self.assertEqual(self.result.source_url, "https://source.example.org/article")
        self.assertEqual(self.items[0]["source_url"], "https://source.example.org/article")

    def test_mathml_kept_as_code_text_and_reported(self):
        self.assertEqual(self.blocks[-1], {"type": "code", "indent": 0, "text": "a = b 2",
                                           "source_url": "https://source.example.org/article"})
        self.assertTrue(any("equation" in warning for warning in self.result.warnings))
        self.assertFalse(any(block["type"] == "math" for block in self.blocks))

    def test_floating_image_uses_full_resolution_and_keeps_ocr_text(self):
        image = self.items[3]["blocks"][0]
        self.assertEqual(image["type"], "image")
        self.assertEqual(image["alt_text"], "Quarterly revenue chart Q1 Q2")
        self.assertEqual(image["data_status"], "available")
        self.assertEqual((image["width"], image["height"]), (6.667, 5.0))
        self.assertIn("aaa222", self.calls[0][0])

    def test_object_ids_are_unique(self):
        ids = [block["object_id"] for item in self.items for block in item["blocks"] if "object_id" in block]
        self.assertEqual(len(ids), len(set(ids)))


class ObjectsPageTests(unittest.TestCase):
    def setUp(self):
        self.result, self.calls = convert("page_objects.html")
        self.blocks = self.result.items[0]["blocks"]

    def test_nested_table(self):
        table = self.blocks[0]
        self.assertEqual((table["type"], table["row_count"], table["col_count"]), ("table", 2, 2))
        self.assertTrue(table["borders_visible"])
        self.assertEqual(table["column_widths"], [2.0, 3.0])
        header, body = table["rows"]
        self.assertEqual(header["cells"][0]["background"], "#DEEAF6")
        self.assertEqual(header["cells"][0]["blocks"][0]["marks"], [{"start": 0, "end": 4, "type": "bold"}])
        cell = body["cells"][0]["blocks"]
        self.assertEqual([block["type"] for block in cell], ["paragraph", "list_item", "table"])
        nested = cell[2]
        self.assertFalse(nested["borders_visible"])
        self.assertEqual(nested["rows"][0]["cells"][0]["blocks"][0]["text"], "deep cell")
        self.assertEqual(body["cells"][1]["background"], "#FFF2CC")

    def test_attachment_and_printout(self):
        kinds = [(block["type"], block.get("is_printout_page")) for block in self.blocks[2:5]]
        self.assertEqual(kinds, [("file", None), ("image", True), ("image", True)])
        pdf = self.blocks[2]
        self.assertEqual((pdf["filename"], pdf["extension"], pdf["mime"], pdf["data_status"]),
                         ("Contract.pdf", "pdf", "application/pdf", "available"))
        self.assertEqual([block["printout_page_number"] for block in self.blocks[3:5]], [0, 1])
        self.assertEqual(self.blocks[3]["alt_text"], "CONTRACT page one text")
        self.assertIn("bbb223", [url for url, _, _ in self.calls if "bbb22" in url][0])  # full resolution

    def test_unsafe_names_and_foreign_hosts(self):
        evil = self.blocks[5]
        self.assertNotIn("/", evil["filename"])
        self.assertEqual(evil["data_status"], "missing")
        audio = self.blocks[6]
        self.assertEqual((audio["media_kind"], audio["data_status"]), ("audio", "missing"))

    def test_video_embed(self):
        self.assertEqual(self.blocks[7], {"type": "embed", "indent": 0, "url": "https://www.youtube.com/watch?v=xyz",
                                          "thumbnail_asset": None, "alt_text": None})


class SmallCasesTests(unittest.TestCase):
    def blocks(self, body):
        html = f'<html><head><title>T</title></head><body>{body}</body></html>'
        return PageConverter(lambda *a: {"asset": None, "data_status": "missing"}).convert(html)

    def test_unpositioned_body_is_one_outline_in_document_order(self):
        result = self.blocks("<p>One</p><p>Two<br/>lines</p>")
        self.assertEqual(len(result.items), 1)
        self.assertIsNone(result.items[0]["x"])
        self.assertEqual([b["text"] for b in result.items[0]["blocks"]], ["One", "Two\nlines"])
        self.assertEqual(compute(result.items)[0], [{"kind": "item", "item": "i0"}])

    def test_whitespace_and_control_characters(self):
        result = self.blocks("<p>  a \n  b\x07 <span style='font-weight:bold'> c </span> </p><p>   </p>")
        [block] = result.items[0]["blocks"]
        self.assertEqual(block["text"], "a b� c")
        self.assertEqual(block["marks"], [{"start": 5, "end": 6, "type": "bold"}])

    def test_inline_equation_kept_as_text(self):
        result = self.blocks("<p>Area <math><mi>r</mi><mo>^</mo><mn>2</mn></math> here</p>")
        [block] = result.items[0]["blocks"]
        self.assertEqual((block["type"], block["text"]), ("paragraph", "Area r ^ 2 here"))
        self.assertTrue(result.warnings)

    def test_image_inside_paragraph_follows_text(self):
        result = self.blocks('<p>Look <img src="https://graph.microsoft.com/v1.0/me/onenote/resources/r1/$value" '
                             'alt="ocr"/></p>')
        blocks = result.items[0]["blocks"]
        self.assertEqual([b["type"] for b in blocks], ["paragraph", "image"])

    def test_web_link_to_a_onenote_page(self):
        href = ("https://onedrive.live.com/view.aspx?resid=X&id=documents&wd=target%28Meetings.one%7C"
                "8a9b0c1d-2e3f-4051-6273-8495a6b7c8d9%2FStandup%7C3f2504e0-4f89-11d3-9a0c-0305e82c3301%2F%29")
        target = onenote_link(href)
        self.assertEqual(target["page_id"], "{3F2504E0-4F89-11D3-9A0C-0305E82C3301}")
        self.assertEqual(target["section_id"], "{8A9B0C1D-2E3F-4051-6273-8495A6B7C8D9}")
        self.assertIsNone(onenote_link("https://example.com/"))

    def test_tags(self):
        self.assertEqual([(t["label"], t["checkable"], t["completed"]) for t in parse_tags("to-do:completed, idea")],
                         [("To Do", True, True), ("Idea", False, False)])
        self.assertTrue(parse_tags("call-back")[0]["checkable"])

    def test_resource_urls_prefers_full_resolution(self):
        html = (FIXTURES / "page_objects.html").read_text(encoding="utf-8")
        urls = [url for url, _ in resource_urls(html)]
        self.assertTrue(any("bbb223" in url for url in urls))
        self.assertFalse(any("bbb222" in url for url in urls))
        self.assertEqual(len(urls), 5)


if __name__ == "__main__":
    unittest.main()
