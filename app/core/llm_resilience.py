import os
import pickle
import hashlib
import json
import datetime
from app.core.circuit_breaker import CircuitBreakerConfig, run_with_circuit_breaker
from app.core.security import _get_redis
from app.core.logging_config import setup_logging
from app.core.config import settings
from langchain_community.callbacks import get_openai_callback

logger = setup_logging()

OPENAI_CIRCUIT = CircuitBreakerConfig(
    name="openai:primary",
    failure_threshold=settings.OPENAI_CIRCUIT_FAILURE_THRESHOLD,
    recovery_timeout_seconds=settings.OPENAI_CIRCUIT_RECOVERY_TIMEOUT,
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


# Bump this (or set env LLM_CACHE_KEY_VERSION) to force-invalidate ALL cached LLM
# responses — e.g. after a change to the cache-keying logic itself. v2 = cache keys
# now incorporate the prompt-template text (see _extract_prompt_text below), so a
# prompt edit no longer silently serves a stale, pre-edit response.
CACHE_KEY_VERSION = os.getenv("LLM_CACHE_KEY_VERSION", "v2")


def _extract_prompt_text(val, _depth: int = 0) -> str:
    """Pull the literal prompt-template text out of a LangChain chain/prompt object.

    The action lambda's closure captures `chain = prompt | llm`. The INPUT variables
    are already hashed, but the prompt TEMPLATE was not — so editing a prompt left the
    cache key unchanged and old responses kept being served. We walk the runnable to
    collect every template string (and the model name), and hash that too. We extract
    ONLY template strings + model id — never the LLM client object — so the key stays
    deterministic across processes.
    """
    if val is None or _depth > 5:
        return ""
    texts: list[str] = []

    # RunnableSequence (prompt | llm | ...) exposes its parts via `.steps`.
    steps = getattr(val, "steps", None)
    if isinstance(steps, (list, tuple)):
        for s in steps:
            texts.append(_extract_prompt_text(s, _depth + 1))

    # ChatPromptTemplate: each message wraps a prompt with `.template`.
    messages = getattr(val, "messages", None)
    if isinstance(messages, (list, tuple)):
        for m in messages:
            inner = getattr(getattr(m, "prompt", None), "template", None)
            if isinstance(inner, str):
                texts.append(inner)
            direct = getattr(m, "template", None)
            if isinstance(direct, str):
                texts.append(direct)

    # PromptTemplate / single-message prompt: `.template`.
    tmpl = getattr(val, "template", None)
    if isinstance(tmpl, str):
        texts.append(tmpl)

    # Pin the model id too, so swapping models also busts the cache.
    for attr in ("model_name", "model"):
        m = getattr(val, attr, None)
        if isinstance(m, str):
            texts.append(f"__model__:{m}")

    # RunnableBinding (e.g. with_structured_output / .bind) wraps a `.bound` runnable.
    bound = getattr(val, "bound", None)
    if bound is not None and bound is not val:
        texts.append(_extract_prompt_text(bound, _depth + 1))

    return "\n".join(t for t in texts if t)


def _generate_cache_key(operation_name: str, action) -> str:
    """
    Generates a secure SHA-256 cache key based on the operation name, a key-format
    version salt, the closure variables (input data) AND the prompt-template text
    captured inside the action lambda. Including the template means a prompt edit
    yields a NEW key instead of serving a stale cached response.
    """
    hash_obj = hashlib.sha256(operation_name.encode('utf-8'))
    hash_obj.update(CACHE_KEY_VERSION.encode('utf-8'))
    if hasattr(action, '__closure__') and action.__closure__:
        for cell in action.__closure__:
            try:
                val = cell.cell_contents
                if isinstance(val, dict):
                    serialized = json.dumps(val, sort_keys=True)
                elif hasattr(val, 'dict'):
                    serialized = json.dumps(val.dict(), sort_keys=True)
                else:
                    serialized = str(val)
                hash_obj.update(serialized.encode('utf-8'))
            except Exception:
                pass
            # Always also fold in any prompt-template text reachable from this value.
            try:
                prompt_text = _extract_prompt_text(val)
                if prompt_text:
                    hash_obj.update(prompt_text.encode('utf-8'))
            except Exception:
                pass
    return f"llm_cache:{hash_obj.hexdigest()}"


def run_openai_guarded(operation_name: str, action, fallback=None):
    """
    Guarded OpenAI Executor with Production-Grade Cost Governance:
    1. Multi-layer Request Caching (via Redis).
    2. Dynamic daily dollar quota checks.
    3. LangChain Callback-driven exact token & USD cost tracking.
    4. Circuit breaker failure isolation.
    """
    r = _get_redis()
    today = datetime.date.today().isoformat()
    
    # --- 1. Daily Quota Check ---
    daily_budget = settings.LLM_DAILY_BUDGET_USD
    if r:
        try:
            current_cost = float(r.get(f"llm_cost:{today}") or 0.0)
            if current_cost >= daily_budget:
                logger.error(
                    f"🚨 [COST GOVERNANCE] Daily OpenAI budget exceeded (${current_cost:.4f} / ${daily_budget:.2f}). "
                    f"Blocking call and failing safe."
                )
                if fallback is not None:
                    return fallback() if callable(fallback) else fallback
                raise Exception("Daily OpenAI API cost quota exceeded. Blocked for cost governance.")
        except Exception as q_err:
            if "cost quota exceeded" in str(q_err):
                raise q_err
            logger.warning(f"Failed to check daily quota: {q_err}")

    # --- 2. Request Cache Lookup ---
    cache_key = _generate_cache_key(operation_name, action)
    if r and settings.LLM_CACHE_ENABLED:
        try:
            cached_val = r.get(cache_key)
            if cached_val:
                logger.info(f"✨ [LLM CACHE HIT] {operation_name} response resolved from Redis.")
                return pickle.loads(cached_val)
        except Exception as c_err:
            logger.warning(f"Cache resolution error: {c_err}")

    # --- 3. Guarded Execution & Token Tracking ---
    def execute_with_tracking():
        with get_openai_callback() as cb:
            result = action()

            # Record Token Consumption Metrics
            if cb.total_tokens > 0:
                logger.info(
                    f"📊 [LLM COST] {operation_name} -> Tokens: {cb.total_tokens} "
                    f"(Prompt: {cb.prompt_tokens}, Completion: {cb.completion_tokens}) | Cost: ${cb.total_cost:.5f}"
                )
                if r:
                    try:
                        r.incrbyfloat(f"llm_cost:{today}", cb.total_cost)
                        r.expire(f"llm_cost:{today}", 172800) # Keep day's cost for 48h

                        # Increment Prometheus/Telemetry aggregate counters
                        r.incrby(f"llm_tokens_total", cb.total_tokens)
                        r.incrbyfloat(f"llm_cost_total", cb.total_cost)
                    except Exception as metric_err:
                        logger.warning(f"Failed to update cost metrics: {metric_err}")

                # Per-agent-per-company granular write (same Redis hashes as CostGuard).
                try:
                    from app.core.cost import record_guarded_cost
                    record_guarded_cost(operation_name, cb.total_cost, cb.total_tokens)
                except Exception:
                    pass
            
            # --- 4. Cache Insertion ---
            if r and result and settings.LLM_CACHE_ENABLED:
                try:
                    cache_ttl = settings.LLM_CACHE_TTL_SECONDS
                    r.setex(cache_key, cache_ttl, pickle.dumps(result))
                    logger.debug(f"💾 [LLM CACHE WRITE] Cached {operation_name} result in Redis.")
                except Exception as w_err:
                    logger.warning(f"Cache write error: {w_err}")
                    
            return result

    return run_with_circuit_breaker(
        OPENAI_CIRCUIT,
        operation_name,
        execute_with_tracking,
        should_trip=is_openai_provider_failure,
        on_open=fallback,
    )
