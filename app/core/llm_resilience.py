import os

from app.core.circuit_breaker import CircuitBreakerConfig, run_with_circuit_breaker


OPENAI_CIRCUIT = CircuitBreakerConfig(
    name="openai:primary",
    failure_threshold=int(os.getenv("OPENAI_CIRCUIT_FAILURE_THRESHOLD", "3")),
    recovery_timeout_seconds=int(os.getenv("OPENAI_CIRCUIT_RECOVERY_TIMEOUT", "180")),
)

_OPENAI_PROVIDER_MARKERS = (
    "rate limit",
    "too many requests",
    "api connection",
    "connection error",
    "timeout",
    "timed out",
    "service unavailable",
    "temporarily unavailable",
    "overloaded",
    "internal server error",
    "server error",
    "bad gateway",
    "gateway timeout",
    "429",
    "500",
    "502",
    "503",
    "504",
)


def is_openai_provider_failure(exc: Exception) -> bool:
    message = str(exc).lower()
    class_name = exc.__class__.__name__.lower()
    module_name = exc.__class__.__module__.lower()

    if "openai" in module_name:
        return True

    if any(marker in class_name for marker in ("ratelimit", "timeout", "connection", "servererror")):
        return True

    return any(marker in message for marker in _OPENAI_PROVIDER_MARKERS)


def run_openai_guarded(operation_name: str, action, fallback=None):
    return run_with_circuit_breaker(
        OPENAI_CIRCUIT,
        operation_name,
        action,
        should_trip=is_openai_provider_failure,
        on_open=fallback,
    )
