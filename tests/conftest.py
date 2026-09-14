"""Shared test setup.

`pipeline.config` validates at import, so the environment must be set before any test
module imports it. Real environment variables still win — this only fills the gaps.
"""

import os
from pathlib import Path

# An environment variable would outrank .env, so only fall back when there is no .env
# to respect -- otherwise the suite would silently ignore the developer's own database.
if not Path(".env").exists():
    os.environ.setdefault("DATABASE_URL", "postgresql://pipeline:pipeline@localhost:5432/pipeline")
os.environ.setdefault("LOG_LEVEL", "INFO")

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from pipeline.api.app import app
from pipeline.config import settings
from pipeline.db import get_session

TEST_DB = "pipeline_test"
TABLES = "paper, author, category, paper_author, paper_category, embedding, ingest_state"


def _url_for(database: str) -> str:
    return settings.sync_database_url.rsplit("/", 1)[0] + "/" + database


def _async_url_for(database: str) -> str:
    return settings.async_database_url.rsplit("/", 1)[0] + "/" + database


@pytest.fixture(scope="session")
def test_engine():
    """A throwaway database, built by the real migrations so the schema matches production."""
    admin = create_engine(_url_for("postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)"))
        connection.execute(text(f"CREATE DATABASE {TEST_DB}"))
    admin.dispose()

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", _url_for(TEST_DB))
    command.upgrade(config, "head")

    engine = create_engine(_url_for(TEST_DB))
    yield engine
    engine.dispose()


@pytest.fixture
def session(test_engine) -> Session:
    """One session per test, with every table emptied first."""
    factory = sessionmaker(bind=test_engine, expire_on_commit=False)
    with factory() as session:
        session.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        session.commit()
        yield session


@pytest.fixture
def client(test_engine, request):
    """The API, pointed at the test database.

    NullPool because each TestClient runs the app in its own event loop: a pooled asyncpg
    connection created under one loop and reused under the next is a hang, not an error.

    Parametrize indirectly with a time zone name to prove the queries do not depend on it.
    """
    server_timezone = getattr(request, "param", None)
    connect_args = {"server_settings": {"timezone": server_timezone}} if server_timezone else {}
    engine = create_async_engine(
        _async_url_for(TEST_DB), poolclass=NullPool, connect_args=connect_args
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_session():
        async with factory() as api_session:
            yield api_session

    app.dependency_overrides[get_session] = override_get_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
