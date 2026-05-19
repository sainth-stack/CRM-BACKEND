#!/usr/bin/env bash

# Launch Celery Beat in the foreground for Render Beat scheduler service.
echo "[DEPLOY] Launching Celery Beat..."
celery -A app.workers.config.celery_app beat --loglevel=info
