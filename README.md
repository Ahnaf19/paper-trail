# paper-trail

A CLI that ingests arXiv papers into PostgreSQL, and one FastAPI service that both serves
aggregations computed in SQL and answers natural-language questions grounded in the stored
abstracts — every answer carrying a trail back to the papers it came from.

```
  arXiv Atom API                      [ external ]
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

Requires Docker and roughly 3 GB of disk (the image bakes in the embedding model).

```bash
cp .env.example .env          # then put a Groq key in LLM_API_KEY — see Configuration
docker compose up --build     # database + migrations + API

# in another terminal: load the corpus (~2 minutes for 1,200 papers)
docker compose exec api paper ingest -c cs.AI -c cs.CL -c cs.LG

curl -s localhost:8000/stats/top-categories?n=5
```

There is no manual migration step — the container runs `alembic upgrade head` before the
API starts, so a fresh database becomes a working one with one command.

`/docs` serves the interactive OpenAPI page once the stack is up.

## Configuration

Everything is an environment variable; nothing is hardcoded. `.env.example` lists them all.

| Variable | Default | Notes |
|---|---|---|
| `POSTGRES_USER` / `_PASSWORD` / `_DB` | `arxiv` | Used by the database container. |
| `DATABASE_URL` | `…@localhost:5432/arxiv` | For running the CLI **from your host**. The API container overrides it to reach `db` on the compose network. |
| `LLM_API_KEY` | — | 🔴 The only value you must supply. Free at [console.groq.com/keys](https://console.groq.com/keys), no card. |
| `LLM_BASE_URL` | `https://api.groq.com/openai/v1` | Any OpenAI-compatible endpoint. |
| `LLM_MODEL` | `openai/gpt-oss-20b` | Tested 15 Sep 2026 — see the warning under `/ask`. |
| `RELEVANCE_THRESHOLD` | `0.73` | Calibrated, not chosen. See `/ask`. |
| `RETRIEVAL_TOP_K` | `5` | Papers passed to the model as context. |
| `ARXIV_PAGE_SIZE` | `200` | arXiv's documented ceiling is 2000. |
| `ARXIV_MIN_REQUEST_INTERVAL` | `3.0` | arXiv's Terms of Use floor. Cannot be set lower. |
| `LOG_LEVEL` | `INFO` | |

**Without `LLM_API_KEY` everything still works except answer generation** — `/ask` returns
the retrieved papers with an explicit message instead of an answer. Aggregations, ingestion
and retrieval are unaffected.

## Ingesting papers

```bash
docker compose exec api paper ingest -c cs.AI -c cs.CL -c cs.LG   # default --max 1100
docker compose exec api paper ingest -c cs.AI --full              # ignore the watermark
docker compose exec api paper wipe                                # delete everything
```

`-c` takes an arXiv category code and is repeatable. The full list of valid codes is arXiv's
own taxonomy: **<https://arxiv.org/category_taxonomy>**. To see which categories are actually
loaded in your corpus, call `GET /stats/top-categories?n=100` — the 1,200-paper default run
spans 92 of them.

A mistyped code cannot be detected: arXiv answers it exactly as it answers a real but empty
category — `200`, `totalResults=0`, no entries. So rather than guess, a run that fetched no
usable page at all logs a **warning** naming the likely cause and **exits non-zero**, instead
of the green `0 papers` success it would otherwise look like.

**Re-running is safe and cheap.** Papers are upserted on `arxiv_id`, so a second run creates
no duplicates — that is a database constraint, not application logic. Papers are sorted by
last-update descending, so one high-water mark catches both new papers and revised old ones;
the scan stops as soon as a whole page is older than the mark.

Measured on a 1,200-paper corpus: a cold run takes **~128 s** (≈86% of that is computing
embeddings), and a re-run with nothing new takes **3.0 s per page** — which is exactly the
mandated throttle and nothing else, because unchanged papers are never re-embedded.

Ingestion is deliberately **synchronous**. arXiv's Terms of Use require "a single connection
at a time", so there is no concurrency to win; async here would add ceremony and trickier
transaction handling for zero throughput.

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
```

The line that matters is **`routers/` vs `services/`**. A router validates input and calls a
service; the service does the work and knows nothing about HTTP. That is what lets the
aggregation queries and the question-answering flow be tested directly, and it keeps status
codes out of business logic — `qa.answer_question` returns a result with a `degraded` flag,
and the router decides that means 503.

The same split runs through ingestion: `arxiv_client` knows HTTP and nothing about Atom,
`arxiv_parser` knows Atom and nothing about the database, `upsert` knows the database and has
never seen a URL. That is why the parser can be tested against a saved XML fixture with no
network at all.

## Schema

```
  paper                         author            category
  ─────                         ──────            ────────
  id            PK              id    PK          id    PK
  arxiv_id      UNIQUE  ◄── identity for idempotent re-ingest
  title                         name  UNIQUE      code  UNIQUE
  abstract                         ▲                 ▲
  primary_category                 │                 │
  published     TIMESTAMPTZ        │                 │
  updated       TIMESTAMPTZ  ◄── drives the watermark│
  doi           NULL               │                 │
  journal_ref   NULL               │                 │
     ▲  ▲  ▲                       │                 │
     │  │  └──── paper_author ──────┘                 │
     │  │        (paper_id, author_id, position)      │
     │  └─────── paper_category ──────────────────────┘
     │           (paper_id, category_id)
     │
     └────────── embedding
                 paper_id      PK/FK   ON DELETE CASCADE
                 vector        VECTOR(384)
                 content_hash  sha256(title + "\n" + abstract)
                 model         which model produced it

  ingest_state — watermark, keyed by the sorted category set (see below)
```

Three details carry most of the design:

- **`arxiv_id` is stored without its version suffix**, and is unique. That makes idempotent
  re-ingest a constraint rather than a read-then-write check that can race.
- **`content_hash` plus `model`** decide whether a paper needs re-embedding. A metadata-only
  revision bumps `updated` but must not trigger a re-embed — and changing the embedding model
  invalidates every row automatically, through the same mechanism.
- **The watermark is keyed by the category set.** Ingest `cs.AI` alone, then re-run adding
  `cs.LG`, and a naive watermark would stop early and silently skip every `cs.LG` paper older
  than the mark — a successful-looking run with a large hole in it. A key mismatch forces a
  full scan instead.

**Embeddings are written in the same transaction as the paper.** A crash between storing a
paper and storing its vector is not handled; it is impossible. That is the whole reason
pgvector was chosen over a separate vector store — a separate store reintroduces a dual-write
problem that migrations and transactions would no longer cover.

## Aggregation API

Three endpoints, not one `?metric=` endpoint: a time series and a top-N list are genuinely
different shapes, and splitting them gives a better OpenAPI document and a simpler client.
Every count is computed by Postgres with `GROUP BY` — **the API never contacts arXiv**, and
answers with the network disconnected.

Interactive docs are at `/docs` once the service is running.

### `GET /stats/papers-over-time`

Paper counts per period and category.

| Param | Type | Default | Notes |
|---|---|---|---|
| `granularity` | `month` \| `year` | `month` | Anything else is a 422. |
| `category` | string | all | Restrict to one category, e.g. `cs.LG`. A code the corpus does not hold is a **404**, not an empty list — see below. |
| `from`, `to` | `YYYY-MM-DD` | unbounded | **Inclusive** of both days, resolved in UTC. |

```console
$ curl -s 'localhost:8000/stats/papers-over-time?granularity=year&category=cs.CL'
[{"period": "2023", "category": "cs.CL", "count": 1},
 {"period": "2024", "category": "cs.CL", "count": 4},
 {"period": "2025", "category": "cs.CL", "count": 17},
 {"period": "2026", "category": "cs.CL", "count": 319}]
```

**An unknown `category` is a 404, deliberately.** Otherwise `[]` would mean two different
things — "this category has no papers in that window" and "you mistyped the code" — and a
caller plotting an empty chart could not tell which. Valid codes come from
[arXiv's taxonomy](https://arxiv.org/category_taxonomy); `/stats/top-categories` lists the
ones actually loaded.

```console
$ curl -s 'localhost:8000/stats/papers-over-time?category=cs.Al'    # capital i, not L
{"detail": "no category 'cs.Al' in this corpus — see GET /stats/top-categories for what is loaded"}
```

### `GET /stats/top-categories`

```console
$ curl -s 'localhost:8000/stats/top-categories?n=5'
[{"category": "cs.AI", "count": 622}, {"category": "cs.LG", "count": 606},
 {"category": "cs.CL", "count": 341}, {"category": "cs.CV", "count": 126},
 {"category": "stat.ML", "count": 86}]
```

Counts exceed the corpus size in total because a paper carries several categories — each
is counted once per category it belongs to, and once only.

### `GET /stats/top-authors`

```console
$ curl -s 'localhost:8000/stats/top-authors?n=3'
[{"author": "Philip Torr", "count": 5}, {"author": "Zihan Wang", "count": 5},
 {"author": "Jian Li", "count": 4}]
```

`n` defaults to 10 and is bounded to 1–100 on both top-N endpoints. The time series is
not paginated — its row count is bounded by the corpus (one row per period per category),
not by anything the caller can send, so there is no parameter to cap.

### `GET /health`

Returns `{"status": "ok"}` — after querying the database, so a container healthcheck gates
on "can actually serve" rather than "process is alive".

**Every endpoint returns 503 `{"detail": "database unavailable"}` when the database is
down**, never a 500 or a traceback. The driver's message goes to the server log, where an
operator can act on it, and not into the response.

### Reading the responses

Each endpoint returns a **flat list of labelled points**, which is the shape charting
libraries take directly — no client-side reshaping, no nesting to flatten.

A few behaviours are deliberate and worth stating rather than leaving to be discovered:

- **Periods are anchored to UTC.** `date_trunc` on a `timestamptz` otherwise uses the
  database session's time zone, so the same corpus would bucket differently per caller.
- **Periods with no papers are omitted, not zero-filled.** Charting libraries handle a
  missing point; inventing a zero row would assert something about a gap in the data.
- **The time series is keyed on `published`**, when a paper first appeared — not
  `updated`, which moves whenever an author corrects a typo.
- **Counts are distinct papers.** A paper in three categories contributes to three
  category buckets, and is counted once in each.
- **Ties are broken by name**, so identical requests return identical ordering.
- **An empty corpus returns `[]` with 200.** An empty chart is a valid chart.
- **Author identity is the name string.** arXiv publishes no author IDs, so two people
  who share a name are one row here. Cheap to state, expensive to fix.

## Asking questions — `POST /ask`

Embeds the question, retrieves the nearest abstracts by cosine similarity, and answers from
those alone.

```console
$ curl -s localhost:8000/ask -H 'content-type: application/json' \
    -d '{"question": "How can LLM agents reuse experience from earlier tasks?"}'
{
  "answer": "LLM agents can reuse earlier experience by explicitly extracting and storing
             reusable components from past interactions... LifeMem clusters accumulated
             interaction trajectories into reusable skills (2609.12655)...",
  "sources": [
    {"arxiv_id": "2609.12655", "title": "LifeMem: Enabling Lifelong Experience Reuse for LLM Agents", "score": 0.841},
    {"arxiv_id": "2609.04280", "title": "...", "score": 0.79}
  ]
}
```

### It says no when it should

```console
$ curl -s localhost:8000/ask -H 'content-type: application/json' \
    -d '{"question": "What is the capital of France?"}'
{"answer": "I don't have information about that in the paper dataset.", "sources": []}
```

🔴 **The relevance check runs before the model is called.** Below the threshold the LLM is
never contacted at all — so the refusal is a property of the control flow rather than an
instruction the model might ignore, and an off-topic question costs zero tokens.

### The threshold was calibrated, not chosen

`RELEVANCE_THRESHOLD = 0.73`, from 23 questions against this corpus:

| group | top-1 cosine similarity |
|---|---|
| answerable from the corpus (12 questions) | 0.765 – 0.872 |
| **real CS, absent from the corpus (5)** | 0.639 – **0.706** |
| off-topic (6) | 0.471 – 0.583 |

The middle row is the whole point. bge-family embeddings score *unrelated computer science*
at 0.64–0.71, so an intuitive `0.5` would confidently answer **"what is the CAP theorem?"**
out of machine-learning abstracts. Separating arXiv from sourdough is trivial and proves
nothing. The separable window is (0.706, 0.765]; 0.73 sits near its midpoint.

**Re-calibrate if the corpus or the embedding model changes** — the number is a property of
both, not a universal constant.

### Configuration

| Variable | Default | Notes |
|---|---|---|
| `LLM_BASE_URL` | `https://api.groq.com/openai/v1` | Any OpenAI-compatible endpoint. |
| `LLM_MODEL` | `openai/gpt-oss-20b` | Tested 15 Sep 2026. See the warning below. |
| `LLM_API_KEY` | — | Free from console.groq.com/keys, no card required. |
| `RELEVANCE_THRESHOLD` | `0.73` | See calibration above. |
| `RETRIEVAL_TOP_K` | `5` | Papers passed as context. |

⚠️ **Model IDs move.** `llama-3.3-70b-versatile` — the obvious choice from memory — is now
Enterprise-only and returns 404 on a free key. `openai/gpt-oss-20b` was a Groq *production*
model on 15 Sep 2026 and is what the samples above came from. If it has been retired by the
time you read this, set `LLM_MODEL` to any current id; nothing else needs to change.

**Without a key, `/ask` still works** — it returns 503 with the retrieved papers in `sources`
and an explicit message. Retrieval succeeded; discarding it with a 500 would be the worse
answer. The same applies to a timeout or a retired model id.

### Performance

Measured over the 1,200-paper corpus with `EXPLAIN ANALYZE`:

| Endpoint | Execution |
|---|---|
| `papers-over-time` | 4.2 ms |
| `top-categories` | 1.5 ms |
| `top-authors` | 6.5 ms |

**No indexes are defined beyond the primary and unique keys.** Every plan is a sequential
scan over a few thousand rows, and an index added without a measurement is decoration.
The join tables are where one would first earn its place — roughly above 100k link rows,
or ~50,000 papers.

## Tests

```bash
uv sync && uv run pytest        # needs a local Postgres; see below
```

100 tests. They **never touch the network, an LLM, or download a model** — the embedding
provider and the LLM client both sit behind small interfaces, and the suite injects fakes
before the app starts. The test database is created and migrated by the real Alembic
migrations, so the schema under test is the schema that ships.

They are aimed at what would actually break: XML parsing against a saved real capture,
idempotent re-ingest, staleness detection, aggregation counts on seeded data, and the
no-good-match path.

## Known limitations

These are deliberate, and stated rather than hidden.

- **Answers come from abstracts, not papers.** A single arXiv API response carries the title,
  authors, categories, dates and abstract — never the full text. So *"what learning rate did
  they use?"* lives in a PDF we never fetch. This bounds what `/ask` can ever be right about.
- **Author identity is the name string.** arXiv publishes no author IDs, so two researchers
  sharing a name are one row in `top-authors`. Cheap to state, expensive to fix properly.
- **Indirect prompt injection is bounded, not eliminated.** Abstracts are third-party text
  entering an LLM prompt; one containing *"ignore previous instructions"* was tested and the
  model answered from the legitimate content instead. But the real protection is
  architectural: **the model has no tools, no database access and no network**, so the worst
  case is a wrong answer, never an action.
- **No ANN index on the vectors.** Exact search over 1,200 vectors is 7.6 ms. An index would
  trade recall for microseconds; it starts to earn its place around 100k rows.
- **The corpus is a snapshot.** `paper ingest` re-run picks up new and revised papers via
  the watermark, but nothing schedules it.

## What I would do with more time

Roughly in the order I would pick them up.

1. **Run the ingest on a machine arXiv will talk to.** Both IPs available to me are
   rate-limited by arXiv (429 on every uncached request), so the 1,200-paper corpus was
   captured once through a VPN and replayed locally through the real client. Every stage is
   exercised, but a long live multi-page run has not been.
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
7. **An ANN index, once measured.** Exact search is 7.6 ms over 1,200 vectors. At ~100k it
   would be worth benchmarking HNSW against the recall it costs — but not before.
