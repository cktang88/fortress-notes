"""Measure related-paragraph scores with the real ColBERT model.

Slow and optional: it loads ``lightonai/Agent-ModernColBERT`` (from the Hugging
Face cache; set HF_HUB_OFFLINE=1 to stay offline) and is not part of the unit
suite. Run from ``backend/``:

    HF_HUB_OFFLINE=1 uv run --frozen python scripts/calibrate_related.py

It scores every paragraph of a ~40-note sample against every other with the same
normalized score the app uses (``embeddings.relevance``: MaxSim divided by the
number of query tokens) and prints how genuinely related pairs, same-domain
neighbours and unrelated pairs are distributed, plus how many of each a cutoff
keeps. The chosen cutoff lives in ``embeddings.RELATED_MIN_SCORE``.
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from app import embeddings, relevance  # noqa: E402

# (group, domain, text). Same group = genuinely related; same domain but another
# group = a neighbour (shares a broad subject only); anything else is unrelated.
NOTES: list[tuple[str, str, str]] = [
    ("sourdough", "cooking", "Fed the sourdough starter at 8am, it doubled by noon. Use 20% rye next time."),
    ("sourdough", "cooking", "Sourdough loaf came out dense. Probably under-proofed; the levain wasn't bubbly enough."),
    ("sourdough", "cooking", "Bread"),
    ("curry", "cooking", "Chickpea curry: onions, garlic, ginger, garam masala, a can of coconut milk, simmer 20 min."),
    ("curry", "cooking", "Next time the coconut chickpea curry needs more ginger and a squeeze of lime at the end."),
    ("knives", "cooking", "Sharpen the chef's knife on the 1000 grit whetstone, then hone before each use."),
    ("japan", "travel", "Kyoto itinerary: Fushimi Inari at dawn, then Kiyomizu-dera, dinner in Pontocho alley."),
    ("japan", "travel", "Book the JR pass before the Japan trip; the Shinkansen from Tokyo to Kyoto takes 2h15."),
    ("japan", "travel", "Japan"),
    ("packing", "travel", "Packing list for carry-on only: two shirts, merino socks, charger, passport, toothbrush."),
    ("lisbon", "travel", "Lisbon: tram 28 is packed, walk up to the Alfama instead. Pastéis de nata at Manteigaria."),
    ("lisbon", "travel", "Weekend in Lisbon cost about 600 euros including flights; stayed near Bairro Alto."),
    ("bathroom", "home", "Bathroom renovation quote from the tiler: 4,200 for walls and floor, two weeks of work."),
    ("bathroom", "home", "Chose the matte grey tiles for the bathroom floor and white subway tiles for the shower."),
    ("paint", "home", "Living room paint: two coats of eggshell in Farrow & Ball Skimming Stone, prime the patches first."),
    ("garden", "home", "Planted tomatoes and basil in the raised bed; water every other morning in July."),
    ("garden", "home", "The tomato plants have blight on the lower leaves. Remove them and stop watering from above."),
    ("standup", "work", "Standup: Maya is blocked on the API migration, waiting on credentials from the platform team."),
    ("standup", "work", "API migration status: credentials arrived, Maya expects the cutover on Thursday."),
    ("q3", "work", "Q3 planning meeting: focus on onboarding conversion, drop the dashboard redesign for now."),
    ("q3", "work", "Action items from quarterly planning: hire a designer for onboarding, revisit the roadmap in October."),
    ("oneonone", "work", "1:1 with Sam: wants more ownership, suggested leading the incident review process."),
    ("running", "health", "Ran 8 km in 44 minutes, knee felt fine. Increase the long run by 1 km each week."),
    ("running", "health", "Half marathon training plan: three easy runs, one tempo run and one long run per week."),
    ("sleep", "health", "Sleep has been bad: no screens after 10pm, no coffee after 2pm, keep the room cool."),
    ("doctor", "health", "Doctor said cholesterol is slightly high; retest in six months and eat more fiber."),
    ("budget", "finance", "Monthly budget: rent 1,450, groceries 400, transport 90, savings 600."),
    ("budget", "finance", "Overspent on groceries again this month, 520 instead of the 400 budgeted."),
    ("taxes", "finance", "Tax return: gather the P60, charitable donation receipts and the freelance invoices by January."),
    ("invest", "finance", "Moved the emergency fund to a high-interest savings account at 4.1%."),
    ("dune", "books", "Finished Dune. The ecology of Arrakis and the spice economy were more interesting than Paul."),
    ("dune", "books", "Dune Messiah is darker than the first book; Paul sees the jihad coming and can't stop it."),
    ("habits", "books", "Atomic Habits notes: make it obvious, attractive, easy and satisfying. Habit stacking works."),
    ("cat", "pets", "Took the cat to the vet for her vaccines; she weighs 4.3 kg, next visit in a year."),
    ("cat", "pets", "The cat keeps scratching the sofa. Try a taller scratching post and double-sided tape."),
    ("car", "errands", "Car service due at 60,000 km: oil change, brake pads front, replace the wiper blades."),
    ("wifi", "tech", "Wi-Fi drops in the bedroom. Move the router off the floor or buy a mesh extender."),
    ("wifi", "tech", "Bought a mesh Wi-Fi system; the bedroom now gets 300 Mbps instead of dropping out."),
    ("birthday", "family", "Mum's 70th birthday: book the restaurant for 12 people, order the cake from the bakery."),
    ("guitar", "hobbies", "Practice the F barre chord for ten minutes a day; it still buzzes on the B string."),
]


def main() -> None:
    model = embeddings._get_model()
    if model is None:
        sys.exit("ColBERT model unavailable (is it in the Hugging Face cache?)")
    started = time.monotonic()
    texts = [text for *_, text in NOTES]
    queries = [np.asarray(e) for e in model.encode(texts, is_query=True, show_progress_bar=False)]
    docs = [np.asarray(e) for e in model.encode(texts, is_query=False, show_progress_bar=False)]
    print(f"encoded {len(NOTES)} paragraphs twice in {time.monotonic() - started:.1f}s")
    # Raw MaxSim, exactly what embeddings.score_documents returns.
    raw = np.array([[float((q @ d.T).max(axis=1).sum()) for d in docs] for q in queries])
    tokens = [len(q) for q in queries]

    per_token: dict[str, list[float]] = {"related": [], "neighbour": [], "unrelated": []}
    normalized: dict[str, list[float]] = {"related": [], "neighbour": [], "unrelated": []}
    ratios: list[float] = []
    unrelated_pairs: list[tuple[float, str, str]] = []
    for i, (group, domain, text) in enumerate(NOTES):
        others = {str(j): raw[i, j] for j in range(len(NOTES)) if j != i}
        kept_all = relevance.relevant_model_scores(others, tokens[i], cutoff=-10.0)
        for j, (other_group, other_domain, other_text) in enumerate(NOTES):
            if i == j:
                continue
            kind = "related" if group == other_group else "neighbour" if domain == other_domain else "unrelated"
            per_token[kind].append(raw[i, j] / tokens[i])
            normalized[kind].append(kept_all[str(j)])
            if kind == "unrelated":
                ratios.append(raw[i, j] / tokens[i])
                unrelated_pairs.append((kept_all[str(j)], text[:45], other_text[:45]))

    def describe(name: str, values: list[float]) -> None:
        values = sorted(values)
        q = statistics.quantiles(values, n=100)
        print(
            f"  {name:>10}: n={len(values):4d} min={values[0]:+.3f} p5={q[4]:+.3f} "
            f"median={statistics.median(values):+.3f} p95={q[94]:+.3f} p99={q[98]:+.3f} max={values[-1]:+.3f}"
        )

    print("\nMaxSim / query tokens:")
    for kind, values in per_token.items():
        describe(kind, values)
    print(f"\nunrelated raw / query tokens: median {statistics.median(ratios):.3f}, "
          f"p90 {statistics.quantiles(ratios, n=10)[-1]:.3f}")
    print("\nrelevance.relevant_model_scores (normalized):")
    for kind, values in normalized.items():
        describe(kind, values)
    print("\nhighest unrelated pairs:")
    for score, a, b in sorted(unrelated_pairs, reverse=True)[:6]:
        print(f"  {score:+.3f}  {a!r} -> {b!r}")
    print("\ncutoff  related  neighbour  unrelated")
    for cutoff in [x / 100 for x in range(0, 31, 2)]:
        kept = [sum(s >= cutoff for s in v) / len(v) for v in normalized.values()]
        marker = "  <- RELATED_MIN_SCORE" if abs(cutoff - relevance.RELATED_MIN_SCORE) < 0.005 else ""
        print(f"  {cutoff:.2f}   {kept[0]:6.1%}   {kept[1]:6.1%}    {kept[2]:6.2%}{marker}")

    # What the "↗ N related" chip would show per paragraph (39 candidates each).
    print("\ncutoff  chip finds a true relation  chip includes something off-topic")
    for cutoff in [x / 100 for x in range(10, 27, 2)]:
        hits = off = 0
        for i, (group, *_rest) in enumerate(NOTES):
            others = {str(j): raw[i, j] for j in range(len(NOTES)) if j != i}
            kept = relevance.relevant_model_scores(others, tokens[i], cutoff=cutoff)
            hits += any(NOTES[int(j)][0] == group for j in kept)
            off += any(NOTES[int(j)][0] != group for j in kept)
        marker = "  <- RELATED_MIN_SCORE" if abs(cutoff - relevance.RELATED_MIN_SCORE) < 0.005 else ""
        print(f"  {cutoff:.2f}   {hits:2d}/{len(NOTES)}                       {off:2d}/{len(NOTES)}{marker}")

if __name__ == "__main__":
    main()
