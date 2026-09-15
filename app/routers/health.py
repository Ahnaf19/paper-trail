"""Liveness for the container healthcheck."""

from fastapi import APIRouter
from sqlalchemy import text

from app.dependencies import SessionDep

router = APIRouter(tags=["ops"])


@router.get("/health")
async def health(session: SessionDep) -> dict[str, str]:
    """Healthy means "can serve", and serving needs the database — so it is queried.

    A database fault raises, and the app-wide handler turns it into a 503.
    """
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}
