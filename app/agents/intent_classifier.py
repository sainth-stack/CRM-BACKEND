# Intent Classification Agent
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
import enum
from app.core.logging_config import logger
from app.core.llm_resilience import run_openai_guarded

# Specialized LLM for sentiment and intent analysis
llm = ChatOpenAI(
    model="gpt-4o-mini", 
    temperature=0,
    top_p=1,
    frequency_penalty=0,
    presence_penalty=0,
    seed=42,
    request_timeout=120
)

class IntentType(str, enum.Enum):
    POSITIVE = "POSITIVE"
    BOOKING = "BOOKING"
    NEGATIVE = "NEGATIVE"
    NEUTRAL = "NEUTRAL"

class IntentClassification(BaseModel):
    intent: IntentType = Field(description="Classification of the prospect's reply")
    reasoning: str = Field(description="Brief explanation of why this intent was chosen")

INTENT_PROMPT = """You are an Expert Sales Intent Classifier.
Your task is to analyze an email reply from a prospect and determine their intent based on our previous outreach.

Original Email Sent by Us:
{original_email}

Prospect's Reply:
{prospect_reply}

CLASSIFICATION RULES:
1. BOOKING: The prospect is interested AND explicitly shares a specific day, date, time, or scheduling slot they are available for (e.g., "available on Wednesday at 4.00 PM EST", "How about tomorrow morning at 10am?", "Let's meet Monday at 2pm", "I can do 2 PM on Thursday").
2. POSITIVE: The prospect is interested, wants a meeting, asks for a call, wants a demo, or expresses clear interest in next steps, but does NOT provide any specific date, day, time, or availability slot yet (e.g., "Yes, I would be interested", "Sure, let's talk next week", "Send me more details and a calendar link", "I'd love to connect").
3. NEGATIVE: The prospect says "No", "Not interested", "Stop emailing me", "Remove from list", or clearly rejects the proposal.
4. NEUTRAL: The prospect is hesitant, asks a general question, says "Check back later", expresses mild skepticism but doesn't say No, or provides a non-committal answer that requires further convincing.

STRICT CONSTRAINTS:
- No assumptions.
- If they say "Talk next month", it is NEUTRAL (requires a follow-up/convincing).
- If they ask for pricing or more info before meeting, it is NEUTRAL.

Return only the structured intent classification.
"""

def classify_reply_intent(original_email: str, prospect_reply: str) -> dict:
    """
    Sentiment & Intent Discovery Protocol.
    Analyzes prospect communication to determine the optimal next step in the outreach lifecycle.
    Classifies replies into POSITIVE (Interest), NEGATIVE (Opt-out), or NEUTRAL (Information Gap).
    """
    structured_llm = llm.with_structured_output(IntentClassification)
    prompt = ChatPromptTemplate.from_template(INTENT_PROMPT)
    chain = prompt | structured_llm
    
    try:
        logger.info("[INTENT] Analyzing prospect reply for sentiment signals...")
        result = run_openai_guarded(
            "intent_classification",
            lambda: chain.invoke({
                "original_email": original_email,
                "prospect_reply": prospect_reply
            }),
            fallback={"intent": "NEUTRAL", "reasoning": "Fallback due to temporarily unavailable classification service"},
        )
        if isinstance(result, dict):
            logger.info(f"[INTENT] Classification complete: {result.get('intent')}")
            return result
        logger.info(f"[INTENT] Classification complete: {result.intent}")
        return result.model_dump()
    except Exception as e:
        logger.error(f"[INTENT] Critical classification error: {e}")
        return {"intent": "NEUTRAL", "reasoning": "Fallback due to operational error"}

class ScheduleExtraction(BaseModel):
    date: str | None = Field(description="The date requested (YYYY-MM-DD)")
    time: str | None = Field(description="The specific time requested (e.g. '14:00')")
    timezone: str | None = Field(description="The timezone if mentioned")
    reasoning: str = Field(description="Brief logic for the extraction")

def extract_schedule_info(text: str, current_date: str, target_location: str) -> dict:
    """
    Coordination Agent: Temporal Extraction.
    Surgically extracts requested meeting times from prospect communications.
    """
    structured_llm = llm.with_structured_output(ScheduleExtraction)
    prompt = ChatPromptTemplate.from_template("""
    You are a Scheduling Precision Agent. 
    Analyze the prospect's reply below and extract the requested date and time for a meeting.
    
    Context:
    - Current Date: {current_date}
    - Prospect Location: {target_location}
    
    Prospect's Reply:
    {text}
    
    RULES:
    1. Extract the date in YYYY-MM-DD format. If they say "next Tuesday", calculate it based on the current date.
    2. Extract the time in 24h format (e.g., 2:00 PM -> 14:00).
    3. If no specific date or time is mentioned, return null for those fields.
    4. Focus only on the 'next meeting' request.
    """)
    
    chain = prompt | structured_llm
    try:
        logger.info("[INTENT] Extracting temporal coordinates for scheduling...")
        result = run_openai_guarded(
            "schedule_extraction",
            lambda: chain.invoke({
                "text": text,
                "current_date": current_date,
                "target_location": target_location
            }),
            fallback={"date": None, "time": None, "timezone": None, "reasoning": "Fallback due to service unavailability"},
        )
        if isinstance(result, dict):
            return result
        return result.model_dump()
    except Exception as e:
        logger.error(f"[INTENT] Temporal extraction failed: {e}")
        return {"date": None, "time": None, "timezone": None, "reasoning": "Critical failure"}
