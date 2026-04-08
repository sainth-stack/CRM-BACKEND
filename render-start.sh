#!/usr/bin/env bash

# AI-PRIORI Enterprise Startup Script
# ===================================
# This script initializes the unified engine, synchronizes the database, 
# and launches the distributed task workers.

# 1. Database Synchronization
# Ensure the database schema matches the current production code.
echo "[DEPLOY] Synchronizing database schema..."
python sync_db.py

# 2. Celery Worker
# Start the worker to handle heavy-lifting (Research, Finding DMs, Drafting).
# Concurrency is limited to 2 to respect Render's memory constraints.
echo "[DEPLOY] Launching Celery Worker..."
celery -A app.workers.config.celery_app worker --loglevel=info --concurrency=2 &

# 3. Celery Beat (Scheduler)
# Start the heartbeat to trigger periodic inbox polling and meeting reminders.
echo "[DEPLOY] Launching Celery Beat..."
celery -A app.workers.config.celery_app beat --loglevel=info &

# 4. FastAPI Gateway
# Start the primary API server. This must stay in the foreground.
echo "[DEPLOY] Launching FastAPI Web Service on port $PORT..."
uvicorn main:app --host 0.0.0.0 --port $PORT
