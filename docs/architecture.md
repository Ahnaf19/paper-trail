# Architecture

[← README](../README.md) · siblings: [data model](data-model.md) · [RAG](rag.md) · [API](api.md) · [evaluation](evaluation.md)

One write path and two read paths over a single Postgres. They share the database and
nothing else — **the read paths never contact arXiv, and the write path never talks to the
LLM.**

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="img/architecture-dark.svg">
    <img alt="System architecture. The write path, paper ingest, is the only thing that talks to arXiv: the Atom API at one request per 3 seconds with backoff on 429, then fetch (ArxivClient), parse (parse_feed), upsert (upsert_papers) and embed (refresh_embeddings). Upsert and embed sit inside one transaction per page, so a paper cannot commit without its vector. The watermark advances separately, after the loop, and only if the walk reached the previous mark. Everything lands in PostgreSQL with pgvector — papers, authors, categories and 384-dim vectors — which then feeds two read paths: GET /stats/*, three GROUP BY queries computed by Postgres with no network at all, and POST /ask, which embeds, retrieves top-k, gates and generates against an OpenAI-compatible LLM." src="img/architecture-light.svg" width="860">
  </picture>
</p>

## The write path — `paper ingest`

`app/services/ingest.py` drives the loop. Each stage is its own module and knows nothing of
its neighbours' concerns:

- **`ArxivClient`** knows HTTP and throttling, but not Atom.
- **`parse_feed`** knows Atom, but not the database.
- **`upsert_papers`** knows the database, but has never seen a URL.

### One transaction per page

A page is upserted **and embedded** inside one transaction, then committed.

- **A crash loses at most the page in flight**, never the run.
- **A paper can never commit without its vector** — both writes are in the same transaction,
  so the inconsistent state is unreachable rather than handled.

### Rate limiting, and the 429 that isn't documented

- **The documented rule:** one request every 3 seconds, _"a single connection at a time"_.
  The client sleeps the remainder of the interval before each request and logs **after** the
  sleep — so the gap between two log lines *is* the throttle, provable from the output.
- 🔴 **It is necessary but not sufficient.** Verified 14 Sep 2026: a first *uncached* query
  returned **HTTP 429 repeatedly**, while a common cached query returned 200 instantly from
  the Fastly edge. Neither arXiv's API docs nor its Terms of Use mention 429 at all.
- **So 429 and 5xx are retryable** — bounded exponential backoff (`2s, 4s, 8s…`, honouring
  `Retry-After`), exiting non-zero after `ARXIV_MAX_RETRIES` rather than hanging.

### Deliberately synchronous

- **There is no concurrency to win.** The bottleneck is a deliberate `time.sleep`, and the
  Terms of Use forbid parallel connections anyway.
- **Async would cost, not pay** — harder testing and trickier transaction handling, for zero
  throughput.
- **The API is async** where the multi-second LLM call makes it pay; **the CLI is sync.**
  Neither style leaks into the other.

### Idempotent re-runs

- **Identity is `arxiv_id`**, version suffix stripped, `UNIQUE`. Re-ingest is
  `INSERT … ON CONFLICT DO UPDATE` — **a database constraint, not a read-then-write that can
  race.**
- **Unchanged data is not rewritten.** A paper moves only when arXiv's `updated` is newer, so
  `RETURNING` names exactly what changed.
- **Authors and categories get the same treatment** — get-or-create in two statements per
  page regardless of row count, never one per author.

### The watermark, and why a truncated run refuses to advance it

- **One mark catches new *and* revised papers.** The feed is sorted by `lastUpdatedDate`
  descending, so a revision bumps `updated` and floats back to the top. The scan stops once a
  whole page predates the mark.
- **The mark asserts *"everything at or above this is ingested"*** — which only holds if the
  walk came down far enough to meet the previous mark.
- 🔴 **A run cut short by `--max` can stop above it**, leaving a span never fetched.
  Advancing over that span would bury it permanently below the next run's stop threshold.
- **So `_advance_watermark` moves only if this run's oldest paper reaches the previous mark.**
  Otherwise the mark stays put and the run reports **`[gap left open]`** — re-run with a
  larger `--max`, or `--full`.
- **The same guard covers exceptions.** The mark is written only after the loop exits
  cleanly, so a crash never records progress that was not made.

> [!NOTE]
> **`--max` rounds up to a whole page.** It is checked _after_ each page commits, and pages
> are 200 — so any value in 1,001–1,200 yields exactly 1,200 papers.

### The watermark is keyed by the category set

`ingest_state.categories_key` is the sorted set, e.g. `cs.AI|cs.CL|cs.LG`.

- **A shared mark would silently skip papers.** Ingest `cs.AI`, then re-run adding `cs.LG`:
  the older `cs.LG` papers look "already done" and are never fetched.
- **A key mismatch forces a full scan** instead.

### Why `-c` is required

The query is built from `cat:` clauses joined with `+OR+`. With no category there is no
`search_query` to send — **arXiv has no "everything" query.** Omitting `-c` exits 2 with a
copyable example rather than guessing a default.

## The read paths

### `GET /stats/*`

- **Every aggregate is a `GROUP BY`** executed by Postgres over stored rows — nothing is
  recomputed from arXiv.
- **All three endpoints answer with the network disconnected.**
- **Responses are flat lists of labelled points** — the shape a charting library takes
  directly.

### `POST /ask`

```
question ──▶ embed_query ──▶ retrieve top-k ──▶ gate 1 ──▶ fit_context ──▶ LLM ──▶ answer
             (thread)        (one SQL join)      │                                 + sources
                                                 └── below threshold ──▶ refuse, 0 tokens
```

Three things in that path are load-bearing:

- **Encoding runs in a thread.** `embed_query` is CPU-bound; awaiting it would stall the
  event loop for every other request — the exact async trap the repo standards call out.
- **Retrieval is one statement, not top-k-then-look-up.** The paper metadata is joined in
  the same query that ranks the vectors, so **a source is a row that was joined and cannot
  fail to resolve.**
- **`sources` is derived from the papers that fit the context budget**, not from everything
  that cleared the threshold — otherwise a large `top-k` would cite context the model never
  saw.

Detail on the gates, the prompt and the failure modes: **[docs/rag.md](rag.md)**.

## Layering

```
routers/    HTTP only — validate, delegate, choose the status code
services/   everything the application does; no FastAPI import anywhere in here
models/     SQLAlchemy ORM — the schema's source of truth
schemas/    Pydantic at every boundary that crosses a wire (HTTP, or arXiv's XML)
```

- **A service returns a result object; the router decides what status code that means.**
  `AnswerResult.degraded` is the clearest case — the service records that generation failed
  *after* retrieval succeeded, and the router turns that into `503` with the sources intact.
- **Two `Protocol` seams keep the tests off the network** — `Embedder` and `LLMClient`. The
  suite fakes both, which is why it runs with no network, no LLM and no model download.

## Failure behaviour

| What fails                                                 | What happens                                                                                                               |
| ---------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| arXiv returns 429 / 5xx                                    | Bounded exponential backoff, `Retry-After` honoured; exits non-zero after `ARXIV_MAX_RETRIES` with the database consistent |
| Ingest crashes mid-run                                     | The in-flight page rolls back; committed pages stand; the watermark is not advanced, so a re-run repairs it                |
| `--max` stops the walk early                               | `[gap left open]` — the mark stays put, nothing is silently skipped                                                        |
| No `LLM_API_KEY`                                           | `/ask` returns `503` with the retrieved papers in `sources` and an explicit message                                        |
| LLM times out, is rate limited, or the model id is retired | Same `503` shape, with a message naming which                                                                              |
| Database unreachable                                       | `GET /health` reports it — "ok" means _can serve_, not _process is alive_                                                  |
| Any unhandled error                                        | `{"detail": "<message>"}`. No stack traces, no driver text, no provider body echoed back                                   |

The LLM client never logs the exception object or the provider's response body — a base URL
can carry a key in its query string.

## Startup

- **`alembic upgrade head` runs in the container entrypoint**, before the API starts — a
  fresh database becomes a working one with one command and **no manual migration step**.
- **The embedding model is warmed in the lifespan**, before traffic is served. The cost shows
  up in the logs and is gated by the healthcheck, rather than landing on the first request.
