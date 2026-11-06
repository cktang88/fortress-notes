"""Deterministic, realistic-looking workspace for performance benchmarks.

``build(notes_dir, notes=2000)`` writes ~10 paragraphs per note (about 20,000 for
2,000 notes) as Markdown and imports them into ``notes_dir/.fortress.sqlite3``.
A few notes carry planted facts so Ask questions have known answers
(``QUESTIONS``), and some questions have no answer at all.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

PEOPLE = """Ana Marta Joao Priya Kenji Lena Omar Sofia Liam Chen Fatima Diego Noah Ines Ravi
Hana Tomas Elena Yusuf Clara Mateo Aisha Felix Nora""".split()
PLACES = """Lisbon Porto Kyoto Berlin Oslo Seville Chicago Toronto Nairobi Hanoi Lima Krakow
Vienna Dublin Austin Marseille Bergen Tbilisi Osaka Valencia""".split()
WEEKDAYS = "Monday Tuesday Wednesday Thursday Friday Saturday Sunday".split()
MONTHS = """January February March April May June July August September October November
December""".split()

TOPICS: dict[str, dict[str, list[str]]] = {
    "travel": {
        "titles": ["Trip to {place}", "{place} itinerary", "Weekend in {place}", "{place} notes"],
        "sentences": [
            "We took the {time} train to {place} and got in just before dinner.",
            "The hotel near the old town was small but quiet, and the breakfast was {adj}.",
            "{person} found a tiny restaurant that served grilled fish with lemon and olive oil.",
            "Walking along the river at sunset was the best part of the day.",
            "Tickets for the museum cost {num} euros each; book online to skip the queue.",
            "The tram to the castle is crowded after ten, so go early in the morning.",
            "It rained most of {weekday}, which turned into a long lazy afternoon in a cafe.",
            "Next time stay closer to the station; the taxi rides added up quickly.",
            "The market on {weekday} mornings has the best cheese and fresh bread.",
            "{person} wants to come back in {month} when it is less busy.",
        ],
    },
    "work": {
        "titles": ["Meeting with {person}", "Project {code} status", "{month} planning", "1:1 {person}"],
        "sentences": [
            "{person} raised a concern about the deadline for the {code} migration.",
            "We agreed to ship the first version by the end of {month} and iterate after.",
            "The budget for the quarter is {num} thousand, mostly for contractors.",
            "Action item: {person} writes the design doc and shares it by {weekday}.",
            "Customer feedback says onboarding is confusing on the second screen.",
            "The retro surfaced that code reviews are taking more than two days.",
            "We should cut scope instead of moving the launch date again.",
            "Hiring plan: two backend engineers and one designer before {month}.",
            "The dashboard showed latency spikes every night around the backup window.",
            "{person} will own the rollout plan and the incident runbook.",
        ],
    },
    "cooking": {
        "titles": ["Recipe: {dish}", "{dish} attempt", "Dinner ideas", "Baking notes"],
        "sentences": [
            "Roast the vegetables at {num0} degrees for about forty minutes, turning once.",
            "The {dish} needed more salt and a squeeze of lemon at the end.",
            "Let the dough rest overnight in the fridge for a better crust.",
            "{person} suggested adding smoked paprika, which worked really well.",
            "Toast the spices in a dry pan until fragrant before grinding them.",
            "Use day-old rice for fried rice, otherwise it turns mushy.",
            "Simmer the sauce slowly; rushing it makes it taste flat.",
            "Doubling the recipe works but needs a bigger pot and ten more minutes.",
            "Next time try brown butter instead of oil for a nuttier flavour.",
            "The {dish} keeps for three days in an airtight container.",
        ],
    },
    "programming": {
        "titles": ["Debugging {code}", "Notes on {tech}", "{tech} performance", "Refactor plan"],
        "sentences": [
            "The memory leak came from a cache that never evicted old entries.",
            "Switching the hot loop to {tech} cut the run time from {num} seconds to two.",
            "Write a failing test first, then bisect the commits to find the regression.",
            "The index on the created column made the dashboard query ten times faster.",
            "{person} pointed out that the retry logic hides real network errors.",
            "Profiling showed most time was spent serialising JSON, not in the database.",
            "Keep the public API small and move helpers into a private module.",
            "Batching the writes into one transaction fixed the lock contention.",
            "The flaky test depended on the order of a dictionary iteration.",
            "Use a connection pool instead of opening a new socket per request.",
        ],
    },
    "reading": {
        "titles": ["Book: {book}", "Reading notes {month}", "Quotes from {book}", "{book} review"],
        "sentences": [
            "The main argument of {book} is that small habits compound over years.",
            "Chapter {num} was slow, but the ending tied the threads together nicely.",
            "{person} recommended it after we talked about attention and focus.",
            "The author keeps returning to the idea that constraints help creativity.",
            "I disagree with the claim that willpower is a limited resource.",
            "The best chapter is about how cities shape the way people meet.",
            "Lend my copy to {person}; they asked about it twice.",
            "The translation reads a bit stiff in places but the ideas survive.",
            "Worth rereading the section on deliberate practice before the course.",
            "Three stars: good ideas, too many anecdotes.",
        ],
    },
    "health": {
        "titles": ["Running log {month}", "Sleep experiment", "Doctor visit", "Training plan"],
        "sentences": [
            "Ran {num} kilometres along the coast; the last stretch felt easy.",
            "Sleeping before eleven made a clear difference to morning energy.",
            "The physio said to stretch the calves every day and rest the knee for a week.",
            "Cutting coffee after lunch helped me fall asleep faster.",
            "{person} and I signed up for the half marathon in {month}.",
            "Strength training twice a week, mostly squats and rows.",
            "Blood pressure was normal; next check-up in six months.",
            "Walking meetings are a nice way to get more steps in.",
            "Drink more water on long runs, especially when it is hot.",
            "The new shoes gave me a blister on the first long run.",
        ],
    },
    "home": {
        "titles": ["Garden plan", "Apartment repairs", "Moving checklist", "{month} chores"],
        "sentences": [
            "Plant the tomatoes after the last frost, usually in late {month}.",
            "The kitchen tap drips again; the washer needs replacing.",
            "{person} can lend us a ladder for painting the hallway.",
            "Water the herbs every second day, more often in summer.",
            "The landlord agreed to fix the heating before winter.",
            "Compost the coffee grounds; the roses seem to like them.",
            "Buy two more shelves for the books in the living room.",
            "The internet provider charges {num} a month after the promotion ends.",
            "Label the boxes by room, not by content, when moving.",
            "The balcony gets afternoon sun, good for peppers and basil.",
        ],
    },
    "finance": {
        "titles": ["Budget {month}", "Savings plan", "Tax notes", "Subscriptions audit"],
        "sentences": [
            "Rent and groceries take about {num} percent of the monthly income.",
            "Cancel the streaming service we never watch anymore.",
            "Move the emergency fund to an account with better interest.",
            "{person} said the accountant needs the receipts before {month}.",
            "Track small purchases for a month to see where money goes.",
            "The electricity bill doubled in winter; check the meter reading.",
            "Set up an automatic transfer to savings on payday.",
            "Travel costs for the {place} trip came to roughly {num0} euros.",
            "Index funds for the long term, cash for the next two years.",
            "Ask the bank about the fee on international transfers.",
        ],
    },
}
DISHES = "risotto ramen shakshuka lasagna curry focaccia paella dumplings chili gazpacho".split()
TECHS = "Rust NumPy SQLite Postgres Redis Kafka React Go asyncio WebAssembly".split()
BOOKS = [
    "The Long Walk",
    "Atomic Habits",
    "Invisible Cities",
    "The Overstory",
    "Deep Work",
    "Thinking in Systems",
    "The Dispossessed",
    "Range",
]
ADJS = "excellent average forgettable generous disappointing wonderful".split()

# Facts planted in specific notes, with questions whose answers they hold.
FACTS = [
    ("Porto apartment", "The wifi password at the Porto apartment is sardinha2024, written on the fridge."),
    ("Vet visit", "Our cat Miso is allergic to chicken, so the vet put her on a salmon diet."),
    ("Car service", "The car had its last oil change on 14 March at 87,400 kilometres."),
    ("Kyoto ryokan", "We booked the ryokan in Gion for two nights, check-in after 3 pm."),
    ("Mum's birthday", "Mum's birthday dinner is at Taberna do Largo on 9 October at 8 pm."),
    ("Lease details", "The apartment lease ends on 31 August and needs two months' notice."),
    ("Bike lock", "The combination for the blue bike lock is 4-7-1-9."),
    ("Dentist", "Dr. Okafor recommended a night guard because I grind my teeth."),
    ("Insurance", "Home insurance renews in November; the policy number is HX-55120."),
    ("Sourdough starter", "Feed the sourdough starter, called Bubbles, with rye flour every 12 hours."),
    ("Conference talk", "My talk at the Berlin conference is on 22 May in room B, 25 minutes long."),
    ("Passport", "My passport expires in February 2027, renew it six months before."),
]
QUESTIONS = [
    ("What is the wifi password at the Porto apartment?", "sardinha2024"),
    ("What is our cat allergic to?", "chicken"),
    ("When was the last oil change on the car?", "14 March"),
    ("Where are we staying in Kyoto?", "Gion"),
    ("Where is mum's birthday dinner?", "Taberna do Largo"),
    ("When does the apartment lease end?", "31 August"),
    ("What's the combination for the bike lock?", "4-7-1-9"),
    ("Why did the dentist recommend a night guard?", "grind"),
    ("What is the home insurance policy number?", "HX-55120"),
    ("How often do I feed the sourdough starter?", "12 hours"),
    ("Which room is my Berlin conference talk in?", "room B"),
    ("When does my passport expire?", "February 2027"),
    ("Which shoes gave me a blister?", None),  # vague: answerable loosely
    ("What did the physio say about my knee?", "rest the knee"),
    ("How long should vegetables roast?", "forty minutes"),
    ("What is the name of my neighbour's dog?", None),  # not in the notes
    ("What is the capital of Australia?", None),  # not in the notes
    ("Who recommended adding smoked paprika?", None),  # many notes; any name is fine
]


def _fill(template: str, rng: random.Random) -> str:
    return template.format(
        person=rng.choice(PEOPLE),
        place=rng.choice(PLACES),
        time=f"{rng.randint(6, 21)}:{rng.choice(['00', '15', '30', '45'])}",
        adj=rng.choice(ADJS),
        num=rng.randint(2, 60),
        num0=rng.randint(15, 25) * 10,
        weekday=rng.choice(WEEKDAYS),
        month=rng.choice(MONTHS),
        code=rng.choice(["Atlas", "Beacon", "Comet", "Delta", "Ember", "Falcon"]),
        dish=rng.choice(DISHES),
        tech=rng.choice(TECHS),
        book=rng.choice(BOOKS),
    )


def _paragraph(topic: dict[str, list[str]], rng: random.Random) -> str:
    count = rng.choice([1, 2, 2, 3, 3, 4, 5])
    return " ".join(_fill(rng.choice(topic["sentences"]), rng) for _ in range(count))


def build(notes_dir: Path, notes: int = 2000, seed: int = 7) -> dict[str, int]:
    """Write ``notes`` Markdown notes and import them into the SQLite store."""

    from app.block_store import bootstrap_markdown

    rng = random.Random(seed)
    notes_dir.mkdir(parents=True, exist_ok=True)
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    paragraphs = 0
    fact_slots = dict(zip(rng.sample(range(notes), len(FACTS)), FACTS))
    names = list(TOPICS)
    for index in range(notes):
        topic = TOPICS[names[index % len(names)]]
        title = _fill(rng.choice(topic["titles"]), rng)
        body = [_paragraph(topic, rng) for _ in range(rng.randint(4, 16))]
        if index in fact_slots:
            title, fact = fact_slots[index]
            body.insert(rng.randrange(len(body) + 1), fact)
        paragraphs += len(body)
        stamp = (start + timedelta(hours=index * 3)).isoformat()
        text = "\n\n".join(body)
        (notes_dir / f"note-{index:05d}.md").write_text(
            f"---\ntitle: {title!r}\ncreated_at: {stamp}\nupdated_at: {stamp}\n---\n{text}\n",
            encoding="utf-8",
        )
    bootstrap_markdown(notes_dir, notes_dir / ".fortress.sqlite3")
    return {"notes": notes, "paragraphs": paragraphs}
