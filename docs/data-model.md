# Data model

[← README](../README.md) · siblings: [architecture](architecture.md) · [RAG](rag.md) · [API](api.md) · [evaluation](evaluation.md)

Nine tables. Papers, authors and categories are separate entities joined by link tables;
vectors live beside them in the same Postgres. The declarative models in `app/models/` are
the source of truth, and every migration in `alembic/versions/` is generated from them.

## How the tables relate

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

## Schema

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

`doi` and `journal_ref` are nullable because arXiv omits them for most preprints — most
papers have neither, and a schema that required them would reject the common case.

`position` on `paper_author` preserves author order, which is meaningful in this domain:
first author and last author are not interchangeable with the middle of the list.

## Four decisions worth naming

### `arxiv_id` carries no version suffix and is unique

arXiv serves `2509.01234v3`; the paper is `2509.01234`. Stripping the version and making the
result `UNIQUE` turns idempotent re-ingest into a **database constraint rather than
application logic** — an `ON CONFLICT DO UPDATE` instead of a read-then-write check that can
race two runs against each other.

Keeping the version would have been the subtle bug: every revision would arrive as a new
row, the corpus would grow on every re-run, and `top-authors` would count the same paper
repeatedly.

### `content_hash` + `model` decide re-embedding

The hash is over `title + "\n" + abstract` — exactly the text that gets embedded, nothing
else. Two consequences fall out of that, both wanted:

- **A metadata-only revision costs nothing.** arXiv bumps `updated` when a DOI or a journal
  reference is added. The paper row is refreshed; the hash is unchanged; no vector is
  recomputed.
- **Swapping the embedding model invalidates every row through the same field.** `model` is
  part of the staleness check, so changing `EMBEDDING_MODEL` makes every stored vector stale
  automatically — no migration, no separate reconciliation step.

The hash is a change detector and never a retrieval artifact: similarity search does not
read it.

### The watermark is keyed by the category set

Ingest `cs.AI`, then re-run adding `cs.LG`. A single shared watermark would stop the new run
early — the `cs.LG` papers older than the mark look "already done" — and **silently skip
every one of them**. Keying on the sorted set means a changed category list is a different
key, no mark is found, and the run does a full scan instead.

Full reasoning on how the mark advances, and why a truncated run refuses to move it:
**[docs/architecture.md](architecture.md)**.

### Embeddings are written in the paper's transaction

The vector is inserted in the same transaction as the paper it belongs to. A crash between
the two **is not handled — it is impossible.**

That is the whole reason for pgvector rather than a separate vector store. A dedicated
vector database would mean two systems, two writes, and the dual-write problem: a paper
committed to Postgres whose vector never reached the other store, discoverable only when a
question fails to retrieve it. One transaction removes the failure mode instead of
mitigating it. The corpus is thousands of vectors, not billions — there is no scale argument
pulling the other way.

## No indexes beyond the primary and unique keys

Measured, not assumed — warm medians of nine calls through the service functions themselves,
on the 2,370-paper corpus. Every plan is a sequential scan:

| Query | Median |
| --- | --- |
| `top-categories?n=10` | 4.5 ms |
| `top-authors?n=10` | 12.9 ms |
| `papers-over-time` (month, all) | 7.8 ms |
| `/ask` retrieval — top-5 of 2,370 vectors | 2.8 ms |

**Aggregations gain nothing from an index.** An index buys selectivity; a `GROUP BY` over
the whole corpus has none — Postgres reads every row regardless of what indexes exist.

**Vector search genuinely would be faster.** HNSW over the same 2,370 vectors
(`EXPLAIN ANALYZE`, warm median of seven):

| | Exact (today) | HNSW |
| --- | --- | --- |
| Execution | 1.18 ms | **0.14 ms** |
| Result | exact | **approximate** |
| Build | — | 410 ms |
| Index size | — | 4752 kB, on a 4760 kB table |

8.7× faster — and not worth it, because of where that millisecond sits:

```
  /ask   ├ embed ┤├ retrieve 2.8 ms ┤├──────────── LLM 1–3 s ────────────┤
                  └ HNSW saves 1.0 ms of this → under 0.1% of the request
```

So the trade is: **under 0.1% of end-to-end latency, bought with an approximate result and
an index the size of the table.** Worth revisiting around 100k vectors, where the exact scan
stops being free and the index stops being proportionally enormous. Not before.

## Migrations

Alembic, generated from `app/models` and read before committing — autogenerate reliably
misses server defaults, constraint changes and custom column types, the `vector` column
included.

The first migration runs `CREATE EXTENSION IF NOT EXISTS vector`, so a **fresh empty
Postgres goes to a working schema with one command**. `MetaData` carries a
`naming_convention`, so constraint and index names are deterministic rather than
autogenerated noise.

The container entrypoint runs `alembic upgrade head` before the API starts. The test suite
builds its throwaway `arxiv_test` database from the same migrations — **the schema under
test is the schema that ships.**
