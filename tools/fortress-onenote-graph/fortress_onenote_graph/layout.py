"""Reading-order layout of a page's positioned items: a port of the Rust
converter's ``src/layout.rs`` (same tolerances, bands, columns, bridges, scattered
flag; tools/fortress-onenote/SCHEMA.md "Layout"), so both routes lay pages out alike.

Items without a position (a page that is not absolutely positioned) keep
their document order as plain rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

V_TOLERANCE = 0.15
H_TOLERANCE = 0.1
MAX_COLUMNS = 4
GRID_MAX_COLUMNS = 5
GRID_TOP_TOLERANCE = 0.5
MAX_DEPTH = 6


@dataclass(frozen=True, eq=False)
class Box:
    id: str
    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + max(self.w, 0.01)

    @property
    def bottom(self) -> float:
        return self.y + max(self.h, 0.01)


def _r3(value: float) -> float:
    return round(value * 1000) / 1000


def _item(box: Box) -> dict[str, Any]:
    return {"kind": "item", "item": box.id}


def _sorted(boxes: list[Box]) -> list[Box]:
    return sorted(boxes, key=lambda b: (b.y, b.x))


def _h_overlap(a: Box, b: Box) -> bool:
    return max(a.x, b.x) < min(a.right, b.right) - H_TOLERANCE


def _v_overlap(a: Box, b: Box) -> bool:
    return max(a.y, b.y) < min(a.bottom, b.bottom) - V_TOLERANCE


def compute(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Layout rows for manifest items, and the number of scattered bands."""

    positioned = [item for item in items if item.get("x") is not None and item.get("y") is not None]
    if len(positioned) != len(items):  # mixed or unpositioned: document order
        return [{"kind": "item", "item": item["id"]} for item in items], 0
    boxes = [Box(item["id"], float(item["x"]), float(item["y"]), float(item["est_width"]),
                 float(item["est_height"])) for item in items]
    counter = [0]
    rows = _layout(boxes, 0, counter)
    return rows, counter[0]


def _row_top(row: dict[str, Any], tops: dict[str, float]) -> float:
    if row["kind"] == "item":
        return tops.get(row["item"], 0.0)
    return min((_row_top(r, tops) for column in row["columns"] for r in column["rows"]), default=float("inf"))


def _layout(boxes: list[Box], depth: int, scattered: list[int]) -> list[dict[str, Any]]:
    ordered = _sorted(boxes)
    if len(ordered) <= 1 or depth >= MAX_DEPTH:
        return [_item(b) for b in ordered]
    bands: list[list[Any]] = []  # [bottom, members]
    for box in ordered:
        if bands and box.y < bands[-1][0] - V_TOLERANCE:
            bands[-1][0] = max(bands[-1][0], box.bottom)
            bands[-1][1].append(box)
        else:
            bands.append([box.bottom, [box]])
    rows: list[dict[str, Any]] = []
    for _, members in bands:
        if len(members) == 1:
            rows.append(_item(members[0]))
            continue
        found = _bridges(members)
        if found:
            rows.extend(_without_bridges(members, found, depth, scattered))
            continue
        cols: list[list[Any]] = []  # [left, right, members]
        for box in sorted(members, key=lambda b: (b.x, b.y)):
            if cols and box.x < cols[-1][1] - H_TOLERANCE:
                cols[-1][1] = max(cols[-1][1], box.right)
                cols[-1][2].append(box)
            else:
                cols.append([box.x, box.right, [box]])
        if len(cols) == 1:
            rows.extend(_item(b) for b in _sorted(members))
            continue
        total = sum(max(right - left, 0.01) for left, right, _ in cols)
        is_scattered = _is_scattered(cols)
        if is_scattered:
            scattered[0] += 1
        rows.append({
            "kind": "columns", "scattered": is_scattered,
            "columns": [{"width": _r3(max(right - left, 0.01) / total), "x": _r3(left), "span": _r3(right - left),
                         "rows": _layout(m, depth + 1, scattered)} for left, right, m in cols],
        })
    return rows


def _is_scattered(cols: list[list[Any]]) -> bool:
    if len(cols) <= MAX_COLUMNS:
        return False
    tops = [min(b.y for b in m) for _, _, m in cols]
    return len(cols) > GRID_MAX_COLUMNS or max(tops) - min(tops) > GRID_TOP_TOLERANCE


def _bridges(members: list[Box]) -> list[Box]:
    if len(members) < 3:
        return []
    found: list[Box] = []
    for box in sorted(members, key=lambda b: (-b.w, b.y)):
        touching = [other for other in members if other is not box and other not in found
                    and _h_overlap(other, box) and _v_overlap(other, box)]
        if any(_v_overlap(p, q) and not _h_overlap(p, q)
               for i, p in enumerate(touching) for q in touching[i + 1:]):
            found.append(box)
    return found


def _without_bridges(members: list[Box], found: list[Box], depth: int, scattered: list[int]) -> list[dict[str, Any]]:
    rest = [m for m in members if m not in found]
    tops = {m.id: m.y for m in members}
    keyed = [(_row_top(row, tops), index, row) for index, row in enumerate(_layout(rest, depth + 1, scattered))]
    n = len(keyed)
    for k, box in enumerate(_sorted(found)):
        keyed.append((box.y, n + k, _item(box)))
    keyed.sort(key=lambda entry: (entry[0], entry[1]))
    return [row for _, _, row in keyed]
