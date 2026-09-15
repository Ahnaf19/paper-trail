"""The application: long-lived resources, routers, and error handlers wired together."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger

from app.core.errors import DB_UNAVAILABLE, database_unavailable
from app.core.logging import configure_logging
from app.routers import analytics, ask, health
from app.services.embeddings import FastEmbedEmbedder
from app.services.llm import OpenAICompatibleClient

DESCRIPTION = """
Aggregations over a corpus of arXiv papers, computed in SQL, plus a grounded question
answering endpoint over their abstracts. Papers are loaded by the `paper ingest` CLI.
"""


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Warm the embedder before serving, so the model download is a startup cost.

    Tests assign `app.state.embedder` first and this leaves it alone — which keeps the
    suite off the network without a test-only config flag.
    """
    if not getattr(application.state, "embedder", None):
        application.state.embedder = FastEmbedEmbedder()
    owns_llm = not getattr(application.state, "llm", None)
    if owns_llm:
        application.state.llm = OpenAICompatibleClient()
    await asyncio.to_thread(application.state.embedder.embed_query, "warmup")
    logger.info("embedder ready: {}", application.state.embedder.model_id)
    yield
    if owns_llm:
        await application.state.llm.aclose()


def create_app() -> FastAPI:
    """One app per process. Routers are mounted here, never imported for side effects."""
    configure_logging()
    application = FastAPI(
        title="arXiv corpus API", version="0.1.0", description=DESCRIPTION, lifespan=lifespan
    )

    application.include_router(health.router)
    application.include_router(analytics.router)
    application.include_router(ask.router)
    for failure in DB_UNAVAILABLE:
        application.add_exception_handler(failure, database_unavailable)
    return application


app = create_app()
