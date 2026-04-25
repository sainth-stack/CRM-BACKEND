#!/usr/bin/env bash

# Legacy local bootstrap script.
# Prefer separate web / worker / beat services in deployment.

# 1. Database Migration
echo "[DEPLOY] Applying database migrations..."
alembic upgrade head

# 2. Celery Worker
echo "[DEPLOY] Launching Celery Worker..."
celery -A app.workers.config.celery_app worker --loglevel=info --concurrency=2 &

# 3. Celery Beat (Scheduler)
echo "[DEPLOY] Launching Celery Beat..."
celery -A app.workers.config.celery_app beat --loglevel=info &

# 4. FastAPI Gateway
# Start the primary API server. This must stay in the foreground.
echo "[DEPLOY] Launching FastAPI Web Service on port $PORT..."
uvicorn main:app --host 0.0.0.0 --port $PORT
