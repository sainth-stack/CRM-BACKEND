"""Central LLM provider / model-routing abstraction.

Single place that decides which model each agent role uses, so model choice,
timeouts, and provider are swappable without touching agent code.

All roles use gpt-4o-mini. Override via env vars if needed:
  REASONING_MODEL, WRITER_MODEL, CHEAP_MODEL, ENRICHMENT_MODEL
"""
from __future__ import annotations

import os

from langchain_openai import ChatOpenAI

_MODEL_BY_ROLE = {
    "reasoning":  os.getenv("REASONING_MODEL",  "gpt-4o-mini"),
    "writer":     os.getenv("WRITER_MODEL",      "gpt-4o-mini"),
    "cheap":      os.getenv("CHEAP_MODEL",       "gpt-4o-mini"),
    "enrichment": os.getenv("ENRICHMENT_MODEL",  "gpt-4o-mini"),
}

# Back-compat aliases referenced in a few places.
REASONING_MODEL = _MODEL_BY_ROLE["reasoning"]
CHEAP_MODEL     = _MODEL_BY_ROLE["cheap"]
WRITER_MODEL    = _MODEL_BY_ROLE["writer"]

# Fixed seed for deterministic generation. Override via LLM_SEED if needed.
LLM_SEED = int(os.getenv("LLM_SEED", "42"))


def get_chat_llm(role: str = "cheap", *, timeout: int = 90, max_retries: int = 2, **overrides) -> ChatOpenAI:
    """Return a configured ChatOpenAI for the given agent role.

    role: "reasoning" | "writer" | "cheap" | "enrichment" (unknown -> cheap).
    overrides: forwarded to ChatOpenAI (e.g. max_tokens=...).
    """
    model = _MODEL_BY_ROLE.get(role, _MODEL_BY_ROLE["cheap"])

    params: dict = {
        "model":       model,
        "timeout":     timeout,
        "max_retries": max_retries,
        "temperature": 0,
        "top_p":       1,
        "seed":        LLM_SEED,
    }

    params.update(overrides)

    from app.core.cost import cost_guard
    callbacks = params.pop("callbacks", None) or []
    if cost_guard not in callbacks:
        callbacks = [*callbacks, cost_guard]
    params["callbacks"] = callbacks

    return ChatOpenAI(**params)
