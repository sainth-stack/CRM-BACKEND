import time
import random
import asyncio
import logging
from functools import wraps

logger = logging.getLogger("ai_priori.resilience")

def retry_with_backoff(
    max_attempts: int = 5,
    base_delay_sec: float = 1.0,
    max_delay_sec: float = 30.0,
    backoff_factor: float = 2.0,
    exceptions_to_catch: tuple = (Exception,)
):
    """
    Async decorator that retries an asynchronous function with exponential backoff and random jitter.
    Fights transient network drops, OpenAI 429 rate limits, and thundering herd API traffic.
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            attempts = 0
            delay = base_delay_sec
            
            while True:
                try:
                    attempts += 1
                    return await func(*args, **kwargs)
                except exceptions_to_catch as e:
                    from app.services.observability_service import ObservabilityService
                    
                    if attempts >= max_attempts:
                        logger.error(f"[RESILIENCE] All {max_attempts} retry attempts exhausted. Fatal error: {e}")
                        ObservabilityService.increment_metric(f"resilience_failure:{func.__name__}")
                        raise e
                    
                    # Calculate exponential delay with full random jitter
                    # Jitter staggers concurrent retries and prevents cascading cluster overload
                    jitter = random.uniform(0.5, 1.5)
                    sleep_time = min(delay * backoff_factor * jitter, max_delay_sec)
                    
                    logger.warning(
                        f"[RESILIENCE] Attempt {attempts}/{max_attempts} failed with: {e}. "
                        f"Retrying in {round(sleep_time, 2)} seconds..."
                    )
                    
                    ObservabilityService.increment_metric(f"resilience_retry:{func.__name__}")
                    
                    await asyncio.sleep(sleep_time)
                    delay = min(delay * backoff_factor, max_delay_sec)
        return wrapper
    return decorator
