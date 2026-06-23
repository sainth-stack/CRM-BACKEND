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

Per-agent-per-company granularity:
  Every metered call writes into two Redis hashes keyed by campaign:
    llm:r:usd:{campaign_id}  → hash  field="{domain}|{agent}"  value=float
    llm:r:tok:{campaign_id}  → hash  field="{domain}|{agent}"  value=int
  These are read by `get_cost_report(campaign_id)` and exposed via the admin API.

`agent_label_var` and `company_domain_var` (both in logging_config) are set by
each agent before its LLM call. The sentinel "—" is used when either is absent
so the hash field is always well-formed.

Pricing is per-1M-tokens, env-overridable (PRICE_<MODEL>_IN / _OUT).
"""
from __future__ import annotations

import datetime
import os

from langchain_core.callbacks import BaseCallbackHandler

from app.core.config import settings
from app.core.logging_config import campaign_id_var, company_domain_var, agent_label_var, logger


class CostBudgetExceeded(Exception):
    """Raised to abort an LLM call when a cost budget is exhausted."""


# Default USD price per 1,000,000 tokens: (input, output).
_DEFAULT_PRICES = {
    "gpt-4o-mini": (0.15, 0.60),
}
_FALLBACK_PRICE = (0.50, 1.50)

# Redis TTL for per-campaign cost hashes (30 days).
_REPORT_TTL_SEC = 60 * 60 * 24 * 30


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


# --------------------------------------------------------------------------- #
# Process-local fallback accounting (survives Redis outages).                  #
# --------------------------------------------------------------------------- #
import threading

_local_lock = threading.Lock()
_local_state: dict = {"day": None, "daily_usd": 0.0, "campaign_usd": {}}


def _local_roll_day(today: str) -> None:
    if _local_state["day"] != today:
        _local_state["day"] = today
        _local_state["daily_usd"] = 0.0
        _local_state["campaign_usd"] = {}


def _local_check(today: str, cid: str | None) -> None:
    with _local_lock:
        _local_roll_day(today)
        if _local_state["daily_usd"] >= settings.LLM_DAILY_BUDGET_USD:
            raise CostBudgetExceeded(
                f"[local] Daily LLM budget exceeded "
                f"(${_local_state['daily_usd']:.2f} / ${settings.LLM_DAILY_BUDGET_USD:.2f})."
            )
        if cid and _local_state["campaign_usd"].get(cid, 0.0) >= settings.LLM_CAMPAIGN_BUDGET_USD:
            raise CostBudgetExceeded(
                f"[local] Campaign {cid} LLM budget exceeded "
                f"(${_local_state['campaign_usd'][cid]:.2f} / ${settings.LLM_CAMPAIGN_BUDGET_USD:.2f})."
            )


def _local_add(today: str, cid: str | None, cost: float) -> None:
    with _local_lock:
        _local_roll_day(today)
        _local_state["daily_usd"] += cost
        if cid:
            _local_state["campaign_usd"][cid] = _local_state["campaign_usd"].get(cid, 0.0) + cost


# --------------------------------------------------------------------------- #
# Per-agent-per-company Redis write                                             #
# --------------------------------------------------------------------------- #

def _write_granular(r, cid: str | None, domain: str | None, agent: str | None,
                    cost: float, tokens: int) -> None:
    """Write (cost, tokens) to the per-campaign per-agent-per-company hashes.

    No-ops silently on Redis error so accounting never blocks the LLM call.
    """
    if not r or not cid:
        return
    field = f"{domain or '—'}|{agent or '—'}"
    try:
        r.hincrbyfloat(f"llm:r:usd:{cid}", field, cost)
        r.hincrby(f"llm:r:tok:{cid}", field, tokens)
        r.expire(f"llm:r:usd:{cid}", _REPORT_TTL_SEC)
        r.expire(f"llm:r:tok:{cid}", _REPORT_TTL_SEC)
    except Exception as e:
        logger.warning(f"[COST] granular write failed: {e}")


# --------------------------------------------------------------------------- #
# Public helper for run_openai_guarded (legacy path)                           #
# --------------------------------------------------------------------------- #

def record_guarded_cost(operation_name: str, cost_usd: float, tokens: int) -> None:
    """Called by run_openai_guarded after a successful token-counted call.

    Writes the legacy path's cost into the same per-agent-per-company hashes so
    the cost report covers ALL agents regardless of which execution path they use.
    """
    today = datetime.date.today().isoformat()
    cid    = campaign_id_var.get()
    domain = company_domain_var.get()

    _local_add(today, cid, cost_usd)

    r = _redis()
    _write_granular(r, cid, domain, operation_name, cost_usd, tokens)


# --------------------------------------------------------------------------- #
# CostGuard — LangChain callback (new path via get_chat_llm)                   #
# --------------------------------------------------------------------------- #

class CostGuard(BaseCallbackHandler):
    def on_llm_start(self, serialized, prompts, **kwargs):
        today = datetime.date.today().isoformat()
        cid = campaign_id_var.get()
        r = _redis()
        if not r:
            _local_check(today, cid)
            return
        try:
            daily = float(r.get(f"llm_cost:{today}") or 0.0)
            if daily >= settings.LLM_DAILY_BUDGET_USD:
                raise CostBudgetExceeded(
                    f"Daily LLM budget exceeded (${daily:.2f} / ${settings.LLM_DAILY_BUDGET_USD:.2f})."
                )
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
            logger.warning(f"[COST] Redis budget check failed ({e}); using local fallback.")
            _local_check(today, cid)

    def on_llm_end(self, response, **kwargs):
        try:
            out = getattr(response, "llm_output", None) or {}
            usage = out.get("token_usage") or out.get("usage") or {}
            prompt_tokens     = usage.get("prompt_tokens", 0) or 0
            completion_tokens = usage.get("completion_tokens", 0) or 0
            if prompt_tokens == 0 and completion_tokens == 0:
                return

            model  = out.get("model_name") or out.get("model") or ""
            p_in, p_out = _model_price(model)
            cost   = (prompt_tokens / 1_000_000) * p_in + (completion_tokens / 1_000_000) * p_out
            tokens = prompt_tokens + completion_tokens

            today  = datetime.date.today().isoformat()
            cid    = campaign_id_var.get()
            domain = company_domain_var.get()
            agent  = agent_label_var.get()

            _local_add(today, cid, cost)

            r = _redis()
            if r:
                try:
                    r.incrbyfloat(f"llm_cost:{today}", cost)
                    r.expire(f"llm_cost:{today}", 172800)
                    r.incrbyfloat("llm_cost_total", cost)
                    r.incrby("llm_tokens_total", tokens)
                    if cid:
                        r.incrbyfloat(f"llm_cost_campaign:{cid}", cost)
                        r.expire(f"llm_cost_campaign:{cid}", 604800)
                except Exception as e:
                    logger.warning(f"[COST] Redis cost write failed ({e}); local accounting retained.")

                # Per-agent-per-company granular write.
                _write_granular(r, cid, domain, agent, cost, tokens)

            logger.info(
                f"📊 [COST] model={model or '?'} agent={agent or '—'} "
                f"domain={domain or '—'} tokens={tokens} "
                f"cost=${cost:.5f} campaign={cid or '-'}"
            )
        except Exception as e:
            logger.warning(f"[COST] tracking error: {e}")


# Shared singleton attached to every get_chat_llm instance.
cost_guard = CostGuard()


# --------------------------------------------------------------------------- #
# Report builder                                                               #
# --------------------------------------------------------------------------- #

def get_cost_report(campaign_id: str) -> dict:
    """Read per-agent-per-company cost hashes from Redis and return a structured report.

    Returns a dict with:
      companies: list of per-company breakdowns (sorted by total cost desc)
      agents:    list of per-agent totals across the campaign
      campaign:  overall totals
      missing:   True when Redis data is unavailable
    """
    r = _redis()
    if not r:
        return {"missing": True, "reason": "Redis unavailable"}

    try:
        usd_hash = r.hgetall(f"llm:r:usd:{campaign_id}") or {}
        tok_hash = r.hgetall(f"llm:r:tok:{campaign_id}") or {}
    except Exception as e:
        return {"missing": True, "reason": str(e)}

    if not usd_hash:
        return {"missing": False, "companies": [], "agents": [], "campaign": {"usd": 0.0, "tokens": 0}}

    # Decode Redis byte strings if necessary.
    def _dec(v):
        return v.decode() if isinstance(v, bytes) else str(v)

    rows: list[dict] = []
    for raw_field, raw_usd in usd_hash.items():
        field = _dec(raw_field)
        domain, agent = (field.split("|", 1) + ["—"])[:2]
        usd    = float(_dec(raw_usd) or 0)
        tokens = int(_dec(tok_hash.get(raw_field, b"0") or b"0"))
        rows.append({"domain": domain, "agent": agent, "usd": usd, "tokens": tokens})

    # ---- per-company view ----
    from collections import defaultdict
    by_company: dict[str, dict] = defaultdict(lambda: {"domain": "", "agents": {}, "usd": 0.0, "tokens": 0})
    for row in rows:
        dom = row["domain"]
        ag  = row["agent"]
        by_company[dom]["domain"] = dom
        by_company[dom]["agents"][ag] = {
            "usd":    round(row["usd"], 6),
            "tokens": row["tokens"],
        }
        by_company[dom]["usd"]    += row["usd"]
        by_company[dom]["tokens"] += row["tokens"]

    companies = sorted(
        [{"domain": v["domain"], "usd": round(v["usd"], 6), "tokens": v["tokens"], "agents": v["agents"]}
         for v in by_company.values()],
        key=lambda x: x["usd"],
        reverse=True,
    )

    # ---- per-agent view ----
    by_agent: dict[str, dict] = defaultdict(lambda: {"agent": "", "usd": 0.0, "tokens": 0})
    for row in rows:
        ag = row["agent"]
        by_agent[ag]["agent"]   = ag
        by_agent[ag]["usd"]    += row["usd"]
        by_agent[ag]["tokens"] += row["tokens"]

    agents = sorted(
        [{"agent": v["agent"], "usd": round(v["usd"], 6), "tokens": v["tokens"]}
         for v in by_agent.values()],
        key=lambda x: x["usd"],
        reverse=True,
    )

    total_usd    = round(sum(r["usd"]    for r in rows), 6)
    total_tokens = sum(r["tokens"] for r in rows)

    return {
        "missing":   False,
        "campaign":  {"usd": total_usd, "tokens": total_tokens},
        "companies": companies,
        "agents":    agents,
    }
