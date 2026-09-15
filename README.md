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
[{"category": "cs.AI", "count": 622}, {"category": "cs.LG", "count": 606},
 {"category": "cs.CL", "count": 341}]

// POST /ask
{
  "answer": "Early-stage LLM agents relied on very simple, retrieval-augmented memory
             systems. These were designed mainly to store and recall facts, perform
             multi-hop retrieval, and track time-based changes, but they did not organize
             knowledge into structured hierarchies such as ledgers, to-do lists, or
             trees... (2602.11243).",
  "sources": [
    {"arxiv_id": "2602.11243", "title": "Evaluating Memory Structure in LLM Agents",
     "score": 0.8432},
    {"arxiv_id": "2609.12808", "title": "K-Bench: A Benchmark for LLM Unlearning in
      Agentic Deployments", "score": 0.7756},
    {"arxiv_id": "2609.12436", "title": "LifeFuse-Mem: Lifecycle-Aware State Fusion...",
     "score": 0.7716}
    // ... 2 more, down to 0.7674
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
docker compose exec api paper ingest -c cs.AI -c cs.CL -c cs.LG   # default --max 1100
docker compose exec api paper ingest -c cs.AI --full              # ignore the watermark
docker compose exec api paper wipe                                # delete everything
```

> [!NOTE]
> **`--max 1100` yields 1,200 papers.**

The limit is checked _after_ each page commits, and pages are 200 — so page 6 crosses 1,100 and stops at 1,200. 1,100 rather than 1,000 is headroom over the brief's "at least 1,000".

- **`-c` is required and repeatable.** The query is assembled from `cat:` clauses, so with
  no category there is no `search_query` to send — arXiv has no "everything" query. Omitting
  it exits 2 with an example to copy. Valid codes:
  [arXiv's taxonomy](https://arxiv.org/category_taxonomy). Loaded ones:
  `GET /stats/top-categories?n=100` — the default run spans 92.
- **Re-running is safe and cheap.** Papers upsert on `arxiv_id` — a database constraint, not application logic. Sorted by last-update descending, so one high-water mark catches new _and_ revised papers; the scan stops once a whole page predates the mark.
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

Measured, not assumed. Every plan is a sequential scan:

| Query                                     | Execution |
| ----------------------------------------- | --------- |
| `top-categories`                          | 1.9 ms    |
| `papers-over-time`                        | 8.5 ms    |
| `/ask` retrieval — top-5 of 1,200 vectors | 7.8 ms    |

**The aggregations don't want an index.** An index buys _selectivity_; a `GROUP BY` over the
whole corpus has none. Postgres reads every row regardless, so a seq scan is the right plan.

**Vector search would genuinely be faster with one** — that's the real trade. Building HNSW
over the same 1,200 vectors:

|            | Exact (today) | HNSW                        |
| ---------- | ------------- | --------------------------- |
| Execution  | 7.8 ms        | **0.6 ms**                  |
| Result     | exact         | **approximate**             |
| Index size | —             | 2408 kB, on a 2400 kB table |

12× faster, and still wrong here: **those 7 ms sit inside a request whose LLM call takes 1–3
seconds.** You would trade a correctness guarantee for 0.3% of end-to-end latency, and store
an index bigger than the table. It earns its place when the scan itself becomes perceptible —
around 100k vectors, not 1.2k.

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
[{"category": "cs.AI", "count": 622}, {"category": "cs.LG", "count": 606},
 {"category": "cs.CL", "count": 341}]
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
sufficient alone, which is why both are there.

### Threshold calibration

The threshold is the one constant here that cannot be reasoned about.

> [!NOTE]
> It is a property of the embedding model _and_ the corpus.

So it is measured, by a script that ships with the repo:

```bash
uv run python scripts/calibrate_threshold.py
```

**Method:** 23 questions in three groups. Embed each, take the **top-1** cosine similarity
against the stored abstracts, and look for a value that sits above everything we must refuse
and below everything we must answer.

| Group                                                      | Must   | Measured (15 Sep 2026, 1,200 papers) |
| ---------------------------------------------------------- | ------ | ------------------------------------ |
| In-corpus — _"How is RAG evaluated?"_ (12)                 | answer | 0.745 – 0.868                        |
| **Adjacent CS, absent — _"What is the CAP theorem?"_ (5)** | refuse | **0.649 – 0.799**                    |
| Off-topic — _"How do I bake sourdough?"_ (6)               | refuse | 0.471 – 0.583                        |

The third group is the useless test: separating arXiv from sourdough is trivial. **The middle group decides the number**, and it is also where the method runs out of road — those five questions overlap the answerable range, so _no threshold separates them cleanly_.

> [!WARNING]
> The embedder reads _consensus, stability and guarantees_ topical overlap. `bge-small` encodes what a text is **about**, not whether it **contains an answer**

- In group 2 the score `0.799` came from _"What guarantees does the Raft consensus algorithm provide?"_
- which matches **"Algorithmic stability via ensembling"**
- So a question sharing ML vocabulary will always score high (dataset has papers from category `cs.AI`).

> [!NOTE]
> `RELEVANCE_THRESHOLD = 0.71` removes the cheap failures at zero token cost.

Every off-topic question and 4 of 5 adjacent ones — and leaves the residue to gate 2.
Pushing it above 0.799 would also refuse 8 of the 12 questions the corpus genuinely answers.

**Why 0.71 and not 0.73.** Every value in the gap `(0.687, 0.745]` behaves _identically_ on
all 23 questions, so the evidence does not pick one — the choice is only about which side
gets the margin. The two errors are not symmetric:

| Set too low               | Set too high                    |
| ------------------------- | ------------------------------- |
| one wasted LLM call       | a real question refused         |
| gate 2 usually catches it | **no second gate on this side** |

So the margin leans low. 0.71 leaves **0.035** under the answerable floor against **0.023**
over the adjacent ceiling; 0.73 had it backwards — 0.015 and 0.043 — putting three times the
protection on the side that costs nothing. The corpus shifts on every ingest, and that
headroom is what absorbs the drift.

**Re-run the script if the corpus or the embedding model changes.** It prints the separable window, or says plainly that there isn't one.

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
| `RELEVANCE_THRESHOLD`                 | `0.71`                           | Calibrated, not chosen. See `/ask`.                                                                          |
| `RETRIEVAL_TOP_K`                     | `5`                              | Papers passed to the model as context.                                                                       |
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

- 108 tests that **never touch the network, an LLM, or download a model**
- They run against a throwaway `arxiv_test` database, so your corpus is untouched.
- The embedding provider and the LLM client both sit behind small interfaces, and the suite injects fakes before the app starts.
- The test database is created and migrated by the real Alembic migrations, so the schema under test is the schema that ships.
- They are aimed at what would actually break: XML parsing against a saved real capture,
  idempotent re-ingest, staleness detection, aggregation counts on seeded data, and the
  no-good-match path.

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
- **The relevance threshold does not separate cleanly.** Real computer science that this
  corpus lacks can score above answerable questions — _"what guarantees does Raft provide?"_
  reaches 0.799 by matching _"Algorithmic stability via ensembling"_. The grounded prompt
  catches it, but that second gate is probabilistic where the first is not. A cross-encoder
  re-rank over the top-k would be the real fix.
- **A refusal from the second gate still returns sources.** When the model declines, those
  five papers are what was searched, not what grounded an answer — the response shape cannot
  currently say which.
- **No ANN index on the vectors.** Measured trade-off in [Data model](#no-indexes-beyond-the-primary-and-unique-keys).
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
7. **An ANN index, once measured.** Exact search is 7.8 ms over 1,200 vectors. At ~100k it
   would be worth benchmarking HNSW against the recall it costs — but not before.
