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

**Reference docs** — [the brief](docs/brief.md) · [architecture](docs/architecture.md) ·
[data model](docs/data-model.md) · [rag](docs/rag.md) · [evaluation](docs/evaluation.md) ·
[api](docs/api.md)

## Quick start

```bash
cp .env.example .env          # then put a Groq key in LLM_API_KEY
docker compose up --build     # database + migrations + API

# in another terminal — load the corpus (~2 min)
docker compose exec api paper ingest -c cs.AI -c cs.CL -c cs.LG

curl -s 'localhost:8000/stats/top-categories?n=3'

curl -s -X POST localhost:8000/ask -H 'Content-Type: application/json' \
  -d '{"question": "How do LLM agents structure long-term memory?"}'
```

```jsonc
// GET /stats/top-categories?n=3
[{"category": "cs.AI", "count": 1276}, {"category": "cs.LG", "count": 1160},
 {"category": "cs.CL", "count": 667}]

// POST /ask
{
  "answer": "LLM agents structure long-term memory in several ways.
             - **Structured organization**: arranging memories into explicit formats such as
               ledgers, to-do lists, or trees (2602.11243).
             - **Graph-based cognition**: REALM models memory as a heterogeneous cognitive
               graph, reconsolidating memories from retrieval feedback (2609.16053).
             - **Lifecycle awareness**: LifeFuse-Mem separates durable from transient
               knowledge, so temporary context cannot overwrite long-term state
               (2609.12436)...",
  "sources": [
    {"arxiv_id": "2602.11243", "title": "Evaluating Memory Structure in LLM Agents",
     "score": 0.8731},
    {"arxiv_id": "2609.16053", "title": "Retrieval-Driven Memory Reconsolidation for
      Long-Term LLM Agents", "score": 0.8556},
    {"arxiv_id": "2609.12436", "title": "LifeFuse-Mem: Lifecycle-Aware State Fusion Against
      Temporary Overwriting for Long-Term Memory", "score": 0.8306}
    // ... 2 more, down to 0.8089
  ]
}
```

Every id in `sources` is a row that was joined, so it always resolves to a stored paper —
and the answer cites them inline.

> [!TIP]
>
> - **No manual migration step.** The container runs `alembic upgrade head` before the API
>   starts, so a fresh database becomes a working one with one command.
> - **Interactive OpenAPI docs at [`/docs`](http://localhost:8000/docs)** once the stack is up.

> [!NOTE]
> Needs Docker and ~1.7 GB of disk — a 1.23 GB app image (the embedding model is baked in)
> plus 464 MB of Postgres.

## Evaluation

`/ask` refuses what this corpus cannot answer:

- first on a similarity threshold (**gate 1**,
  before the model is called)
- then in the grounded prompt (**gate 2**)

```bash
uv run python scripts/evaluate.py             # retrieval + threshold sweep — offline, free
uv run python scripts/evaluate.py --with-llm  # + end-to-end answer/refuse behaviour
```

**Evaluation methodology** at [docs/evaluation.md](docs/evaluation.md). Run log:
[`eval/RESULTS.md`](eval/RESULTS.md).

- **Dataset** — [`eval/golden.jsonl`](eval/golden.jsonl):
  - 121 rows
  - 97 positives written (paper-first); 24 must-refuse hand-written.
- **Retrieval** — over 2,370 papers: `recall@1` 0.897 · **`recall@5` 0.948** · `MRR@20` 0.918.
- **Threshold `t`** — `0.75`

**Choosing `t`** — the lowest value reaching the fewest false passes while still at the
92/97 retrieval ceiling:

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/img/threshold-dark.svg">
    <img alt="Threshold sweep. From 0.70 to 0.75 the only answers lost are the 5 retrieval never found, while must-refuse questions let through fall from 4 to 2. At 0.78 a sixth answer is lost, and 0.80 reaches zero false passes only by losing ten answers." src="docs/img/threshold-light.svg" width="800">
  </picture>
</p>

**Both gates, end to end** — all 24 must-refuse, plus 40 positives sampled from the 97 (limited LLM calls available due to free tier):

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/img/gates-dark.svg">
    <img alt="End-to-end outcomes. Of 24 must-refuse questions, 22 stop at gate 1 with zero tokens and 2 at gate 2; none are answered. Of 40 positives sampled from the 97, 37 cite the correct paper and none are wrongly refused." src="docs/img/gates-light.svg" width="820">
  </picture>
</p>

## Ingesting papers

```bash
docker compose exec api paper ingest -c cs.AI -c cs.CL -c cs.LG   # default --max 1200
docker compose exec api paper ingest -c cs.AI --full              # ignore the watermark
docker compose exec api paper wipe                                # delete everything
```

> [!NOTE]
> `-c` (category) is required and repeatable.

The query is built from `cat:` clauses, so with no category there is no `search_query` to
send — arXiv has no "everything" query.

Category code reference: [arXiv's taxonomy](https://arxiv.org/category_taxonomy)

## Data model

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

- **`arxiv_id` is version-stripped and `UNIQUE`** — idempotent re-ingest is a database
  constraint, not a read-then-write check that can race.
- **`content_hash` + `model` decide re-embedding** — a metadata-only revision bumps `updated`
  without triggering one; swapping models invalidates every row through the same field.
- **The watermark is keyed by the category set**, so adding a category forces a full scan
  instead of silently skipping its older papers.
- **Embeddings are written in the paper's transaction** — a crash between the two is not
  handled, it is impossible. Hence pgvector over a separate vector store and its dual-write
  problem.
- **No indexes beyond the primary and unique keys.** Measured: every plan is a sequential
  scan, 2.8–12.9 ms on 2,370 papers. HNSW is 8.7× faster on the vectors and saves 1 ms inside
  a request whose LLM call takes 1–3 seconds.

More details at **[docs/data-model.md](docs/data-model.md)**

## APIs

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

- Read details at **[docs/api.md](docs/api.md)**
- Interactive OpenAPI at [`/docs`](http://localhost:8000/docs) while running.

## Asking questions — `POST /ask`

Embed the question → retrieve the nearest abstracts → answer from those alone.

> [!IMPORTANT]
> The relevance check runs before the model is called. Refusing is control flow and an off-topic question costs zero tokens.

A single cosine threshold cannot separate "this corpus answers that" from "it doesn't". Thus introduced two gates, each catching what the other misses:

|     | Gate                                                     | Catches                | Cost         |
| --- | -------------------------------------------------------- | ---------------------- | ------------ |
| 1   | `score >= RELEVANCE_THRESHOLD`, **before** generation    | the clear cases        | zero tokens  |
| 2   | the grounded prompt — _answer only from these abstracts_ | what slips past gate 1 | one LLM call |

```console
"What is the CAP theorem?"           top-1 0.687  → gate 1 refuses, LLM never called
{"answer": "I don't have information about that in the paper dataset.", "sources": []}

"What guarantees does Raft provide?" top-1 0.799  → gate 1 PASSES, gate 2 refuses
{"answer": "The provided abstracts do not contain any information about
            the Raft consensus algorithm.", "sources": [...5 papers...]}
```

Gate 1 is deterministic; gate 2 is but probabilistic.

> [!WARNING]
> **Model ids move.** `openai/gpt-oss-20b` was a Groq _production_ model on 15 Sep 2026 and
> produced every sample here. If it has been retired, set `LLM_MODEL` to any current id —
> nothing else changes. **Without a key `/ask` still works**: `503` with the retrieved papers
> in `sources` and an explicit message.

- The RAG pipeline details: **[docs/rag.md](docs/rag.md)**
- **Embeddings** are computed in-process by [`fastembed`](https://github.com/qdrant/fastembed):
  `BAAI/bge-small-en-v1.5`, quantised ONNX, 384 dimensions, 63 MB baked into the image — warmed at startup, so the cost is in the logs rather than on the first request. read: **[docs/rag.md](docs/rag.md#embeddings)**

## Configuration

| Variable                              | Default                          | Notes                                                                                       |
| ------------------------------------- | -------------------------------- | ------------------------------------------------------------------------------------------- |
| `POSTGRES_USER` / `_PASSWORD` / `_DB` | `arxiv`                          | Used by the database container                                                              |
| `DATABASE_URL`                        | `…@localhost:5432/arxiv`         | For the CLI **from your host**; the API container overrides it to reach `db`                |
| `LLM_API_KEY`                         | —                                | 🔴 The only value to be set. Free at [console.groq.com/keys](https://console.groq.com/keys) |
| `LLM_BASE_URL`                        | `https://api.groq.com/openai/v1` | Any OpenAI-compatible endpoint                                                              |
| `LLM_MODEL`                           | `openai/gpt-oss-20b`             | Tested 15 Sep 2026 — see the warning above                                                  |
| `RELEVANCE_THRESHOLD`                 | `0.75`                           | Measured, not chosen → [evaluation](docs/evaluation.md#the-threshold-sweep)                 |
| `RETRIEVAL_TOP_K`                     | `5`                              | `10` measured and rejected → [rag](docs/rag.md#retrieval)                                   |
| `ARXIV_PAGE_SIZE`                     | `200`                            | arXiv's documented ceiling is 2000                                                          |
| `ARXIV_MIN_REQUEST_INTERVAL`          | `3.0`                            | arXiv's Terms of Use floor. Cannot be set lower                                             |
| `LOG_LEVEL`                           | `INFO`                           |                                                                                             |

> [!IMPORTANT]
> **`LLM_API_KEY` is the only value to be set.** Without it everything works except answer generation.

## Tests

```bash
cp .env.example .env       # if you have not already
docker compose up -d db    # Postgres + pgvector on :5432 — the only prerequisite
uv sync && uv run pytest
```

- No network, no LLM, no model download — the embedding provider and LLM client sit behind interfaces the suite fakes.
- They run against a throwaway `arxiv_test` database built by the real migrations, so the schema under test is the schema that ships.
- Aimed at what breaks: XML parsing against a saved capture, idempotent re-ingest, staleness
  detection, the watermark's gap guard, aggregation counts on seeded data, and the no-match path.

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
docs/                brief · architecture · data-model · rag · evaluation · api · img/
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
  corpus coupling. Detailed in [docs/evaluation.md](docs/evaluation.md#limits).
- **No ANN index on the vectors.** Measured trade-off in
  [docs/data-model.md](docs/data-model.md#no-indexes-beyond-the-primary-and-unique-keys).
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
