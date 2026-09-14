#!/bin/sh
# Migrations run here, not in a README step: NFR-1 allows no manual step beyond `.env`.
set -e

echo "applying database migrations..."
alembic upgrade head

exec uvicorn pipeline.api.app:app --host 0.0.0.0 --port "${API_PORT:-8000}"
