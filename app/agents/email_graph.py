"""Email drafting as a LangGraph sub-graph: pain-first cold email.

Architecture (2 focused LLM calls per compose cycle):
  compose  (reasoning LLM):
    Call 1 — structured output for the PAIN PLAN (8 fields).
              Plan fields only; no email text in schema so the model
              focuses entirely on strategic reasoning.
    Call 2 — plain-text email generation, grounded in the plan.
              No schema constraints = no field-omission edge cases.
  validate (no LLM):
    Deterministic checks — placeholders, banned openers, word count,
    greeting format, sign-off, sender-name-in-para-1-2 rule, and
    at least one ICP pain keyword in paragraph 1.

Typical cost: 2 LLM calls (plan + write). A bounded rewrite fires only
when deterministic validation fails (rare), making worst case 4 calls.
"""
from __future__ import annotations

import os
import re
from typing import List, Literal, Optional, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import StateGraph, START, END
from pydantic import BaseModel, Field

from app.core.llm import get_chat_llm
from app.core.logging_config import logger

MAX_ATTEMPTS = int(os.getenv("EMAIL_MAX_DRAFT_ATTEMPTS", "1"))

_BANNED_PHRASES = (
    "hope this finds you well", "hope this email finds you", "hope you are doing well",
    "hope you're doing well", "hope all is well", "i hope this", "reaching out to you today",
)


# ---------------------------------------------------------------------------
# Schema 1: PLAN ONLY (structured output call)
# Small schema = model reliably fills every field.
# Dropped from old schema: capability_bridge, value_proof, recipient_play
#   (all drove the sender-first paragraph that the team rejected).
# ---------------------------------------------------------------------------
class PainFirstPlan(BaseModel):
    primary_pain: str = Field(
        description="The single most specific, evidenced business pain this company has RIGHT NOW — "
                    "drawn from pain_hooks, matched_pains, and need_evidence. One concrete statement."
    )
    pain_evidence: str = Field(
        description="2-3 specific facts that PROVE this pain is real for THIS company. "
                    "Ground every word in research_summary, news_hooks, or growth_hooks."
    )
    pain_consequence: str = Field(
        description="What this pain is costing them. Frame as a recognisable industry pattern; "
                    "never invent a number about THIS company."
    )
    urgency_trigger: str = Field(
        description="Single growth/news hook making this pain urgent RIGHT NOW. Empty string if none."
    )
    sender_relevance: str = Field(
        description="ONE sentence: how the sender has solved this exact pain before. "
                    "Use sender_proof or matched_services. Proof, not a pitch."
    )
    ask: str = Field(
        description="Ultra-light CTA. Decision-owner → 'worth a quick chat?'. "
                    "Champion → 'does this resonate?'. Never ask for a 30-min call."
    )
    strategic_observation: str = Field(
        description="Core strategic insight powering this email. Kept for follow-up continuity."
    )
    tone: str = Field(
        description="C-suite = peer insight; VP/Director = direct, evidence-led; Manager = consultative."
    )


class Critique(BaseModel):
    """Kept for output-contract compatibility with drafting_service."""
    verdict: Literal["pass", "revise"]
    issues: List[str]
    score: int


# ---------------------------------------------------------------------------
# Graph state
# ---------------------------------------------------------------------------
class DraftState(TypedDict, total=False):
    ctx: dict
    strategy: dict
    draft: dict
    critique: dict
    attempts: int
    max_attempts: int


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------
_PLAN_PROMPT = ChatPromptTemplate.from_template(
    """You are a B2B cold email strategist. Analyse the target company intelligence below and
fill the strategic PLAN that will drive a pain-first cold email.
Every field must be grounded in the provided data — never invent facts.

SENDER (background only — referenced in sender_relevance field):
- Company: {sender_name}
- Services: {sender_services}
- Proof points: {sender_proof}
- Differentiators: {sender_advantages}

PROSPECT:
- Name: {prospect_first_name} | Title: {prospect_role} | Seniority: {prospect_seniority}
- Company: {target_company}
- Role in buying decision: {recipient_role_signal}

TARGET INTELLIGENCE:
- Research dossier: {research_summary}
- ICP pain signals: {pain_hooks}
- Growth hooks: {growth_hooks}
- News hooks: {news_hooks}
- Why now: {opportunity_reason}
- Evidenced need: {need_evidence}
- Pains we solve here: {matched_pains}
- Services that map: {matched_services}

CAMPAIGN OBJECTIVE: {objective}

{refine_block}"""
)

_WRITE_PROMPT = (
    "Write a cold email using the plan below. Output ONLY the two labelled blocks.\n\n"
    "PLAN:\n"
    "  Primary pain:      {primary_pain}\n"
    "  Evidence:          {pain_evidence}\n"
    "  Consequence:       {pain_consequence}\n"
    "  Urgency trigger:   {urgency_trigger}\n"
    "  Sender relevance:  {sender_relevance}\n"
    "  Ask:               {ask}\n"
    "  Tone:              {tone}\n\n"
    "EMAIL STRUCTURE — copy this layout exactly, filling in the <placeholders>:\n\n"
    "SUBJECT: <≤10-word subject line naming their pain or situation — NOT the sender's product>\n\n"
    "BODY:\n"
    "Hi {prospect_first_name},\n\n"
    "<Para 1: 2-3 sentences about the specific pain grounded in their reality. "
    "DO NOT mention {sender_name}.>\n\n"
    "<Para 2: 1-2 sentences on the consequence + urgency trigger if any. "
    "Still NO mention of {sender_name}.>\n\n"
    "<Para 3: 1 sentence of soft sender relevance. 1 ultra-light ask.>\n\n"
    "Best regards,\n\n"
    "{user_name}\n"
    "{sender_name}\n\n"
    "RULES (hard constraints):\n"
    "  - 100-150 words in the BODY total.\n"
    "  - {sender_name} must NOT appear in Para 1 or Para 2.\n"
    "  - No placeholders of any kind — fill with real facts or omit.\n"
    "  - No filler openers (hope this finds you, I am reaching out, etc.).\n"
    "  - Each paragraph is a single unbroken block of text (no line breaks within a paragraph).\n"
    "  - Blank line between every section as shown above.\n"
    "  - No sales jargon in the email text."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _reasoning_llm():
    return get_chat_llm("reasoning", timeout=60, max_retries=1)


def _split_paragraphs(body: str) -> List[str]:
    """Return content paragraphs (greeting and sign-off stripped)."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    return [
        p for p in paras
        if not re.match(r"^hi\s+\w+,?\s*$", p, re.I)
        and not re.match(r"^best\b", p, re.I)
        and not re.match(r"^regards\b", p, re.I)
    ]


def _parse_write_output(text: str) -> dict:
    """Extract subject and body from the plain-text write call output."""
    subject = ""
    body = ""
    m_subject = re.search(r"^SUBJECT:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
    if m_subject:
        subject = m_subject.group(1).strip()
    m_body = re.search(r"^BODY:[ \t]*([\s\S]+)", text, re.MULTILINE | re.IGNORECASE)
    if m_body:
        body = m_body.group(1).strip()
    return {"subject": subject, "body": body}


# ---------------------------------------------------------------------------
# Deterministic validation — no LLM.
# ---------------------------------------------------------------------------
_PLACEHOLDER_RE = re.compile(r"\[[^\]\n]+\]|\{[^}\n]+\}|<[A-Za-z][A-Za-z0-9 _/-]*>")

_PLAN_KEYS = (
    "primary_pain", "pain_evidence", "pain_consequence", "urgency_trigger",
    "sender_relevance", "ask", "strategic_observation", "tone",
)


def _deterministic_issues(draft: dict, ctx: dict) -> List[str]:
    subject = draft.get("subject", "") or ""
    body = draft.get("body", "") or ""
    blob = f"{subject}\n{body}"
    issues: List[str] = []

    # 1. No placeholders
    ph = _PLACEHOLDER_RE.search(blob)
    if ph:
        issues.append(f"Remove placeholder '{ph.group(0)}' — fill with a real value or omit it.")

    # 2. No banned filler openers
    low = blob.lower()
    for phrase in _BANNED_PHRASES:
        if phrase in low:
            issues.append(f"Remove the filler opener containing '{phrase}'.")
            break

    # 3. Word count
    words = len(body.split())
    if words < 80:
        issues.append(f"Body is too short ({words} words); aim for 100-150 words.")
    elif words > 200:
        issues.append(f"Body is too long ({words} words); tighten to 100-150 words.")

    # 4. Greeting format
    first = (ctx.get("prospect_first_name") or "").strip()
    if first and not re.search(rf"hi\s+{re.escape(first)}\b", body, re.IGNORECASE):
        issues.append(f"Open with the greeting 'Hi {first},' on its own line.")

    # 5. Sign-off present
    if "best regards" not in low:
        issues.append("Sign off with 'Best regards,' followed by sender name and company.")

    # 6. Sender name must NOT appear in paragraphs 1 or 2 (pain-first rule)
    sender = (ctx.get("sender_name") or "").strip().lower()
    if sender:
        content_paras = _split_paragraphs(body)
        early_paras = " ".join(content_paras[:2]).lower()
        if sender in early_paras:
            issues.append(
                f"'{ctx['sender_name']}' must not appear in paragraphs 1 or 2. "
                "Keep paragraphs 1-2 entirely about the prospect's pain."
            )

    # 7. At least one ICP pain signal must surface in paragraph 1
    pain_signals = []
    for field in ("pain_hooks", "matched_pains", "need_evidence"):
        raw = ctx.get(field) or ""
        pain_signals.extend(
            w.strip().lower() for w in re.split(r"[;,]", raw) if len(w.strip()) > 4
        )
    if pain_signals:
        content_paras = _split_paragraphs(body)
        if content_paras:
            para1_low = content_paras[0].lower()
            if not any(sig[:12] in para1_low for sig in pain_signals[:8]):
                issues.append(
                    "Paragraph 1 must reference a specific pain from the ICP research "
                    "(pain_hooks / matched_pains / need_evidence). Ground it in their reality."
                )

    return issues


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
def _node_compose(state: DraftState):
    ctx = state["ctx"]
    attempts = state.get("attempts", 0)

    refine_block = ""
    critique = state.get("critique")
    if critique and critique.get("issues"):
        issues_text = "\n".join(f"- {i}" for i in critique["issues"])
        refine_block = (
            "REVISION REQUIRED — your previous attempt failed these checks. "
            f"Fix ALL of them:\n{issues_text}\n"
        )

    llm = _reasoning_llm()

    # ── Call 1: Strategic Plan (structured output, small schema) ──────────
    plan_chain = _PLAN_PROMPT | llm.with_structured_output(PainFirstPlan)
    plan: PainFirstPlan = plan_chain.invoke({
        "sender_name":           ctx["sender_name"],
        "sender_services":       ctx["sender_services"],
        "sender_proof":          ctx.get("sender_proof", "N/A"),
        "sender_advantages":     ctx.get("sender_advantages", "N/A"),
        "prospect_first_name":   ctx["prospect_first_name"],
        "prospect_role":         ctx["prospect_role"],
        "prospect_seniority":    ctx["prospect_seniority"],
        "target_company":        ctx["target_company"],
        "recipient_role_signal": ctx.get("recipient_role_signal", "N/A"),
        "research_summary":      ctx["research_summary"],
        "growth_hooks":          ctx["growth_hooks"],
        "news_hooks":            ctx["news_hooks"],
        "pain_hooks":            ctx["pain_hooks"],
        "opportunity_reason":    ctx["opportunity_reason"],
        "need_evidence":         ctx.get("need_evidence", "N/A"),
        "matched_pains":         ctx.get("matched_pains", "N/A"),
        "matched_services":      ctx.get("matched_services", "N/A"),
        "objective":             ctx["objective"],
        "refine_block":          refine_block,
    })

    strategy = {k: getattr(plan, k, "") for k in _PLAN_KEYS}

    # ── Call 2: Write the email (plain text, no schema) ──────────────────
    write_prompt = _WRITE_PROMPT.format(
        primary_pain=plan.primary_pain,
        pain_evidence=plan.pain_evidence,
        pain_consequence=plan.pain_consequence,
        urgency_trigger=plan.urgency_trigger or "(none)",
        sender_relevance=plan.sender_relevance,
        ask=plan.ask,
        tone=plan.tone,
        prospect_first_name=ctx["prospect_first_name"],
        sender_name=ctx["sender_name"],
        user_name=ctx["user_name"],
    )
    write_messages = [
        SystemMessage(content="You are an elite B2B cold email ghostwriter. Follow all instructions precisely."),
        HumanMessage(content=write_prompt),
    ]
    write_response = llm.invoke(write_messages)
    draft = _parse_write_output(write_response.content)

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
        logger.info("[EMAIL-GRAPH] Max attempts reached; accepting best effort.")
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
