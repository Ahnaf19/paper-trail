# paper-trail

A CLI that ingests arXiv papers into PostgreSQL, and one FastAPI service that both serves
aggregations computed in SQL and answers natural-language questions grounded in the stored
abstracts — _**every answer carrying a trail back to the papers it came from.**_

```
  arXiv Atom API                                             [ external ]
       │   1 request / 3s · exponential backoff on 429
       ▼
  paper ingest         fetch ▸ parse ▸ upsert ▸ embed        one transaction per page
       │
       ▼
  PostgreSQL + pgvector                                      papers · authors · categories
       │                                                     · 384-dim vectors
       ├──────────────────────────────┐
       ▼                              ▼
  GET /stats/*                   POST /ask
  GROUP BY in SQL                embed ▸ top-k ▸ threshold ▸ generate
                                      │
                                      ▼
                                 OpenAI-compatible LLM       [ external ]
```

The two read paths never contact arXiv. The write path never talks to the LLM.

## Quick start

```bash
cp .env.example .env          # then put a Groq key in LLM_API_KEY
docker compose up --build     # database + migrations + API

# in another terminal — load the corpus (~2 min)
docker compose exec api paper ingest -c cs.AI -c cs.CL -c cs.LG

curl -s 'localhost:8000/stats/top-categories?n=3'

curl -s -X POST localhost:8000/ask -H 'Content-Type: application/json' \
  -d '{"question": "How agent memory looked like in early stage of LLMs?"}'
```

```jsonc
// GET /stats/top-categories?n=3
[{"category": "cs.AI", "count": 1276}, {"category": "cs.LG", "count": 1160},
 {"category": "cs.CL", "count": 667}]

// POST /ask
{
  "answer": "The abstracts suggest that early LLM agents used simple retrieval-augmented
             memory. These systems could store and recall basic facts, perform multi-hop
             retrieval, and track time-based changes, but they did not organize knowledge
             into structured hierarchies (e.g. ledgers, trees, or to-do lists)... (2602.11243).",
  "sources": [
    {"arxiv_id": "2602.11243", "title": "Evaluating Memory Structure in LLM Agents",
     "score": 0.8425},
    {"arxiv_id": "2605.28108", "title": "Ask Now, Use Later: Benchmarking the Proactivity
      Gap in Long-Lived LLM Agents", "score": 0.8070},
    {"arxiv_id": "2609.16053", "title": "Retrieval-Driven Memory Reconsolidation for
      Long-Term LLM Agents", "score": 0.7926}
    // ... 2 more, down to 0.7844
  ]
}
```

Every id in `sources` is a row that was joined, so it always resolves to a stored paper —
and the answer cites them inline.

> [!TIP]
>
> - **No manual migration step.** The container runs `alembic upgrade head` before the API
>   starts, so a fresh database becomes a working one with one command.
> - **Interactive OpenAPI docs at [`/docs`](http://localhost:8000/docs)** once the stack is
>   up. Full reference: [`docs/api.md`](docs/api.md).

> [!NOTE]
> Needs Docker and ~1.7 GB of disk — a 1.23 GB app image (the embedding model is baked in)
> plus 464 MB of Postgres.

## Ingesting papers

```bash
docker compose exec api paper ingest -c cs.AI -c cs.CL -c cs.LG   # default --max 1200
docker compose exec api paper ingest -c cs.AI --full              # ignore the watermark
docker compose exec api paper wipe                                # delete everything
```

> [!NOTE]
> **`--max` rounds up to a whole page.** Checked _after_ each page commits, and pages are 200
> — so any value in 1,001–1,200 yields exactly 1,200 papers.

- **`-c` is required and repeatable.** The query is built from `cat:` clauses, so with no
  category there is no `search_query` to send — arXiv has no "everything" query. Omitting it
  exits 2 with a copyable example. Codes: [arXiv's taxonomy](https://arxiv.org/category_taxonomy);
  loaded ones via `GET /stats/top-categories?n=110`, which spans 104 here.
- **Re-running is safe and cheap.** Papers upsert on `arxiv_id`, a database constraint rather
  than application logic. The feed is sorted by last-update descending, so one high-water mark
  catches new _and_ revised papers and the scan stops once a whole page predates it.
- **A truncated run does not fake progress.** The mark advances only if the walk reached back
  to the previous one. If `--max` stops it short, the span between was never fetched — the mark
  stays put and the run ends `[gap left open]`. Re-run with a larger `--max`, or `--full`.
- **Deliberately synchronous.** arXiv's Terms of Use require "a single connection at a time",
  so there is no concurrency to win.

## Data model

### How the tables relate

```
                    ┌─────────────┐
                    │  embedding  │   one vector per paper, same transaction
                    └──────┬──────┘
                           │ 1:1
                    ┌──────┴──────┐
        ┌───────────┤    paper    ├───────────┐
        │           └─────────────┘           │
   M:N  │ paper_author        paper_category  │  M:N
        │ (+ author order)                    │
  ┌─────┴─────┐                         ┌─────┴─────┐
  │  author   │                         │ category  │
  └───────────┘                         └───────────┘

  ingest_state        standalone — one watermark per sorted category set
```

### Schema

```
paper
  id                PK
  arxiv_id          UNIQUE        ← version-stripped; the idempotency key
  title             text
  abstract          text
  primary_category  text
  published         timestamptz   ← what the time series buckets on
  updated           timestamptz   ← what the watermark tracks
  doi               nullable
  journal_ref       nullable

embedding
  paper_id          PK, FK → paper  ON DELETE CASCADE
  vector            vector(384)
  content_hash      sha256(title + "\n" + abstract)   ← decides re-embedding
  model             which model produced it            ← so does this
  created_at        timestamptz

author                          category
  id      PK                      id    PK
  name    UNIQUE                  code  UNIQUE

paper_author                    paper_category
  paper_id   PK, FK               paper_id     PK, FK
  author_id  PK, FK               category_id  PK, FK
  position   smallint

ingest_state
  id                PK
  categories_key    UNIQUE        ← sorted set, e.g. "cs.AI|cs.CL|cs.LG"
  watermark         timestamptz
```

### Four decisions worth naming

- **`arxiv_id` carries no version suffix and is unique** — idempotent re-ingest becomes a database constraint, not a read-then-write check that can race.
- **`content_hash` + `model` decide re-embedding** — a metadata-only revision bumps `updated` without triggering one, and swapping models invalidates every row through that same field.
- **The watermark is keyed by the category set.** Ingest `cs.AI`, then re-run adding `cs.LG`:
  a shared watermark would stop early and silently skip every older `cs.LG` paper. A key
  mismatch forces a full scan instead.
- **Embeddings are written in the paper's transaction** — a crash between the two is not
  handled, it is impossible. Hence pgvector rather than a separate vector store and its
  dual-write problem.

### No indexes beyond the primary and unique keys

Measured, not assumed — warm medians of nine calls through the service functions
themselves, on the 2,370-paper corpus. Every plan is a sequential scan:

| Query                                     | Median   |
| ----------------------------------------- | -------- |
| `top-categories?n=10`                     | 4.5 ms   |
| `top-authors?n=10`                        | 12.9 ms  |
| `papers-over-time` (month, all)           | 7.8 ms   |
| `/ask` retrieval — top-5 of 2,370 vectors | 2.8 ms   |

**Aggregations gain nothing.** An index buys selectivity; a `GROUP BY` over the whole corpus
has none — Postgres reads every row regardless.

**Vector search would be faster.** HNSW over the same 2,370 vectors (`EXPLAIN ANALYZE`, warm
median of seven):

|            | Exact (today) | HNSW                        |
| ---------- | ------------- | --------------------------- |
| Execution  | 1.18 ms       | **0.14 ms**                 |
| Result     | exact         | **approximate**             |
| Build      | —             | 410 ms                      |
| Index size | —             | 4752 kB, on a 4760 kB table |

8.7× faster, and not worth it: the 1 ms saved sits inside a request whose LLM call takes 1–3
seconds — under 0.1% of end-to-end latency, for an approximate result and an index the size of
the table. Worth revisiting around 100k vectors.

## APIs

Five endpoints. Every aggregate is computed by Postgres with `GROUP BY` — **the API never
contacts arXiv**, and answers with the network disconnected.

| Endpoint                      | Returns                                                       |
| ----------------------------- | ------------------------------------------------------------- |
| `GET /health`                 | Liveness, including the database — so "ok" means _can serve_  |
| `GET /stats/top-categories`   | Top N categories by distinct paper count                      |
| `GET /stats/top-authors`      | Top N authors by distinct paper count                         |
| `GET /stats/papers-over-time` | Paper counts per period and category                          |
| `POST /ask`                   | Grounded answer from the stored abstracts, with cited sources |

```console
$ curl -s 'localhost:8000/stats/top-categories?n=3'
[{"category": "cs.AI", "count": 1276}, {"category": "cs.LG", "count": 1160},
 {"category": "cs.CL", "count": 667}]
```

- **Full reference: [`docs/api.md`](docs/api.md)**
- Interactive OpenAPI docs at [`/docs`](http://localhost:8000/docs) while the service runs.

## Asking questions — `POST /ask`

Embed the question → retrieve the nearest abstracts → answer from those alone. Shapes and
examples: [`docs/api.md`](docs/api.md#post-ask).

> [!IMPORTANT]
> **The relevance check runs before the model is called.** Refusing is control flow, not an
> instruction the model might ignore — and an off-topic question costs zero tokens.

### Refusing to answer: two gates

A single cosine threshold cannot separate "this corpus answers that" from "it doesn't".
Two gates, each catching what the other misses:

|     | Gate                                                     | Catches                | Cost         |
| --- | -------------------------------------------------------- | ---------------------- | ------------ |
| 1   | `score >= RELEVANCE_THRESHOLD`, **before** generation    | the clear cases        | zero tokens  |
| 2   | the grounded prompt — _answer only from these abstracts_ | what slips past gate 1 | one LLM call |

Measured on this corpus:

```console
"What is the CAP theorem?"          top-1 0.687  → gate 1 refuses, LLM never called
{"answer": "I don't have information about that in the paper dataset.", "sources": []}

"What guarantees does Raft provide?" top-1 0.799 → gate 1 PASSES, gate 2 refuses
{"answer": "The provided abstracts do not contain any information about
            the Raft consensus algorithm.", "sources": [...5 papers...]}
```

Gate 1 is deterministic but imprecise; gate 2 is precise but probabilistic. The split is
measured: across 24 must-refuse questions, **22 stop at gate 1, 2 at gate 2, none are answered**
([Evaluation](#evaluation)).

### Where the threshold comes from

A property of the embedding model _and_ the corpus, so it is measured against a labelled set
rather than chosen — method in [Evaluation](#evaluation).

| t        | correct answers | false passes | of which adjacent-CS |
| -------- | --------------- | ------------ | -------------------- |
| 0.70     | 92/97           | 4/24         | 4/14                 |
| 0.71     | 92/97           | 3/24         | 3/14                 |
| **0.75** | **92/97**       | **2/24**     | **2/14**             |
| 0.78     | 91/97           | 2/24         | 2/14                 |
| 0.80     | 87/97           | 0/24         | 0/14                 |

Retrieval puts the correct paper in the top-5 for **92 of 97** questions — a ceiling no
threshold can beat, since a gate cannot repair retrieval, only avoid discarding it. Everything
from 0.70 to 0.76 sits at that ceiling, so the negatives decide. `0.75` is the lowest value
reaching the fewest false passes.

> [!WARNING]
> **The remaining overlap is structural, not a tuning failure.** `bge-small` encodes what a
> text is _about_, not whether it _contains an answer_.

The two that still clear gate 1 are real computer science absent from this corpus:

| Question                                                             | top-1 | Nearest paper in the corpus                        |
| -------------------------------------------------------------------- | ----- | -------------------------------------------------- |
| _"What guarantees does the Raft consensus algorithm provide?"_       | 0.799 | Algorithmic stability via ensembling                |
| _"What problem does consistent hashing solve in a distributed cache?"_ | 0.796 | Shared KV Caching for Replicated 27B Inference      |

Refusing both requires `t ≥ 0.80`, which costs **five** real answers — hence gate 2 rather
than a higher gate 1. A cross-encoder re-ranker is the real fix.

### The model id

> [!WARNING]
> **Model ids move.** `openai/gpt-oss-20b` was a Groq _production_ model
> on 15 Sep 2026 and produced every sample here. If it has been retired, set `LLM_MODEL` to any current id; nothing else changes.

**Without a key `/ask` still works** — 503 with the retrieved papers in `sources` and an
explicit message. The same applies to a timeout or a retired model id.

## Configuration

All configuration is environment variables; `.env.example` lists them.

| Variable                              | Default                          | Notes                                                                                                        |
| ------------------------------------- | -------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `POSTGRES_USER` / `_PASSWORD` / `_DB` | `arxiv`                          | Used by the database container.                                                                              |
| `DATABASE_URL`                        | `…@localhost:5432/arxiv`         | For running the CLI **from your host**. The API container overrides it to reach `db` on the compose network. |
| `LLM_API_KEY`                         | —                                | 🔴 The only value to be set. Free at [console.groq.com/keys](https://console.groq.com/keys), no card.        |
| `LLM_BASE_URL`                        | `https://api.groq.com/openai/v1` | Any OpenAI-compatible endpoint.                                                                              |
| `LLM_MODEL`                           | `openai/gpt-oss-20b`             | Tested 15 Sep 2026 — see the warning under `/ask`.                                                           |
| `RELEVANCE_THRESHOLD`                 | `0.75`                           | Measured against a labelled set, not chosen. See [Evaluation](#evaluation).                                  |
| `RETRIEVAL_TOP_K`                     | `5`                              | Papers passed to the model as context. `10` was measured and rejected — see `eval/RESULTS.md`.               |
| `ARXIV_PAGE_SIZE`                     | `200`                            | arXiv's documented ceiling is 2000.                                                                          |
| `ARXIV_MIN_REQUEST_INTERVAL`          | `3.0`                            | arXiv's Terms of Use floor. Cannot be set lower.                                                             |
| `LOG_LEVEL`                           | `INFO`                           |                                                                                                              |

> [!IMPORTANT]
> **`LLM_API_KEY` is the only value you must supply.** Without it everything works except
> answer generation — `/ask` returns the retrieved papers and an explicit message.

### Embeddings

Computed **in-process** by [`fastembed`](https://github.com/qdrant/fastembed).

|         |                                                           |
| ------- | --------------------------------------------------------- |
| Model   | `BAAI/bge-small-en-v1.5`, quantised ONNX · 384 dimensions |
| Size    | 63 MB of weights, baked into the image at build time      |
| Runtime | ONNX Runtime — **no PyTorch**, which would add ~800 MB    |

- **Local**, because a hosted embedder means a second key, a per-call cost, a rate limit and
  a network hop on the hottest path — for a model small enough to run inline.
- `LLM_API_KEY` is for _generation_ only; there is no embedding key.
- A running container never fetches weights. Outside Docker, `fastembed` downloads them on
  first use — which is why the first local run is slower.
- The model is warmed at startup, not on the first request, so the cost is visible in the logs
  and gated by the healthcheck.

## Tests

```bash
cp .env.example .env       # if you have not already
docker compose up -d db    # Postgres + pgvector on :5432 — the only prerequisite
uv sync && uv run pytest
```

127 tests. No network, no LLM, no model download — the embedding provider and LLM client sit
behind interfaces the suite fakes. They run against a throwaway `arxiv_test` database built by
the real migrations, so the schema under test is the schema that ships.

Aimed at what breaks: XML parsing against a saved capture, idempotent re-ingest, staleness
detection, the watermark's gap guard, aggregation counts on seeded data, and the no-match path.

## Evaluation

Tests prove the code runs. They say nothing about whether the right papers come back, or what
the relevance gate costs.

```bash
uv run python scripts/evaluate.py             # retrieval + threshold sweep — offline, free
uv run python scripts/evaluate.py --with-llm  # + end-to-end answer/refuse behaviour
```

Run history and before/after: [`eval/RESULTS.md`](eval/RESULTS.md).

### The dataset

[`eval/golden.jsonl`](eval/golden.jsonl) — 121 rows, committed, built by
[`scripts/build_golden_set.py`](scripts/build_golden_set.py):

| Kind                                           | n   | Source           |
| ---------------------------------------------- | --- | ---------------- |
| Positives — `big` band (category ≥ 100 papers) | 38  | generated        |
| Positives — `mid` band (10–99)                 | 34  | generated        |
| Positives — `thin` band (< 10)                 | 25  | generated        |
| Negatives — adjacent computer science          | 14  | **hand-written** |
| Negatives — off-topic                          | 10  | **hand-written** |

> [!IMPORTANT]
> **Positives are authored paper-first** — sample a paper, then generate a question its
> abstract answers. The reverse (ask, then label what retrieval returned) makes
> `recall@5 = 1.0` by construction.

Stratified by each paper's **rarest** category: five categories hold most of the corpus, so a
uniform sample would test retrieval only where it is densest. Negatives are hand-written —
generating "questions about things absent from this corpus" produces things present in it.

### Retrieval  (97 positives · 2,370 papers · 16 Sep 2026)

| Metric    | Overall   |     | Band                 | n   | recall@1 | recall@5 | MRR   |
| --------- | --------- | --- | -------------------- | --- | -------- | -------- | ----- |
| recall@1  | 0.897     |     | `big` (≥ 100 papers) | 38  | 0.947    | 0.947    | 0.955 |
| recall@5  | **0.948** |     | `mid` (10–99)        | 34  | 0.824    | 0.971    | 0.875 |
| recall@10 | 0.979     |     | `thin` (< 10)        | 25  | 0.920    | 0.920    | 0.920 |
| MRR@20    | 0.918     |     |                      |     |          |          |       |

Bands are reported separately because thin categories are over-sampled — the overall figure is
not what real traffic sees. `mid` has the worst `recall@1` and the best `recall@5`: the right
paper is found, just not first. `thin` is flat across every `k` — rank 1 or nowhere.

> [!NOTE]
> **Not precision@k.** With one relevant paper per question, `precision@5` cannot exceed 0.2
> however perfect the ranking. `recall@k` here is hit-rate@k.

### End to end, through `/ask`

All 24 must-refuse questions, plus a fixed-seed sample of 40 positives.

| Positives (40)                 |          | Must-refuse (24)                |          |
| ------------------------------ | -------- | ------------------------------- | -------- |
| answered, correct paper cited  | 37/40    | refused at gate 1 — 0 LLM calls | 22/24    |
| answered, correct paper missed | 3/40     | refused at gate 2 — 1 LLM call  | 2/24     |
| **wrongly refused**            | **0/40** | **answered anyway**             | **0/24** |

The sweep named both gate-1 survivors in advance — Raft and consistent hashing — and the
grounded prompt refused both.

The offline sweep predicts 92/97 = 94.8% answerable with the right paper cited; live gives
37/40 = 92.5%. Within sampling noise, so the free report substitutes for the 20-minute one
while iterating.

### What this replaced

Threshold selection began as *"find a gap between three score ranges"*, which has no answer
when the ranges overlap — and they do.

|                    | Before — range separation                | After — labelled classification            |
| ------------------ | ---------------------------------------- | ------------------------------------------ |
| Data               | 23 hand-written questions                | 121 rows, 97 generated paper-first         |
| Question asked     | is there a gap?                          | what does each threshold cost?             |
| Output             | `NO SEPARABLE WINDOW`, no recommendation | `0.75`, derived from a stated rule         |
| `RETRIEVAL_TOP_K`  | a convention                             | measured: +2.1pp for +34% context          |

The overlap remains. It is now a trade-off curve instead of a dead end.

### Limits

- **Recall is an upper bound.** Questions were generated from the abstracts they point at, so
  they share vocabulary. The generator rejects any lifting five consecutive title words, which
  narrows the gap without closing it.
- **Single-label ground truth.** One correct paper per question, though others may be relevant
  — which is what rules out `precision@k` and `nDCG`.
- **Bound to its corpus.** arXiv has no "as of" query, so a corpus ingested another day is a
  different corpus. `golden.meta.json` records the fingerprint; missing papers are reported
  **skipped**, never as retrieval failures.
- **Answer text is not scored.** Faithfulness and answer relevance need an LLM judge validated
  against human labels, or RAGAS. The end-to-end report checks citation and refusal, not prose.

## Project layout

```
app/
  main.py            FastAPI app: lifespan, routers, exception handlers
  cli.py             `paper ingest` / `paper wipe`
  dependencies.py    FastAPI dependencies and the annotated aliases routers use
  core/              config (pydantic-settings) · logging (loguru) · errors + handlers
  db/                engines and session factories
  models/            SQLAlchemy ORM — the schema's source of truth
  schemas/           Pydantic models for anything crossing a boundary (HTTP, or XML)
  routers/           HTTP only: validate, delegate, shape the response
  services/          everything the application actually does — no FastAPI imports here
  utils/             small helpers with no domain knowledge
alembic/versions/    migrations, generated from `app/models` and read before committing
tests/               pytest — no network, no LLM, no model download
scripts/             build_golden_set.py (one-off, needs a key) · evaluate.py (the runner)
eval/                golden.jsonl + its corpus fingerprint · RESULTS.md · one JSON per run
docs/                api.md — the endpoint reference
```

## Known limitations

- **Answers come from abstracts, not papers.** An arXiv API response carries title, authors,
  categories, dates and abstract — never full text. _"What learning rate did they use?"_ is
  outside what `/ask` can ever be right about.
- **Author identity is the name string.** arXiv publishes no author IDs, so two researchers
  sharing a name are one row in `top-authors`.
- **Indirect prompt injection is bounded, not eliminated.** An abstract containing _"ignore
  previous instructions"_ was tested; the model answered from the legitimate content. The real
  protection is architectural — **no tools, no database access, no network** — so the worst
  case is a wrong answer, never an action.
- **The threshold does not separate cleanly.** 2 of 24 must-refuse questions clear gate 1;
  refusing them costs 5 real answers. Gate 2 catches the residue, but probabilistically where
  gate 1 is deterministic. A cross-encoder re-rank is the fix.
- **A bounded ingest cannot backfill.** If `--max` stops the walk above the watermark, the mark
  refuses to advance and the run reports `[gap left open]` — nothing is lost silently, but
  closing the gap needs a larger `--max` chosen by hand.
- **A gate-2 refusal still returns sources.** Those papers are what was searched, not what
  grounded an answer; the response shape cannot currently distinguish the two.
- **The evaluation has its own limits** — synthetic positives, single-label ground truth,
  corpus coupling. Detailed under [Evaluation](#evaluation).
- **No ANN index on the vectors.** Measured trade-off in [Data model](#no-indexes-beyond-the-primary-and-unique-keys).
- **The corpus is a snapshot.** Re-running `paper ingest` picks up new and revised papers;
  nothing schedules it.

## What I would do with more time

In the order I would pick them up.

1. **Resumable backfill.** The incremental path keeps a corpus current; it cannot catch one up.
   A run stopped by `--max` above the watermark leaves a span unfetched, and the guard refuses
   to advance over it — but the remedy is manual. The fix is a second piece of state: a floor
   alongside the ceiling, so a bounded run resumes instead of restarting at the top of the feed.
2. **Schedule the ingest.** The incremental path is built and cheap; nothing triggers it. A
   cron container or external scheduler.
3. **A cross-encoder re-rank over the top-k.** The direct fix for the two false passes, and the
   eval already exists to prove it worked.
4. **A read-only database role for the API.** Both read paths only `SELECT` but connect as the
   owner. Cheap hardening.
5. **Re-embed as a separate command.** Changing the embedding model currently means re-running
   ingest; `paper reembed` would work from stored text and never touch arXiv.
6. **Gap-fill the time series behind a flag.** Empty periods are omitted; `?fill=true` using
   `generate_series` would suit some charts.
7. **An ANN index, once it earns one.** Exact search is 1.2 ms over 2,370 vectors — worth
   benchmarking HNSW around 100k, not before.
