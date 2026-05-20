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


from fastapi import Response

@router.get("/prometheus")
def prometheus_metrics(
    db: Session = Depends(get_db)
):
    """
    Prometheus Exposition Text Endpoint.
    Enables native, out-of-the-box monitoring and alerting via Prometheus and Grafana.
    """
    lines = []
    
    # 1. DB connection pool stats
    pool_size = 0
    checked_out = 0
    try:
        pool = db.bind.pool
        pool_size = pool.size()
        checked_out = pool.checkedout()
    except Exception:
        pass
        
    lines.append("# HELP outreach_db_pool_size Database connection pool capacity size")
    lines.append("# TYPE outreach_db_pool_size gauge")
    lines.append(f"outreach_db_pool_size {pool_size}")
    
    lines.append("# HELP outreach_db_connections_active Active checked out DB connections")
    lines.append("# TYPE outreach_db_connections_active gauge")
    lines.append(f"outreach_db_connections_active {checked_out}")

    # 2. Redis Metrics
    celery_len = 0
    redis_latency = 0.0
    daily_llm_cost = 0.0
    total_llm_cost = 0.0
    total_llm_tokens = 0
    try:
        import time
        import datetime
        r = _get_redis()
        if r:
            start_time = time.time()
            r.ping()
            redis_latency = (time.time() - start_time) * 1000
            celery_len = r.llen("celery") or 0
            
            # AI Governance telemetry
            today = datetime.date.today().isoformat()
            daily_llm_cost = float(r.get(f"llm_cost:{today}") or 0.0)
            total_llm_cost = float(r.get("llm_cost_total") or 0.0)
            total_llm_tokens = int(r.get("llm_tokens_total") or 0)
    except Exception:
        pass
        
    lines.append("# HELP outreach_redis_latency_ms Redis connection ping latency in milliseconds")
    lines.append("# TYPE outreach_redis_latency_ms gauge")
    lines.append(f"outreach_redis_latency_ms {redis_latency:.2f}")

    lines.append("# HELP outreach_celery_queue_length Celery default task queue length")
    lines.append("# TYPE outreach_celery_queue_length gauge")
    lines.append(f"outreach_celery_queue_length {celery_len}")

    lines.append("# HELP outreach_llm_daily_cost_usd Daily OpenAI API cost in USD")
    lines.append("# TYPE outreach_llm_daily_cost_usd gauge")
    lines.append(f"outreach_llm_daily_cost_usd {daily_llm_cost:.5f}")

    lines.append("# HELP outreach_llm_total_cost_usd Cumulative OpenAI API cost in USD")
    lines.append("# TYPE outreach_llm_total_cost_usd counter")
    lines.append(f"outreach_llm_total_cost_usd {total_llm_cost:.5f}")

    lines.append("# HELP outreach_llm_total_tokens Cumulative tokens consumed across all LLM operations")
    lines.append("# TYPE outreach_llm_total_tokens counter")
    lines.append(f"outreach_llm_total_tokens {total_llm_tokens}")

    # 3. Process Resources
    cpu_percent = 0.0
    mem_rss = 0.0
    try:
        import psutil
        process = psutil.Process(os.getpid())
        cpu_percent = process.cpu_percent(interval=0.01)
        mem_rss = process.memory_info().rss / (1024 * 1024)
    except Exception:
        pass

    lines.append("# HELP outreach_process_cpu_percent Process CPU utilization percentage")
    lines.append("# TYPE outreach_process_cpu_percent gauge")
    lines.append(f"outreach_process_cpu_percent {cpu_percent:.2f}")

    lines.append("# HELP outreach_process_memory_rss_mb Process resident set memory size in megabytes")
    lines.append("# TYPE outreach_process_memory_rss_mb gauge")
    lines.append(f"outreach_process_memory_rss_mb {mem_rss:.2f}")

    # 4. Observability Service Telemetry
    try:
        from app.services.observability_service import ObservabilityService
        telemetry = ObservabilityService.get_metrics_summary()
        
        # Counters
        for k, v in telemetry.get("counters", {}).items():
            sanitized_name = k.replace(":", "_").replace("-", "_")
            lines.append(f"# HELP outreach_{sanitized_name} Live system telemetry counter for {k}")
            lines.append(f"# TYPE outreach_{sanitized_name} counter")
            lines.append(f"outreach_{sanitized_name} {v}")
            
        # Latencies
        for k, v in telemetry.get("latencies_avg_sec", {}).items():
            sanitized_name = k.replace(":", "_").replace("-", "_")
            lines.append(f"# HELP outreach_{sanitized_name}_avg_seconds Average duration latency for {k}")
            lines.append(f"# TYPE outreach_{sanitized_name}_avg_seconds gauge")
            lines.append(f"outreach_{sanitized_name}_avg_seconds {v:.4f}")
    except Exception:
        pass

    content = "\n".join(lines) + "\n"
    return Response(content=content, media_type="text/plain")


@router.get("")
def health():
    """System Vitality Check."""
    return {"status": "ok"}
