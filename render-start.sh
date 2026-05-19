#!/usr/bin/env bash

# 1. Database Migration
echo "[DEPLOY] Applying database migrations..."
alembic upgrade head

# 2. FastAPI Gateway
# Start the primary API server. This must stay in the foreground.
echo "[DEPLOY] Launching FastAPI Web Service on port $PORT..."
uvicorn main:app --host 0.0.0.0 --port $PORT
