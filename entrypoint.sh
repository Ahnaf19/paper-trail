#!/bin/sh
# Migrations run here, not in a README step: NFR-1 allows no manual step beyond `.env`.
set -e

echo "applying database migrations..."
alembic upgrade head

# Always 8000 inside the container. The container port is an implementation detail that
# the compose port mapping and the healthcheck both hardcode; binding $API_PORT here
# instead would make all three disagree and leave the container permanently unhealthy.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
