#!/usr/bin/env bash

# Launch Celery Worker for Render / Production container.
# If CELERY_QUEUE env variable is set, launch a dedicated worker for that workload.
if [ -n "$CELERY_QUEUE" ]; then
    echo "[DEPLOY] Launching DEDICATED Celery Worker for queue: $CELERY_QUEUE"
    celery -A app.workers.config.celery_app worker --loglevel=info --concurrency=2 -Q "$CELERY_QUEUE"
else
    echo "[DEPLOY] Launching DEFAULT Celery Worker for ALL queues"
    celery -A app.workers.config.celery_app worker --loglevel=info --concurrency=2 -Q heavy_research,outbound_dispatch,inbox_polling,orchestrator
fi
