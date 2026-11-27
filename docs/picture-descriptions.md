# Picture descriptions

An optional module (`backend/app/captions.py`, off by default, turned on in the Mac
app's Settings › General) that writes a few words about what each picture shows, so
search finds photos by their content: "dog on the beach", "receipt", "people in a
meeting". OCR still reads the words in pictures; this adds what they show.

## How it works

- **On and off.** Settings stores the switch and the app sends it to the backend
  whenever it connects (`PUT /api/descriptions`; `PICTURE_DESCRIPTIONS=true` for a
  backend run alone). On: the pinned model (1.0 GB) is downloaded once, in a child
  process, with progress in Settings; then pictures are described one by one. Off: a
  download stops at once, describing after the picture in progress; the model leaves
  memory. Descriptions
  already made stay searchable.
- **Cache and queue.** Like OCR: one row per content hash and `MODEL` in
  `picture_descriptions`; a picture is pending while it has no finished row for the
  current `MODEL`, newest first; 3 attempts; bumping `MODEL` re-describes lazily while
  the old descriptions stay searchable. The table is created at startup if missing;
  the schema version stays 16.
- **Its own lane.** `captions` is an *idle* lane: it runs only while no search is
  scoring and no other lane (OCR, indexing, mirrors, imports) has work.
- **Searchable, apart from OCR.** The description is appended to the picture's
  searchable text (`textutil.ASSET_TEXT_EXTRAS`), so full-text search, ColBERT,
  related notes and Ask see it as they see OCR text; either can be recomputed alone.
  It is never in the note or its Markdown mirror. The inspector shows it under the
  picture's text as "Generated description".
- **Skipped:** pictures under 64 px (icons). Text-heavy pictures are described too:
  the screenshot became "an inbox page for a project update" and the chart "a bar chart
  of monthly sales", accurate and searchable words OCR doesn't give.
- **Runtimes.** On Apple Silicon the model runs on the GPU with MLX
  (`backend/app/captions_mlx.py`, a port of the forward pass; MLX is already a
  dependency there). Elsewhere, or after any MLX error, transformers on the CPU.

## The test set

`backend/tests/fixtures/captions`: 25 photos from Wikimedia Commons (CC0, public
domain, CC BY, CC BY-SA; author, licence and source in `labels.json`, resized to
512 px) and 3 drawn ones (a chart, an e-mail screenshot, a 24 px icon), 1.2 MB.
Each picture is labelled with its subject words and every object word a correct
description may use.

- **Subject rate**: the description names the main subject.
- **Hallucination rate**: the share of descriptions that name an object (from a
  ~250-word vocabulary of things one searches for) the picture does not show. Each
  flagged word was checked against the picture; true misreadings stayed flagged
  (a tram called a bus, a marker called a pencil).
- **Search**: 18 descriptive queries ("dog swimming", "people in a meeting") through
  unified search over notes that hold only the picture (hit = an expected picture in
  the top 3), and 10 queries for things in no picture ("elephant", "birthday cake")
  that should return no picture.

## Candidates (all permissive licences)

| model | licence | download | why in or out |
|---|---|---|---|
| **SmolVLM-500M-Instruct** | Apache-2.0 | 1.0 GB | **chosen** |
| SmolVLM-256M-Instruct | Apache-2.0 | 0.5 GB | measured |
| SmolVLM2-256M/500M-Video-Instruct | Apache-2.0 | 1.0 / 2.0 GB | measured; its processor needs torchvision (no build for our torch 2.15 yet) and num2words, so it would need a workaround (SmolVLM's processor makes the same prompt) |
| SmolVLM2-2.2B-Instruct | Apache-2.0 | 9.0 GB | too large for a background job |
| Florence-2-base (florence-community, native in transformers) | MIT | 0.46 GB | measured |
| Qwen3.5-0.8B (Feb 2026) | Apache-2.0 | 1.7 GB | measured; needs torchvision too (worked around for the test) |
| Qwen3.5-2B | Apache-2.0 | 4.5 GB | 12 s and 10.7 GB per picture on the CPU |
| Moondream 2 | Apache-2.0 | 3.9 GB | runs custom code from the model repository (`trust_remote_code`); Moondream 3 is BSL |
| LFM2.5-VL, InternVL3-1B, Gemma, PaliGemma, FastVLM | not permissive | | out |

## Numbers

Linux: 4-core Xeon, torch CPU, float32, measured beside other work. Mac: GitHub's
macOS 26 runner (virtual M1, 3 cores, no MPS device; MLX reaches its GPU), torch CPU
float32, and MLX through mlx-vlm 0.8.0 (measured in its own environment). Prompts:
*short* "Caption this image in a few words." (24 tokens), *sentence* "Describe this
image in one sentence." (40 tokens); greedy, one 512 px tile.

| model | prompt | subject | hallucination | s/picture Linux CPU | s/picture M1 CPU | s/picture M1 MLX | peak memory (CPU) |
|---|---|---|---|---|---|---|---|
| **SmolVLM-500M** | **short** | **0.93** | **0.07** | 1.9 | 2.1 | 0.64 (ours: **0.60**) | 3.3 GB |
| SmolVLM-500M | sentence | 0.89 | 0.11 | 2.4 | 3.1 | 0.74 | 3.3 GB |
| SmolVLM-256M | short | 0.78 | 0.11 | 1.4 | 1.8 | 0.56 | 1.9 GB |
| SmolVLM-256M | sentence | 0.70 | 0.22 | 1.8 | 1.6 | 0.56 | 1.9 GB |
| SmolVLM2-500M | short | 0.96 | 0.11 | 2.1 | 3.0 | 1.56 | 2.3 GB |
| SmolVLM2-500M | sentence | 0.96 | 0.15 | 2.0 | 4.1 | 1.47 | 2.3 GB |
| SmolVLM2-256M | short | 0.93 | 0.30 | 1.4 | | | 1.4 GB |
| Florence-2-base | `<CAPTION>` | 0.85 | 0.22 | 2.5 | 3.0 | not supported | 1.7 GB |
| Florence-2-base | `<DETAILED_CAPTION>` | 0.93 | 0.37 | 2.3 | | | 1.7 GB |
| Qwen3.5-0.8B | sentence | 0.93 | 0.07 | 4.0 | | 1.46 | 5.6 GB |
| Qwen3.5-0.8B | short | 0.81 | 0.11 | 2.4 | | 1.03 | 5.6 GB |

- **Length drives hallucination.** Every model hallucinated as much or more with the
  longer prompt, and most with Florence's detailed caption (0.37): once the obvious is
  said, small models pad with plausible things ("a person holding a stick", "a clock
  tower", "the Sydney Opera House"). A few words keeps the subject and drops the
  padding. The module keeps only the first sentence.
- **SmolVLM-500M with the short prompt** ties Qwen3.5-0.8B for the fewest
  hallucinations (2 of 27: a tram interior called a bus, a marker called a pencil)
  with a near-best subject rate, at half Qwen's time and a fifth of its memory, with
  transformers' own processor (no workaround, no new dependency). SmolVLM2-500M names
  the subject a little more often but hallucinates more and needs the workaround;
  the 256M models and Florence-2 miss or invent more.
- **Search with SmolVLM-500M:** 16 of 18 descriptive queries find an expected picture
  in the top 3, with full-text search alone and with ColBERT (misses: "tram",
  described as a train station and a bus; "red marker", described as a pencil). Of
  the 10 queries for absent things, none returns a picture with words only; with
  ColBERT, "snowboarding" returns the snowy mountain picture.
- **Runtime.** On the M1, MLX describes 3.3x faster than torch on the CPU (0.64 vs
  2.1 s) and leaves the CPU cores free. mlx-vlm would add about a dozen packages
  (datasets, opencv, mlx-audio, llguidance, …), so `captions_mlx.py` ports the
  forward pass to MLX directly (160 lines). In the macOS CI job it describes in 0.60 s
  per picture (as fast as mlx-vlm) with the same scores, and writes word for word the
  same descriptions as transformers on the CPU for all 8 pictures compared.
  bfloat16 on the CPU halves the memory and was 1.7x faster on the Linux Xeon (it has
  bf16 units), but 7x slower on the M1 (14.5 s), so the CPU fallback stays float32.
- **What it costs:** 1.0 GB on disk once; on the CPU about 2 s and 2-3 GB of memory
  per picture while it runs, on Apple Silicon's GPU about 0.6 s and 1 GB. A workspace
  of 1,000 pictures is described in about 10 minutes on a Mac, once.

## Reproduce

Runs: the candidates on the M1 with torch and mlx-vlm in
[run 45955770650](https://github.com/cktang88/fortress-notes/actions/runs/45955770650)
(the mlx job; its CPU job was stopped after 90 minutes), float32 against bfloat16 in
[run 45959498643](https://github.com/cktang88/fortress-notes/actions/runs/45959498643),
the MLX port in the macOS job of the *Backend* workflow (`REALMODEL captions` lines).

`backend/scripts/caption_bench.py MODEL [--prompt short] [--dtype float32]
[--runtime mlx]` runs a model over the set and prints the scores; the real-model test
(`FORTRESS_REAL_CAPTIONS=1`, run by the macOS CI job) holds the pinned model to floors
a little below the numbers above (subject ≥ 0.85, hallucination ≤ 0.15, ≥ 80 % of the
queries found, ≤ 2 absent queries returning a picture) and checks MLX against the CPU.
