"""Scoring picture descriptions against tests/fixtures/captions/labels.json.

* Subject: the description names the picture's main subject (any of its words).
* Hallucination: the description uses an object word (:data:`VOCABULARY`, concrete
  things one would search for) that the picture's labels don't allow. A description
  with any such word counts as hallucinating; ``objects`` counts the words.

Used by tests/test_captions.py (real-model floors) and the research numbers in
docs/picture-descriptions.md.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "captions"

# Concrete things a search might name. Scene words that describe the whole picture
# rather than an object in it (view, background, image, scene, color) are left out.
VOCABULARY = frozenset("""
person people man woman boy girl child baby crowd hiker climber passenger player
lady guy men women
dog puppy cat kitten horse cow sheep goat pig bird duck goose chicken fish elephant bear
zebra giraffe lion tiger monkey deer rabbit squirrel butterfly insect bee animal otter
seal beaver mouse rat snake turtle frog
car truck bus train tram streetcar trolley bicycle bike motorcycle scooter boat ship
sailboat yacht airplane plane helicopter van taxi vehicle tractor
tree forest mountain hill lake river sea ocean beach sand snow ice grass field flower
sunflower tulip rose rock stone cloud sky sun moon bridge road street sidewalk path
building house tower church castle skyscraper city town village fence wall door window
roof bench sign pole lamp waterfall desert island harbor dock pier garden park
playground pond glacier valley coast shore cliff wave fountain statue
table desk chair sofa couch bed pillow cushion blanket shelf bookshelf bookcase book
laptop computer keyboard monitor screen phone television tv clock vase plant cup mug lamp
bottle plate bowl fork knife spoon kitchen sink oven refrigerator fridge microwave
stove counter cabinet curtain carpet rug mirror painting poster whiteboard
blackboard chalkboard board paper pen pencil marker bag backpack suitcase umbrella hat
shoe toy ball guitar piano camera candle lighter flame fire cake pizza bread sandwich
salad fruit apple banana vegetable tomato cheese coffee tea wine beer food
receipt ticket chart graph map flag kite skateboard surfboard ski snowboard racket
basket box envelope letter newspaper magazine notebook document email card menu
cigarette toilet towel bathtub shower dress shirt jacket glasses sunglasses
helmet jar scissors tool hammer clothes uniform baseball
football soccer tennis golf
""".split())

_IRREGULAR = {"men": "man", "women": "woman", "children": "child", "leaves": "leaf",
              "shelves": "shelf", "knives": "knife", "geese": "goose", "mice": "mouse",
              "feet": "foot", "teeth": "tooth", "bookshelves": "bookshelf"}


def _singular(word: str) -> str:
    if word in _IRREGULAR:
        return _IRREGULAR[word]
    if word in VOCABULARY or len(word) < 4:
        return word
    for suffix, replacement in (("ies", "y"), ("es", ""), ("s", "")):
        if word.endswith(suffix) and word[: -len(suffix)] + replacement in VOCABULARY:
            return word[: -len(suffix)] + replacement
    return word


# Phrases whose object word is no object in the picture.
_PHRASES = (("mountain bike", "bike"), ("at the camera", ""), ("into the camera", ""),
            ("toward the camera", ""), ("from the camera", ""), ("body of water", "water"))


def words(text: str) -> set[str]:
    text = text.lower()
    for phrase, replacement in _PHRASES:
        text = text.replace(phrase, replacement)
    return {_singular(word) for word in re.findall(r"[a-z]+", text)}


def labels() -> dict:
    return json.loads((FIXTURES / "labels.json").read_text(encoding="utf-8"))


def score(captions: dict[str, str]) -> dict[str, object]:
    """Subject rate, hallucination rate (share of descriptions with a wrong object word)
    and the wrong words, over the labelled pictures in ``captions`` (skipped ones aside)."""

    images = labels()["images"]
    rows, subject_hits, hallucinating, objects, wrong_objects = {}, 0, 0, 0, 0
    scored = 0
    for name, caption in captions.items():
        label = images[name]
        if label.get("skip"):
            continue
        scored += 1
        found = words(caption)
        allowed = {_singular(w) for w in label["allowed"] + label["subject"]}
        named = found & VOCABULARY
        wrong = sorted(named - allowed)
        has_subject = bool(found & {_singular(w) for w in label["subject"]})
        subject_hits += has_subject
        hallucinating += bool(wrong)
        objects += len(named)
        wrong_objects += len(wrong)
        rows[name] = {"caption": caption, "subject": has_subject, "wrong": wrong}
    return {
        "images": scored,
        "subject_rate": subject_hits / scored if scored else 0.0,
        "hallucination_rate": hallucinating / scored if scored else 0.0,
        "wrong_object_share": wrong_objects / objects if objects else 0.0,
        "rows": rows,
    }
