# arXiv corpus — ingest, aggregate, ask

A CLI that ingests arXiv papers into PostgreSQL, an HTTP API that serves aggregations
computed in SQL, and (in progress) a `/ask` endpoint that answers questions grounded in
the stored abstracts.

> **Status:** ingestion and the aggregation API are built. The RAG endpoint and the
> container entrypoint are the remaining work.

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
| `category` | string | all | Restrict to one category, e.g. `cs.LG`. |
| `from`, `to` | `YYYY-MM-DD` | unbounded | **Inclusive** of both days, resolved in UTC. |

```console
$ curl -s 'localhost:8000/stats/papers-over-time?granularity=year&category=cs.LG'
[{"period": "2019", "category": "cs.LG", "count": 16},
 {"period": "2020", "category": "cs.LG", "count": 5}]
```

### `GET /stats/top-categories`

```console
$ curl -s 'localhost:8000/stats/top-categories?n=5'
[{"category": "cs.AI", "count": 50}, {"category": "cs.LG", "count": 21},
 {"category": "stat.ML", "count": 16}, {"category": "cs.CV", "count": 13},
 {"category": "cs.CL", "count": 8}]
```

### `GET /stats/top-authors`

```console
$ curl -s 'localhost:8000/stats/top-authors?n=3'
[{"author": "Vahid Behzadan", "count": 3}, {"author": "William Hsu", "count": 3},
 {"author": "Abhishek Sharma", "count": 1}]
```

`n` defaults to 10 and is bounded to 1–100 on both top-N endpoints.

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
