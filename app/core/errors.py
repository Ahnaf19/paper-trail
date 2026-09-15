"""Domain failures and the handlers that turn them into responses.

One place to answer "what can go wrong, and what does the client see?" — the alternative is
that answer living in five routers and drifting apart.
"""

from fastapi import Request, status
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError


class ArxivUnavailableError(RuntimeError):
    """arXiv could not be reached within the configured retries."""


class LLMUnavailableError(RuntimeError):
    """The model could not be reached or refused the request. Carries no provider detail."""


# A refused connection reaches us as a bare OSError: asyncpg raises it before SQLAlchemy has
# a DBAPI error to wrap, so catching SQLAlchemyError alone misses the likeliest fault.
# OSError is broad, and only accurate while the database is the sole thing we open.
DB_UNAVAILABLE: tuple[type[Exception], ...] = (SQLAlchemyError, OSError)


async def database_unavailable(request: Request, exc: Exception) -> JSONResponse:
    """503, not 500: the database being down is an infrastructure state, not a bug here."""
    logger.error("database unavailable serving {}: {}", request.url.path, exc)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "database unavailable"},  # never the driver's text
    )
