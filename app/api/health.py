from fastapi import APIRouter, Depends, HTTPException
from starlette.requests import Request
from sqlalchemy.orm import Session
import os

from app.db.database import get_db
from app.db import models
from app.core.security import get_current_user, _get_redis
from app.core.logging_config import setup_logging

logger = setup_logging()
router = APIRouter()

@router.get("/email")
def email_health_check(current_user: models.User = Depends(get_current_user)):
    """
    Sector Health Status.
    Authorized diagnostic check. Limited to Super Admins to prevent infrastructure metadata leakage.
    """
    if current_user.role != models.UserRole.SUPER_ADMIN:
        return {"status": "OK", "mode": "STRICT_GMAIL_NATIVE"}

    config_status = {
        "GMAIL_TOKEN_JSON": "SET" if os.getenv("GMAIL_TOKEN_JSON") else "MISSING",
        "GMAIL_CREDENTIALS_JSON": "SET" if os.getenv("GMAIL_CREDENTIALS_JSON") else "MISSING",
        "EMAIL_USER": "CONFIGURED" if os.getenv("EMAIL_USER") else "MISSING",
        "NEON_DB_URL": "CONNECTED" if os.getenv("NEON_DB_URL") else "MISSING",
        "REDIS_INFRA": "ACTIVE" if os.getenv("REDIS_URL") else "FALLBACK_LOCAL"
    }
    
    # Live Ping
    r = _get_redis()
    config_status["REDIS_LIVENESS"] = "PONG" if r else "UNREACHABLE"
    
    return {"status": "HEALTHY", "config": config_status}


@router.get("/dependencies")
def dependency_health_check(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Dependency health snapshot for operators.
    Gives the current actor a safe readout of the backing services that drive outreach execution.
    """
    mailbox_connected = (
        db.query(models.OAuthAccount).filter(models.OAuthAccount.user_id == current_user.id).first() is not None
    )
    redis_client = _get_redis()

    payload = {
        "status": "healthy" if redis_client else "degraded",
        "dependencies": {
            "redis": "up" if redis_client else "down",
            "gmail_mailbox": "connected" if mailbox_connected else "disconnected",
            "gmail_system_vault": "configured" if os.getenv("GMAIL_TOKEN_JSON") else "missing",
            "cal": "configured" if os.getenv("CAL_API_KEY") and os.getenv("CAL_EVENT_TYPE_ID") else "missing",
        },
    }

    if current_user.role == models.UserRole.SUPER_ADMIN:
        payload["environment"] = {
            "frontend_url": "configured" if os.getenv("FRONTEND_URL") else "default",
            "neon_db_url": "configured" if os.getenv("NEON_DB_URL") else "missing",
        }

    return payload


@router.get("/metrics")
def get_system_metrics(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Enterprise Observability Telemetry.
    Provides detailed system stats, database pool metrics, and active Redis queue telemetry.
    """
    if current_user.role not in [models.UserRole.SUPER_ADMIN, models.UserRole.ADMIN]:
        raise HTTPException(status_code=403, detail="Metrics readout is restricted to administrative identities.")

    # 1. DB Pool Metrics
    db_metrics = {}
    try:
        pool = db.bind.pool
        db_metrics = {
            "pool_size": pool.size(),
            "checked_out_connections": pool.checkedout(),
            "overflow_connections": pool.overflow() if hasattr(pool, "overflow") else 0,
            "checked_in_connections": pool.checkedin() if hasattr(pool, "checkedin") else 0,
        }
    except Exception as e:
        db_metrics = {"error": f"Failed to retrieve pool stats: {str(e)}"}

    # 2. Redis Metrics & Latency
    redis_metrics = {}
    try:
        import time
        r = _get_redis()
        if r:
            start_time = time.time()
            r.ping()
            latency_ms = (time.time() - start_time) * 1000
            
            # Retrieve basic Celery task queue lengths
            celery_queue_len = r.llen("celery")
            
            redis_metrics = {
                "status": "UP",
                "ping_latency_ms": round(latency_ms, 2),
                "celery_queue_length": celery_queue_len
            }
        else:
            redis_metrics = {"status": "DOWN", "error": "Redis client is unavailable."}
    except Exception as e:
        redis_metrics = {"status": "DEGRADED", "error": str(e)}

    # 3. Memory & OS footprint
    process_metrics = {}
    try:
        import psutil
        process = psutil.Process(os.getpid())
        mem_info = process.memory_info()
        process_metrics = {
            "process_memory_rss_mb": round(mem_info.rss / (1024 * 1024), 2),
            "process_memory_vms_mb": round(mem_info.vms / (1024 * 1024), 2),
            "cpu_percent": process.cpu_percent(interval=0.1),
            "open_file_descriptors": len(process.open_files()) if hasattr(process, "open_files") else 0
        }
    except ImportError:
        # Fallback if psutil is not available
        process_metrics = {
            "process_memory_rss_mb": "N/A",
            "cpu_percent": "N/A",
            "note": "Install psutil for detailed memory/CPU metrics."
        }
    except Exception as e:
        process_metrics = {"error": str(e)}

    # 4. Storage mode health check
    storage_mode = os.getenv("STORAGE_MODE", "local")
    storage_health = {
        "configured_mode": storage_mode,
        "s3_bucket": os.getenv("AWS_STORAGE_BUCKET_NAME", "N/A")
    }

    # 5. Production Observability Telemetry (Task Durations, Retries, AI Latency, Sends)
    from app.services.observability_service import ObservabilityService
    telemetry = ObservabilityService.get_metrics_summary()

    return {
        "database": db_metrics,
        "redis": redis_metrics,
        "process": process_metrics,
        "storage": storage_health,
        "telemetry": telemetry
    }


@router.get("")
def health():
    """System Vitality Check."""
    return {"status": "ok"}
