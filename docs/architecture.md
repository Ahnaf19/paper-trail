# Architecture

[← README](../README.md) · siblings: [data model](data-model.md) · [RAG](rag.md) · [API](api.md) · [evaluation](evaluation.md)

One write path and two read paths over a single Postgres. They share the database and
nothing else — **the read paths never contact arXiv, and the write path never talks to the
LLM.**

```
  ┌─ write path ──────────────────────────────────────────────────────────────┐
  │                                                                           │
  │   arXiv Atom API ──▶ fetch ──▶ parse ──▶ upsert ──▶ embed ──▶ watermark   │
  │   1 req / 3 s          │         │         │          │          │        │
  │   backoff on 429       │         │         └──────────┘          │        │
  │                        │         │        one transaction        │        │
  │                        │         │          per page             │        │
  │                    ArxivClient  parse_feed   upsert_papers   _advance_    │
  │                                              refresh_embeddings watermark │
  └───────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
                        PostgreSQL + pgvector
                                      │
       ┌──────────────────────────────┴──────────────────────────────┐
       ▼                                                             ▼
  ┌─ read: aggregates ─────────────┐              ┌─ read: answers ──────────────────┐
  │  GET /stats/*                  │              │  POST /ask                       │
  │  GROUP BY, computed in SQL     │              │  embed ▸ top-k ▸ gate ▸ generate │
  │  no network at all             │              │            │                     │
  └────────────────────────────────┘              │            ▼                     │
                                                  │  OpenAI-compatible LLM  [ext]    │
                                                  └──────────────────────────────────┘
```

## The write path — `paper ingest`

`app/services/ingest.py` drives the loop; each stage is its own module and knows nothing
about its neighbours' concerns. `ArxivClient` knows HTTP and throttling but not Atom;
`parse_feed` knows Atom but not the database; `upsert_papers` knows the database but has
never seen a URL.

### One transaction per page

A page is fetched, parsed, upserted **and embedded** inside a single transaction, then
committed. A crash loses at most the page in flight, never the run — and a paper can never
be committed without its vector, because both writes are in the same transaction.

### Rate limiting, and the 429 that isn't documented

arXiv's Terms of Use ask for one request every 3 seconds and _"a single connection at a
time"_. The client sleeps the remainder of that interval before every request, and logs
**after** the sleep — so the gap between two log lines is the throttle, provable from the
output.

The documented rule is necessary but not sufficient. Verified 14 Sep 2026: a first
_uncached_ query returned **HTTP 429 repeatedly**, while a common cached query returned 200
instantly from the Fastly edge. Neither arXiv's API docs nor its Terms of Use mention 429 or
any penalty mechanism. So the client also treats `429` and `5xx` as retryable with bounded
exponential backoff (`2s, 4s, 8s…`, honouring `Retry-After` when present), and exits
non-zero after `ARXIV_MAX_RETRIES` rather than hanging.

### Deliberately synchronous

There is no concurrency to win. The bottleneck is a deliberate `time.sleep`, and the Terms
of Use forbid parallel connections anyway. Async here would buy zero throughput in exchange
for harder testing and trickier transaction handling. The **API** is async, where the
multi-second LLM call actually makes it pay; the **CLI** is sync. Neither style leaks into
the other.

### Idempotent re-runs

Identity is `arxiv_id` with the version suffix stripped, and it is `UNIQUE`. Re-ingest is an
`INSERT … ON CONFLICT DO UPDATE`, so **duplicate prevention is a database constraint, not a
read-then-write check that can race**. A paper is only rewritten when arXiv's `updated` is
newer than the stored one, so a re-run over unchanged data touches nothing and `RETURNING`
names exactly what moved.

Authors and categories get the same treatment: get-or-create in two statements per page
regardless of row count, never one statement per author.

### The watermark, and why a truncated run refuses to advance it

The feed is sorted by `lastUpdatedDate` descending, so **one high-water mark catches new
_and_ revised papers** — a revision bumps `updated` and floats back to the top. The scan
stops once an entire page predates the mark.

The mark asserts _"everything at or above this is ingested"_. That only holds if the walk
came down far enough to **meet the previous mark**. A run cut short by `--max` can stop
above it, leaving a span that was never fetched — and advancing over that span would bury it
permanently below the next run's stop threshold.

So `_advance_watermark` moves the mark only when this run's oldest paper reaches at or below
the previous mark. Otherwise it logs, leaves the mark where it is, and the run reports
**`[gap left open]`**. Re-run with a larger `--max`, or `--full`.

The same guard covers exceptions: the mark is written only after the loop exits cleanly, so
a crash never records progress that was not made.

> [!NOTE]
> **`--max` rounds up to a whole page.** It is checked _after_ each page commits, and pages
> are 200 — so any value in 1,001–1,200 yields exactly 1,200 papers.

### The watermark is keyed by the category set

`ingest_state.categories_key` is the sorted set, e.g. `cs.AI|cs.CL|cs.LG`. Ingest `cs.AI`,
then re-run adding `cs.LG`: a shared watermark would stop early and silently skip every
older `cs.LG` paper. A key mismatch forces a full scan instead.

### Why `-c` is required

The query is built from `cat:` clauses joined with `+OR+`. With no category there is no
`search_query` to send — **arXiv has no "everything" query**. Omitting it exits 2 with a
copyable example rather than guessing a default.

## The read paths

### `GET /stats/*`

Every aggregate is a `GROUP BY` executed by Postgres over stored rows. Nothing is recomputed
from arXiv, and **all three endpoints answer with the network disconnected**. Responses are
flat lists of labelled points — the shape a charting library consumes directly.

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

A service returns a result object; the router decides what status code that means.
`AnswerResult.degraded` is the clearest case — the service records that generation failed
after retrieval succeeded, and the router turns that into `503` with the sources intact.

Two `Protocol` seams keep the test suite off the network entirely: `Embedder` and
`LLMClient`. The suite fakes both, which is why the whole suite runs with no network, no
LLM and no model download.

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

The container entrypoint runs `alembic upgrade head` before the API starts, so a fresh
database becomes a working one with one command and **no manual migration step**.

The embedding model is then warmed in the lifespan, before the app serves traffic — the cost
is visible in the logs and gated by the healthcheck, rather than landing on whoever sends
the first request.
