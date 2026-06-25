"""
Single initial email drafter as a LangGraph sub-graph.

Architecture (2 LLM calls per prospect, 4 worst-case with retry):
  Call 1 — Sequence Strategist (structured output → Email1Plan).
            Plans the initial cold email: hook, pain, proof, CTA style.

  Call 2 — Email 1 Writer (plain text, prospect-centric cold email).

  Validate (no LLM) — deterministic checks.

On validation failure: only the writer is retried. The plan is preserved.

Follow-up and breakup emails are drafted on-demand after intent classification,
not upfront. See ghostwriter_worker.draft_followup_worker.
"""
from __future__ import annotations

import os
import re
from datetime import date
from typing import List, Literal, Optional, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import StateGraph, START, END
from pydantic import BaseModel, Field

from app.core.llm import get_chat_llm
from app.core.logging_config import logger

SEQ_MAX_ATTEMPTS = int(os.getenv("EMAIL_MAX_DRAFT_ATTEMPTS", "2"))

_BANNED_PHRASES = (
    "hope this finds you well", "hope this email finds you",
    "hope you are doing well", "hope you're doing well",
    "hope all is well", "i hope this", "reaching out to you today",
    "it's interesting to consider", "it's intriguing",
    "i'm curious how", "i wanted to reach out",
    "touching base", "just checking in",
    "per my previous", "as per my last",
    "would you be open to", "i'd love to connect",
    "let's schedule", "let's hop on",
)

_GENERIC_PARA1_OPENERS = (
    "in a high-mix", "in a custom", "in the manufacturing",
    "as a manufacturer", "as a custom", "companies in your",
    "in your industry", "in the [", "as a [", "in a [",
)


# ─────────────────────────────────────────────────────────────────────────────
# Schema: Email1Plan
# ─────────────────────────────────────────────────────────────────────────────

class Email1Plan(BaseModel):
    persona_focus: Literal[
        "operational_efficiency", "revenue_growth",
        "financial_outcomes", "strategic_growth", "technical_capability"
    ] = Field(description="Lens through which this prospect evaluates solutions.")

    hook_type: Literal[
        "recent_news", "growth_signal", "need_evidence", "pain_hook", "research_insight"
    ] = Field(description=(
        "recent_news: ONLY when news has explicit date ≤ 12 months from current_month. "
        "growth_signal: named expansion event. need_evidence: specific MEDDPICC observation. "
        "pain_hook: specific operational pain grounded in THIS company's characteristics. "
        "research_insight: last resort — most specific company fact from research/description."
    ))
    hook_value: str = Field(description=(
        "The specific hook — a concrete company fact the prospect will recognise. "
        "Must name something specific to THIS company. Never a generic industry statement."
    ))
    hook_source_date: Optional[str] = Field(
        default=None,
        description="ISO date extracted verbatim from the signal. Required only for recent_news."
    )
    primary_pain: str = Field(description=(
        "The single most relevant pain — framed as inference, never assertion. "
        "'Companies doing X often face Y' not 'You have Y problem'."
    ))
    pain_evidence_source: str = Field(
        description="Which field: matched_pains, need_evidence, pain_hooks, or inferred_from_hook."
    )
    selected_service: str = Field(
        description="ONE sender service that directly addresses the primary pain."
    )
    selected_proof: str = Field(
        description="ONE proof point verbatim from sender_proof. 'N/A' if none available. Never fabricate."
    )
    pain_consequence: str = Field(description=(
        "One sentence — industry pattern for what this pain costs. "
        "Never a claim about THIS company. No invented numbers."
    ))
    email_goal: Literal["book_discovery", "generate_curiosity"] = Field(
        description="book_discovery: VP+ or urgent pain. generate_curiosity: Manager or weak signals."
    )
    reasoning: List[str] = Field(description="2-4 bullets: WHY each selection. Stored for analytics.")


# ─────────────────────────────────────────────────────────────────────────────
# Graph state
# ─────────────────────────────────────────────────────────────────────────────

class SequenceState(TypedDict, total=False):
    ctx: dict
    plan: dict
    email1: dict
    critiques: dict       # {1: {verdict, issues, score}}
    write_attempts: int
    max_attempts: int


# ─────────────────────────────────────────────────────────────────────────────
# Prompt templates
# ─────────────────────────────────────────────────────────────────────────────

_SEQUENCE_STRATEGIST_PROMPT = ChatPromptTemplate.from_template(
    """You are a B2B outbound email strategist. Plan the initial cold email to send to this prospect.
The email answers: "Why are you contacting me?" — prospect-centric, no sales pitch, no product dump.

SENDER:
- Company: {sender_name}
- Services: {sender_services}
- Proof points: {sender_proof}
- Differentiators: {sender_advantages}

PROSPECT:
- Name: {prospect_first_name} | Title: {prospect_role} | Seniority: {prospect_seniority}
- Company: {target_company}
- Role in buying decision: {recipient_role_signal}

COMPANY INTELLIGENCE:
- Research summary: {research_summary}
- Description: {description}
- Company type / sub-vertical: {company_type}
- Employee count: {employee_count}
- Location: {location}

SIGNALS (evaluate each individually):
News hooks (include date if present):
{news_hooks}

Growth hooks:
{growth_hooks}

Pain hooks:
{pain_hooks}

MEDDPICC:
- Need evidence: {need_evidence}
- Matched pains: {matched_pains}
- Matched services: {matched_services}

RUNTIME:
- Current month: {current_month}

CAMPAIGN OBJECTIVE: {objective}

═══════════════════════════════════════════════════
EMAIL PLANNING — PROSPECT-CENTRIC INITIAL OUTREACH
═══════════════════════════════════════════════════

HOOK SELECTION (priority order — pick the highest that qualifies):

  Priority 1 — recent_news:
    ✓ ONLY when news has EXPLICIT date AND ≤ 12 months from {current_month}.
    ✓ Calculate months_old. If > 12 months OR no date → skip entirely.
    If multiple qualify, pick the most recent.

  Priority 2 — growth_signal:
    Named expansion: new facility, new market, headcount growth, acquisition, certification.
    Must name a specific event — not 'company is growing'.

  Priority 3 — need_evidence:
    Specific grounded MEDDPICC observation. Not a generic pain category.

  Priority 4 — pain_hook:
    MUST ground hook_value in a company-specific fact (product type, certification,
    customer sector, process, or named characteristic). NEVER use a generic industry
    statement. WRONG: "High-mix environments have scheduling issues."
    RIGHT: "{target_company}'s custom-engineered product model means every order is
    built to spec — a fundamentally different scheduling problem than standard lines."

  Priority 5 — research_insight:
    Most specific fact from research/description. Name something unique to this company.

PAIN SELECTION: ONE pain logically connected to the hook. Inference, not assertion.
SERVICE MAPPING: ONE sender service addressing the pain.
PROOF SELECTION: ONE proof verbatim from sender_proof aligned to selected_service.
  If proof is about downtime but service is scheduling analytics → choose different proof or N/A.
PERSONA FRAMING: C-suite → strategic_growth. VP/SVP → revenue_growth or operational_efficiency.
  Director/Head → operational_efficiency or financial_outcomes. Manager → technical_capability.
EMAIL GOAL: book_discovery (VP+ or urgent pain) / generate_curiosity (Manager or weak signals).

ABSOLUTE CONSTRAINTS:
- No news hook without an explicit date ≤ 30 days → hook_type ≠ recent_news.
- Never invent facts, dates, statistics, or outcomes.
- Hook value for pain_hook / research_insight must reference something specific to THIS company.

{refine_block}"""
)


_EMAIL1_WRITE = """\
You are an elite B2B cold email ghostwriter.
Execute the strategy below precisely. No research. No decisions. Write only.

STRATEGY:
  Persona focus    : {persona_focus}
  Hook ({hook_type}): {hook_value}
  Pain             : {primary_pain}
  Pain consequence : {pain_consequence}
  Sender service   : {selected_service}
  Proof            : {selected_proof}
  Email goal       : {email_goal}

PROSPECT : {prospect_first_name} at {target_company}
SENDER   : {sender_name}
SIGNED BY: {user_name}

OUTPUT FORMAT:
SUBJECT: <≤10-word subject about their situation — NOT the sender's product>

BODY:
Hi {prospect_first_name},

<Para 1 — 2-3 sentences.
 Start with a specific company fact the prospect will recognise.
 BANNED openers: "In a [industry]...", "As a [type]...", "Companies in your space..."
 Draw ONE operational inference — observation + curiosity. Never assert a problem as fact.
 DO NOT mention {sender_name}.>

<Para 2 — 1-2 sentences.
 Use pain_consequence as an industry pattern.
 "Companies going through X often find..." or "The challenge in these situations is usually..."
 No invented statistics. Still NO mention of {sender_name}.>

<Para 3 — 2 sentences max.
 Sentence 1: soft sender relevance — one line connecting {sender_name} to the pain.
 Sentence 2: CTA.
   book_discovery    → "Worth a quick conversation?" or "Would that be worth 15 minutes?"
   generate_curiosity → "Curious if that's on your radar this year?" or "How are you approaching that today?"
 NEVER "does this resonate?">

Best regards,

{user_name}
{sender_name}

HARD RULES:
1.  100-150 words in BODY.
2.  {sender_name} NOT in Para 1 or Para 2.
3.  No placeholders.
4.  No filler: "hope this finds you", "I am reaching out", "touching base", "it's interesting to consider".
5.  Pain is inference — never assert as fact.
6.  NEVER "does this resonate?"
7.  Never invent a statistic not in proof_value.
8.  If proof is "N/A" — omit proof entirely.
9.  Each paragraph one unbroken block. Blank line between sections.
10. Refer to the sender ONLY as '{sender_name}'. NEVER use any descriptive label, category,
    or alternative name sourced from research data (e.g. 'At Data Quality Consulting',
    'the data consultancy'). The exact string '{sender_name}' is the only permitted sender name.
{revision_notes}\
"""


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

# Module-level singleton — one ChatOpenAI (and its httpx connection pool) shared
# across all concurrent drafts in this process. The old pattern (get_chat_llm
# called inside each node function) created a fresh ChatOpenAI per node call:
# at STAGE6_CONCURRENCY=20 with 4 calls per draft that's 80 httpx clients per
# batch, each carrying its own SSL context and connection pool — unnecessary
# RAM pressure and GC churn on t2.medium.
_DRAFT_LLM = get_chat_llm("reasoning", timeout=60, max_retries=1)


def _llm():
    return _DRAFT_LLM


def _parse_output(text: str) -> dict:
    """Extract SUBJECT and BODY from plain-text writer output."""
    subject   = ""
    body      = ""
    m_subject = re.search(r"^SUBJECT:\s*(.+)$", text, re.MULTILINE | re.IGNORECASE)
    if m_subject:
        subject = m_subject.group(1).strip()

    m_body = re.search(r"^BODY:[ \t]*([\s\S]+)", text, re.MULTILINE | re.IGNORECASE)
    if m_body:
        body = m_body.group(1).strip()
    elif m_subject:
        body = text[m_subject.end():].strip()

    return {"subject": subject, "body": body}


def _split_paragraphs(body: str) -> List[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    return [
        p for p in paras
        if not re.match(r"^hi\s+\w+,?\s*$", p, re.I)
        and not re.match(r"^best\b", p, re.I)
        and not re.match(r"^regards\b", p, re.I)
    ]


def _plan_to_dict(p: Email1Plan) -> dict:
    return {
        "e1_persona_focus":     p.persona_focus,
        "e1_hook_type":         p.hook_type,
        "e1_hook_value":        p.hook_value,
        "e1_hook_source_date":  p.hook_source_date,
        "e1_primary_pain":      p.primary_pain,
        "e1_pain_evidence_src": p.pain_evidence_source,
        "e1_selected_service":  p.selected_service,
        "e1_selected_proof":    p.selected_proof,
        "e1_pain_consequence":  p.pain_consequence,
        "e1_email_goal":        p.email_goal,
        "e1_reasoning":         p.reasoning,
        # Legacy compat keys for ghostwriter_worker
        "strategic_observation": p.primary_pain,
        "pain_hypothesis":       p.primary_pain,
        "hook":                  p.hook_value,
    }


# ─────────────────────────────────────────────────────────────────────────────
# News freshness guard
# ─────────────────────────────────────────────────────────────────────────────

from app.core.config import settings as _settings
_NEWS_CUTOFF_MONTHS = _settings.NEWS_CUTOFF_MONTHS

_DATE_PATTERNS = [
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"), lambda m: date(int(m[0]), int(m[1]), int(m[2]))),
    (re.compile(r"^(\d{4})-(\d{2})$"),          lambda m: date(int(m[0]), int(m[1]), 1)),
    (re.compile(r"^(\d{4})$"),                   lambda m: date(int(m[0]), 1, 1)),
]

def _parse_hook_date(raw: str) -> Optional[date]:
    """Return a date object from a hook_source_date string, or None if unparseable."""
    if not raw:
        return None
    raw = raw.strip()
    for pattern, extractor in _DATE_PATTERNS:
        m = pattern.match(raw)
        if m:
            try:
                return extractor(m.groups())
            except ValueError:
                return None
    return None


def news_is_within_cutoff(hook_source_date: Optional[str], current_month: str) -> bool:
    """Return True iff the news date is within _NEWS_CUTOFF_MONTHS of current_month.

    current_month must be in YYYY-MM format (e.g. '2026-06').
    Returns False if either date is missing or unparseable.
    """
    if not hook_source_date:
        return False
    news_date = _parse_hook_date(hook_source_date)
    if news_date is None:
        return False
    try:
        cy, cm = int(current_month[:4]), int(current_month[5:7])
    except (ValueError, IndexError):
        return False
    current = date(cy, cm, 1)
    # months_old = how many calendar months back the news date is
    months_old = (current.year - news_date.year) * 12 + (current.month - news_date.month)
    return 0 <= months_old <= _NEWS_CUTOFF_MONTHS


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic validators
# ─────────────────────────────────────────────────────────────────────────────

_PLACEHOLDER_RE = re.compile(r"\[[^\]\n]+\]|\{[^}\n]+\}|<[A-Za-z][A-Za-z0-9 _/-]*>")


def _base_issues(draft: dict, min_words: int, max_words: int) -> List[str]:
    subject = draft.get("subject", "") or ""
    body    = draft.get("body", "") or ""
    blob    = f"{subject}\n{body}"
    issues: List[str] = []

    ph = _PLACEHOLDER_RE.search(blob)
    if ph:
        issues.append(f"Remove placeholder '{ph.group(0)}' — fill or omit.")

    low = blob.lower()
    for phrase in _BANNED_PHRASES:
        if phrase in low:
            issues.append(f"Remove banned phrase '{phrase}'.")
            break

    words = len(body.split())
    if words < min_words:
        issues.append(f"Body too short ({words} words); must be {min_words}-{max_words} words.")
    elif words > max_words + 30:
        issues.append(f"Body too long ({words} words); tighten to {min_words}-{max_words} words.")

    if "best regards" not in low:
        issues.append("Sign off with 'Best regards,' followed by sender name and company.")

    return issues


def _email1_issues(draft: dict, ctx: dict, plan: dict) -> List[str]:
    body   = draft.get("body", "") or ""
    issues = _base_issues(draft, min_words=95, max_words=160)

    first = (ctx.get("prospect_first_name") or "").strip()
    if first and not re.search(rf"hi\s+{re.escape(first)}\b", body, re.IGNORECASE):
        issues.append(f"Open with greeting 'Hi {first},' on its own line.")

    sender = (ctx.get("sender_name") or "").strip().lower()
    content_paras = _split_paragraphs(body)
    if sender and content_paras:
        early = " ".join(content_paras[:2]).lower()
        if sender in early:
            issues.append(f"'{ctx['sender_name']}' must not appear in Para 1 or Para 2.")

    if content_paras:
        para1_start = content_paras[0].lower()[:80]
        for opener in _GENERIC_PARA1_OPENERS:
            if para1_start.startswith(opener):
                issues.append(
                    f"Para 1 opens with generic frame ('{opener}...'). "
                    "Start with a specific company fact."
                )
                break

    if "does this resonate" in body.lower():
        issues.append("Remove 'does this resonate?' — use a natural question.")

    return issues


# ─────────────────────────────────────────────────────────────────────────────
# Nodes
# ─────────────────────────────────────────────────────────────────────────────

def _node_plan(state: SequenceState) -> dict:
    """Call 1: Sequence Strategist — plans the initial email."""
    ctx = state["ctx"]

    critiques = state.get("critiques") or {}
    all_issues = []
    for email_num, crit in critiques.items():
        for issue in (crit.get("issues") or []):
            all_issues.append(f"Email {email_num}: {issue}")
    refine_block = ""
    if all_issues:
        issues_text  = "\n".join(f"- {i}" for i in all_issues)
        refine_block = (
            "REVISION REQUIRED — previous attempt failed. Adjust your plan to fix:\n"
            + issues_text + "\n"
        )

    llm = _llm()
    chain = _SEQUENCE_STRATEGIST_PROMPT | llm.with_structured_output(Email1Plan)
    plan: Email1Plan = chain.invoke({
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
        "description":           ctx.get("description", "N/A"),
        "company_type":          ctx.get("company_type", "N/A"),
        "employee_count":        ctx.get("employee_count", "N/A"),
        "location":              ctx.get("location", "N/A"),
        "news_hooks":            ctx["news_hooks"],
        "growth_hooks":          ctx["growth_hooks"],
        "pain_hooks":            ctx["pain_hooks"],
        "need_evidence":         ctx.get("need_evidence", "N/A"),
        "matched_pains":         ctx.get("matched_pains", "N/A"),
        "matched_services":      ctx.get("matched_services", "N/A"),
        "current_month":         ctx["current_month"],
        "objective":             ctx["objective"],
        "refine_block":          refine_block,
    })

    plan_dict = _plan_to_dict(plan)

    # Deterministic guard: if the LLM chose recent_news but the date is stale or missing,
    # demote to growth_signal so the writer never sees a stale news hook.
    if plan_dict.get("e1_hook_type") == "recent_news":
        if not news_is_within_cutoff(plan_dict.get("e1_hook_source_date"), ctx.get("current_month", "")):
            logger.warning(
                "[SEQ-GRAPH] LLM chose recent_news but date '%s' is outside 12-month window — "
                "demoting to growth_signal.", plan_dict.get("e1_hook_source_date")
            )
            plan_dict["e1_hook_type"] = "growth_signal"
            plan_dict["e1_hook_source_date"] = None

    return {"plan": plan_dict}


def _node_write_emails(state: SequenceState) -> dict:
    """Call 2: Write Email 1 from the plan."""
    ctx            = state["ctx"]
    plan           = state.get("plan") or {}
    llm            = _llm()
    write_attempts = state.get("write_attempts", 0) + 1

    critiques = state.get("critiques") or {}

    def _revision(email_num: int) -> str:
        crit = critiques.get(email_num) or {}
        issues = crit.get("issues") or []
        if not issues:
            return ""
        return (
            "\nREVISION — fix every issue below:\n"
            + "\n".join(f"- {i}" for i in issues) + "\n"
        )

    e1_prompt = _EMAIL1_WRITE.format(
        persona_focus      = plan.get("e1_persona_focus", ""),
        hook_type          = plan.get("e1_hook_type", ""),
        hook_value         = plan.get("e1_hook_value", ""),
        primary_pain       = plan.get("e1_primary_pain", ""),
        pain_consequence   = plan.get("e1_pain_consequence", ""),
        selected_service   = plan.get("e1_selected_service", ""),
        selected_proof     = plan.get("e1_selected_proof", "N/A"),
        email_goal         = plan.get("e1_email_goal", "generate_curiosity"),
        prospect_first_name= ctx["prospect_first_name"],
        target_company     = ctx["target_company"],
        sender_name        = ctx["sender_name"],
        user_name          = ctx["user_name"],
        revision_notes     = _revision(1),
    )
    e1_resp = llm.invoke([
        SystemMessage(content="You are an elite B2B cold email ghostwriter. Follow all instructions."),
        HumanMessage(content=e1_prompt),
    ])
    email1 = _parse_output(e1_resp.content)

    return {"email1": email1, "write_attempts": write_attempts}


def _node_validate(state: SequenceState) -> dict:
    ctx    = state.get("ctx") or {}
    plan   = state.get("plan") or {}
    email1 = state.get("email1") or {}

    i1 = _email1_issues(email1, ctx, plan)

    critiques = {
        1: {"verdict": "pass" if not i1 else "revise", "issues": i1, "score": 90 if not i1 else 40},
    }
    return {"critiques": critiques}


def _route_after_validate(state: SequenceState):
    critiques      = state.get("critiques") or {}
    all_pass       = all(c.get("verdict") == "pass" for c in critiques.values())
    write_attempts = state.get("write_attempts", 0)
    if all_pass:
        return END
    if write_attempts >= state.get("max_attempts", SEQ_MAX_ATTEMPTS):
        logger.info(f"[SEQ-GRAPH] Max write attempts ({write_attempts}) reached; accepting best-effort draft.")
        return END
    return "write_emails"


# ─────────────────────────────────────────────────────────────────────────────
# Graph assembly
# ─────────────────────────────────────────────────────────────────────────────

_compiled_seq = None


def get_sequence_graph():
    global _compiled_seq
    if _compiled_seq is None:
        builder = StateGraph(SequenceState)
        builder.add_node("strategise",   _node_plan)
        builder.add_node("write_emails", _node_write_emails)
        builder.add_node("validate",     _node_validate)
        builder.add_edge(START,           "strategise")
        builder.add_edge("strategise",    "write_emails")
        builder.add_edge("write_emails",  "validate")
        builder.add_conditional_edges(
            "validate",
            _route_after_validate,
            ["write_emails", END],
        )
        _compiled_seq = builder.compile()
    return _compiled_seq
