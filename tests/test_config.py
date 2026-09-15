"""Settings carry one piece of real logic — deriving both driver URLs from one
DATABASE_URL — so that is what these tests cover."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings

DSN = "postgresql://user:pw@db:5432/pipeline"


def test_sync_url_uses_psycopg():
    assert Settings(database_url=DSN).sync_database_url.startswith("postgresql+psycopg://")


def test_async_url_uses_asyncpg():
    assert Settings(database_url=DSN).async_database_url.startswith("postgresql+asyncpg://")


def test_both_urls_keep_credentials_host_and_database():
    settings = Settings(database_url=DSN)
    for url in (settings.sync_database_url, settings.async_database_url):
        assert url.endswith("@db:5432/pipeline")
        assert "user:pw" in url


def test_existing_driver_is_replaced_not_stacked():
    settings = Settings(database_url="postgresql+asyncpg://user:pw@db:5432/pipeline")
    assert settings.sync_database_url.startswith("postgresql+psycopg://")
    assert "asyncpg" not in settings.sync_database_url


def test_postgres_scheme_is_normalised_to_postgresql():
    """Some providers hand out `postgres://`, which SQLAlchemy 2.0 does not accept."""
    settings = Settings(database_url="postgres://user:pw@db:5432/pipeline")
    assert settings.sync_database_url == "postgresql+psycopg://user:pw@db:5432/pipeline"
    assert settings.async_database_url == "postgresql+asyncpg://user:pw@db:5432/pipeline"


def test_invalid_log_level_is_rejected_at_construction():
    with pytest.raises(ValidationError):
        Settings(database_url=DSN, log_level="LOUD")


def test_malformed_database_url_is_rejected_at_construction():
    with pytest.raises(ValidationError):
        Settings(database_url="not-a-url")
