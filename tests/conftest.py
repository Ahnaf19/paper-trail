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
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from pipeline.config import settings

TEST_DB = "pipeline_test"
TABLES = "paper, author, category, paper_author, paper_category, ingest_state"


def _url_for(database: str) -> str:
    return settings.sync_database_url.rsplit("/", 1)[0] + "/" + database


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
