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
    relevance_threshold: float = Field(default=0.71, ge=0.0, le=1.0)
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


# RELEVANCE_THRESHOLD, measured -- not guessed. Reproduce with:
#     uv run python scripts/calibrate_threshold.py
#
# 23 questions, three groups, top-1 cosine similarity (15 Sep 2026, 1,200 papers):
#     in-corpus, must answer (12)           0.745 .. 0.868
#     adjacent CS, must refuse (5)          0.649 .. 0.799   <- decides the number
#     off-topic, must refuse (6)            0.471 .. 0.583
#
# The middle group OVERLAPS the first, so no threshold separates them cleanly. The 0.799 is
# "what guarantees does Raft provide?" matching "Algorithmic stability via ensembling":
# bge encodes what a text is *about*, not whether it contains an answer, so any question
# sharing ML vocabulary scores high. 0.71 removes the cheap failures at zero token cost --
# all six off-topic and four of five adjacent -- and the grounded prompt is the second gate
# for the residue. Raising it past 0.799 would refuse 8 of the 12 answerable questions.
#
# Every value in (0.687, 0.745] behaves identically on these 23 questions, so the choice is
# only about which side gets the margin. Too low costs one LLM call that gate 2 usually
# catches; too high refuses a real question with no second gate behind it. So the margin
# leans low: 0.71 leaves 0.035 under the answerable floor, against 0.023 over the adjacent
# ceiling. The corpus moves on every ingest; that headroom is what absorbs the drift.
#
# Re-run the script if the corpus or the embedding model changes.

settings = Settings()
