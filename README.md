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
> **`--max` rounds up to a whole page.** The limit is checked _after_ each page commits and
> pages are 200, so any value in 1,001–1,200 yields exactly 1,200 papers. The default is
> written as `1200` so the flag and the resulting corpus agree — headroom over the brief's
> "at least 1,000" without a number that needs explaining.

- **`-c` is required and repeatable.** The query is assembled from `cat:` clauses, so with
  no category there is no `search_query` to send — arXiv has no "everything" query. Omitting
  it exits 2 with an example to copy. Valid codes:
  [arXiv's taxonomy](https://arxiv.org/category_taxonomy). Loaded ones:
  `GET /stats/top-categories?n=110` — the corpus below spans 104.
- **Re-running is safe and cheap.** Papers upsert on `arxiv_id` — a database constraint, not application logic. Sorted by last-update descending, so one high-water mark catches new _and_ revised papers; the scan stops once a whole page predates the mark.
- **A truncated run does not fake progress.** The mark advances only when the walk came
  down far enough to meet the previous one. If `--max` stops it short, the span in between
  was never fetched — so the mark stays put and the run ends `[gap left open]`. Advancing
  over that span would bury it below the next run's stop threshold permanently. Re-run with
  a larger `--max`, or `--full`.
- **Deliberately synchronous.** arXiv's Terms of Use require "a single connection at a time", so there is no concurrency to win — async would add ceremony for zero throughput.

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
- **Embeddings are written in the paper's transaction.** A crash between the two is not
  handled — it is impossible. That is why pgvector, not a separate vector store, which would
  reintroduce a dual-write problem transactions no longer cover.

### No indexes beyond the primary and unique keys

Measured, not assumed — warm medians of nine calls through the service functions
themselves, on the 2,370-paper corpus. Every plan is a sequential scan:

| Query                                     | Median   |
| ----------------------------------------- | -------- |
| `top-categories?n=10`                     | 4.5 ms   |
| `top-authors?n=10`                        | 12.9 ms  |
| `papers-over-time` (month, all)           | 7.8 ms   |
| `/ask` retrieval — top-5 of 2,370 vectors | 2.8 ms   |

**The aggregations don't want an index.** An index buys _selectivity_; a `GROUP BY` over the
whole corpus has none. Postgres reads every row regardless, so a seq scan is the right plan.

**Vector search would genuinely be faster with one** — that's the real trade. Building HNSW
over the same 2,370 vectors (`EXPLAIN ANALYZE`, warm median of seven):

|            | Exact (today) | HNSW                        |
| ---------- | ------------- | --------------------------- |
| Execution  | 1.18 ms       | **0.14 ms**                 |
| Result     | exact         | **approximate**             |
| Build      | —             | 410 ms                      |
| Index size | —             | 4752 kB, on a 4760 kB table |

8.7× faster, and still wrong here: **the 1 ms it saves sits inside a request whose LLM call
takes 1–3 seconds.** You would trade a correctness guarantee for well under 0.1% of end-to-end
latency, and store an index the size of the table. It earns its place when the scan itself
becomes perceptible — around 100k vectors, not 2.4k.

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

- Embed the question → retrieve the nearest abstracts → answer from those alone.
- Shapes and examples are in [`docs/api.md`](docs/api.md#post-ask)

> [!IMPORTANT]
> **The relevance check runs before the model is called.** Below the threshold the LLM is
> never contacted — so refusing is a property of control flow, not an instruction the model
> might ignore, and an off-topic question costs zero tokens.

### Refusing to answer: two gates

A single cosine threshold cannot separate "this corpus answers that" from "it doesn't".
Two independent gates do the job, and each catches what the other misses:

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

Gate 1 is deterministic but imprecise; gate 2 is precise but probabilistic. Neither is
sufficient alone, which is why both are there — and the split is measured, not assumed: across
24 must-refuse questions, **22 stop at gate 1, 2 at gate 2, and none are answered**
([Evaluation](#evaluation)).

### Where the threshold comes from

The threshold is the one constant here that cannot be reasoned about.

> [!NOTE]
> It is a property of the embedding model _and_ the corpus, so it is **measured** against a
> labelled dataset — see [Evaluation](#evaluation) for the method.

| t        | correct answers | false passes | of which adjacent-CS |
| -------- | --------------- | ------------ | -------------------- |
| 0.70     | 92/97           | 4/24         | 4/14                 |
| 0.71     | 92/97           | 3/24         | 3/14                 |
| **0.75** | **92/97**       | **2/24**     | **2/14**             |
| 0.78     | 91/97           | 2/24         | 2/14                 |
| 0.80     | 87/97           | 0/24         | 0/14                 |

Retrieval puts the correct paper inside the top-5 for **92 of 97** questions, so 92 is a
**ceiling no threshold can beat** — a gate cannot repair retrieval, only avoid discarding what
it found. Everything from 0.70 to 0.76 sits at that ceiling, so the positives cannot separate
those values and the negatives decide. `RELEVANCE_THRESHOLD = 0.75` is the lowest value
reaching the fewest false passes.

> [!WARNING]
> **The remaining overlap is structural, not a tuning failure.** `bge-small` encodes what a
> text is _about_, not whether it _contains an answer_.

The two questions that still clear gate 1 are real computer science this corpus has never seen:

| Question                                                             | top-1 | Nearest paper in the corpus                        |
| -------------------------------------------------------------------- | ----- | -------------------------------------------------- |
| _"What guarantees does the Raft consensus algorithm provide?"_       | 0.799 | Algorithmic stability via ensembling                |
| _"What problem does consistent hashing solve in a distributed cache?"_ | 0.796 | Shared KV Caching for Replicated 27B Inference      |

Raising the threshold until both are refused costs **five** real answers — which is exactly
why gate 2 exists rather than a higher gate 1. A cross-encoder re-ranker is the real fix, and
the eval is what would prove it worked.

### The model id

> [!WARNING]
> **Model ids move.** `openai/gpt-oss-20b` was a Groq _production_ model
> on 15 Sep 2026 and produced every sample here. If it has been retired, set `LLM_MODEL` to any current id; nothing else changes.

**Without a key `/ask` still works** — 503 with the retrieved papers in `sources` and an
explicit message. The same applies to a timeout or a retired model id.

## Configuration

Everything is an environment variable; nothing is hardcoded. `.env.example` lists them all.

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
> **`LLM_API_KEY` is the only value you must supply**, and even without it everything works
> except answer generation — `/ask` returns the retrieved papers and an explicit message.
> Aggregations, ingestion and retrieval are unaffected.

### Embeddings

Computed **in this process** by [`fastembed`](https://github.com/qdrant/fastembed) library.

|         |                                                           |
| ------- | --------------------------------------------------------- |
| Model   | `BAAI/bge-small-en-v1.5`, quantised ONNX · 384 dimensions |
| Size    | 63 MB of weights, baked into the image at build time      |
| Runtime | ONNX Runtime — **no PyTorch**, which would add ~800 MB    |

- **Local, because an embedding API would be a second key, a per-call cost, a rate limit and
  a network hop** on the hottest path in the system — for a model small enough to run inline.
- `LLM_API_KEY` is for _generation_ only. There is no embedding key.
- A running container never fetches weights. Outside Docker `fastembed` downloads them on
  first use, which is why the first local run is slower.

> [!TIP]
> The model is warmed at application startup, not on the first request — so the cost is
> visible in the logs and gated by the container healthcheck.

## Tests

```bash
cp .env.example .env       # if you have not already
docker compose up -d db    # Postgres + pgvector on :5432 — the only prerequisite
uv sync && uv run pytest
```

- 125 tests that **never touch the network, an LLM, or download a model**
- They run against a throwaway `arxiv_test` database, so your corpus is untouched.
- The embedding provider and the LLM client both sit behind small interfaces, and the suite injects fakes before the app starts.
- The test database is created and migrated by the real Alembic migrations, so the schema under test is the schema that ships.
- They are aimed at what would actually break: XML parsing against a saved real capture,
  idempotent re-ingest, staleness detection, aggregation counts on seeded data, and the
  no-good-match path.

## Evaluation

The brief did not ask for tests either; there are 125, because tests are what make code
verifiable. The same argument applies to the RAG path — and no test suite tells you whether
the _right_ papers come back, or what the relevance gate costs.

```bash
uv run python scripts/evaluate.py             # retrieval + threshold sweep — offline, free
uv run python scripts/evaluate.py --with-llm  # + end-to-end answer/refuse behaviour
```

Run history and before/after comparisons: [`eval/RESULTS.md`](eval/RESULTS.md).

### The dataset

[`eval/golden.jsonl`](eval/golden.jsonl) — 121 committed, human-readable rows, built by
[`scripts/build_golden_set.py`](scripts/build_golden_set.py):

| Kind                                            | n   | Source           |
| ----------------------------------------------- | --- | ---------------- |
| Positives — `big` band (category ≥ 100 papers)  | 38  | generated        |
| Positives — `mid` band (10–99)                  | 34  | generated        |
| Positives — `thin` band (< 10)                  | 25  | generated        |
| Negatives — adjacent computer science           | 14  | **hand-written** |
| Negatives — off-topic                           | 10  | **hand-written** |

> [!IMPORTANT]
> **Positives are authored paper-first**: sample a paper, _then_ have the model write a
> question its abstract answers. Doing it the other way round — asking a question and
> labelling whatever retrieval returned — makes `recall@5 = 1.0` by construction.

Sampling is stratified by each paper's **rarest** category. Five categories hold most of this
corpus, so a uniform sample would only test retrieval where it is densest. Negatives stay
hand-written because asking a model for "questions about things this corpus does not contain"
reliably produces things it does.

### Retrieval  (97 positives, 2,370 papers, 16 Sep 2026)

| Metric    | Overall   |
| --------- | --------- |
| recall@1  | 0.897     |
| recall@5  | **0.948** |
| recall@10 | 0.979     |
| MRR@20    | 0.918     |

| Band                    | n   | recall@1 | recall@5 | MRR   |
| ----------------------- | --- | -------- | -------- | ----- |
| `big` (≥ 100 papers)    | 38  | 0.947    | 0.947    | 0.955 |
| `mid` (10–99)           | 34  | 0.824    | 0.971    | 0.875 |
| `thin` (< 10)           | 25  | 0.920    | 0.920    | 0.920 |

Reported per band as well as overall, because thin categories are deliberately over-sampled —
the overall figure is not what real traffic would see. The `mid` band has the worst `recall@1`
and the best `recall@5`, which is what topic confusion looks like: the right paper is found,
just not first. The `thin` band is flat across every `k`, so for a rare category it is rank 1
or nowhere.

> [!NOTE]
> **Not precision@k.** With one relevant paper per question, `precision@5` cannot exceed 0.2
> however perfect the ranking — it measures the value of `k`, not the system. `recall@k` here
> is hit-rate@k.

### End to end, through `/ask`

Every must-refuse question runs; positives are a fixed-seed sample of 40, since report 1 already
measures their retrieval exactly and for nothing.

| Positives (40 sampled)         |       | Must-refuse (all 24)              |           |
| ------------------------------ | ----- | --------------------------------- | --------- |
| answered, correct paper cited  | 37/40 | refused at gate 1 — 0 LLM calls   | 22/24     |
| answered, correct paper missed | 3/40  | refused at gate 2 — 1 LLM call    | 2/24      |
| **wrongly refused**            | **0/40** | **answered anyway**            | **0/24**  |

Not one out-of-corpus question got an answer. The sweep predicted exactly which two would clear
gate 1 — Raft and consistent hashing — and the grounded prompt refused both.

It also validates the cheap report against the expensive one: report 2 predicts 92/97 = 94.8%
of positives answerable with the right paper cited, and the live run returns 37/40 = 92.5%.
Three questions apart, within sampling noise — so the offline sweep can stand in for a
20-minute LLM run when iterating.

### What this replaced

Threshold selection began as *"find a gap between three groups of scores"*. That method has no
answer when the groups overlap, and they do:

| | Before — range separation | After — labelled classification |
| --- | --- | --- |
| Data | 23 hand-written questions | 121 rows, 97 of them generated paper-first |
| Question asked | is there a gap? | what does each threshold cost? |
| Output | `NO SEPARABLE WINDOW` — no recommendation | `0.75`, derived from a stated rule |
| `RETRIEVAL_TOP_K` | a convention | a measured choice, priced at +2.1pp for +34% context |

The overlap did not go away. It stopped being a dead end and became a visible trade-off curve.

### Limits, stated plainly

- **Recall here is an upper bound.** The questions were generated from the abstracts they
  point at, so they share vocabulary with them. A human-written question would score lower.
  The generator rejects any question lifting five consecutive words from its source title,
  which narrows the gap without closing it.
- **Single-label ground truth.** Each question names exactly one correct paper, though others
  may be relevant. That rules out `precision@k` and `nDCG` honestly rather than reporting them
  wrongly.
- **The set is bound to its corpus.** arXiv has no "as of" query, so a corpus ingested on
  another day is a different corpus. `golden.meta.json` records the fingerprint and report 0
  reports missing papers as **skipped**, never as retrieval failures.
- **Answer text is not scored.** Faithfulness and answer relevance need an LLM judge validated
  against human labels, or a framework like RAGAS. The end-to-end report checks that the right
  paper was cited and that a must-refuse question was refused — not how good the prose is.

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

These are deliberate, and stated rather than hidden.

- **Answers come from abstracts, not papers.** A single arXiv API response carries the title,
  authors, categories, dates and abstract — never the full text. So _"what learning rate did
  they use?"_ lives in a PDF we never fetch. This bounds what `/ask` can ever be right about.
- **Author identity is the name string.** arXiv publishes no author IDs, so two researchers
  sharing a name are one row in `top-authors`. Cheap to state, expensive to fix properly.
- **Indirect prompt injection is bounded, not eliminated.** Abstracts are third-party text
  entering an LLM prompt; one containing _"ignore previous instructions"_ was tested and the
  model answered from the legitimate content instead. But the real protection is
  architectural: **the model has no tools, no database access and no network**, so the worst
  case is a wrong answer, never an action.
- **The relevance threshold does not separate cleanly.** Real computer science this corpus
  lacks can outscore answerable questions — _"what guarantees does Raft provide?"_ reaches
  0.799 by matching _"Algorithmic stability via ensembling"_. Measured cost of closing the
  gap: 2 of 24 must-refuse questions clear gate 1, and raising the threshold until none do
  would refuse 5 real answers. The grounded prompt catches the residue, but that second gate
  is probabilistic where the first is not. A cross-encoder re-rank is the real fix.
- **A bounded ingest cannot backfill.** `--max` stops the walk, and if it stops above the
  watermark the span between was never fetched. The mark refuses to advance over that gap and
  the run reports `[gap left open]`, so the corpus never silently loses a window — but closing
  it needs a larger `--max` chosen by hand. See [what I would do with more time](#what-i-would-do-with-more-time).
- **The evaluation has its own limits** — synthetic positives, single-label ground truth, and
  coupling to the corpus it was built on. Stated in full under [Evaluation](#evaluation).
- **A refusal from the second gate still returns sources.** When the model declines, those
  five papers are what was searched, not what grounded an answer — the response shape cannot
  currently say which.
- **No ANN index on the vectors.** Measured trade-off in [Data model](#no-indexes-beyond-the-primary-and-unique-keys).
- **The corpus is a snapshot.** `paper ingest` re-run picks up new and revised papers via
  the watermark, but nothing schedules it.

## What I would do with more time

Roughly in the order I would pick them up.

1. **Resumable backfill.** The incremental path keeps a corpus *current*; it cannot catch one
   *up*. A run stopped by `--max` above the watermark leaves a span unfetched, and the
   watermark guard correctly refuses to advance over it — but the remedy is manual, a larger
   `--max`. The real fix is a second piece of state: a floor alongside the ceiling, so a
   bounded run resumes where the last one stopped instead of restarting at the top of the feed.
2. **Schedule the ingest.** The incremental path is built and cheap — a re-run with nothing
   new costs one throttled request per page — but nothing triggers it. A cron container or
   an external scheduler is a small change.
3. **Gap-fill the time series behind a flag.** Periods with no papers are currently omitted.
   That is honest, but a `?fill=true` using `generate_series` would suit some charts better.
4. **A read-only database role for the API.** Both read paths only `SELECT`, but they connect
   as the owner. Splitting the roles is the real hardening step and costs almost nothing.
5. **Re-embed in batches as a separate command.** Changing the embedding model currently
   means re-running ingest; a `paper reembed` working purely from stored text would be
   clearer and would not touch arXiv at all.
6. **Harden against prompt injection beyond the architectural bound.** Today the model has
   no tools, so the worst case is a wrong answer. If it ever gained tools, the delimiting and
   the system prompt alone would not be enough, and this would need real work.
7. **An ANN index, once measured.** Exact search is 1.2 ms over 2,370 vectors. At ~100k it
   would be worth benchmarking HNSW against the recall it costs — but not before.
