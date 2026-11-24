# Search evaluation

How good is our ColBERT late-interaction search (lightonai/Agent-ModernColBERT, `backend/app/embeddings.py`), next to a single-vector dense model, keyword search and hybrids? Rerun it with `backend/scripts/eval_search.py` (see its docstring). The quality floors in `backend/tests/test_search_eval_real.py` run in the macOS real-model CI job.

Systems:

- **colbert**: our encoder and MaxSim index (`embeddings.score_documents`).
- **dense**: Alibaba-NLP/gte-modernbert-base @ `e7f32e3` (2025-07-04, 149M parameters, ~300 MB). It is the dense model GTE-ModernColBERT was trained from, and GTE-ModernColBERT is our model's parent. It has the same backbone and size as our model, so the comparison isolates single-vector against multi-vector. It is evaluation-only and not a dependency.
- **bm25**: FTS5 `bm25()` with the query words OR-ed.
- **fts**: our AND query (`block_query.search_blocks`).
- **rrf(a+b)**: reciprocal rank fusion, k=60.
- **unified**: the app's search (`search.unified_search`), with words, meaning and titles merged and the cutoffs applied.

Machine: 4-core Linux CPU. MLX numbers come from the macOS CI job (GitHub's M1).

## 1. BEIR sanity check: our code path against published numbers

Full test splits, run through our encoder and MaxSim (documents are `title text`, as in BEIR). Agent-ModernColBERT publishes no BEIR numbers, so the reference is its parent, GTE-ModernColBERT-v1, from its model card. The fine-tune on top of it is light (5k examples).

| dataset | system | nDCG@10 | R@100 | published nDCG@10 | query ms p50 | index s |
|---|---|---|---|---|---|---|
| SciFact (5,183 docs, 300 q) | **colbert (ours)** | **75.8** | 97.0 | 76.3 (parent) | 239 | ~2000 |
| | colbert, model-card AgentIR query format | 74.3 | 97.0 | | 344 | |
| | dense | 76.7 | 97.3 | 77.4 | 65 | 1622 |
| | bm25 | 66.8 | 88.9 | | 2 | |
| | rrf(dense+bm25) | 74.1 | 98.0 | | | |
| | rrf(colbert+bm25) | 73.2 | 97.0 | | | |
| NFCorpus (3,633 docs, 323 q) | **colbert (ours)** | **38.8** | 34.4 | 37.9 (parent) | 173 | 1250 |
| | colbert, AgentIR query format | 37.4 | 33.0 | | 244 | |
| | dense | 34.3 | 31.6 | 36.4 | 50 | 1181 |
| | bm25 | 31.2 | 23.4 | | 0 | |
| | rrf(dense+bm25) | 35.9 | 32.4 | | | |
| | rrf(colbert+bm25) | 36.3 | 33.0 | | | |

Our pipeline reproduces the published quality: SciFact is 0.5 points below and NFCorpus 0.9 points above the published numbers, and the dense baseline lands within 1-2 points of its own card. So there is no prefix, marker, padding, normalisation or truncation bug. The model card's instruction query format ("Instruct: … Reasoning: Empty … Query: …") is worse on BEIR and much worse on short typed queries (Fortress nDCG@10 74.2 vs 94.4), so the app correctly sends plain queries.

MLX against CPU, from the macOS CI job (`test_real_model`): the largest MaxSim difference is 0.005, the rankings are the same, and the Fortress eval on MLX matches the CPU run (nDCG@10 94.4, unified 83.2).

## 2. Fortress notes set

`backend/tests/fixtures/search_eval` has 77 notes with 173 paragraphs (travel, work, recipes, health, finance, code, meetings, lists, German, French, Spanish, Japanese, typos and abbreviations). It also has 109 queries with graded relevant paragraphs: 31 paraphrase, 19 identifier/keyword, 14 short, 10 long, 4 negation, 6 near-duplicate, 10 with no answer, 7 cross-lingual and 8 typo.

### Ranking (nDCG@10 x100, answerable queries)

| system | all | paraphrase | keyword | short | long | negation | near-dup | cross-lingual | typo | query ms |
|---|---|---|---|---|---|---|---|---|---|---|
| **colbert** | **94.4** | **93.0** | **100** | 92.4 | **98.7** | 69.9 | **93.8** | **98.0** | **94.6** | 59 |
| dense | 88.6 | 88.0 | 83.2 | 92.2 | 96.9 | 72.4 | 93.8 | 98.0 | 82.7 | 54 |
| bm25 | 69.0 | 32.4 | 100 | 81.9 | 95.1 | 77.9 | 93.8 | 37.4 | 86.5 | <1 |
| fts (AND) | 27.4 | 0 | 100 | 50.7 | 0 | 0 | 0 | 14.3 | 0 | <1 |
| rrf(dense+bm25) | 87.1 | 74.6 | 98.1 | 93.9 | 99.2 | 83.0 | 93.8 | 79.9 | 85.7 | |
| rrf(colbert+bm25) | 88.5 | 77.0 | 100 | 95.0 | 98.4 | 81.4 | 93.8 | 77.9 | 90.9 | |
| unified (shown results) | 83.1 | 69.2 | 100 | 93.5 | 86.8 | 73.1 | 87.7 | 85.7 | 73.2 | 4 |

The relevant-vs-irrelevant score separability (AUC, which says how well one fixed cutoff can work) is 0.986 for colbert and 0.986 for dense. Unified search is scored only on what it shows after the cutoffs, so its lower nDCG is recall traded for showing nothing on no-answer queries.

### What search shows, before and after this work

| | before | after |
|---|---|---|
| ColBERT nDCG@10 (no cutoff) | 86.1 | **94.4** |
| unified: precision of everything shown | 14% | **70%** |
| unified: recall of relevant paragraphs | 82% | 78% |
| unified: results shown per query | 10.4 | 2.0 |
| no-answer queries showing nothing | 3 / 10 | **10 / 10** |
| related notes: precision | 65% | **89%** |
| related paragraphs: precision | 54% | **68%** |
| notes with nothing related showing something (note / paragraph lists) | 1/7, 5/14 | **0/7, 1/14** |

The related-notes recall of same-topic notes in the top 5 is 25%: the 0.18 cutoff favours precision. On this set, 0.14 gives 68% precision and 33% recall.

Latency on CPU: unified search is 4 ms per query once the query encoding is cached (search encodes the query once), and ColBERT scoring takes 59 ms including the query encode. Related lists take 60 ms (whole note) and 3 ms (paragraph). Indexing 173 paragraphs takes 12 s on the CPU and 23 s in the MLX CI job, including the precomputed paragraph query encodings.

## 3. What the eval found and fixed

1. **Paragraphs were encoded without their note title.** "Fed the starter at 8am" under "Sourdough log" could not be found by "sourdough". The model now reads `title\nparagraph` (`textutil.embedding_text`, `corpus.Note.documents`), and a rename re-encodes the note. This took ColBERT nDCG@10 from 86.1 to 94.4 and AUC from 0.933 to 0.986, and related-note precision from 65% to 89%.
2. **The meaning cutoff was too loose.** `SEARCH_MIN_SCORE` 0.10 showed about 6 paragraphs per query at 27% precision, and 4 of the 10 no-answer queries showed something. It is now 0.22, plus `SEARCH_RELATIVE_SCORE` 0.6, which keeps only results close to the best one: 78% precision, the answer still shown for 88% of queries, and no-answer queries empty. Ask's prefetch keeps the looser 0.10 (`unified_search(recall=True)`), because its LLM filters the sources and questions score lower than typed searches.
3. **Stopwords made title matches and coverage boosts.** "a" matched titles starting with "and", and "to" matched "Books to read", so "how to file a patent application" listed unrelated notes and stopwords reordered meaning results. `textutil.query_terms` now drops stopwords from title matching, the coverage boost and the no-model keyword fallback.
4. **Titles sharing under half the query words counted as matches on their own.** For example, "quantum computing lecture notes" listed every note with "notes" in its title. Such titles no longer count.
5. **Checked and fine:**
   - The 3-character rule still applies to the paragraph text, not to title plus paragraph.
   - Whole-note query clustering (256 vectors) keeps 8-10 of the exact top 10, with score error under 0.02.
   - MLX matches the CPU.
   - Query/document markers, no query expansion and float32 storage match PyLate.

## 4. Recommendation

Keep Agent-ModernColBERT. On personal notes it beats the same-size dense model by 5.8 nDCG points overall (94.4 vs 88.6), with the gap mostly in identifiers (100 vs 83) and typos (95 vs 83). It also beats dense on NFCorpus (38.8 vs 34.3) and is within a point on SciFact. Plain RRF hybrids do not help, because BM25 drags down paraphrase and cross-lingual queries. Our unified merge already gets exact-match recall from FTS.

Known weak spots:
- Negation ("europe trip, not japan") is hard for every system.
- Long notes covering several topics get no related notes, because the whole-note score is diluted. The per-paragraph related lists cover those notes.
- Some paraphrases with a true answer score under the 0.22 cutoff ("people we need to recruit", "team objectives for this quarter").
