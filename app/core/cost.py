"""LLM cost tracking + budget enforcement for the agentic pipeline.

The legacy `run_openai_guarded` only meters calls routed through it (the old
intent/email/user-intel agents). The new Phase 2-4 agents call LangChain chains
directly via `app.core.llm.get_chat_llm`, so they would otherwise be invisible to
cost governance.

`CostGuard` is a LangChain callback attached to every `get_chat_llm` instance. It:
  * meters token usage -> USD into the SAME Redis keys run_openai_guarded uses
    (`llm_cost:{today}`, `llm_cost_total`) so accounting stays unified, plus a
    per-campaign key attributed via the `campaign_id_var` contextvar; and
  * enforces both the global daily budget and a per-campaign budget, raising
    before a call when either is exceeded (fail-safe: Redis errors never block).

Pricing is per-1M-tokens, env-overridable (PRICE_<MODEL>_IN / _OUT), since
gpt-5-mini pricing is not in LangChain's built-in table.
"""
from __future__ import annotations

import datetime
import os

from langchain_core.callbacks import BaseCallbackHandler

from app.core.config import settings
from app.core.logging_config import campaign_id_var, logger


class CostBudgetExceeded(Exception):
    """Raised to abort an LLM call when a cost budget is exhausted."""


# Default USD price per 1,000,000 tokens: (input, output).
# Best-effort defaults — override per model via PRICE_<MODEL>_IN / PRICE_<MODEL>_OUT
# env vars (e.g. PRICE_GPT_5_4_MINI_IN) once exact pricing is known. Longer prefixes
# are matched first so "gpt-5.4-mini" doesn't accidentally match "gpt-5".
_DEFAULT_PRICES = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-5.4-mini": (0.45, 3.60),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    "o4-mini": (1.10, 4.40),
    "o3-mini": (1.10, 4.40),
}
_FALLBACK_PRICE = (0.50, 1.50)  # conservative default for unrecognised mini models


def _model_price(model: str) -> tuple[float, float]:
    base = _FALLBACK_PRICE
    for prefix, price in _DEFAULT_PRICES.items():
        if model and model.startswith(prefix):
            base = price
            break

    def _env(kind: str, default: float) -> float:
        if not model:
            return default
        key = f"PRICE_{model.replace('-', '_').replace('.', '_').upper()}_{kind}"
        raw = os.getenv(key)
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
        return default

    return _env("IN", base[0]), _env("OUT", base[1])


def _redis():
    try:
        from app.core.security import _get_redis
        return _get_redis()
    except Exception:
        return None


class CostGuard(BaseCallbackHandler):
    def on_llm_start(self, serialized, prompts, **kwargs):
        r = _redis()
        if not r:
            return
        today = datetime.date.today().isoformat()
        try:
            daily = float(r.get(f"llm_cost:{today}") or 0.0)
            if daily >= settings.LLM_DAILY_BUDGET_USD:
                raise CostBudgetExceeded(
                    f"Daily LLM budget exceeded (${daily:.2f} / ${settings.LLM_DAILY_BUDGET_USD:.2f})."
                )
            cid = campaign_id_var.get()
            if cid:
                camp = float(r.get(f"llm_cost_campaign:{cid}") or 0.0)
                if camp >= settings.LLM_CAMPAIGN_BUDGET_USD:
                    raise CostBudgetExceeded(
                        f"Campaign {cid} LLM budget exceeded "
                        f"(${camp:.2f} / ${settings.LLM_CAMPAIGN_BUDGET_USD:.2f})."
                    )
        except CostBudgetExceeded:
            logger.error("🚨 [COST GOVERNANCE] %s", kwargs.get("run_id", ""))
            raise
        except Exception as e:
            logger.warning(f"[COST] budget check skipped: {e}")

    def on_llm_end(self, response, **kwargs):
        try:
            out = getattr(response, "llm_output", None) or {}
            usage = out.get("token_usage") or out.get("usage") or {}
            prompt_tokens = usage.get("prompt_tokens", 0) or 0
            completion_tokens = usage.get("completion_tokens", 0) or 0
            if prompt_tokens == 0 and completion_tokens == 0:
                return

            model = out.get("model_name") or out.get("model") or ""
            p_in, p_out = _model_price(model)
            cost = (prompt_tokens / 1_000_000) * p_in + (completion_tokens / 1_000_000) * p_out

            r = _redis()
            if not r:
                return
            today = datetime.date.today().isoformat()
            r.incrbyfloat(f"llm_cost:{today}", cost)
            r.expire(f"llm_cost:{today}", 172800)
            r.incrbyfloat("llm_cost_total", cost)
            r.incrby("llm_tokens_total", prompt_tokens + completion_tokens)

            cid = campaign_id_var.get()
            if cid:
                r.incrbyfloat(f"llm_cost_campaign:{cid}", cost)
                r.expire(f"llm_cost_campaign:{cid}", 604800)  # 7 days

            logger.info(
                f"📊 [COST] model={model or '?'} tokens={prompt_tokens + completion_tokens} "
                f"cost=${cost:.5f} campaign={cid or '-'}"
            )
        except Exception as e:
            logger.warning(f"[COST] tracking error: {e}")


# Shared singleton attached to every get_chat_llm instance.
cost_guard = CostGuard()
