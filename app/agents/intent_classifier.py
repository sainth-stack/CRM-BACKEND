from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
import enum

llm = ChatOpenAI(
    model="gpt-4o-mini", 
    temperature=0,
    top_p=1,
    frequency_penalty=0,
    presence_penalty=0,
    seed=42,
)

class IntentType(str, enum.Enum):
    POSITIVE = "POSITIVE"
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
1. POSITIVE: The prospect is interested, wants a meeting, asks for a call, wants a demo, or expresses clear interest in the next steps (e.g., "Ready for a call", "Send me a calendar link").
2. NEGATIVE: The prospect says "No", "Not interested", "Stop emailing me", "Remove from list", or clearly rejects the proposal.
3. NEUTRAL: The prospect is hesitant, asks a general question, says "Check back later", expresses mild skepticism but doesn't say No, or provides a non-committal answer that requires further convincing.

STRICT CONSTRAINTS:
- No assumptions.
- If they say "Talk next month", it is NEUTRAL (requires a follow-up/convincing).
- If they ask for pricing or more info before meeting, it is NEUTRAL.

Return only the structured intent classification.
"""

def classify_reply_intent(original_email: str, prospect_reply: str) -> dict:
    structured_llm = llm.with_structured_output(IntentClassification)
    prompt = ChatPromptTemplate.from_template(INTENT_PROMPT)
    chain = prompt | structured_llm
    
    try:
        result = chain.invoke({
            "original_email": original_email,
            "prospect_reply": prospect_reply
        })
        return result.model_dump()
    except Exception as e:
        print(f"Error in Intent Classification: {e}")
        return {"intent": "NEUTRAL", "reasoning": "Fallback due to error"}
