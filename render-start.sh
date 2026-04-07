#!/usr/bin/env bash

# Start the FastAPI application (Background tasks and Scheduler handled internally via APScheduler/FastAPI)
echo "Starting Unified Outreach Engine..."
uvicorn main:app --host 0.0.0.0 --port $PORT
