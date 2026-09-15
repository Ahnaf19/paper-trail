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
    retrieval_top_k: int = Field(default=5, ge=1, le=20)
    relevance_threshold: float = Field(default=0.73, ge=0.0, le=1.0)
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


# RELEVANCE_THRESHOLD, calibrated 15 Sep 2026 against the 1,200-paper corpus -- not guessed.
#
# 23 questions, three groups, top-1 cosine similarity:
#     in-corpus (12)                    0.765 .. 0.872
#     adjacent CS, absent from corpus (5)  0.639 .. 0.706   <- the group that decides it
#     off-topic (6)                     0.471 .. 0.583
#
# Separable window is (0.706, 0.765]; 0.73 sits near its midpoint. The middle group is the
# point: bge scores unrelated computer science at 0.64-0.71, so a plausible 0.5 would answer
# "what is the CAP theorem?" out of AI abstracts. Separating arXiv from baking is trivial and
# proves nothing. Re-calibrate if the corpus or the embedding model changes.

settings = Settings()
