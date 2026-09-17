# The brief, stated as requirements

[← README](../README.md) · siblings: [architecture](architecture.md) · [data model](data-model.md) · [RAG](rag.md) · [API](api.md) · [evaluation](evaluation.md)

What the assessment asked for, numbered so each line is individually quotable.
**FR** — functional requirement · **NFR** — non-functional requirement.

The system splits into five components, plus the requirements that cut across all of them:

- **C1 — Ingestion CLI** · fetch, paginate, throttle, re-run safely
- **C2 — Preprocessing** · parse the Atom feed into clean records
- **C3 — Storage** · relational schema, join tables, migrations
- **C4 — Aggregation API** · chart-ready counts, computed in SQL
- **C5 — RAG API** · embed, retrieve, answer or refuse
- **Non-functional** · how it runs, how it is tested, how it reads

## C1 — Ingestion CLI

|             |                                                                                                                                                                     |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **FR-1.1**  | A runnable CLI command fetches papers from the arXiv API for one or more categories. **Not a notebook.** ≥1,000 papers stored on an empty database                  |
| **FR-1.2**  | Paginates using `start` / `max_results` until the requested count is reached                                                                                        |
| **FR-1.3**  | Self-throttles to **≤1 request per 3 seconds**                                                                                                                      |
| **FR-1.4**  | **Re-running is idempotent.** A second identical run creates zero duplicate rows. Identity of a paper is its `arxiv_id`                                             |
| **FR-1.5**  | **Partial failure is safe.** A run that dies mid-way leaves the database consistent; re-running continues or repairs it                                             |
| **FR-1.5a** | **Transient upstream failures are retried, not fatal.** 429 and 5xx trigger bounded exponential backoff; exhausting retries exits non-zero with the database intact |
| **FR-1.6**  | **Incremental update.** A re-run picks up new papers _and_ papers whose arXiv `updated` is newer than the stored one, and refreshes those rows                      |
| **FR-1.7**  | **The RAG index follows the data.** New papers get embeddings; papers whose abstract changed are re-embedded. No stale vectors                                      |
| **FR-1.8**  | A **wipe/reset** command clears papers, authors, categories **and embeddings**, so a fresh import can run from scratch                                              |

## C2 — Preprocessing

|            |                                                                                                                          |
| ---------- | ------------------------------------------------------------------------------------------------------------------------ |
| **FR-2.1** | Parse the Atom/XML response into clean records                                                                           |
| **FR-2.2** | Normalize `published` and `updated` into proper timestamps, not strings                                                  |
| **FR-2.3** | Handle **missing `doi` / `journal_ref`** without crashing — store as null                                                |
| **FR-2.4** | **Flatten nested fields:** a paper has many authors and many categories; `primary_category` is kept as its own attribute |
| **FR-2.5** | Light cleanup on the abstract — collapse newlines and ragged whitespace                                                  |

## C3 — Storage

|            |                                                                               |
| ---------- | ----------------------------------------------------------------------------- |
| **FR-3.1** | Relational database, schema of our own design                                 |
| **FR-3.2** | **Papers, authors and categories are separate tables**, joined by link tables |
| **FR-3.3** | Embedding vectors are persisted and associated back to their paper            |
| **FR-3.4** | **Migrations** reproduce the whole schema from an empty database              |

## C4 — Aggregation API

|            |                                                                                                                                                 |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| **FR-4.1** | 🔴 **Aggregates are computed from the database in SQL** — never by re-fetching or re-parsing arXiv. The API works with the network disconnected |
| **FR-4.2** | **At least 3 aggregations:** paper count by category over time, top N categories by paper count, and top N authors by paper count               |
| **FR-4.3** | Sensible query params — at minimum `n` for top-N, and granularity plus date range for the time series                                           |
| **FR-4.4** | Returns JSON in a shape a charting library consumes directly: a flat list of labelled points                                                    |

## C5 — RAG API

|            |                                                                                                                                                                                           |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **FR-5.1** | Embeddings generated from the abstract, optionally with the title concatenated                                                                                                            |
| **FR-5.2** | **`POST /ask`** — request `{"question": "..."}` → response `{"answer": "...", "sources": [...]}`                                                                                          |
| **FR-5.3** | **`sources` identifies which papers grounded the answer** — every entry resolves to a real stored paper                                                                                   |
| **FR-5.4** | On a query: embed it, retrieve the top-k most similar papers, pass them to the LLM as context                                                                                             |
| **FR-5.5** | 🔴 **No-good-match is handled honestly.** If nothing clears the relevance threshold the API says it does not have the information, does not invent an answer, and returns empty `sources` |
| **FR-5.6** | The answer is grounded in the retrieved context, not in the model's own knowledge                                                                                                         |

## Non-functional

|           |                                                                                                                                                                                                          |
| --------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **NFR-1** | 🔴 **`docker compose up` brings up the database and the API with no manual steps beyond setting `.env`**                                                                                                 |
| **NFR-2** | **README** covers setup, env vars, running ingestion, starting the API, sample curl requests and responses for both APIs, schema and architecture, known limitations, and what to improve with more time |
| **NFR-3** | Reasonable **error handling and logging** — failures logged with context, proper status codes rather than stack traces                                                                                   |
| **NFR-4** | **Meaningful automated tests** — parsing, idempotent upsert, an aggregation query, the no-match path. Not coverage theatre                                                                               |
| **NFR-5** | 🔴 **Commit history shows incremental progress**, not a single commit                                                                                                                                    |
| **NFR-6** | **`.env.example`** lists every required variable. No real secrets committed                                                                                                                              |
| **NFR-7** | All configuration via environment variables — no hardcoded hosts, keys or paths                                                                                                                          |
| **NFR-8** | Code readable, typed and layered well enough that any file can be explained in a sentence                                                                                                                |

## Explicitly out of scope

The brief cares _"less about a polished UI and more about how you structure data, design
APIs, and reason about an LLM-backed system end to end."_

- **No frontend, no charts** — the aggregation endpoints return chart-ready JSON and stop.
- **No authentication or multi-tenancy.**
- **No deployment beyond `docker compose`.**
- **No full-text paper retrieval** — not a choice: the arXiv API returns abstracts only.
