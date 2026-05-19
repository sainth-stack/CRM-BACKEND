import time
import os
import redis
import logging
from contextlib import contextmanager

logger = logging.getLogger("ai_priori.observability")

def _get_redis():
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        return None
    try:
        return redis.from_url(redis_url, decode_responses=True)
    except Exception:
        return None

class ObservabilityService:
    @staticmethod
    def increment_metric(name: str, value: int = 1):
        """Increments a counter metric (e.g. task retries, failed sends)."""
        r = _get_redis()
        if not r: return
        try:
            r.incrby(f"metrics:counter:{name}", value)
        except Exception as e:
            logger.warning(f"Failed to increment counter '{name}' in Redis: {e}")

    @staticmethod
    def record_duration(name: str, duration_sec: float):
        """Pushes a task or AI execution duration into a rolling list of last 100 samples in Redis."""
        r = _get_redis()
        if not r: return
        try:
            key = f"metrics:duration:{name}"
            # Push the duration sample
            r.lpush(key, float(duration_sec))
            # Keep only the last 100 measurements to bound memory footprint
            r.ltrim(key, 0, 99)
        except Exception as e:
            logger.warning(f"Failed to record duration for '{name}' in Redis: {e}")

    @staticmethod
    @contextmanager
    def track_latency(name: str):
        """
        High-fidelity context manager for tracking execution durations.
        Usage:
            with ObservabilityService.track_latency("ai_generation"):
                await run_openai_call()
        """
        start_time = time.time()
        try:
            yield
        finally:
            duration = time.time() - start_time
            ObservabilityService.record_duration(name, duration)

    @staticmethod
    def track_celery_task(task_name: str):
        """
        A decorator for Celery tasks to automatically record duration,
        number of successful executions, and track retries/failures.
        """
        def decorator(func):
            def wrapper(*args, **kwargs):
                # Retrieve self if present (bind=True task)
                self_obj = None
                if args and hasattr(args[0], "request"):
                    self_obj = args[0]
                    # Track retries if present
                    retries = getattr(self_obj.request, "retries", 0)
                    if retries > 0:
                        ObservabilityService.increment_metric(f"celery_retries:{task_name}")

                start_time = time.time()
                try:
                    res = func(*args, **kwargs)
                    duration = time.time() - start_time
                    ObservabilityService.record_duration(f"celery_duration:{task_name}", duration)
                    ObservabilityService.increment_metric(f"celery_success:{task_name}")
                    return res
                except Exception as exc:
                    duration = time.time() - start_time
                    ObservabilityService.record_duration(f"celery_duration:{task_name}", duration)
                    ObservabilityService.increment_metric(f"celery_failure:{task_name}")
                    raise exc
            # Preserve original function properties for Celery task serialization
            wrapper.__name__ = func.__name__
            wrapper.__doc__ = func.__doc__
            wrapper.__module__ = func.__module__
            return wrapper
        return decorator

    @staticmethod
    def get_metrics_summary():
        """Aggregates all counters and average durations for system health visualization."""
        r = _get_redis()
        if not r:
            return {"status": "Redis unavailable - metrics collection offline."}

        summary = {"counters": {}, "latencies_avg_sec": {}}
        try:
            # 1. Read counters
            counter_keys = r.keys("metrics:counter:*")
            for k in counter_keys:
                metric_name = k.replace("metrics:counter:", "")
                summary["counters"][metric_name] = int(r.get(k) or 0)

            # 2. Read latencies and compute average
            duration_keys = r.keys("metrics:duration:*")
            for k in duration_keys:
                metric_name = k.replace("metrics:duration:", "")
                vals = [float(x) for x in r.lrange(k, 0, -1) if x]
                if vals:
                    summary["latencies_avg_sec"][metric_name] = round(sum(vals) / len(vals), 4)
        except Exception as e:
            logger.warning(f"Failed to gather metrics summary from Redis: {e}")
            return {"error": str(e)}

        return summary
