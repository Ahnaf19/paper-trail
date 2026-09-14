"""Builds the FastAPI application and serves the health check."""

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from loguru import logger
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from pipeline.api.analytics import router as analytics_router
from pipeline.db import get_session
from pipeline.logging import configure_logging

_DESCRIPTION = """
Aggregations over a corpus of arXiv papers, computed in SQL, plus a grounded question
answering endpoint over their abstracts. Papers are loaded by the `pipeline ingest` CLI.
"""


def create_app() -> FastAPI:
    """One app per process. Routers are mounted here, never imported for side effects."""
    configure_logging()
    application = FastAPI(title="arXiv corpus API", version="0.1.0", description=_DESCRIPTION)

    @application.get("/health", tags=["ops"])
    async def health(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, str]:
        """Healthy means "can serve", and serving needs the database — so it is checked."""
        try:
            await session.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            logger.error("health check failed: {}", exc)
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable"
            ) from exc
        return {"status": "ok"}

    application.include_router(analytics_router)
    return application


app = create_app()
