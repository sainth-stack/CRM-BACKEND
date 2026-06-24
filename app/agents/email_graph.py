"""Email drafting as a LangGraph sub-graph: strategy-first cold email.

Architecture (2 focused LLM calls per compose cycle):
  compose:
    Call 1 — Strategist (structured output → EmailStrategy).
              Ranks signals, applies freshness filter, selects ONE hook /
              ONE pain / ONE service / ONE proof. No email copy produced here.
    Call 2 — Writer (plain text).
              Executes the strategy verbatim. No research, no decisions.
  validate (no LLM):
    Deterministic checks — placeholders, banned openers, word count,
    greeting format, sign-off, sender-name-in-para-1-2 rule, and
    at least one ICP pain keyword in paragraph 1.

Typical cost: 2 LLM calls (strategise + write).
Worst case:   4 LLM calls (one bounded retry when deterministic validation fails).
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

MAX_ATTEMPTS = int(os.getenv("EMAIL_MAX_DRAFT_ATTEMPTS", "2"))

_BANNED_PHRASES = (
    "hope this finds you well", "hope this email finds you", "hope you are doing well",
    "hope you're doing well", "hope all is well", "i hope this", "reaching out to you today",
    "it's interesting to consider", "i'm curious how this might", "i wanted to reach out",
    "i wanted to follow", "touching base",
)

# Generic openers that make para 1 sound like a mass-blast, not a targeted email.
_GENERIC_PARA1_OPENERS = (
    "in a high-mix", "in a custom", "in the manufacturing", "in the machinery",
    "as a manufacturer", "as a custom", "companies in your", "in your industry",
    "in the [", "as a [", "in a [",
)


# ---------------------------------------------------------------------------
# Schema: EmailStrategy  (output of the strategist call)
# ---------------------------------------------------------------------------

class SelectedHook(BaseModel):
    hook_type: Literal["recent_news", "growth_signal", "need_evidence", "pain_hook", "research_insight"] = Field(
        description=(
            "Category of the selected hook. "
            "recent_news: ONLY when the news item contains an EXPLICIT date AND is ≤ 30 days old. "
            "growth_signal: concrete expansion event (new facility, new market, headcount growth). "
            "need_evidence: specific grounded observation from MEDDPICC need_evidence. "
            "pain_hook: specific operational pain from pain_hooks. "
            "research_insight: last resort — most specific fact from research_summary."
        )
    )
    value: str = Field(
        description=(
            "The specific hook text — a concrete named event or fact the prospect will immediately "
            "recognise as real. Never generic ('as a manufacturer', 'companies like yours')."
        )
    )
    source_date: Optional[str] = Field(
        default=None,
        description=(
            "ISO date (YYYY-MM-DD or YYYY-MM) extracted verbatim from the signal. "
            "Required when hook_type is recent_news. Null for all other types."
        )
    )


class SelectedPain(BaseModel):
    value: str = Field(
        description=(
            "The single most relevant pain for this company — framed as inference, never assertion. "
            "'Companies expanding production capacity often run into X' is correct. "
            "'You have X problem' is wrong."
        )
    )
    evidence_source: str = Field(
        description="Which data field this came from: matched_pains, need_evidence, pain_hooks, or inferred_from_hook."
    )


class SelectedService(BaseModel):
    value: str = Field(
        description=(
            "The ONE sender service or capability that directly addresses the selected pain. "
            "Must come from matched_services or sender_services. Never list multiple."
        )
    )


class SelectedProof(BaseModel):
    value: str = Field(
        description=(
            "ONE proof point copied verbatim from sender_proof. "
            "Set to 'N/A' if sender_proof is empty. Never invent a statistic or outcome."
        )
    )


class EmailStrategy(BaseModel):
    persona_focus: Literal[
        "operational_efficiency", "revenue_growth",
        "financial_outcomes", "strategic_growth", "technical_capability"
    ] = Field(
        description=(
            "The lens through which this prospect evaluates solutions, based on role + seniority. "
            "C-suite/Owner → strategic_growth. VP/SVP (revenue domain) → revenue_growth. "
            "VP/SVP (ops domain) → operational_efficiency. Director/Head → operational_efficiency or financial_outcomes. "
            "Manager → technical_capability or operational_efficiency. Finance roles → financial_outcomes."
        )
    )
    selected_hook: SelectedHook
    selected_pain: SelectedPain
    selected_service: SelectedService
    selected_proof: SelectedProof
    pain_consequence: str = Field(
        description=(
            "One sentence framing what this pain costs — written as a recognisable industry pattern, "
            "never a claim about THIS company. No invented numbers. "
            "Example: 'Teams going through rapid expansion often find visibility across sites becomes the first thing that breaks.'"
        )
    )
    email_goal: Literal["book_discovery", "generate_curiosity"] = Field(
        description=(
            "book_discovery: use when prospect is VP+ OR pain signal is urgent/evidenced. "
            "generate_curiosity: use for Manager-level OR when signals are weak or generic."
        )
    )
    reasoning: List[str] = Field(
        description=(
            "2-4 short bullets explaining WHY each selection was made. "
            "Be specific — stored for analytics (hook type → reply rate, pain → meeting rate)."
        )
    )


# ---------------------------------------------------------------------------
# Kept for output-contract compatibility with drafting_service
# ---------------------------------------------------------------------------
class Critique(BaseModel):
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
_STRATEGIST_PROMPT = ChatPromptTemplate.from_template(
    """You are a B2B outreach message strategist. Your ONLY job is to analyse all available
signals and produce a precise EmailStrategy. Do NOT write any email copy.
The writer will execute your strategy without doing any further research or decisions.

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

SIGNALS (each on its own line — read carefully before selecting):
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
- Current month: {current_month}  ← use this to calculate signal freshness

CAMPAIGN OBJECTIVE: {objective}

═══════════════════════════════════════════════════
SELECTION RULES — execute in order, pick exactly ONE per category:
═══════════════════════════════════════════════════

STEP 1 — HOOK SELECTION (use the highest-priority type that has a qualifying signal):

  Priority 1 — recent_news:
    ✓ Use ONLY when: the news item contains an EXPLICIT date (year+month or full date).
    ✓ Use ONLY when: the date is ≤ 30 days before {current_month}.
      - ≤ 30 days old → qualifying
      - > 30 days old → DISCARD, do not use under any circumstances
    ✗ If NO news item has an explicit date → skip this type entirely.
    ✗ If all dated news is > 30 days old → skip this type entirely.
    If multiple qualify, pick the most recent.

  Priority 2 — growth_signal:
    A concrete named expansion event: new facility, new market entry, headcount growth,
    new product launch, acquisition, new contract/certification.
    Must be specific — not 'company is growing'.

  Priority 3 — need_evidence:
    A specific, grounded observation from MEDDPICC need_evidence.
    Must be concrete — not a generic pain category.

  Priority 4 — pain_hook:
    A specific operational pain from pain_hooks.
    The hook VALUE for this type must still be grounded in a specific company characteristic
    (its product type, certification, customer sector, operational model, or a named process).
    NEVER use a generic industry statement like 'high-mix environments often have scheduling issues'.
    Example of WRONG: "High-mix production environments create scheduling complexity."
    Example of RIGHT: "InsulTech's custom-engineered thermal blanket model means every order is
    built to spec — that's a fundamentally different scheduling problem than standard product lines."

  Priority 5 — research_insight:
    Last resort. The single most specific operational characteristic from
    research_summary or description. Never use a generic industry statement.
    Must name something unique to THIS company — a certification, process, customer sector,
    product type, or company age/tenure fact — not an industry generality.

STEP 2 — PAIN SELECTION:
  Choose the ONE pain most logically connected to the selected hook.
  Prefer matched_pains or need_evidence. Otherwise infer from the hook context.
  Frame as inference — NEVER assertion ('you have X problem').

STEP 3 — SERVICE MAPPING:
  Choose ONE sender service from matched_services or sender_services that directly
  addresses the selected pain. If multiple qualify, pick the tightest fit.

STEP 4 — PROOF SELECTION:
  Copy ONE proof point verbatim from sender_proof.
  The proof MUST logically connect to the selected_service — if the proof is about
  downtime reduction but the service is scheduling analytics, choose a different proof
  or set value to 'N/A'. A mismatched proof is worse than no proof.
  If sender_proof is empty or 'N/A', set value to 'N/A'. Never fabricate.

STEP 5 — PERSONA FRAMING:
  Map role + seniority to persona_focus:
  C-suite / Owner / President / Founder     → strategic_growth
  VP / SVP / EVP (sales, revenue, growth)  → revenue_growth
  VP / SVP / EVP (ops, supply, engineering) → operational_efficiency
  Director / Head of (any domain)           → operational_efficiency or financial_outcomes
  Manager (any domain)                      → technical_capability or operational_efficiency
  Finance (CFO, Controller, FP&A)           → financial_outcomes

STEP 6 — EMAIL GOAL:
  book_discovery   → prospect is VP+ OR pain signal is urgent/evidenced
  generate_curiosity → Manager-level OR signals are weak / generic

STEP 7 — REASONING (stored for analytics):
  Write 2-4 short bullets: WHY this hook, WHY this pain, WHY this service, WHY this proof.
  For proof: explicitly state whether it connects to the selected service and why.

ABSOLUTE CONSTRAINTS:
- If no news hook has an explicit date → hook_type must NOT be recent_news.
- Select exactly ONE value for every field. No lists, no 'or', no alternatives.
- Never invent a fact, date, statistic, or outcome.
- pain_consequence is an industry pattern — never a claim about this specific company.
- If no strong signal exists, use research_insight with the most specific available fact.
  Never leave selected_hook empty.
- Hook value for pain_hook and research_insight must reference something specific to THIS
  company — never a generic "companies like yours..." or "in this industry..." framing.

{refine_block}"""
)

_WRITE_INSTRUCTIONS = """\
You are an elite B2B cold email ghostwriter.
You have been handed a complete strategy. Execute it precisely.
Do NOT do any research. Do NOT make any decisions. Write only.

STRATEGY:
  Persona focus : {persona_focus}
  Hook ({hook_type}): {hook_value}
  Pain          : {pain_value}
  Pain consequence: {pain_consequence}
  Sender service: {service_value}
  Proof         : {proof_value}
  Email goal    : {email_goal}

PROSPECT : {prospect_first_name} at {target_company}
SENDER   : {sender_name}
SIGNED BY: {user_name}

══════════════════════════════════════════
EMAIL STRUCTURE — reproduce this layout exactly:
══════════════════════════════════════════

SUBJECT: <≤10-word subject line about their situation or trigger — NOT about the sender's product>

BODY:
Hi {prospect_first_name},

<Para 1 — 2-3 sentences.
 Open with the hook: name the specific company fact or event the prospect will recognise.
 The very first word or phrase must ground the reader in {target_company}'s specific reality —
 name the company, a specific certification, product type, customer sector, or named process.
 NEVER open with a generic industry framing: 'In a high-mix environment...', 'As a manufacturer...',
 'Companies in your space...', 'In the [industry] sector...' are all BANNED openers.
 Then draw ONE inference about what that means operationally — frame as observation, not accusation.
 NEVER assert a problem as fact. DO NOT mention {sender_name} anywhere in this paragraph.>

<Para 2 — 1-2 sentences.
 Use pain_consequence as an industry pattern — 'Companies going through X often find...'
 or 'The challenge in these situations is usually...'
 No invented statistics or percentages.
 Still NO mention of {sender_name} in this paragraph.>

<Para 3 — 2 sentences max.
 Sentence 1: one line of soft sender relevance connecting {sender_name} to the pain.
 Sentence 2: the CTA — calibrated to email_goal:
   book_discovery    → 'Worth a quick conversation?' OR 'Would that be worth 15 minutes?'
   generate_curiosity → 'Curious if that's on your radar this year?' OR 'How are you approaching that today?'
 NEVER write 'does this resonate?'>

Best regards,

{user_name}
{sender_name}

══════════════════════════════════════════
HARD RULES — any violation triggers a rewrite:
══════════════════════════════════════════
1.  100-150 words in BODY total (greeting line + sign-off count toward the total).
2.  {sender_name} must NOT appear in Para 1 or Para 2.
3.  No placeholders of any kind ([X], {{Y}}, <Z>) — fill with real text or omit entirely.
4.  BANNED openers for Para 1: 'In a [industry/environment]...', 'As a [company type]...',
    'Companies in your space...', 'In the [sector]...' — Para 1 must open with a specific
    company fact, not a generic industry frame.
5.  BANNED phrases anywhere: 'hope this finds you', 'I am reaching out', 'touching base',
    'it's interesting to consider', 'I'm curious how this might', 'I wanted to'.
6.  Pain is inference only — NEVER assert as fact.
7.  NEVER write 'does this resonate?'
8.  NEVER invent a statistic, percentage, or dollar figure not present in proof_value.
9.  If proof_value is 'N/A' — omit proof entirely. Do not reference it.
10. Each paragraph is one unbroken block of text. No line breaks inside a paragraph.
11. Blank line between every section (greeting / para 1 / para 2 / para 3 / sign-off).
12. Refer to the sender company ONLY as '{sender_name}'. NEVER use any descriptive label,
    category, or alternative name sourced from research data (e.g. 'At Data Quality Consulting',
    'the data consultancy', 'your data partner'). The exact string '{sender_name}' is the ONLY
    permitted name for the sender.\
"""


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


def _strategy_to_dict(s: EmailStrategy) -> dict:
    return {
        "persona_focus":        s.persona_focus,
        "hook_type":            s.selected_hook.hook_type,
        "hook_value":           s.selected_hook.value,
        "hook_source_date":     s.selected_hook.source_date,
        "pain_value":           s.selected_pain.value,
        "pain_evidence_source": s.selected_pain.evidence_source,
        "service_value":        s.selected_service.value,
        "proof_value":          s.selected_proof.value,
        "pain_consequence":     s.pain_consequence,
        "email_goal":           s.email_goal,
        "reasoning":            s.reasoning,
        # legacy keys — kept for follow-up drafter compatibility
        "strategic_observation": s.selected_pain.value,
        "pain_hypothesis":       s.selected_pain.value,
        "hook":                  s.selected_hook.value,
    }


# ---------------------------------------------------------------------------
# Deterministic validation — no LLM
# ---------------------------------------------------------------------------
_PLACEHOLDER_RE = re.compile(r"\[[^\]\n]+\]|\{[^}\n]+\}|<[A-Za-z][A-Za-z0-9 _/-]*>")


def _deterministic_issues(draft: dict, ctx: dict) -> List[str]:
    subject = draft.get("subject", "") or ""
    body    = draft.get("body", "") or ""
    blob    = f"{subject}\n{body}"
    issues: List[str] = []

    # 1. No placeholders
    ph = _PLACEHOLDER_RE.search(blob)
    if ph:
        issues.append(f"Remove placeholder '{ph.group(0)}' — fill with a real value or omit it.")

    # 2. No banned phrases anywhere in the email
    low = blob.lower()
    for phrase in _BANNED_PHRASES:
        if phrase in low:
            issues.append(f"Remove banned phrase '{phrase}' — it makes the email sound AI-generated.")
            break

    # 3. Word count — floor raised to 95 to catch sub-100 drafts
    words = len(body.split())
    if words < 95:
        issues.append(f"Body is too short ({words} words); must be 100-150 words.")
    elif words > 200:
        issues.append(f"Body is too long ({words} words); tighten to 100-150 words.")

    # 4. Greeting format
    first = (ctx.get("prospect_first_name") or "").strip()
    if first and not re.search(rf"hi\s+{re.escape(first)}\b", body, re.IGNORECASE):
        issues.append(f"Open with the greeting 'Hi {first},' on its own line.")

    # 5. Sign-off present
    if "best regards" not in low:
        issues.append("Sign off with 'Best regards,' followed by sender name and company.")

    # 6. Sender name must NOT appear in paragraphs 1 or 2
    sender = (ctx.get("sender_name") or "").strip().lower()
    content_paras = _split_paragraphs(body)
    if sender:
        early = " ".join(content_paras[:2]).lower()
        if sender in early:
            issues.append(
                f"'{ctx['sender_name']}' must not appear in paragraphs 1 or 2. "
                "Keep those paragraphs entirely about the prospect's world."
            )

    # 7. Para 1 must not open with a generic industry framing
    content_paras_check = content_paras
    if content_paras_check:
        para1_start = content_paras_check[0].lower()[:80]
        for opener in _GENERIC_PARA1_OPENERS:
            if para1_start.startswith(opener):
                issues.append(
                    f"Para 1 opens with a generic industry frame ('{opener}...'). "
                    "Start with a specific company fact, certification, product type, or named event."
                )
                break

    # 8. At least one ICP pain keyword in paragraph 1
    pain_signals: List[str] = []
    for field in ("pain_hooks", "matched_pains", "need_evidence"):
        raw = ctx.get(field) or ""
        pain_signals.extend(
            w.strip().lower() for w in re.split(r"[;,\n\-•]", raw) if len(w.strip()) > 4
        )
    if pain_signals:
        if content_paras:
            para1_low = content_paras[0].lower()
            if not any(sig[:12] in para1_low for sig in pain_signals[:10]):
                issues.append(
                    "Paragraph 1 must reference a specific pain from the ICP research "
                    "(pain_hooks / matched_pains / need_evidence). Ground it in their reality."
                )

    # 8. 'does this resonate?' is banned
    if "does this resonate" in low:
        issues.append("Remove 'does this resonate?' — use a natural engagement question instead.")

    return issues


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
def _node_compose(state: DraftState):
    ctx      = state["ctx"]
    attempts = state.get("attempts", 0)

    refine_block  = ""
    writer_revise = ""
    critique = state.get("critique")
    if critique and critique.get("issues"):
        issues_text  = "\n".join(f"- {i}" for i in critique["issues"])
        refine_block = (
            "REVISION REQUIRED — your previous attempt failed these checks. "
            f"Fix ALL of them before producing the new draft:\n{issues_text}\n"
        )
        # Writer also gets the issues directly so word-count / banned-phrase
        # violations are corrected at the prose level, not just the plan level.
        writer_revise = (
            "\nREVISION NOTES — your previous draft failed validation. Fix every issue below:\n"
            + issues_text + "\n"
        )

    llm = _reasoning_llm()

    # ── Call 1: Strategist (structured output → EmailStrategy) ───────────
    strategist_chain = _STRATEGIST_PROMPT | llm.with_structured_output(EmailStrategy)
    strategy: EmailStrategy = strategist_chain.invoke({
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

    strategy_dict = _strategy_to_dict(strategy)

    # ── Call 2: Writer (plain text, no schema) ────────────────────────────
    write_prompt = _WRITE_INSTRUCTIONS.format(
        persona_focus      = strategy.persona_focus,
        hook_type          = strategy.selected_hook.hook_type,
        hook_value         = strategy.selected_hook.value,
        pain_value         = strategy.selected_pain.value,
        pain_consequence   = strategy.pain_consequence,
        service_value      = strategy.selected_service.value,
        proof_value        = strategy.selected_proof.value,
        email_goal         = strategy.email_goal,
        prospect_first_name= ctx["prospect_first_name"],
        target_company     = ctx["target_company"],
        sender_name        = ctx["sender_name"],
        user_name          = ctx["user_name"],
    ) + writer_revise
    write_response = llm.invoke([
        SystemMessage(content="You are an elite B2B cold email ghostwriter. Follow all instructions precisely."),
        HumanMessage(content=write_prompt),
    ])
    draft = _parse_write_output(write_response.content)

    return {"strategy": strategy_dict, "draft": draft, "attempts": attempts + 1}


def _node_validate(state: DraftState):
    issues  = _deterministic_issues(state.get("draft") or {}, state.get("ctx") or {})
    verdict = "pass" if not issues else "revise"
    return {"critique": {"verdict": verdict, "issues": issues, "score": 90 if not issues else 40}}


def _route_after_validate(state: DraftState):
    critique = state.get("critique") or {}
    if critique.get("verdict") == "pass":
        return END
    if state.get("attempts", 0) >= state.get("max_attempts", MAX_ATTEMPTS):
        logger.info("[EMAIL-GRAPH] Max attempts reached; accepting best-effort draft.")
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
