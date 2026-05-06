import time
import random
from functools import wraps
from app.core.logging_config import logger

def with_retries(max_attempts=3, base_delay=2.0, max_delay=30.0, exceptions=(Exception,)):
    """
    Robust retry decorator with exponential backoff and jitter.
    
    Args:
        max_attempts: Maximum number of times to try the function.
        base_delay: Initial delay in seconds before the first retry.
        max_delay: Maximum delay in seconds between retries.
        exceptions: Tuple of exceptions to catch and retry on.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            attempt = 1
            delay = base_delay
            while attempt <= max_attempts:
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    # Do not retry if we have reached max attempts
                    if attempt >= max_attempts:
                        logger.error(f"[RETRY FAILED] {func.__name__} failed after {max_attempts} attempts: {e}")
                        raise
                    
                    # Optional: Add checks here for specific non-retryable status codes (like 400, 401)
                    # For example, if it's an HTTPError, check the status code
                    status_code = getattr(e, 'status', None) or getattr(e, 'status_code', None)
                    if status_code in [400, 401, 403, 404]:
                        logger.error(f"[RETRY ABORTED] {func.__name__} encountered non-retryable status code {status_code}: {e}")
                        raise

                    # Apply exponential backoff with jitter
                    # Jitter prevents thundering herd problem
                    sleep_time = min(delay * (2 ** (attempt - 1)), max_delay)
                    jitter = random.uniform(0, 0.1 * sleep_time) 
                    total_sleep = sleep_time + jitter
                    
                    logger.warning(f"[RETRY {attempt}/{max_attempts}] {func.__name__} failed with {type(e).__name__}: {e}. Retrying in {total_sleep:.2f}s...")
                    time.sleep(total_sleep)
                    attempt += 1
            return None
        return wrapper
    return decorator
