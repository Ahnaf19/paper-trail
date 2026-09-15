# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    PYTHONUNBUFFERED=1 \
    EMBEDDING_CACHE_DIR=/opt/models \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies before source, so editing code does not reinstall the world.
COPY pyproject.toml uv.lock ./
RUN uv sync --no-install-project --no-dev

COPY . .
RUN uv sync --no-dev

# Bake the 128 MB embedding model in. Downloaded on first /ask instead, the reviewer's
# first request looks broken for half a minute -- and the API would need network access
# it otherwise never uses.
#
# Warmed through the application's own code path, not a bare fastembed call, so the baked
# cache is by construction the directory the app reads. DATABASE_URL is required at import
# and unused here: settings are validated on import, no connection is opened.
RUN DATABASE_URL=postgresql://build:build@localhost/build \
    python -c "from app.services.embeddings import FastEmbedEmbedder; FastEmbedEmbedder().embed_query('warm')"

RUN useradd --create-home --uid 10001 app && chown -R app:app /app /opt/models
USER app

EXPOSE 8000
ENTRYPOINT ["/app/entrypoint.sh"]
