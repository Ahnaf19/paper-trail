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

### Embeddings run locally — there is no embedding API key

`LLM_API_KEY` is for *generation* only. Embeddings are computed **in this process**, by
[`fastembed`](https://github.com/qdrant/fastembed) running `BAAI/bge-small-en-v1.5` on ONNX
Runtime — a library, not a hosted service. No API key, no per-call cost, no rate limit, and
no network at query time.

| | |
|---|---|
| Model | `BAAI/bge-small-en-v1.5`, quantised ONNX · 384 dimensions |
| On disk | **63 MB** of weights (~64 MB with the tokenizer) |
| Runtime | ONNX Runtime — **no PyTorch**, which keeps the image small |
| Where | Baked into the Docker image at build time (`EMBEDDING_CACHE_DIR=/opt/models`) |

That last row is the point: the model is downloaded **once, during `docker build`**, so a
running container never reaches out for weights. Outside Docker, `fastembed` downloads them
on first use and caches them, which is why the very first local run is slower than the rest.

The model is warmed during application startup rather than on the first request, so the cost
is visible in the logs and gated by the container healthcheck.

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

### The columns

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

### Why it looks like this

- **`arxiv_id` is stored without its version suffix, and is unique.** That makes idempotent
  re-ingest a database constraint rather than a read-then-write check that can race.
- **`content_hash` + `model` decide re-embedding.** A metadata-only revision bumps `updated`
  but must not trigger a re-embed — and changing the embedding model invalidates every row
  automatically, through the same mechanism rather than a separate migration.
- **The watermark is keyed by the category set.** Ingest `cs.AI` alone, then re-run adding
  `cs.LG`, and a shared watermark would stop early and silently skip every `cs.LG` paper
  older than the mark — a successful-looking run with a large hole in it. A key mismatch
  forces a full scan instead.
- **Embeddings are written in the same transaction as the paper.** A crash between storing a
  paper and storing its vector is not handled; it is impossible. That is the whole reason
  pgvector was chosen over a separate vector store — a separate store reintroduces a
  dual-write problem that transactions no longer cover.

### No indexes beyond the primary and unique keys

Deliberate, and measured rather than assumed. Every plan is a sequential scan:

| Query | Plan | Execution |
|---|---|---|
| `top-categories` | seq scan + hash join + top-N heapsort | **1.9 ms** |
| `papers-over-time` | seq scan ×3 + group aggregate | **8.5 ms** |
| `/ask` retrieval, top-5 of 1,200 vectors | seq scan + top-N heapsort | **7.8 ms** |

**For the aggregations, a seq scan is the correct plan, not a tolerated one.** An index buys
*selectivity*, and a `GROUP BY` over the whole corpus has none — Postgres must read every row
whatever the access path.

**For vector search an index genuinely would be faster**, and that is the interesting case.
Building an HNSW index over the same 1,200 vectors:

| | Exact (today) | HNSW |
|---|---|---|
| Execution | 7.8 ms | **0.6 ms** |
| Result | exact nearest neighbours | **approximate** |
| Index size | — | 2408 kB, against a 2400 kB table |

So it is ~12× faster — and still the wrong trade here. Those 7 ms sit inside a request whose
LLM call takes 1–3 **seconds**: the saving is roughly 0.3% of end-to-end latency, invisible
to a caller, bought by giving up a correctness guarantee and storing an index larger than the
table it indexes. An ANN index earns its place when the scan itself becomes perceptible —
on the order of 100k vectors, not 1.2k.

## APIs

Five endpoints. Every aggregate is computed by Postgres with `GROUP BY` — **the API never
contacts arXiv**, and answers with the network disconnected.

| Endpoint | Returns |
|---|---|
| `GET /health` | Liveness, including the database — so "ok" means *can serve* |
| `GET /stats/top-categories` | Top N categories by distinct paper count |
| `GET /stats/top-authors` | Top N authors by distinct paper count |
| `GET /stats/papers-over-time` | Paper counts per period and category |
| `POST /ask` | Grounded answer from the stored abstracts, with cited sources |

```console
$ curl -s 'localhost:8000/stats/top-categories?n=3'
[{"category": "cs.AI", "count": 622}, {"category": "cs.LG", "count": 606},
 {"category": "cs.CL", "count": 341}]
```

📖 **Full reference — parameters, accepted values, responses, examples and error codes:
[`docs/api.md`](docs/api.md)**
· Interactive OpenAPI docs at [`/docs`](http://localhost:8000/docs) while the service runs.

## Asking questions — `POST /ask`

Embeds the question, retrieves the nearest abstracts by cosine similarity, and answers from
those alone. Request and response shapes are in [`docs/api.md`](docs/api.md#post-ask); what
follows is why it behaves the way it does.

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
model on 15 Sep 2026 and is what the documented samples came from. If it has been retired by the
time you read this, set `LLM_MODEL` to any current id; nothing else needs to change.

**Without a key, `/ask` still works** — it returns 503 with the retrieved papers in `sources`
and an explicit message. Retrieval succeeded; discarding it with a 500 would be the worse
answer. The same applies to a timeout or a retired model id.

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
