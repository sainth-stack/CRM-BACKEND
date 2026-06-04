"""Central LLM provider / model-routing abstraction.

Single place that decides which model each agent role uses, so model choice,
timeouts, and provider are swappable without touching agent code (Provider
Abstraction + Prompt/model-as-config constraints).

Role -> model routing (mini models only; all env-overridable):
  * "reasoning" -> gpt-5.4-mini  (ICP judgment, deep-research synthesis, email
                                  Strategist + Critic — quality + tool-use heavy)
  * "writer"    -> gpt-4.1-mini  (email prose generation — fast, high-volume)
  * "cheap"     -> gpt-4o-mini   (bulk stakeholder role-scoring, simple agents)

Override any of them with REASONING_MODEL / WRITER_MODEL / CHEAP_MODEL — e.g. set
all three to gpt-4o-mini to run the cheapest config, with zero code change.

Reasoning-family models (gpt-5*, o1/o3/o4*) reject custom temperature/top_p/seed
(only default sampling is allowed), so we apply deterministic sampling knobs only
to the non-reasoning families (gpt-4.1 / gpt-4o). Optional REASONING_EFFORT is
forwarded for reasoning models when set.
"""
from __future__ import annotations

import os

from langchain_openai import ChatOpenAI

_MODEL_BY_ROLE = {
    "reasoning": os.getenv("REASONING_MODEL", "gpt-5.4-mini"),
    "writer": os.getenv("WRITER_MODEL", "gpt-4.1-mini"),
    "cheap": os.getenv("CHEAP_MODEL", "gpt-4o-mini"),
}

# Back-compat aliases (still referenced in a couple of places / tests).
REASONING_MODEL = _MODEL_BY_ROLE["reasoning"]
CHEAP_MODEL = _MODEL_BY_ROLE["cheap"]
WRITER_MODEL = _MODEL_BY_ROLE["writer"]

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
    else:
        params.update({"temperature": 0, "top_p": 1, "seed": 42})

    params.update(overrides)

    # Reasoning models (gpt-5*, o1/o3/o4*) accept ONLY default sampling. LangChain
    # would otherwise send its built-in temperature=0.7 and the API would 400, so
    # pin temperature=1 (the allowed value) and drop top_p/seed entirely. This also
    # protects against an override pointing a role at a reasoning model.
    if restricted:
        params["temperature"] = 1
        params.pop("top_p", None)
        params.pop("seed", None)

    # Attach cost tracking + budget enforcement to every agentic LLM call so the
    # new Phase 2-4 agents are metered and governed (the legacy run_openai_guarded
    # path only covers the old agents).
    from app.core.cost import cost_guard
    callbacks = params.pop("callbacks", None) or []
    if cost_guard not in callbacks:
        callbacks = [*callbacks, cost_guard]
    params["callbacks"] = callbacks

    return ChatOpenAI(**params)
