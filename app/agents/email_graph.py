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
    hook: str = Field(description="The single most specific fact/angle to lead with (from the research).")
    pain_hypothesis: str = Field(description="The specific business pain this outreach addresses.")
    capability_bridge: str = Field(description="Which sender capability solves that pain, and how.")
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
    """You are a B2B outreach strategist. Choose the SINGLE strongest angle for one cold email.

SENDER:
- Company: {sender_name}
- Services: {sender_services}
- Capability -> pain map: {sender_map}

PROSPECT:
- Name: {prospect_name} | Title: {prospect_role} | Seniority: {prospect_seniority}
- Company: {target_company}

TARGET INTELLIGENCE:
- Research dossier: {research_summary}
- Growth hooks: {growth_hooks}
- News hooks: {news_hooks}
- Pain hooks: {pain_hooks}
- Why now: {opportunity_reason}

CAMPAIGN OBJECTIVE (the user's goal — the email must serve it): {objective}

Pick the most specific, credible hook (a real fact from the intelligence — never generic),
the pain it implies, the exact sender capability that bridges to it, the core strategic
observation, and the tone to use for this seniority. Do not write the email yet."""
)

_WRITER_PROMPT = ChatPromptTemplate.from_template(
    """You are an elite B2B ghostwriter. Write ONE cold email from this strategy.

STRATEGY:
- Hook: {hook}
- Pain hypothesis: {pain_hypothesis}
- Capability bridge: {capability_bridge}
- Tone: {tone}

DETAILS:
- Sender company: {sender_name} | Sender name: {user_name}
- Recipient first name: {prospect_first_name} | Title: {prospect_role} | Seniority: {prospect_seniority}
- Recipient company: {target_company}

{refine_block}

RULES:
- Structure: a greeting line, then exactly 3 short paragraphs — (1) the hook anchored in their reality,
  (2) the bridge to {sender_name}'s capability solving the pain, (3) a single low-friction discovery-call ask.
- 100-150 words. Senior, direct, insight-led. Zero marketing fluff.
- NO placeholders or bracketed tokens of any kind.
- Each paragraph is one continuous string; double line breaks between sections only.
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

EMAIL SUBJECT: {subject}
EMAIL BODY:
{body}

Fail (verdict=revise) if ANY of these are violated:
1. Personalization is generic or not grounded in the research (no specific fact tied to the prospect's company).
2. Contains placeholders/bracketed tokens, or "Hope this finds you well"-type filler.
3. Does not serve the campaign objective.
4. Wrong sign-off format, or more than one ask, or clearly over/under length (target ~100-150 words).
5. Tone mismatched to seniority.
List concrete issues to fix. Give an overall score 0-100. verdict=pass only if it is genuinely send-ready."""
)


def _reasoning_llm():
    return get_chat_llm("reasoning", timeout=90)


def _writer_llm():
    # Slight temperature for natural prose variety; the Critic gates quality.
    return get_chat_llm("writer", timeout=90, temperature=0.6)


def _node_strategist(state: DraftState):
    ctx = state["ctx"]
    chain = _STRATEGIST_PROMPT | _reasoning_llm().with_structured_output(StrategyBrief)
    brief: StrategyBrief = chain.invoke({
        "sender_name": ctx["sender_name"],
        "sender_services": ctx["sender_services"],
        "sender_map": ctx["sender_map"],
        "prospect_name": ctx["prospect_name"],
        "prospect_role": ctx["prospect_role"],
        "prospect_seniority": ctx["prospect_seniority"],
        "target_company": ctx["target_company"],
        "research_summary": ctx["research_summary"],
        "growth_hooks": ctx["growth_hooks"],
        "news_hooks": ctx["news_hooks"],
        "pain_hooks": ctx["pain_hooks"],
        "opportunity_reason": ctx["opportunity_reason"],
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
        "subject": draft["subject"],
        "body": draft["body"],
    })
    return {"critique": critique.model_dump()}


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
