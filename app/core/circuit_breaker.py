import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.core.logging_config import logger


class CircuitOpenError(RuntimeError):
    """Raised when a dependency circuit is currently open."""


@dataclass(frozen=True)
class CircuitBreakerConfig:
    name: str
    failure_threshold: int = 3
    recovery_timeout_seconds: int = 180


_LOCAL_STATE_LOCK = threading.Lock()
_LOCAL_STATE: dict[str, dict[str, Any]] = {}


def _get_redis():
    try:
        import redis

        client = redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            socket_connect_timeout=1,
        )
        client.ping()
        return client
    except Exception:
        return None


def _state_key(config: CircuitBreakerConfig) -> str:
    return f"circuit:{config.name}"


def _ttl_seconds(config: CircuitBreakerConfig) -> int:
    return max(config.recovery_timeout_seconds * 3, 1800)


def _default_state() -> dict[str, Any]:
    return {
        "state": "closed",
        "failure_count": 0,
        "opened_at": None,
        "last_failure": None,
        "last_failure_at": None,
    }


def _load_state(config: CircuitBreakerConfig) -> dict[str, Any]:
    client = _get_redis()
    if client:
        raw_state = client.get(_state_key(config))
        if raw_state:
            try:
                state = json.loads(raw_state)
                return {**_default_state(), **state}
            except Exception:
                logger.warning("[CIRCUIT] Corrupt state payload for %s. Resetting.", config.name)
        return _default_state()

    with _LOCAL_STATE_LOCK:
        return {**_default_state(), **_LOCAL_STATE.get(config.name, {})}


def _save_state(config: CircuitBreakerConfig, state: dict[str, Any]) -> None:
    payload = {**_default_state(), **state}
    client = _get_redis()
    if client:
        client.setex(_state_key(config), _ttl_seconds(config), json.dumps(payload))
        return

    with _LOCAL_STATE_LOCK:
        _LOCAL_STATE[config.name] = payload


def get_circuit_snapshot(config: CircuitBreakerConfig) -> dict[str, Any]:
    return _load_state(config)


def reset_circuit(config: CircuitBreakerConfig) -> None:
    _save_state(config, _default_state())


def force_open_circuit(config: CircuitBreakerConfig, error: str = "forced_open") -> None:
    now = time.time()
    _save_state(
        config,
        {
            "state": "open",
            "failure_count": max(config.failure_threshold, 1),
            "opened_at": now,
            "last_failure": error,
            "last_failure_at": now,
        },
    )


def circuit_is_open(config: CircuitBreakerConfig) -> tuple[bool, dict[str, Any]]:
    state = _load_state(config)
    if state.get("state") != "open":
        return False, state

    opened_at = state.get("opened_at") or 0
    if time.time() - float(opened_at) >= config.recovery_timeout_seconds:
        state["state"] = "half_open"
        _save_state(config, state)
        return False, state

    return True, state


def record_circuit_success(config: CircuitBreakerConfig) -> None:
    _save_state(config, _default_state())


def record_circuit_failure(config: CircuitBreakerConfig, error: str | None = None) -> None:
    state = _load_state(config)
    failure_count = int(state.get("failure_count") or 0) + 1
    now = time.time()
    next_state = "open" if state.get("state") == "half_open" or failure_count >= config.failure_threshold else "closed"
    _save_state(
        config,
        {
            "state": next_state,
            "failure_count": failure_count,
            "opened_at": now if next_state == "open" else None,
            "last_failure": (error or "")[:1000] if error else None,
            "last_failure_at": now,
        },
    )


def run_with_circuit_breaker(
    config: CircuitBreakerConfig,
    operation_name: str,
    action: Callable[[], Any],
    *,
    should_trip: Callable[[Exception], bool] | None = None,
    on_open: Callable[[], Any] | Any | None = None,
):
    is_open, state = circuit_is_open(config)
    if is_open:
        logger.warning(
            "[CIRCUIT] %s is OPEN. Skipping %s. Last failure: %s",
            config.name,
            operation_name,
            state.get("last_failure"),
        )
        if callable(on_open):
            return on_open()
        if on_open is not None:
            return on_open
        raise CircuitOpenError(f"Circuit open for {config.name}")

    try:
        result = action()
    except Exception as exc:
        if should_trip is None or should_trip(exc):
            record_circuit_failure(config, str(exc))
        raise

    record_circuit_success(config)
    return result
