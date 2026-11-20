"""The layout port gives the Rust converter's answers (cases from src/layout.rs)."""

from __future__ import annotations

import unittest

from fortress_onenote_graph.layout import compute


def bx(name, x, y, w, h):
    return {"id": name, "x": x, "y": y, "est_width": w, "est_height": h}


def show(rows):
    out = []
    for row in rows:
        if row["kind"] == "item":
            out.append(row["item"])
        else:
            out.append(("!" if row["scattered"] else "") + "[" + "|".join(show(c["rows"]) for c in row["columns"]) + "]")
    return ",".join(out)


def run(boxes):
    return show(compute(boxes)[0])


class LayoutTests(unittest.TestCase):
    def test_cases_shared_with_the_rust_converter(self):
        self.assertEqual(run([]), "")
        self.assertEqual(run([bx("b", 1, 6, 12, 2), bx("a", 1, 1, 12, 3), bx("c", 1.2, 9, 8, 1)]), "a,b,c")
        self.assertEqual(run([bx("right", 9, 1.1, 3, 5), bx("left", 1, 1, 6, 4)]), "[left|right]")
        self.assertEqual(run([bx("a", 0, 0, 4, 6), bx("b", 5, 1, 4, 3), bx("c", 10, 2.5, 4, 2),
                              bx("footer", 0, 8, 14, 1)]), "[a|b|c],footer")
        self.assertEqual(run([bx("left", 0, 0, 6, 10), bx("r2", 7, 5, 5, 2), bx("r1", 7, 0.5, 5, 3)]),
                         "[left|r1,r2]")
        self.assertEqual(run([bx("left", 0, 0, 6, 12), bx("top", 7, 0, 10, 2), bx("m1", 7, 3, 4, 2),
                              bx("m2", 12, 3, 5, 2)]), "[left|top,[m1|m2]]")
        self.assertEqual(run([bx("a", 1, 1, 12, 3), bx("b", 1.05, 3.9, 12, 2)]), "a,b")
        self.assertEqual(run([bx("a", 0, 0, 4, 2), bx("b", 6, 1.9, 4, 2)]), "a,b")
        self.assertEqual(run([bx("a", 0, 0, 4, 2), bx("b", 4.05, 0, 4, 2)]), "[a|b]")
        self.assertEqual(run([bx("a", 1, 1, 10, 5), bx("b", 3, 2, 10, 5)]), "a,b")
        cols = [bx(f"c{i}", 0.5 + i * 4.2, 2.8, 3.9, 6.7) for i in range(4)]
        self.assertEqual(run(cols + [bx("img", 4.7, 3.1, 5.6, 3.3)]), "[c0|c1|c2|c3],img")
        self.assertEqual(run([bx("header", 0, 0, 20, 3), bx("a", 0, 2.5, 8, 4), bx("b", 10, 2.5, 8, 4)]),
                         "header,[a|b]")
        self.assertEqual(run([bx("a", 0, 0, 10, 3), bx("b", 0, 3, 10, 3), bx("img", 2, 1, 4, 4)]), "a,img,b")

    def test_scattered_and_grid(self):
        rows, scattered = compute([bx(f"n{i}", i * 3, 0, 2, 1) for i in range(6)])
        self.assertEqual((show(rows), scattered), ("![n0|n1|n2|n3|n4|n5]", 1))
        rows, scattered = compute([bx(f"c{i}", i * 4.2, 2.8, 3.9, 6.7) for i in range(5)])
        self.assertEqual((show(rows), scattered), ("[c0|c1|c2|c3|c4]", 0))
        self.assertEqual(compute([bx(f"c{i}", i * 4.2, i * 0.4, 3.9, 6.7) for i in range(5)])[1], 1)

    def test_unpositioned_items_keep_document_order(self):
        rows, _ = compute([{"id": "a", "x": None, "y": None, "est_width": 13, "est_height": 1},
                           bx("b", 0, 0, 4, 2)])
        self.assertEqual(show(rows), "a,b")


if __name__ == "__main__":
    unittest.main()
