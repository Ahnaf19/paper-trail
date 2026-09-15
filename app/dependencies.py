"""FastAPI dependencies, and the annotated aliases routers use.

Declared once here rather than per router — three copies of `Annotated[AsyncSession, ...]`
is how they quietly stop matching.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.services.embeddings import Embedder
from app.services.llm import LLMClient


async def get_session() -> AsyncIterator[AsyncSession]:
    """One session per request, rolled back and returned to the pool when it ends."""
    async with AsyncSessionLocal() as session:
        yield session


def get_embedder(request: Request) -> Embedder:
    """Loaded once at startup and held on app.state — never constructed per request."""
    return request.app.state.embedder


def get_llm(request: Request) -> LLMClient:
    return request.app.state.llm


SessionDep = Annotated[AsyncSession, Depends(get_session)]
EmbedderDep = Annotated[Embedder, Depends(get_embedder)]
LLMDep = Annotated[LLMClient, Depends(get_llm)]
