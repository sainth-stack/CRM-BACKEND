from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler
from starlette.requests import Request
from contextlib import asynccontextmanager
import gc
import os

from app.core.logging_config import setup_logging
from app.core.limiter import limiter
from app.workers import sweep_stuck_campaigns_task
from app.api import auth, export, campaigns, drafts, prospects, health

# Initialize Enterprise Logging
logger = setup_logging()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Startup: Resurrection Protocol
    # Scans for campaigns that were processing when server last died
    logger.info("Mobilizing Outreach Resurrection Protocol...")
    try:
        sweep_stuck_campaigns_task()
    except Exception as e:
        logger.error(f"Failed to run startup campaign sweeper: {e}")
    yield
    # 2. Shutdown: Cleanup
    logger.info("Decommissioning API server... performing memory sweep.")
    gc.collect()

app = FastAPI(title="Outreach v3 API", lifespan=lifespan)

# --- Observability Middleware ---
@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f"[GATEWAY] Incoming {request.method} request to {request.url.path}")
    response = await call_next(request)
    logger.info(f"[GATEWAY] Completed {request.method} {request.url.path} | Status: {response.status_code}")
    return response

# --- CORS (Env-configured, production-safe) ---
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
ALLOWED_ORIGINS = [FRONTEND_URL, "http://localhost:5173"]

# Dynamically parse ALLOWED_ORIGINS env variable (comma-separated, resilient to quotes)
env_origins = os.getenv("ALLOWED_ORIGINS")
if env_origins:
    for origin in env_origins.split(","):
        clean_origin = origin.strip().strip('"').strip("'")
        if clean_origin and clean_origin not in ALLOWED_ORIGINS:
            ALLOWED_ORIGINS.append(clean_origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Attach rate limiter state and error handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# --- Modular Route Registration ---
app.include_router(auth.router)
app.include_router(auth.capability_router)
app.include_router(export.router)
app.include_router(campaigns.router, prefix="/campaigns", tags=["campaigns"])
app.include_router(drafts.router, prefix="/drafts", tags=["drafts"])
app.include_router(prospects.router, prefix="/prospects", tags=["prospects"])
app.include_router(health.router, prefix="/health", tags=["health"])
