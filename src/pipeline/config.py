"""Typed settings, read from the environment and validated at import."""

from typing import Final, Literal

from pydantic import PostgresDsn
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

    @property
    def sync_database_url(self) -> str:
        """For the ingestion CLI and Alembic (PRD §6.9)."""
        return _with_driver(str(self.database_url), _SYNC_DRIVER)

    @property
    def async_database_url(self) -> str:
        """For the API (PRD §6.9)."""
        return _with_driver(str(self.database_url), _ASYNC_DRIVER)


settings = Settings()
