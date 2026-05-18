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
    Strict Stage 1 input-review agent.
    It may correct obvious spelling and rephrase supplied prompt context, but it must not infer missing campaign facts.
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
    ) -> InputValidationReview:
        prompt = prompt or ""

        if not os.getenv("OPENAI_API_KEY"):
            return self._safe_passthrough(
                target_industry=target_industry,
                target_location=target_location,
                prompt=prompt,
                reason="Input review model unavailable; preserved sanitized user-provided value without assumptions.",
            )

        llm = ChatOpenAI(model=self.model_name, temperature=0)
        structured_llm = llm.with_structured_output(InputValidationReview)
        review_prompt = ChatPromptTemplate.from_template(
            """
            You are the 'Strict Semantic Guardian' and Stage 1 Input Validation Agent.
            Your mission is to sanitize and clarify user inputs while maintaining 100% semantic equivalence.

            Review these fields:
            - target_industry: {target_industry}
            - target_location: {target_location}
            - prompt: {prompt}

            STRICT ADHERENCE RULES:
            1. SPELLING: Correct all spelling and typographical errors in target_industry and target_location (e.g., 'manufaturing' -> 'Manufacturing', 'unted kingdom' -> 'United Kingdom').
            2. SEMANTIC LOCK: You must NOT change the meaning. You must NOT add new industries, locations, target personas, or value propositions not mentioned by the user.
            3. INTENT CLARIFICATION: Rephrase the prompt ONLY to make it clearer and more structured. If the user's intent is 'reach out to factory owners', do not change it to 'reach out to manufacturing executives' unless 'manufacturing' was already in the context.
            4. NO INFERENCE: If a field is vague, do not 'guess'. Ask a clarification question instead.
            5. NO STRATEGY INVENTION: Do not add email goals, proof points, or competitive advantages that aren't in the original text.
            
            If the input is already perfect, return it exactly as is.
            Return structured JSON only.
            """
        )

        try:
            return (review_prompt | structured_llm).invoke(
                {
                    "target_industry": target_industry,
                    "target_location": target_location,
                    "prompt": prompt,
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
