"""Per-page checks: reference ``Model`` vs Fortress ``Model``.

Every check returns ``{"name", "status": "pass"|"fail"|"n/a", "summary", "details": [str]}``.
"n/a" means neither side has anything of that kind.
"""

from __future__ import annotations

import re
from collections import Counter

from model import Model, words

CHECKS = ("title", "text", "marks", "headings", "lists", "checks", "links", "tables", "images", "image_alt",
          "files", "math", "ink", "tags", "align", "layout", "export")
MAX_DETAILS = 40


def _result(name, ok, summary, details=(), applicable=True):
    status = "n/a" if not applicable else ("pass" if ok else "fail")
    return {"name": name, "status": status, "summary": summary, "details": list(details)[:MAX_DETAILS],
            "detail_count": len(list(details))}


def _context(text: str, word: str, width: int = 50) -> str:
    match = re.search(rf"(?i)(?<!\w){re.escape(word)}(?!\w)", text)
    if not match:
        return word
    start, end = max(0, match.start() - width), min(len(text), match.end() + width)
    return ("…" if start else "") + text[start:match.start()] + "⟦" + match.group(0) + "⟧" + text[match.end():end] + ("…" if end < len(text) else "")


def _counter_diff(ref: Counter, ours: Counter):
    return ref - ours, ours - ref


def check_title(ref: Model, ours: Model):
    ok = words(ref.title) == words(ours.title)
    return _result("title", ok, "same" if ok else f"{ref.title!r} vs {ours.title!r}",
                   [] if ok else [f"reference: {ref.title}", f"fortress: {ours.title}"])


def check_text(ref: Model, ours: Model):
    missing, extra = _counter_diff(Counter(ref.word_list()), Counter(ours.word_list()))
    # Inline equations: one2html renders MathML (not counted), Fortress keeps OneNote's linear text.
    math_words = Counter(w for text in ref.math for w in words(text))
    explained = Counter({w: min(n, math_words[w]) for w, n in extra.items() if math_words[w]})
    extra = extra - explained
    details = [f"missing ×{n}: {_context(ref.text, w)}" for w, n in sorted(missing.items())]
    details += [f"extra ×{n}: {_context(ours.text, w)}" for w, n in sorted(extra.items())]
    total = sum(Counter(ref.word_list()).values())
    summary = f"{total} words; missing {sum(missing.values())}, extra {sum(extra.values())}"
    if explained:
        summary += f" (+{sum(explained.values())} equation words as linear text)"
    return _result("text", not missing and not extra, summary, details, applicable=bool(total or ours.tokens))


def _mark_counter(model: Model) -> Counter:
    return Counter((mark, token.word) for token in model.tokens for mark in token.marks)


def check_marks(ref: Model, ours: Model):
    ref_marks, our_marks = _mark_counter(ref), _mark_counter(ours)
    missing, extra = _counter_diff(ref_marks, our_marks)
    by_kind: dict[str, list] = {}
    for (mark, word), n in missing.items():
        by_kind.setdefault(f"missing {mark}", []).append(word)
    for (mark, word), n in extra.items():
        by_kind.setdefault(f"extra {mark}", []).append(word)
    details = [f"{kind} ({len(ws)} words): {' '.join(ws[:15])}" for kind, ws in sorted(by_kind.items())]
    applicable = bool(ref_marks or our_marks)
    return _result("marks", not details, f"{sum(ref_marks.values())} marked words; {len(details)} differing mark kinds",
                   details, applicable)


def _multiset_check(name, ref_items, our_items, fmt):
    missing, extra = _counter_diff(Counter(ref_items), Counter(our_items))
    details = [f"missing: {fmt(item)}" for item in missing.elements()]
    details += [f"extra: {fmt(item)}" for item in extra.elements()]
    return _result(name, not details, f"{len(ref_items)} vs {len(our_items)}", details,
                   applicable=bool(ref_items or our_items))


def check_headings(ref: Model, ours: Model):
    # Empty heading-styled lines are blank lines (dropped on import).
    return _multiset_check("headings", [(lvl, " ".join(words(t))) for lvl, t in ref.headings if words(t)],
                           [(lvl, " ".join(words(t))) for lvl, t in ours.headings if words(t)],
                           lambda i: f"h{i[0]} {i[1]!r}")


def check_lists(ref: Model, ours: Model):
    return _multiset_check(
        "lists",
        [(" ".join(words(t)), o, d, _CSS_LIST_STYLES.get(s, s) if o else "bullet") for t, o, d, s in ref.list_items],
        [(" ".join(words(t)), o, d, s if o else "bullet") for t, o, d, s in ours.list_items],
        lambda i: f"{i[3] if i[1] else 'bullet'} depth {i[2]}: {i[0][:80]!r}",
    )


_CSS_LIST_STYLES = {"lower-latin": "lower_alpha", "upper-latin": "upper_alpha", "lower-alpha": "lower_alpha",
                    "upper-alpha": "upper_alpha", "lower-roman": "lower_roman", "upper-roman": "upper_roman"}


def check_checks(ref: Model, ours: Model):
    return _multiset_check("checks", [(" ".join(words(t)), c) for t, c in ref.checks],
                           [(" ".join(words(t)), c) for t, c in ours.checks],
                           lambda i: f"[{'x' if i[1] else ' '}] {i[0][:80]!r}")


# Mirrors backend/app/onenote_import.py _SAFE_LINK_SCHEMES.
WEB_SCHEMES = frozenset({"http", "https", "mailto", "tel", "ftp", "ftps", "sftp", "news", "sms", "geo"})
_PAGE_ID_RE = re.compile(r"page-id=(\{[0-9A-Fa-f-]{36}\})")


def check_links(ref: Model, ours: Model, imported_pages: set[str]):
    ours_by_text: dict[str, list[str]] = {}
    for text, href in ours.links:
        ours_by_text.setdefault(" ".join(words(text)), []).append(href)
    details, info = [], []
    for text, href in ref.links:
        key = " ".join(words(text))
        candidates = ours_by_text.get(key, [])
        if href.lower().startswith("onenote:"):
            internal = [h for h in candidates if h.startswith(("[[", "(("))]
            match = _PAGE_ID_RE.search(href)
            target = match.group(1).upper() if match else None
            if internal:
                candidates.remove(internal[0])
            elif target and target in imported_pages:
                details.append(f"onenote link not rewritten although its page was imported: {text!r} -> {href[:120]}")
            else:
                info.append(f"onenote link to a page outside this export (kept as text): {text!r}")
            continue
        scheme = re.match(r"^\s*([A-Za-z][A-Za-z0-9+.-]*):", href)
        if (scheme is None and not href.lower().startswith("www.")) or (scheme and scheme.group(1).lower() not in WEB_SCHEMES):
            # The importer keeps only web/mail links clickable (file:, UNC, javascript:, ms-word: ...
            # stay text; the target is in the block's provenance).
            info.append(f"non-web link kept as text by design: {text[:60]!r} -> {href[:80]}")
            continue
        if scheme is None and re.match(r"^www\.[^\s/]+", href):
            href = "http://" + href  # OneNote opens scheme-less www. links as web links (so does the importer)
        normalized = href.rstrip("/")
        hit = next((h for h in candidates if h.rstrip("/") == normalized), None)
        if hit is not None:
            candidates.remove(hit)
        else:
            details.append(f"missing link {text!r} -> {href[:120]}" + (f" (fortress has {candidates[0][:80]})" if candidates else ""))
    leftover = [(t, h) for t, hs in ours_by_text.items() for h in hs]
    extra = [f"extra link {t!r} -> {h[:100]}" for t, h in leftover if not h.startswith(("[[", "(("))]
    result = _result("links", not details, f"{len(ref.links)} vs {len(ours.links)}", details + extra + info,
                     applicable=bool(ref.links or ours.links))
    if extra and not details:
        result["summary"] += f" ({len(extra)} extra, e.g. auto-linked URLs)"
    return result


def check_tables(ref: Model, ours: Model):
    details = []
    if len(ref.tables) != len(ours.tables):
        details.append(f"table count {len(ref.tables)} vs {len(ours.tables)}")
    for index, (a, b) in enumerate(zip(ref.tables, ours.tables)):
        if (a["rows"], a["cols"]) != (b["rows"], b["cols"]):
            details.append(f"table {index + 1}: shape {a['rows']}×{a['cols']} vs {b['rows']}×{b['cols']}")
            continue
        for r, (ra, rb) in enumerate(zip(a["cells"], b["cells"])):
            for c, (ca, cb) in enumerate(zip(ra, rb)):
                if ca != cb:
                    details.append(f"table {index + 1} cell ({r + 1},{c + 1}): {ca[:60]!r} vs {cb[:60]!r}")
        for r, (ra, rb) in enumerate(zip(a["bg"], b["bg"])):
            for c, (ca, cb) in enumerate(zip(ra, rb)):
                if ca != cb:
                    details.append(f"table {index + 1} cell ({r + 1},{c + 1}) background {ca} vs {cb}")
    shapes = ", ".join(f"{t['rows']}×{t['cols']}" for t in ref.tables)
    return _result("tables", not details, f"{len(ref.tables)} tables ({shapes})", details,
                   applicable=bool(ref.tables or ours.tables))


def _image_match(image: dict, pool: list[dict]) -> dict | None:
    """The Fortress image with the same bytes (or the same original, e.g. an XPS printout),
    preferring one with the same alt text (printout pages share one original)."""

    if not image["sha"]:
        return None
    candidates = [o for o in pool if image["sha"] == o["sha"]] or \
        [o for o in pool if image["sha"] == o.get("original_sha")]
    same_alt = [o for o in candidates if words(o["alt"]) == words(image.get("alt") or "")]
    return (same_alt or candidates or [None])[0]


def check_images(ref: Model, ours: Model):
    ours_pool = list(ours.images)
    details = []
    for image in sorted(ref.images, key=lambda i: not i.get("alt")):  # alt-matched pairs first
        hit = _image_match(image, ours_pool)
        if hit is None and image["sha"] is None:
            continue  # no data in the file either (Fortress shows "Image missing from the OneNote export")
        if hit is None:
            details.append(f"missing image {image['name']} ({image['size']} bytes, sha {str(image['sha'])[:12]})")
            continue
        ours_pool.remove(hit)
    details += [f"extra image {o['name'] or '?'} (sha {str(o['sha'])[:12]})" for o in ours_pool]
    return _result("images", not details, f"{len(ref.images)} vs {len(ours.images)}", details,
                   applicable=bool(ref.images or ours.images))


def check_image_alt(ref: Model, ours: Model):
    details = []
    pool = list(ours.images)
    for image in sorted(ref.images, key=lambda i: not i.get("alt")):  # alt-matched pairs first
        hit = _image_match(image, pool)
        if hit is None:
            continue
        pool.remove(hit)
        if image["alt"] and words(image["alt"]) != words(hit["alt"]):
            details.append(f"{image['name']}: alt {image['alt'][:80]!r} vs {hit['alt'][:80]!r}")
    with_alt = sum(1 for i in ref.images if i["alt"])
    return _result("image_alt", not details, f"{with_alt} images with alt/OCR text", details,
                   applicable=bool(ref.images))


def check_files(ref: Model, ours: Model):
    pool = list(ours.files)
    details = []
    for item in ref.files:
        hit = next((o for o in pool if o["sha"] == item["sha"] and (item["sha"] or o["name"] in item["name"]
                                                                      or item["name"].startswith(o["name"].rsplit(".", 1)[0]))), None)
        if hit is None:
            details.append(f"missing attachment {item['name']} ({item['size']} bytes)")
            continue
        pool.remove(hit)
        if item["size"] is not None and hit["size"] not in (None, item["size"]):
            details.append(f"{item['name']}: size {item['size']} vs {hit['size']}")
    details += [f"extra attachment {o['name']} ({o['size']} bytes)" for o in pool]
    return _result("files", not details, f"{len(ref.files)} vs {len(ours.files)}", details,
                   applicable=bool(ref.files or ours.files))


def check_count(name, ref_items, our_items, fmt=str):
    ok = len(ref_items) == len(our_items)
    details = [] if ok else [f"reference: {fmt(i)}" for i in ref_items] + [f"fortress: {fmt(i)}" for i in our_items]
    return _result(name, ok, f"{len(ref_items)} vs {len(our_items)}", details,
                   applicable=bool(ref_items or our_items))


def check_ink(ref: Model, ours: Model):
    """Drawings: present on both sides, recognised handwriting kept.

    one2html draws every ink object separately while the converter merges a
    page's drawings into one SVG, so counts differ by design; the side-by-side
    image shows the strokes.
    """

    ref_words = Counter(w for i in ref.ink for w in words(i.get("text") or ""))
    our_words = Counter(w for i in ours.ink for w in words(i.get("text") or ""))
    details = []
    if bool(ref.ink) != bool(ours.ink):
        details.append(f"ink objects: {len(ref.ink)} vs {len(ours.ink)}")
    missing = ref_words - our_words
    if missing:
        details.append(f"recognised handwriting missing: {' '.join(missing.elements())}")
    return _result("ink", not details, f"{len(ref.ink)} vs {len(ours.ink)} drawings", details,
                   applicable=bool(ref.ink or ours.ink))


def check_tags(ref: Model, ours: Model):
    return _multiset_check("tags", [" ".join(words(t))[:60] for _, t in ref.tags],
                           [" ".join(words(t))[:60] for _, t in ours.tags], lambda i: f"tag on {i!r}")


def check_align(ref: Model, ours: Model):
    return _multiset_check("align", [(a, " ".join(words(t))[:60]) for a, t in ref.aligned],
                           [(a, " ".join(words(t))[:60]) for a, t in ours.aligned], lambda i: f"{i[0]}: {i[1]!r}")


# --- layout ------------------------------------------------------------------------------------


def _match_containers(ref: Model, ours: Model) -> dict[int, dict]:
    """Reference container -> where its words landed in Fortress.

    The two word streams are aligned (difflib); each reference container gets the
    Fortress region most of its words fell into (``(top block,)`` or ``(top block,
    column, block, column…)`` up to the innermost column) and the first aligned
    Fortress word index (document order).
    """

    from difflib import SequenceMatcher

    # Align each reference container on its own (Fortress may order containers
    # differently, which a single whole-page alignment would hide).
    b = ours.word_list()
    mapping: dict[int, int] = {}
    by_container: dict[int, list[int]] = {}
    for index, token in enumerate(ref.tokens):
        by_container.setdefault(token.container, []).append(index)
    for indices in by_container.values():
        a = [ref.tokens[i].word for i in indices]
        matcher = SequenceMatcher(None, a, b, autojunk=False)
        for block in matcher.get_matching_blocks():
            for k in range(block.size):
                mapping[indices[block.a + k]] = block.b + k
    found: dict[int, dict] = {}
    for index, token in enumerate(ref.tokens):
        if index not in mapping:
            continue
        target = mapping[index]
        path = ours.containers[ours.tokens[target].container]["path"] if ours.tokens[target].container >= 0 else [target]
        cut = max((i for i in range(1, len(path), 2)), default=0) + 1
        region = tuple(path[:cut])
        info = found.setdefault(token.container, {"regions": Counter(), "first": target, "count": 0})
        info["regions"][region] += 1
        info["first"] = min(info["first"], target)
        info["count"] += 1
    result = {}
    for container, info in found.items():
        total = sum(1 for t in ref.tokens if t.container == container)
        if info["count"] >= max(1, total // 3):
            result[container] = {"region": info["regions"].most_common(1)[0][0], "first": info["first"]}
    return result


def check_layout(ref: Model, ours: Model, boxes: list[dict] | None):
    if not boxes:
        return _result("layout", True, "no reference geometry", applicable=False)
    matched = _match_containers(ref, ours)
    details = []
    side_by_side = 0
    for a in range(len(boxes)):
        for b in range(len(boxes)):
            if a == b or a not in matched or b not in matched:
                continue
            A, B = boxes[a], boxes[b]
            if not A or not B or A["h"] <= 0 or B["h"] <= 0:
                continue
            v_overlap = min(A["y"] + A["h"], B["y"] + B["h"]) - max(A["y"], B["y"])
            h_disjoint = A["x"] + A["w"] <= B["x"] + 4
            pa, pb = list(matched[a]["region"]), list(matched[b]["region"])
            if h_disjoint and v_overlap > 0.3 * min(A["h"], B["h"]):
                side_by_side += 1
                # Expect: same columns block, A's column left of B's.
                common = 0
                while common < min(len(pa), len(pb)) and pa[common] == pb[common]:
                    common += 1
                if common >= min(len(pa), len(pb)) or common % 2 == 0:
                    details.append(f"side by side in OneNote but stacked in Fortress: {ref.containers[a]['text'][:50]!r} | {ref.containers[b]['text'][:50]!r}")
                elif pa[common] > pb[common]:
                    details.append(f"column order swapped: {ref.containers[a]['text'][:50]!r} should be left of {ref.containers[b]['text'][:50]!r}")
            elif A["y"] + A["h"] <= B["y"] + 2 and min(A["x"] + A["w"], B["x"] + B["w"]) - max(A["x"], B["x"]) > 0:
                # A above B in the same horizontal region: A must come first.
                if matched[a]["first"] > matched[b]["first"] and not _is_prefix_column_sibling(pa, pb):
                    details.append(f"order: {ref.containers[a]['text'][:50]!r} is above {ref.containers[b]['text'][:50]!r} in OneNote but after it in Fortress")
    unmatched = [c["text"][:50] for i, c in enumerate(ref.containers) if c.get("words") and i not in matched]
    details += [f"outline not located in Fortress: {t!r}" for t in unmatched]
    return _result("layout", not details,
                   f"{len(boxes)} positioned items, {side_by_side} side-by-side pairs, {len(_columns(ours))} columns blocks",
                   details, applicable=len(boxes) > 1)


def _is_prefix_column_sibling(pa, pb):
    """Whether two Fortress paths first differ at a column index (different columns of one block)."""

    common = 0
    while common < min(len(pa), len(pb)) and pa[common] == pb[common]:
        common += 1
    return common < min(len(pa), len(pb)) and common % 2 == 1


def _columns(ours: Model):
    return {tuple(c["path"][:-2]) for c in ours.containers if len(c["path"]) > 1}


def check_export(ref: Model, export: dict | None, tree: dict | None):
    """The Markdown export, rendered by a CommonMark viewer, still shows every word,
    makes no code block out of non-code content and keeps list nesting."""

    if export is None or tree is None:
        return _result("export", True, "no export", applicable=False)
    missing = Counter(ref.word_list()) - Counter(export["words"])
    details = [f"missing ×{n}: {_context(ref.text, w)}" for w, n in sorted(missing.items())]
    if export["pre"] > tree["code"]:
        details.append(f"{export['pre'] - tree['code']} code block(s) in the rendered export that are not code in the note")
    if export["list_depth"] < tree["list_depth"]:
        details.append(f"list nesting {tree['list_depth']} in the note but {export['list_depth']} in the rendered export")
    return _result("export", not details, f"{len(export['words'])} words, {export['pre']} code blocks, list depth {export['list_depth']}",
                   details, applicable=bool(ref.tokens))


def run_all(ref: Model, ours: Model, boxes, imported_pages: set[str], export: dict | None = None,
            tree: dict | None = None) -> list[dict]:
    return [
        check_title(ref, ours),
        check_text(ref, ours),
        check_marks(ref, ours),
        check_headings(ref, ours),
        check_lists(ref, ours),
        check_checks(ref, ours),
        check_links(ref, ours, imported_pages),
        check_tables(ref, ours),
        check_images(ref, ours),
        check_image_alt(ref, ours),
        check_files(ref, ours),
        check_count("math", ref.math, ours.math),
        check_ink(ref, ours),
        check_tags(ref, ours),
        check_align(ref, ours),
        check_layout(ref, ours, boxes),
        check_export(ref, export, tree),
    ]
