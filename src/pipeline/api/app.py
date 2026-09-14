"""Builds the FastAPI application and serves the health check."""

from typing import Annotated

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse
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

# A refused connection reaches us as a bare OSError: asyncpg raises it before SQLAlchemy
# has a DBAPI error to wrap, so catching SQLAlchemyError alone misses the likeliest fault.
_DB_UNAVAILABLE = (SQLAlchemyError, OSError)


async def _database_unavailable(request: Request, exc: Exception) -> JSONResponse:
    """503, not 500: the database being down is an infrastructure state, not a bug here."""
    logger.error("database unavailable serving {}: {}", request.url.path, exc)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "database unavailable"},  # never the driver's text
    )


def create_app() -> FastAPI:
    """One app per process. Routers are mounted here, never imported for side effects."""
    configure_logging()
    application = FastAPI(title="arXiv corpus API", version="0.1.0", description=_DESCRIPTION)

    @application.get("/health", tags=["ops"])
    async def health(session: Annotated[AsyncSession, Depends(get_session)]) -> dict[str, str]:
        """Healthy means "can serve", and serving needs the database — so it is queried."""
        await session.execute(text("SELECT 1"))
        return {"status": "ok"}

    application.include_router(analytics_router)
    for failure in _DB_UNAVAILABLE:
        application.add_exception_handler(failure, _database_unavailable)
    return application


app = create_app()
