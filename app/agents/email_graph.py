"""Email drafting as a LangGraph sub-graph: Strategist -> Writer -> Critic -> (refine loop).

This runs entirely in-process inside a single Celery task (no checkpointer, no
cross-process state), so it is the natural home for a multi-step agent loop:

  strategist (reasoning) : pick the single strongest angle / hook / pain bridge
  writer     (cheap)     : write the email to spec (and fix critic feedback on refine)
  critic     (reasoning) : validate against a rubric; pass or request a revision

The loop is bounded (EMAIL_MAX_DRAFT_ATTEMPTS, default 2 writer passes). Every
LLM output is Pydantic-validated (Output Validation constraint). Mini models only,
routed via app.core.llm.
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

MAX_ATTEMPTS = int(os.getenv("EMAIL_MAX_DRAFT_ATTEMPTS", "2"))


# --------------------------------------------------------------------------- #
# Structured outputs                                                           #
# --------------------------------------------------------------------------- #
class StrategyBrief(BaseModel):
    hook: str = Field(description="The single most specific, evidenced fact/angle to lead with (from the research) — the entry point for the Identify-Pain.")
    pain_hypothesis: str = Field(description="The specific business pain (MEDDPICC: Identify Pain) the sender solves for this company, grounded in the evidence.")
    capability_bridge: str = Field(description="Which exact sender capability/service solves that pain, and the differentiation that fits their likely Decision Criteria.")
    value_proof: str = Field(description="ONE credible proof of value (MEDDPICC: Metrics) to include — a sender proof point, a quantified outcome, or a cost-of-inaction framed as a general pattern ('teams like yours typically…'). NEVER an invented company-specific number.")
    recipient_play: str = Field(description="How to pitch GIVEN this recipient's role vs the economic buyer/champion: if they own the decision, sell the business outcome and ask for a working session; if they're a champion/influencer, give them something forwardable and easy to escalate; if adjacent, ask to be pointed to the owner.")
    ask: str = Field(description="The single, low-friction call-to-action, calibrated to recipient_play (e.g. a 15-min discovery call, or 'who owns this on your side?').")
    strategic_observation: str = Field(description="The core strategic insight powering the email.")
    tone: str = Field(description="Tone guidance appropriate to the prospect's seniority/role.")


class DraftContent(BaseModel):
    subject: str = Field(description="High-impact, specific subject line (no placeholders).")
    body: str = Field(description="The full email body: greeting + 3 paragraphs + signature.")


class Critique(BaseModel):
    verdict: Literal["pass", "revise"] = Field(description="pass if the draft meets every rule; revise otherwise.")
    issues: List[str] = Field(description="Concrete, fixable problems. Empty if pass.")
    score: int = Field(description="0-100 overall quality score.")


# --------------------------------------------------------------------------- #
# Graph state                                                                  #
# --------------------------------------------------------------------------- #
class DraftState(TypedDict, total=False):
    ctx: dict          # gathered context (sender DNA, target intel, prospect, objective)
    strategy: dict     # StrategyBrief
    draft: dict        # DraftContent
    critique: dict     # Critique
    attempts: int
    max_attempts: int


# --------------------------------------------------------------------------- #
# Nodes                                                                        #
# --------------------------------------------------------------------------- #
_STRATEGIST_PROMPT = ChatPromptTemplate.from_template(
    """You are a B2B outreach strategist who plans cold emails using the MEDDPICC framework. Choose the
SINGLE strongest, fully-grounded angle for ONE cold email to this specific person.

SENDER:
- Company: {sender_name}
- Services: {sender_services}
- Capability -> pain map: {sender_map}
- Proof points / outcomes: {sender_proof}
- Differentiators: {sender_advantages}

PROSPECT (the recipient):
- Name: {prospect_name} | Title: {prospect_role} | Seniority: {prospect_seniority}
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

CAMPAIGN OBJECTIVE (the user's goal — the email must serve it): {objective}

Plan the email with MEDDPICC:
1. IDENTIFY PAIN -> pick the most specific, credible hook (a REAL fact from the intelligence, never generic)
   and the pain it implies that THIS sender solves.
2. CAPABILITY + DECISION CRITERIA -> the exact sender capability that bridges to it, leaning on the
   differentiator most relevant to their likely decision criteria.
3. METRICS -> one credible value_proof to include (a sender proof point or a value pattern). If no real
   number exists, frame it as a general pattern — NEVER invent a company-specific metric.
4. ECONOMIC BUYER vs CHAMPION -> compare THIS recipient's title/role to the likely economic buyer and
   champion, and set recipient_play + the ask altitude accordingly (decision-owner: business outcome +
   working session; champion/influencer: forwardable, easy to escalate; adjacent: ask for the right owner).
Output the brief only. Do not write the email yet. Everything must be grounded in the data above."""
)

_WRITER_PROMPT = ChatPromptTemplate.from_template(
    """You are an elite B2B ghostwriter. Write ONE highly personalized, professional cold email from this
MEDDPICC-grounded strategy. It must read like a sharp human wrote it for THIS person — never templated.

STRATEGY:
- Hook (lead with this real fact): {hook}
- Pain it implies (Identify Pain): {pain_hypothesis}
- Capability bridge (+ differentiation): {capability_bridge}
- Value proof to weave in (Metrics): {value_proof}
- How to pitch this recipient (role play): {recipient_play}
- The ask (calibrated): {ask}
- Tone: {tone}

DETAILS:
- Sender company: {sender_name} | Sender name: {user_name}
- Recipient first name: {prospect_first_name} | Title: {prospect_role} | Seniority: {prospect_seniority}
- Recipient company: {target_company}

{refine_block}

RULES:
- Structure: open with a greeting line "Hi {prospect_first_name}," on its own line followed by a blank
  line, then exactly 3 short paragraphs —
  (1) the hook anchored in their reality and the pain it implies,
  (2) the bridge to {sender_name}'s capability, reinforced by the value proof,
  (3) the single ask exactly as framed in 'The ask' / 'role play'.
- 110-160 words. Senior, direct, insight-led. Zero marketing fluff, zero clichés ("hope this finds you well").
- GROUNDING: state only facts present in the strategy. Do NOT invent metrics, momentum, or superlatives.
  If the value proof is a general pattern, phrase it as one ("teams like yours typically…") — never as a
  claim about THIS company's numbers.
- ABSOLUTELY NO PLACEHOLDERS or bracketed/curly tokens of ANY kind (no [Name], [Company], {{metric}}, <X>).
  Every value must be a real, spelled-out fact. If you don't have a specific detail, omit it — never bracket it.
- LINE BREAKS: write each paragraph as ONE continuous line with NO hard line breaks inside it (do not wrap
  lines at a fixed width — let the email client wrap). Separate the greeting and the 3 paragraphs with a
  single blank line (double line break) ONLY.
- Sign off EXACTLY, with a blank line after 'Best regards,' and a single line break between name and company:
  "Best regards,

  {user_name}
  {sender_name}"
"""
)

_CRITIC_PROMPT = ChatPromptTemplate.from_template(
    """You are a strict outreach QA reviewer. Judge this email against the rules.

CAMPAIGN OBJECTIVE: {objective}
TARGET RESEARCH (to confirm personalization is REAL, not invented): {research_summary}
PROSPECT: {prospect_role}, seniority {prospect_seniority}, at {target_company}
RECIPIENT'S ROLE IN THE DECISION: {recipient_role_signal}

EMAIL SUBJECT: {subject}
EMAIL BODY:
{body}

Fail (verdict=revise) if ANY of these are violated:
1. Personalization is generic or not grounded in the research (no specific fact tied to the prospect's company).
2. Contains ANY placeholder or bracketed/curly token ([..], {{..}}, <..>), an unfilled detail, or
   "Hope this finds you well"-type filler.
3. Does not serve the campaign objective.
4. Wrong sign-off format, more than one ask, or clearly over/under length (target ~110-160 words).
5. Tone or ask mismatched to the recipient's role (e.g. asking a non-owner to "buy", or pitching ROI to
   someone who should be asked to forward/point to the owner).
6. States a MEDDPICC hypothesis as a fact (asserts who the buyer is, their decision/paper process, or an
   invented company-specific metric). Value must be a sender proof point or a clearly general pattern.
7. Has hard line breaks INSIDE a paragraph (a paragraph must be one continuous line; only blank lines
   separate sections), or exposes any internal framework jargon (MEDDPICC, "economic buyer", "champion").
List concrete issues to fix. Give an overall score 0-100. verdict=pass only if it is genuinely send-ready."""
)


def _reasoning_llm():
    # Background drafting: cap tail latency (60s x 1 retry) so a slow provider can't
    # stall a draft (3-5 calls) for minutes — matters now that prospects draft concurrently.
    return get_chat_llm("reasoning", timeout=60, max_retries=1)


def _writer_llm():
    # Deterministic prose (temperature is forced to 0 by get_chat_llm for
    # reproducibility — same draft inputs always yield the same email).
    return get_chat_llm("writer", timeout=60, max_retries=1)


def _node_strategist(state: DraftState):
    ctx = state["ctx"]
    chain = _STRATEGIST_PROMPT | _reasoning_llm().with_structured_output(StrategyBrief)
    brief: StrategyBrief = chain.invoke({
        "sender_name": ctx["sender_name"],
        "sender_services": ctx["sender_services"],
        "sender_map": ctx["sender_map"],
        "sender_proof": ctx.get("sender_proof", "N/A"),
        "sender_advantages": ctx.get("sender_advantages", "N/A"),
        "prospect_name": ctx["prospect_name"],
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
    })
    return {"strategy": brief.model_dump()}


def _node_writer(state: DraftState):
    ctx = state["ctx"]
    strat = state["strategy"]
    attempts = state.get("attempts", 0)

    refine_block = ""
    critique = state.get("critique")
    if critique and critique.get("issues"):
        issues = "\n".join(f"- {i}" for i in critique["issues"])
        refine_block = (
            "REVISION REQUIRED — the previous draft failed review. Fix exactly these issues "
            f"while keeping what worked:\n{issues}"
        )

    chain = _WRITER_PROMPT | _writer_llm().with_structured_output(DraftContent)
    draft: DraftContent = chain.invoke({
        "hook": strat["hook"],
        "pain_hypothesis": strat["pain_hypothesis"],
        "capability_bridge": strat["capability_bridge"],
        "value_proof": strat.get("value_proof", "N/A"),
        "recipient_play": strat.get("recipient_play", ""),
        "ask": strat.get("ask", "Ask for a short discovery call."),
        "tone": strat["tone"],
        "sender_name": ctx["sender_name"],
        "user_name": ctx["user_name"],
        "prospect_first_name": ctx["prospect_first_name"],
        "prospect_role": ctx["prospect_role"],
        "prospect_seniority": ctx["prospect_seniority"],
        "target_company": ctx["target_company"],
        "refine_block": refine_block,
    })
    return {"draft": draft.model_dump(), "attempts": attempts + 1}


def _node_critic(state: DraftState):
    ctx = state["ctx"]
    draft = state["draft"]
    chain = _CRITIC_PROMPT | _reasoning_llm().with_structured_output(Critique)
    critique: Critique = chain.invoke({
        "objective": ctx["objective"],
        "research_summary": ctx["research_summary"],
        "prospect_role": ctx["prospect_role"],
        "prospect_seniority": ctx["prospect_seniority"],
        "target_company": ctx["target_company"],
        "recipient_role_signal": ctx.get("recipient_role_signal", "N/A"),
        "subject": draft["subject"],
        "body": draft["body"],
    })
    crit = critique.model_dump()

    # Deterministic guard for the two recurring failure modes — runs regardless of
    # what the LLM critic said, so neither can slip through to a sent email:
    #   (1) placeholder/bracketed tokens ([Name], {metric}, <X>),
    #   (2) the subject line carrying a placeholder too.
    blob = f"{draft.get('subject','')}\n{draft.get('body','')}"
    ph = re.search(r"\[[^\]\n]+\]|\{[^}\n]+\}|<[A-Za-z][A-Za-z0-9 _/-]*>", blob)
    if ph:
        crit["verdict"] = "revise"
        crit["issues"] = (crit.get("issues") or []) + [
            f"Remove the placeholder token '{ph.group(0)}' — fill it with a real value or omit it entirely."
        ]
        crit["score"] = min(crit.get("score", 0) or 0, 40)
    return {"critique": crit}


def _route_after_critic(state: DraftState):
    critique = state.get("critique") or {}
    if critique.get("verdict") == "pass":
        return END
    if state.get("attempts", 0) >= state.get("max_attempts", MAX_ATTEMPTS):
        logger.info("[EMAIL-GRAPH] Max draft attempts reached; accepting best effort.")
        return END
    return "writer"


_compiled = None


def get_email_graph():
    global _compiled
    if _compiled is None:
        builder = StateGraph(DraftState)
        builder.add_node("strategist", _node_strategist)
        builder.add_node("writer", _node_writer)
        builder.add_node("critic", _node_critic)
        builder.add_edge(START, "strategist")
        builder.add_edge("strategist", "writer")
        builder.add_edge("writer", "critic")
        builder.add_conditional_edges("critic", _route_after_critic, ["writer", END])
        _compiled = builder.compile()
    return _compiled
