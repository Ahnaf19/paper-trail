"""Typed settings, read from the environment and validated at import."""

from typing import Final, Literal

from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict

_SYNC_DRIVER: Final = "psycopg"
_ASYNC_DRIVER: Final = "asyncpg"

LogLevel = Literal["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"]


def _with_driver(url: str, driver: str) -> str:
    """Swap in an explicit SQLAlchemy driver, replacing any already present."""
    scheme, separator, rest = url.partition("://")
    if not separator:
        raise ValueError(f"DATABASE_URL is not a URL: {url!r}")
    dialect = scheme.split("+", 1)[0]
    if dialect == "postgres":  # some providers still hand out the short spelling
        dialect = "postgresql"
    return f"{dialect}+{driver}://{rest}"


class Settings(BaseSettings):
    """Instantiated once below, so a missing variable fails the process on import."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: PostgresDsn
    log_level: LogLevel = "INFO"

    # arXiv. Limits and their sources are in PRD §0; these are the tunables.
    arxiv_base_url: str = "https://export.arxiv.org/api/query"
    arxiv_page_size: int = Field(default=200, ge=1, le=2000)  # 2000 is arXiv's documented cap
    arxiv_min_request_interval: float = Field(default=3.0, ge=3.0)  # their floor, not ours to lower
    arxiv_max_retries: int = Field(default=5, ge=0)
    arxiv_timeout_seconds: float = Field(default=60.0, gt=0)
    arxiv_user_agent: str = "paper-trail/0.1"
    arxiv_watermark_margin_days: int = Field(default=1, ge=0)  # covers page-boundary drift

    # Embeddings (PRD §6.2). The dimension is pinned in the schema, not here.
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_batch_size: int = Field(default=64, ge=1)
    # Where the model files live. Left unset fastembed picks $TMPDIR/fastembed_cache,
    # which a tmpfs mount would wipe -- so the image sets this to a real directory.
    embedding_cache_dir: str | None = None

    # Retrieval (PRD §6.10). The threshold is calibrated -- see the note below.
    retrieval_top_k: int = Field(default=5, ge=1, le=20)  # measured, see the note below
    relevance_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    max_question_chars: int = Field(default=1000, ge=1)
    max_context_chars: int = Field(default=12000, ge=1000)  # ~3k tokens, well inside any model

    # LLM (PRD §6.3). Any OpenAI-compatible endpoint; Groq by default because it is free.
    # gpt-oss-20b is a *production* Groq model on 15 Sep 2026 -- the llama ids are Enterprise.
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "openai/gpt-oss-20b"
    llm_api_key: str | None = None  # absent => /ask degrades to retrieval + sources
    llm_timeout_seconds: float = Field(default=30.0, gt=0)

    @property
    def sync_database_url(self) -> str:
        """For the ingestion CLI and Alembic (PRD §6.9)."""
        return _with_driver(str(self.database_url), _SYNC_DRIVER)

    @property
    def async_database_url(self) -> str:
        """For the API (PRD §6.9)."""
        return _with_driver(str(self.database_url), _ASYNC_DRIVER)


# RELEVANCE_THRESHOLD and RETRIEVAL_TOP_K, measured -- not guessed. Reproduce with:
#     uv run python scripts/evaluate.py
#
# Against eval/golden.jsonl -- 97 generated positives and 24 hand-written must-refuse
# questions, on the 2,370-paper corpus of 16 Sep 2026:
#
#       t      correct   false-pass   adjacent CS
#     0.70      92/97       4/24         4/14
#     0.71      92/97       3/24         3/14      <- the previous value
#     0.75      92/97       2/24         2/14      <- here
#     0.78      91/97       2/24         2/14
#     0.80      87/97       0/24         0/14
#
# Retrieval puts the gold paper inside the top-5 for 92 of those 97, so 92 is a CEILING no
# threshold can beat: a gate cannot repair retrieval, only avoid discarding it. Everything
# from 0.70 to 0.76 sits at that ceiling, so the positives cannot separate them and the
# negatives decide -- 0.75 is the lowest value reaching the fewest false passes. It drops
# "How does a bloom filter trade memory for false positives?" (0.7413), which had been
# matching a paper on learned request routing.
#
# Clearing the last two false passes costs five real answers, so it is not worth doing. They
# are "What guarantees does the Raft consensus algorithm provide?" (0.7992, matching
# "Algorithmic stability via ensembling") and "What problem does consistent hashing solve in
# a distributed cache?" (0.7962, matching "Shared KV Caching for Replicated 27B Inference").
# bge encodes what a text is *about*, not whether it contains an answer, so this overlap is
# structural -- the grounded prompt is the second gate for it, and a cross-encoder re-ranker
# would be the real fix.
#
# RETRIEVAL_TOP_K stays at 5: k=10 raises the ceiling to 94/97 but grows the context 34%
# (7,042 -> 9,418 chars), and MAX_CONTEXT_CHARS then trims two thirds of questions, so the
# tenth paper often never reaches the model. Recorded in eval/results/002-topk-10.json.
#
# Re-run the script if the corpus or the embedding model changes.

settings = Settings()
