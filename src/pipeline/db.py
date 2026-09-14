"""Builds the sync and async engines and hands out sessions."""

from collections.abc import AsyncIterator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from pipeline.config import settings

# PRD §6.9 splits the workload: the ingestion CLI and Alembic are sync, the API is async.
# Both engines derive from the single DATABASE_URL the reviewer sets. Engine construction
# is lazy — no connection is opened here — so importing this module is cheap and safe.
sync_engine = create_engine(settings.sync_database_url, pool_pre_ping=True)
async_engine = create_async_engine(settings.async_database_url, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=sync_engine, class_=Session, expire_on_commit=False)
AsyncSessionLocal = async_sessionmaker(bind=async_engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding one async session per request."""
    async with AsyncSessionLocal() as session:
        yield session
