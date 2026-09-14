# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_FROZEN=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/hf \
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
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5')"

RUN useradd --create-home --uid 10001 app && chown -R app:app /app /opt/hf
USER app

EXPOSE 8000
ENTRYPOINT ["/app/entrypoint.sh"]
