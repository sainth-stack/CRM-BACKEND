"""Central LLM provider / model-routing abstraction.

Single place that decides which model each agent role uses, so model choice,
timeouts, and provider are swappable without touching agent code (Provider
Abstraction + Prompt/model-as-config constraints).

Role -> model routing (all roles consolidated onto gpt-4o-mini for cost; all
env-overridable):
  * "reasoning" -> gpt-4o-mini   (ICP judgment, deep-research synthesis, email
                                  Strategist + Critic)
  * "writer"    -> gpt-4o-mini   (email prose generation)
  * "cheap"     -> gpt-4o-mini   (bulk stakeholder role-scoring, simple agents)

All three default to gpt-4o-mini to minimise spend. Override any role via
REASONING_MODEL / WRITER_MODEL / CHEAP_MODEL env vars (e.g. point "reasoning"
back at a stronger model) with zero code change.

Reasoning-family models (gpt-5*, o1/o3/o4*) reject custom temperature/top_p/seed
(only default sampling is allowed), so we apply deterministic sampling knobs only
to the non-reasoning families (gpt-4.1 / gpt-4o). Optional REASONING_EFFORT is
forwarded for reasoning models when set.
"""
from __future__ import annotations

import os

from langchain_openai import ChatOpenAI

_MODEL_BY_ROLE = {
    "reasoning": os.getenv("REASONING_MODEL", "gpt-4o-mini"),
    "writer": os.getenv("WRITER_MODEL", "gpt-4o-mini"),
    "cheap": os.getenv("CHEAP_MODEL", "gpt-4o-mini"),
    # Web-content -> structured-research enrichment (ICP Call 1). Reads the large raw
    # crawl once and distils it; the raw text is then dropped. ICP validation (Call 2)
    # uses the "reasoning" model. Both default to gpt-4o-mini (the nano enrichment was
    # ~2x slower/costlier for no measured ICP-verdict accuracy gain on the test set).
    "enrichment": os.getenv("ENRICHMENT_MODEL", "gpt-4o-mini"),
}

# Back-compat aliases (still referenced in a couple of places / tests).
REASONING_MODEL = _MODEL_BY_ROLE["reasoning"]
CHEAP_MODEL = _MODEL_BY_ROLE["cheap"]
WRITER_MODEL = _MODEL_BY_ROLE["writer"]

# Fixed seed for deterministic generation (same input -> same output). Override
# via LLM_SEED if you ever need a different reproducible stream.
LLM_SEED = int(os.getenv("LLM_SEED", "42"))

# Prefixes whose models reject custom temperature/top_p/seed.
_RESTRICTED_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def _restricts_sampling(model: str) -> bool:
    return any(model.startswith(p) for p in _RESTRICTED_PREFIXES)


def get_chat_llm(role: str = "cheap", *, timeout: int = 90, max_retries: int = 2, **overrides) -> ChatOpenAI:
    """Return a configured ChatOpenAI for the given agent role.

    role: "reasoning" | "writer" | "cheap" (unknown roles fall back to cheap).
    overrides: forwarded to ChatOpenAI (e.g. temperature=0.6, max_tokens=...).
    """
    model = _MODEL_BY_ROLE.get(role, _MODEL_BY_ROLE["cheap"])

    params: dict = {"model": model, "timeout": timeout, "max_retries": max_retries}

    restricted = _restricts_sampling(model)
    if restricted:
        effort = os.getenv("REASONING_EFFORT")
        if effort:
            params["model_kwargs"] = {"reasoning_effort": effort}

    params.update(overrides)

    if restricted:
        # Reasoning models (gpt-5*, o1/o3/o4*) accept ONLY default sampling. Pin
        # temperature=1 (the allowed value) and drop top_p/seed; also protects
        # against an override pointing a role at a reasoning model.
        params["temperature"] = 1
        params.pop("top_p", None)
        params.pop("seed", None)
    else:
        # DETERMINISM GUARANTEE: force deterministic sampling AFTER overrides so a
        # caller-supplied temperature (e.g. the email writer's old 0.6) can never
        # reintroduce randomness. Same input -> same output, best-effort within
        # OpenAI's system_fingerprint. SEED is env-overridable via LLM_SEED.
        params["temperature"] = 0
        params["top_p"] = 1
        params["seed"] = LLM_SEED

    # Attach cost tracking + budget enforcement to every agentic LLM call so the
    # new Phase 2-4 agents are metered and governed (the legacy run_openai_guarded
    # path only covers the old agents).
    from app.core.cost import cost_guard
    callbacks = params.pop("callbacks", None) or []
    if cost_guard not in callbacks:
        callbacks = [*callbacks, cost_guard]
    params["callbacks"] = callbacks

    return ChatOpenAI(**params)
