from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from typing import List, Optional
from app.core.logging_config import logger
from app.core.llm_resilience import run_openai_guarded

# Specialized LLM for structural validation
llm = ChatOpenAI(
    model="gpt-4o-mini", 
    temperature=0,
    seed=42
)

class ValidationResult(BaseModel):
    is_valid: bool = Field(description="Whether the campaign prompt is actionable and clear")
    clarity_score: int = Field(description="Score from 1-10 on prompt clarity")
    missing_elements: List[str] = Field(description="List of critical information missing from the prompt")
    suggestions: List[str] = Field(description="Actionable suggestions to improve the campaign")
    clarification_questions: List[str] = Field(description="Specific questions to ask the user to fill the gaps")

VALIDATION_PROMPT = """You are an Expert Outreach Strategist. 
Your task is to validate a user's campaign prompt for an agentic outreach workflow.

User Campaign Prompt:
{prompt}

CRITERIA FOR VALIDATION:
1. TARGET AUDIENCE: Does the prompt indicate who we are reaching out to? (e.g., tech companies, startups)
2. VALUE PROP: Does it give a hint of what we are offering?
3. CALL TO ACTION: Is there a goal mentioned? (e.g., meet, feedback)
4. ACTIONABLE: Can an AI agent start researching with this?

RELAXATION RULE:
- If the intent is clear but brief, mark is_valid as True.
- Only mark as False if it is completely gibberish or empty.
- Prioritize moving the workflow forward.

OUTPUT REQUIREMENTS:
- set is_valid to true unless it's impossible to work with.
- Provide a clarity score (1-10).
- List missing elements clearly as suggestions, not blockers.
- If elements are missing, generate 1-3 conversational questions to ask the user (e.g., 'Who is your ideal target audience?').
- Give suggestions for improvement.
"""

class CampaignValidator:
    """
    Agent B: Input Validation Agent.
    Ensures the user's campaign prompt is high-quality and contains enough context
    for the subsequent research and drafting agents.
    """
    
    @staticmethod
    async def validate_prompt(prompt: str) -> dict:
        structured_llm = llm.with_structured_output(ValidationResult)
        prompt_template = ChatPromptTemplate.from_template(VALIDATION_PROMPT)
        chain = prompt_template | structured_llm
        
        try:
            logger.info("[VALIDATOR] Validating campaign input prompt...")
            result = run_openai_guarded(
                "campaign_validation",
                lambda: chain.invoke({"prompt": prompt}),
                fallback={
                    "is_valid": True, 
                    "clarity_score": 5, 
                    "missing_elements": [], 
                    "suggestions": ["Manual check recommended due to validation timeout."]
                }
            )
            
            if hasattr(result, 'model_dump'):
                data = result.model_dump()
            else:
                data = result
                
            logger.info(f"[VALIDATOR] Validation complete. Valid: {data.get('is_valid')}")
            return data
            
        except Exception as e:
            logger.error(f"[VALIDATOR] Validation agent failed: {e}")
            return {
                "is_valid": True, # Fail open to not block user, but log error
                "clarity_score": 0,
                "missing_elements": ["Validation service error"],
                "suggestions": [str(e)]
            }
