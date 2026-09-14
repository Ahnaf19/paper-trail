# arXiv corpus — ingest, aggregate, ask

A CLI that ingests arXiv papers into PostgreSQL, an HTTP API that serves aggregations
computed in SQL, and (in progress) a `/ask` endpoint that answers questions grounded in
the stored abstracts.

> **Status:** ingestion, the aggregation API and `/ask` are built and running over a
> 1,200-paper corpus (`cs.AI`, `cs.CL`, `cs.LG`). The container entrypoint is the remaining work.

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
$ curl -s 'localhost:8000/stats/papers-over-time?granularity=year&category=cs.CL'
[{"period": "2023", "category": "cs.CL", "count": 1},
 {"period": "2024", "category": "cs.CL", "count": 4},
 {"period": "2025", "category": "cs.CL", "count": 17},
 {"period": "2026", "category": "cs.CL", "count": 319}]
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
- **The corpus is a snapshot.** `pipeline ingest` re-run picks up new and revised papers via
  the watermark, but nothing schedules it.
