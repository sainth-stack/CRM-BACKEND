import os
from typing import Literal

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from app.core.logging_config import logger


Confidence = Literal["low", "medium", "high"]


class ReviewedTextField(BaseModel):
    original: str = Field(description="The sanitized value supplied by the user.")
    corrected: str = Field(description="The corrected value, or the original value if no safe correction exists.")
    changed: bool = Field(description="Whether the value changed.")
    confidence: Confidence = Field(description="Confidence in the correction.")
    clarification_needed: bool = Field(description="Whether this field requires user clarification.")
    reason: str = Field(description="Short reason for the decision.")


class ReviewedPromptField(BaseModel):
    original: str = Field(description="The sanitized prompt supplied by the user.")
    enhanced: str = Field(description="A clearer prompt using only user-provided information.")
    changed: bool = Field(description="Whether the prompt changed.")
    confidence: Confidence = Field(description="Confidence in the enhancement.")
    clarification_needed: bool = Field(description="Whether this prompt requires user clarification.")
    reason: str = Field(description="Short reason for the decision.")


class InputValidationOverall(BaseModel):
    status: Literal["success", "needs_clarification"]
    requires_user_clarification: bool
    clarification_questions: list[str] = Field(default_factory=list)


class InputValidationReview(BaseModel):
    target_industry: ReviewedTextField
    target_location: ReviewedTextField
    prompt: ReviewedPromptField
    overall: InputValidationOverall


class InputValidationService:
    """
    The single Stage 1 input-validation agent.

    Reviews exactly three user inputs — target_industry, target_location, and the
    campaign prompt. It corrects spelling/typos in industry and location and
    rewrites the prompt when it is not strong enough for the campaign, WITHOUT
    inferring missing campaign facts. Corrected values are persisted by the caller.
    """

    def __init__(self):
        self.model_name = os.getenv("INPUT_VALIDATION_MODEL", "gpt-4o-mini")

    def _safe_passthrough(
        self,
        *,
        target_industry: str,
        target_location: str,
        prompt: str,
        reason: str,
    ) -> InputValidationReview:
        return InputValidationReview(
            target_industry=ReviewedTextField(
                original=target_industry,
                corrected=target_industry,
                changed=False,
                confidence="low",
                clarification_needed=False,
                reason=reason,
            ),
            target_location=ReviewedTextField(
                original=target_location,
                corrected=target_location,
                changed=False,
                confidence="low",
                clarification_needed=False,
                reason=reason,
            ),
            prompt=ReviewedPromptField(
                original=prompt,
                enhanced=prompt,
                changed=False,
                confidence="low",
                clarification_needed=False,
                reason=reason,
            ),
            overall=InputValidationOverall(
                status="success",
                requires_user_clarification=False,
                clarification_questions=[],
            ),
        )

    def review_inputs(
        self,
        *,
        target_industry: str,
        target_location: str,
        prompt: str | None,
        sender_website: str | None = None,
    ) -> InputValidationReview:
        prompt = prompt or ""
        website_value = (sender_website or "").strip() or "none"

        if not os.getenv("OPENAI_API_KEY"):
            return self._safe_passthrough(
                target_industry=target_industry,
                target_location=target_location,
                prompt=prompt,
                reason="Input review model unavailable; preserved sanitized user-provided value without assumptions.",
            )

        # Synchronous call on the HTTP request path: cap tail latency so a slow
        # provider can't hold a web worker for minutes. Worst case ~30s × 2 attempts
        # instead of 120s × 3. Happy path (gpt-4o-mini ~1-3s) is unchanged.
        llm = ChatOpenAI(model=self.model_name, temperature=0, top_p=1, seed=42, request_timeout=30, max_retries=1)
        structured_llm = llm.with_structured_output(InputValidationReview)
        review_prompt = ChatPromptTemplate.from_template(
            """
            You are the Stage-1 Input Validation Agent for a B2B outreach platform.
            You receive three user inputs and return a cleaned, validated version
            WITHOUT inventing any information.

            INPUTS
            - target_industry: {target_industry}
            - target_location: {target_location}
            - prompt (the campaign objective, in the user's own words): {prompt}
            - sender_website: {sender_website}

            CONTEXT — IMPORTANT
            The sender_website is MANDATORY and always provided. The sender's OFFERING
            (products / services) is sourced automatically from that website by a later
            research stage. The user is NEVER required to name a product or service in the
            prompt — DO NOT clarify on "what do you offer" or anything offering-related, and
            DO NOT treat a missing product mention as a problem of any kind.

            TASK 1 — SPELLING (industry & location only):
            Fix spelling, typos, and casing, keeping the user's EXACT term.
            'manufaturing' -> 'Manufacturing', 'unted kingdom' -> 'United Kingdom'.
            Do NOT paraphrase or broaden (do NOT turn 'Fintech' into 'financial
            institutions', or 'SaaS' into 'software companies').

            TASK 2 — CLARIFICATION GATE (very permissive):
            Treat the prompt as ACTIONABLE as long as it carries ANY campaign intent —
            an audience, a goal, a pain to address, targeting criteria, buying signals to
            look for, a use-case, a desired outcome, or even just a direction of outreach.
            A short prompt is fine. A prompt that does not name a product/service is fine.
            A prompt that only describes who to reach or what to evaluate is fine.

            ONLY clarify when the prompt is COMPLETELY empty or pure no-intent filler with
            zero campaign direction whatsoever — i.e. it carries NO audience, NO goal, NO
            pain, NO criteria, and NO outcome. Examples of pure filler that REQUIRES
            clarification: "", "reach out to people", "get me leads", "send some cold
            emails", "find customers" — and even these only require ONE simple question
            like "what's the goal of this outreach?". NEVER ask for product/service info.

            WHEN IN DOUBT, do NOT clarify — the website provides the offering, the CSV
            provides the audience, the industry/location filters constrain the targeting,
            and the prompt only needs to express SOME direction. Default to actionable.

            If clarification IS needed (pure-filler case only):
              - overall.status = "needs_clarification"; requires_user_clarification = true
              - overall.clarification_questions = ONE short, non-offering question
                (e.g. "what outcome are you hoping for from this outreach?")
              - prompt.enhanced = a LIGHT grammar cleanup of the original ONLY (invent nothing)
            Otherwise -> overall.status = "success"; requires_user_clarification = false;
            go to TASK 3.

            TASK 3 — PROMPT ENHANCEMENT (for actionable prompts):
            Produce ONE clear, grammatical, well-structured objective that PRESERVES EVERY
            FACT the user gave. You MUST:
              • keep ALL of the user's facts — the audience/persona, any offering/product
                they mentioned, AND any stated goal, pain, or reason. NEVER drop the user's
                stated goal or pain (if they wrote "to reduce maintenance overhead", keep it).
              • fold in the user's EXACT target_industry and target_location when the prompt
                does not already name them.
            You may fix grammar and reorder for clarity. You may NOT change meaning:
              • NEVER OMIT a fact the user provided.
              • PRESERVE THE ACTION: keep the user's verb and the relationship between the
                audience and any offering they mentioned. Do NOT rephrase to change who does
                what.
              • NEVER ADD a fact the user did not provide — no new benefit clause, persona,
                product, metric, or value — unless the user literally wrote it. Use the
                user's EXACT terms (do not turn 'Fintech' into 'financial institutions').
              • NEVER FABRICATE an offering, product, or service into the enhanced prompt
                even if the user did not mention one — the offering is sourced from the
                website by a later stage.

            For each field set: changed, confidence, clarification_needed, and a short
            reason. Return structured JSON only.
            """
        )

        try:
            return (review_prompt | structured_llm).invoke(
                {
                    "target_industry": target_industry,
                    "target_location": target_location,
                    "prompt": prompt,
                    "sender_website": website_value,
                }
            )
        except Exception as exc:
            logger.error("[INPUT VALIDATION] LLM review failed: %s", exc, exc_info=True)
            return self._safe_passthrough(
                target_industry=target_industry,
                target_location=target_location,
                prompt=prompt,
                reason="Input review failed; preserved sanitized user-provided value without assumptions.",
            )


input_validation_service = InputValidationService()
