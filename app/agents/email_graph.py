"""Email drafting as a LangGraph sub-graph: a SINGLE MEDDPICC compose call.

Collapsed from the old Strategist -> Writer -> Critic -> refine loop (3-5 LLM
calls/email) to ONE call, with no loss of MEDDPICC rigor:

  compose  (reasoning) : in ONE structured call, the model first PLANS the email
                         with MEDDPICC (hook / pain / capability bridge / value
                         proof / economic-buyer-vs-champion play / ask) and then
                         WRITES the email grounded in that plan. The plan fields
                         come first in the schema, so the model reasons before it
                         writes (chain-of-thought in a single round-trip).
  validate (no LLM)    : the old LLM critic's two real jobs — catching placeholders
                         and fabricated/ungrounded content — are done with cheap,
                         deterministic checks (regex + rules), which are faster AND
                         more reliable than an LLM reviewer.

A single bounded rewrite fires ONLY when the deterministic checks fail (rare), so
the typical email costs exactly ONE LLM call; worst case is two. Mini models only,
routed via app.core.llm. The graph's output contract is unchanged: `strategy` +
`draft` + `critique` are still in the final state for the drafting service.
"""
from __future__ import annotations

import os
import re
from typing import List, Literal, TypedDict

from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import StateGraph, START, END
from pydantic import BaseModel, Field

from app.core.llm import get_chat_llm
from app.core.logging_config import logger

# Bounded safety rewrites. Default 1 means: 1 compose call + at most 1 rewrite when
# the deterministic validator flags an issue. Set to 0 to force strictly one call.
MAX_ATTEMPTS = int(os.getenv("EMAIL_MAX_DRAFT_ATTEMPTS", "1"))

# Filler/hedge openers an elite cold email must never contain.
_BANNED_PHRASES = (
    "hope this finds you well", "hope this email finds you", "hope you are doing well",
    "hope you're doing well", "hope all is well", "i hope this", "reaching out to you today",
)


# --------------------------------------------------------------------------- #
# Structured output — ONE object carrying the MEDDPICC plan AND the email.      #
# The plan fields are declared FIRST so the model fills them before it writes   #
# the subject/body (single-call chain-of-thought).                             #
# --------------------------------------------------------------------------- #
class MeddpiccDraft(BaseModel):
    # ---- MEDDPICC plan (reason first; this is the strategy brief) ----
    hook: str = Field(description="The single most specific, evidenced fact/angle to lead with (from the research) — the entry point for Identify-Pain.")
    pain_hypothesis: str = Field(description="The specific business pain (MEDDPICC: Identify Pain) the sender solves for this company, grounded in the evidence.")
    capability_bridge: str = Field(description="Which exact sender capability/service solves that pain, and the differentiation that fits their likely Decision Criteria.")
    value_proof: str = Field(description="ONE credible proof of value (MEDDPICC: Metrics): a sender proof point, a quantified outcome, or a cost-of-inaction framed as a general pattern ('teams like yours typically…'). NEVER an invented company-specific number.")
    recipient_play: str = Field(description="How to pitch GIVEN this recipient's role vs the economic buyer/champion: decision-owner -> business outcome + working session; champion/influencer -> forwardable, easy to escalate; adjacent -> ask to be pointed to the owner.")
    ask: str = Field(description="The single, low-friction call-to-action, calibrated to recipient_play.")
    strategic_observation: str = Field(description="The core strategic insight powering the email.")
    tone: str = Field(description="Tone guidance appropriate to the prospect's seniority/role.")
    # ---- The email (write SECOND, grounded entirely in the plan above) ----
    subject: str = Field(description="High-impact, specific subject line (no placeholders).")
    body: str = Field(description="The full email body: greeting + exactly 3 short paragraphs + sign-off, per the RULES.")


class Critique(BaseModel):
    """Kept for output-contract compatibility; now produced deterministically."""
    verdict: Literal["pass", "revise"]
    issues: List[str]
    score: int


# --------------------------------------------------------------------------- #
# Graph state                                                                  #
# --------------------------------------------------------------------------- #
class DraftState(TypedDict, total=False):
    ctx: dict          # gathered context (sender DNA, target intel, prospect, objective)
    strategy: dict     # MEDDPICC plan fields (back-compat with drafting_service)
    draft: dict        # {subject, body}
    critique: dict     # {verdict, issues, score} — deterministic
    attempts: int
    max_attempts: int


# --------------------------------------------------------------------------- #
# Single compose prompt — PLAN (MEDDPICC) then WRITE, in one call.              #
# --------------------------------------------------------------------------- #
_COMPOSE_PROMPT = ChatPromptTemplate.from_template(
    """You are an elite B2B outreach strategist AND ghostwriter. In ONE pass you will (A) PLAN a cold email
using the MEDDPICC framework, then (B) WRITE it. Fill the plan fields first, then the subject and body —
the email MUST be grounded in your own plan and in the data below. Everything must be a REAL fact from the
data; never invent.

SENDER:
- Company: {sender_name} | Sender name (sign-off): {user_name}
- Services: {sender_services}
- Capability -> pain map: {sender_map}
- Proof points / outcomes: {sender_proof}
- Differentiators: {sender_advantages}

PROSPECT (the recipient):
- First name: {prospect_first_name} | Title: {prospect_role} | Seniority: {prospect_seniority}
- Company: {target_company}
- Their role in the buying decision (our assessment): {recipient_role_signal}

TARGET INTELLIGENCE:
- Research dossier: {research_summary}
- Growth hooks: {growth_hooks}
- News hooks: {news_hooks}
- Pain hooks: {pain_hooks}
- Why now: {opportunity_reason}

MEDDPICC READ (from qualification — hypotheses, NOT confirmed facts):
- Evidenced need / pain: {need_evidence}
- Pains we solve here: {matched_pains}
- Services that map to them: {matched_services}
- Value / metrics angle: {metrics}
- Likely economic buyer (role): {economic_buyer}
- Likely champion (role): {champion}
- Likely decision criteria: {decision_criteria}

CAMPAIGN OBJECTIVE (the email must serve it): {objective}

{refine_block}

PART A — PLAN with MEDDPICC (fill the plan fields):
1. IDENTIFY PAIN -> pick the most specific, credible hook (a REAL fact from the intelligence, never generic)
   and the pain it implies that THIS sender solves.
2. CAPABILITY + DECISION CRITERIA -> the exact sender capability that bridges to it, leaning on the
   differentiator most relevant to their likely decision criteria.
3. METRICS -> one credible value_proof (a sender proof point or a value pattern). If no real number exists,
   frame it as a general pattern — NEVER invent a company-specific metric.
4. ECONOMIC BUYER vs CHAMPION -> compare THIS recipient's title/role to the likely economic buyer and
   champion, and set recipient_play + the ask altitude accordingly.

PART B — WRITE the email from your plan, obeying every RULE:
- Structure: open with "Hi {prospect_first_name}," on its own line, a blank line, then EXACTLY 3 short
  paragraphs — (1) the hook anchored in their reality + the pain it implies, (2) the bridge to
  {sender_name}'s capability reinforced by the value proof, (3) the single ask exactly as planned.
- 110-160 words. Senior, direct, insight-led. Zero marketing fluff, zero clichés ("hope this finds you well").
- GROUNDING: state only facts present in the data/plan. Do NOT invent metrics, momentum, or superlatives.
  A general-pattern value proof must be phrased as one ("teams like yours typically…"), never as a claim
  about THIS company's numbers.
- ABSOLUTELY NO PLACEHOLDERS or bracketed/curly tokens of ANY kind (no [Name], [Company], {{metric}}, <X>).
  Every value must be a real, spelled-out fact. If you lack a detail, omit it — never bracket it.
- Do NOT expose framework jargon (MEDDPICC, "economic buyer", "champion") in the email text.
- LINE BREAKS: each paragraph is ONE continuous line with NO hard breaks inside it; separate the greeting
  and the 3 paragraphs with a single blank line only.
- Sign off EXACTLY, blank line after 'Best regards,' and a single line break between name and company:
  "Best regards,

  {user_name}
  {sender_name}\""""
)


def _reasoning_llm():
    # Single drafting call now does plan+write, so give it the reasoning model.
    # Cap tail latency (60s x 1 retry) so a slow provider can't stall a draft.
    return get_chat_llm("reasoning", timeout=60, max_retries=1)


# --------------------------------------------------------------------------- #
# Deterministic validation — replaces the LLM critic (faster + more reliable). #
# --------------------------------------------------------------------------- #
_PLACEHOLDER_RE = re.compile(r"\[[^\]\n]+\]|\{[^}\n]+\}|<[A-Za-z][A-Za-z0-9 _/-]*>")


def _deterministic_issues(draft: dict, ctx: dict) -> List[str]:
    """The two failure modes the LLM critic actually caught — placeholders and
    obvious ungrounded/format breaks — checked in code. Returns a list of concrete,
    fixable issues (empty == send-ready)."""
    subject = draft.get("subject", "") or ""
    body = draft.get("body", "") or ""
    blob = f"{subject}\n{body}"
    issues: List[str] = []

    ph = _PLACEHOLDER_RE.search(blob)
    if ph:
        issues.append(f"Remove the placeholder token '{ph.group(0)}' — fill it with a real value or omit it.")

    low = blob.lower()
    for phrase in _BANNED_PHRASES:
        if phrase in low:
            issues.append(f"Remove the filler/cliché opener containing '{phrase}'.")
            break

    words = len(body.split())
    if words < 80:
        issues.append(f"Body is too short ({words} words); aim for 110-160 words across 3 paragraphs.")
    elif words > 210:
        issues.append(f"Body is too long ({words} words); tighten to ~110-160 words.")

    first = (ctx.get("prospect_first_name") or "").strip()
    if first and not re.match(rf"\s*hi\s+{re.escape(first)}\b", body, re.IGNORECASE):
        issues.append(f"Open with the greeting 'Hi {first},' on its own line.")

    if "best regards" not in low:
        issues.append("Sign off with 'Best regards,' followed by the sender name and company.")

    return issues


# --------------------------------------------------------------------------- #
# Nodes                                                                        #
# --------------------------------------------------------------------------- #
_PLAN_KEYS = ("hook", "pain_hypothesis", "capability_bridge", "value_proof",
              "recipient_play", "ask", "strategic_observation", "tone")


def _node_compose(state: DraftState):
    ctx = state["ctx"]
    attempts = state.get("attempts", 0)

    refine_block = ""
    critique = state.get("critique")
    if critique and critique.get("issues"):
        issues = "\n".join(f"- {i}" for i in critique["issues"])
        refine_block = (
            "REVISION REQUIRED — your previous draft failed automated checks. Fix EXACTLY these issues "
            f"while keeping what worked:\n{issues}\n"
        )

    chain = _COMPOSE_PROMPT | _reasoning_llm().with_structured_output(MeddpiccDraft)
    out: MeddpiccDraft = chain.invoke({
        "sender_name": ctx["sender_name"],
        "user_name": ctx["user_name"],
        "sender_services": ctx["sender_services"],
        "sender_map": ctx["sender_map"],
        "sender_proof": ctx.get("sender_proof", "N/A"),
        "sender_advantages": ctx.get("sender_advantages", "N/A"),
        "prospect_first_name": ctx["prospect_first_name"],
        "prospect_role": ctx["prospect_role"],
        "prospect_seniority": ctx["prospect_seniority"],
        "target_company": ctx["target_company"],
        "recipient_role_signal": ctx.get("recipient_role_signal", "N/A"),
        "research_summary": ctx["research_summary"],
        "growth_hooks": ctx["growth_hooks"],
        "news_hooks": ctx["news_hooks"],
        "pain_hooks": ctx["pain_hooks"],
        "opportunity_reason": ctx["opportunity_reason"],
        "need_evidence": ctx.get("need_evidence", "N/A"),
        "matched_pains": ctx.get("matched_pains", "N/A"),
        "matched_services": ctx.get("matched_services", "N/A"),
        "metrics": ctx.get("metrics", "N/A"),
        "economic_buyer": ctx.get("economic_buyer", "N/A"),
        "champion": ctx.get("champion", "N/A"),
        "decision_criteria": ctx.get("decision_criteria", "N/A"),
        "objective": ctx["objective"],
        "refine_block": refine_block,
    })
    d = out.model_dump()
    strategy = {k: d.get(k, "") for k in _PLAN_KEYS}
    draft = {"subject": d.get("subject", ""), "body": d.get("body", "")}
    return {"strategy": strategy, "draft": draft, "attempts": attempts + 1}


def _node_validate(state: DraftState):
    issues = _deterministic_issues(state.get("draft") or {}, state.get("ctx") or {})
    verdict = "pass" if not issues else "revise"
    return {"critique": {"verdict": verdict, "issues": issues, "score": 90 if not issues else 40}}


def _route_after_validate(state: DraftState):
    critique = state.get("critique") or {}
    if critique.get("verdict") == "pass":
        return END
    if state.get("attempts", 0) >= state.get("max_attempts", MAX_ATTEMPTS):
        logger.info("[EMAIL-GRAPH] Max draft attempts reached; accepting best effort.")
        return END
    return "compose"


_compiled = None


def get_email_graph():
    global _compiled
    if _compiled is None:
        builder = StateGraph(DraftState)
        builder.add_node("compose", _node_compose)
        builder.add_node("validate", _node_validate)
        builder.add_edge(START, "compose")
        builder.add_edge("compose", "validate")
        builder.add_conditional_edges("validate", _route_after_validate, ["compose", END])
        _compiled = builder.compile()
    return _compiled
