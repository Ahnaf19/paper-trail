"""Sync and async engines, both derived from one DATABASE_URL (PRD §6.9)."""

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

# Construction is lazy — importing this module opens no connection.
sync_engine = create_engine(settings.sync_database_url, pool_pre_ping=True)
async_engine = create_async_engine(settings.async_database_url, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=sync_engine, class_=Session, expire_on_commit=False)
AsyncSessionLocal = async_sessionmaker(bind=async_engine, expire_on_commit=False)
