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
    arxiv_user_agent: str = "pipeline-arxiv-assessment/0.1"
    arxiv_watermark_margin_days: int = Field(default=1, ge=0)  # covers page-boundary drift

    @property
    def sync_database_url(self) -> str:
        """For the ingestion CLI and Alembic (PRD §6.9)."""
        return _with_driver(str(self.database_url), _SYNC_DRIVER)

    @property
    def async_database_url(self) -> str:
        """For the API (PRD §6.9)."""
        return _with_driver(str(self.database_url), _ASYNC_DRIVER)


settings = Settings()
