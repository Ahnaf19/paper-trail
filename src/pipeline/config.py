"""Typed application settings, read from the environment and validated at import."""

from typing import Final, Literal

from pydantic import PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict

# PRD §6.9 splits the workload: the API is async, the CLI and Alembic are sync.
# Each needs its own SQLAlchemy driver, but the reviewer should only ever set one URL.
_SYNC_DRIVER: Final = "psycopg"
_ASYNC_DRIVER: Final = "asyncpg"

LogLevel = Literal["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"]


def _with_driver(url: str, driver: str) -> str:
    """Rewrite a Postgres URL to use an explicit SQLAlchemy driver.

    Replaces any driver already present rather than stacking onto it, and accepts the
    `postgres://` spelling that some hosting providers still hand out.
    """
    scheme, separator, rest = url.partition("://")
    if not separator:
        raise ValueError(f"DATABASE_URL is not a URL: {url!r}")
    dialect = scheme.split("+", 1)[0]
    if dialect == "postgres":
        dialect = "postgresql"
    return f"{dialect}+{driver}://{rest}"


class Settings(BaseSettings):
    """Every value the application needs, sourced from the environment (NFR-7).

    Instantiated once at import. A missing or malformed variable therefore fails the
    process immediately with a message naming it, rather than surfacing as an
    AttributeError on the first request.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: PostgresDsn
    log_level: LogLevel = "INFO"

    @property
    def sync_database_url(self) -> str:
        """Driver URL for the ingestion CLI and Alembic."""
        return _with_driver(str(self.database_url), _SYNC_DRIVER)

    @property
    def async_database_url(self) -> str:
        """Driver URL for the API."""
        return _with_driver(str(self.database_url), _ASYNC_DRIVER)


settings = Settings()
