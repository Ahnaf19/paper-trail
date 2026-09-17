# API reference

[← README](../README.md) · siblings: [architecture](architecture.md) · [data model](data-model.md) · [RAG](rag.md) · [evaluation](evaluation.md)

Base URL `http://localhost:8000` · JSON in, JSON out · no authentication.
Interactive docs (OpenAPI) at **`/docs`** while the service is running.

| | |
|---|---|
| [`GET /health`](#get-health) | Liveness — including the database |
| [`GET /stats/top-categories`](#get-statstop-categories) | Top N categories by paper count |
| [`GET /stats/top-authors`](#get-statstop-authors) | Top N authors by paper count |
| [`GET /stats/papers-over-time`](#get-statspapers-over-time) | Paper counts per period and category |
| [`POST /ask`](#post-ask) | Grounded answer from the stored abstracts |

## Conventions

- **Aggregates are computed in SQL from stored data.** No endpoint contacts arXiv; all of
  them answer with the network disconnected.
- **Responses are flat lists of labelled points** — the shape a charting library takes
  directly, with no client-side reshaping.
- **Errors** are always `{"detail": "<message>"}`. No stack traces, no driver text.
- **Times are UTC.** Period buckets are anchored to UTC so the same corpus buckets
  identically for every caller.
- **Counts are distinct papers.** A paper in three categories counts once in each of the
  three buckets, never three times in one.
- **Ties break by name/code**, so an identical request returns an identical order.
- **An empty corpus returns `[]` with `200`.** An empty chart is a valid chart.

---

## `GET /health`

Liveness for the container healthcheck. Queries the database, so "ok" means *can serve*,
not *process is alive*.

```console
$ curl -s localhost:8000/health
{"status": "ok"}
```

| Status | Body |
|---|---|
| `200` | `{"status": "ok"}` |
| `503` | `{"detail": "database unavailable"}` |

---

## `GET /stats/top-categories`

Categories ranked by how many distinct papers carry them.

**Query parameters**

| Name | Type | Range | Default | Notes |
|---|---|---|---|---|
| `n` | integer | `1`–`100` | `10` | Out of range is a `422`. |

**Response** — `[{ category, count }]`

| Field | Type | Meaning |
|---|---|---|
| `category` | string | arXiv category code, e.g. `cs.LG` |
| `count` | integer | Distinct papers in that category |

```console
$ curl -s 'localhost:8000/stats/top-categories?n=5'
[{"category": "cs.AI", "count": 622}, {"category": "cs.LG", "count": 606},
 {"category": "cs.CL", "count": 341}, {"category": "cs.CV", "count": 126},
 {"category": "stat.ML", "count": 86}]
```

**Reading it:** the counts sum to more than the corpus size because papers carry several
categories. Call it with `n=100` to see every category actually loaded — useful before
filtering `papers-over-time`.

---

## `GET /stats/top-authors`

Authors ranked by distinct papers.

**Query parameters**

| Name | Type | Range | Default | Notes |
|---|---|---|---|---|
| `n` | integer | `1`–`100` | `10` | Out of range is a `422`. |

**Response** — `[{ author, count }]`

```console
$ curl -s 'localhost:8000/stats/top-authors?n=3'
[{"author": "Philip Torr", "count": 5}, {"author": "Zihan Wang", "count": 5},
 {"author": "Jian Li", "count": 4}]
```

**Reading it:** arXiv publishes no author IDs, so the **name string is the identity** — two
researchers who share a name are one row. Stated rather than hidden; fixing it properly needs
disambiguation this corpus cannot support.

---

## `GET /stats/papers-over-time`

Paper counts per time bucket and category, keyed on **`published`** — when a paper first
appeared, not `updated`, which moves whenever an author fixes a typo.

**Query parameters**

| Name | Type | Accepted values | Default | Notes |
|---|---|---|---|---|
| `granularity` | enum | `month` \| `year` | `month` | Anything else is a `422`. |
| `category` | string | any code the corpus holds | *all categories* | Unknown code is a `404`, not `[]`. |
| `from` | date | `YYYY-MM-DD` | unbounded | **Inclusive** of that day. |
| `to` | date | `YYYY-MM-DD` | unbounded | **Inclusive** of that day. |

`from` later than `to` is a `422`.

**Response** — `[{ period, category, count }]`

| Field | Type | Meaning |
|---|---|---|
| `period` | string | `YYYY-MM` for month, `YYYY` for year |
| `category` | string | arXiv category code |
| `count` | integer | Distinct papers published in that bucket |

**Default — every category, by month**

```console
$ curl -s 'localhost:8000/stats/papers-over-time'
[{"period": "2019-12", "category": "cs.AI", "count": 1},
 {"period": "2019-12", "category": "cs.CY", "count": 1},
 {"period": "2020-01", "category": "cs.LG", "count": 1}, ...]      # 411 rows
```

**One category, by year**

```console
$ curl -s 'localhost:8000/stats/papers-over-time?granularity=year&category=cs.CL'
[{"period": "2023", "category": "cs.CL", "count": 1},
 {"period": "2024", "category": "cs.CL", "count": 4},
 {"period": "2025", "category": "cs.CL", "count": 17},
 {"period": "2026", "category": "cs.CL", "count": 319}]
```

**Bounded window** — both ends inclusive

```console
$ curl -s 'localhost:8000/stats/papers-over-time?from=2026-01-01&to=2026-01-31'
[{"period": "2026-01", "category": "cs.AI", "count": 7},
 {"period": "2026-01", "category": "cs.CL", "count": 6},
 {"period": "2026-01", "category": "cs.CV", "count": 2}, ...]
```

**Unknown category**

```console
$ curl -s 'localhost:8000/stats/papers-over-time?category=cs.Al'   # capital i, not L
{"detail": "no category 'cs.Al' in this corpus — see GET /stats/top-categories for what is loaded"}
```

**Reading it:**

- **Periods with no papers are omitted, not zero-filled.** A fabricated zero row would
  assert something about a gap in the data; a chart library handles a missing point.
- **A `404` means the code is not in the corpus; `[]` means the window is empty.** Keeping
  those distinct is why unknown categories are not silently empty. Valid codes come from
  [arXiv's taxonomy](https://arxiv.org/category_taxonomy); the loaded ones from
  `/stats/top-categories`.
- **No pagination.** Row count is bounded by the corpus — one row per period per category —
  not by anything a caller can send.

---

## `POST /ask`

Embeds the question, retrieves the nearest abstracts by cosine similarity, and answers from
those alone. Design and threshold calibration are in the [README](../README.md#where-the-threshold-comes-from).

**Request body**

| Field | Type | Constraints | Notes |
|---|---|---|---|
| `question` | string | 1–1000 chars **after stripping** | `""` and `"   "` are both `422`. |

**Response** — `{ answer, sources }`

| Field | Type | Meaning |
|---|---|---|
| `answer` | string | Grounded answer, or the refusal sentence |
| `sources` | array | Papers **passed to the model as context** — never papers that merely ranked |
| `sources[].arxiv_id` | string | Version-stripped id, e.g. `2609.12655` |
| `sources[].title` | string | Paper title |
| `sources[].score` | float | Cosine similarity to the question, 4 dp |

**Answered**

```console
$ curl -s localhost:8000/ask -H 'content-type: application/json' \
    -d '{"question": "How can LLM agents reuse experience from earlier tasks?"}'
{
  "answer": "LLM agents can reuse past experience by extracting reusable skills and
             trajectories from their own interaction logs, and by pre-building reusable
             artifacts during a study phase. In LifeMem (2609.12655) the agent clusters
             accumulated trajectories by workflow, distils reusable skills, and recalls
             them when solving a new task...",
  "sources": [
    {"arxiv_id": "2609.12655", "title": "LifeMem: Enabling Lifelong Experience Reuse for LLM Agents", "score": 0.8410},
    {"arxiv_id": "2609.04280", "title": "EvoHarnessBench: Can Your Agents Keep Pace with an Evolving Harness?", "score": 0.7903},
    {"arxiv_id": "2605.28108", "title": "Ask Now, Use Later: Benchmarking the Proactivity Gap in Long-Lived LLM Agents", "score": 0.7878},
    {"arxiv_id": "2603.19896", "title": "Utility-Guided Agent Orchestration for Efficient LLM Tool Use", "score": 0.7873},
    {"arxiv_id": "2609.10824", "title": "Studying Without a Syllabus: Task-Agnostic Environment Preprocessing", "score": 0.7860}
  ]
}
```

**Refused** — still `200`; a refusal is an answer, not an error

```console
$ curl -s localhost:8000/ask -H 'content-type: application/json' \
    -d '{"question": "What is the capital of France?"}'
{"answer": "I don't have information about that in the paper dataset.", "sources": []}
```

**Degraded** — `503`, retrieval preserved

```console
$ curl -s localhost:8000/ask -H 'content-type: application/json' \
    -d '{"question": "How can LLM agents reuse experience?"}'     # with no LLM_API_KEY
{"answer": "Could not generate an answer — no API key configured. The most relevant
            papers are listed in sources.",
 "sources": [ ... the same 5 papers, with scores ... ]}
```

**Reading it:**

- **`sources: []` with a refusal means the model was never called.** The relevance check runs
  before generation, so an off-topic question costs zero tokens and cannot hallucinate.
- **Every id in `sources` resolves to a stored paper.** They are rows from the retrieval
  join, not ids looked up afterwards.
- **`sources` on a `503` is still useful** — those are the most relevant papers, and the
  answer text names why generation failed.
- Answers come from **abstracts only**. arXiv's API never returns full text, so
  *"what learning rate did they use?"* is outside what this can be right about.

---

## Status codes

| Code | Meaning | Where |
|---|---|---|
| `200` | Success — **including an honest refusal from `/ask`** | all |
| `404` | `category` is not in the corpus | `papers-over-time` |
| `422` | Bad parameter: `n` out of range · unknown `granularity` · `from` > `to` · empty or over-long `question` | all |
| `503` | Database unreachable (`{"detail": "database unavailable"}`), or the LLM is unreachable (`sources` preserved) | all · `/ask` |
