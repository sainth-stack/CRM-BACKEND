from pydantic import BaseModel, Field
from typing import Optional
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from dotenv import load_dotenv
from app.core.logging_config import logger
from app.core.llm_resilience import run_openai_guarded
from app.core.sanitizer import sanitize_for_llm

load_dotenv()

# Specialized LLM for coordinate extraction and strategic content synthesis
llm = ChatOpenAI(
    model="gpt-4o-mini", 
    temperature=0,
    top_p=1,
    frequency_penalty=0,
    presence_penalty=0,
    seed=42,
)

class ScheduleExtraction(BaseModel):
    date: Optional[str] = Field(description="The date of the meeting in YYYY-MM-DD format, or null if not found")
    time: Optional[str] = Field(description="The time of the meeting in HH:MM format (24h), or null if not found")
    timezone: Optional[str] = Field(description="The timezone mentioned (e.g., IST, PST, EST), or null if not found")
    reasoning: str = Field(description="Why you extracted this date/time")

SCHEDULING_PROMPT = """You are a High-Precision Scheduling Extraction Intelligence.
Analyze the following email reply from a prospect and extract the proposed meeting date, time, and timezone.

Email Reply:
"{reply_text}"

Current Reference Date (Today): {current_date}
Target Company Geography: {location_context}

INSTRUCTIONS:
1. DATE EXTRACTION: Determine the absolute YYYY-MM-DD. 
   - "Tuesday at 11 AM" -> Calculate the upcoming Tuesday from {current_date}.
   - "Next week Monday" -> Calculate the following Monday from {current_date}.
2. TIME EXTRACTION: Convert to 24-hour HH:MM format (e.g., 10 PM -> 22:00, 11 AM -> 11:00).
3. TIMEZONE EXTRACTION: 
   - If IST, PST, etc. are mentioned, USE THEM. 
   - If no timezone is mentioned, return timezone as null.
4. RESOLUTION: If they propose multiple slots, pick the absolute earliest valid slot.
5. VALIDATION: If the reply is just interest and has NO date/time, return both date and time as null.

Return exactly the requested JSON schema.
"""

def extract_schedule_info(reply_text: str, current_date: str, location_context: str):
    """
    Temporal Intelligence Protocol.
    Parses prospect communication to extract precise meeting coordinates (date, time, timezone).
    Translates relative linguistic offsets (e.g., 'next Tuesday') into absolute ISO-8601 timestamps.
    """
    structured_llm = llm.with_structured_output(ScheduleExtraction)
    prompt = ChatPromptTemplate.from_template(SCHEDULING_PROMPT)
    chain = prompt | structured_llm
    
    try:
        logger.info("[DISCOVERY] Extracting scheduling coordinates from prospect reply...")
        safe_reply = sanitize_for_llm(reply_text, context_limit=2000)
        extraction = run_openai_guarded(
            "discovery_schedule_extraction",
            lambda: chain.invoke({
                "reply_text": safe_reply,
                "current_date": current_date,
                "location_context": location_context
            }),
            fallback=None,
        )
        return extraction.model_dump() if extraction else None
    except Exception as e:
        logger.error(f"[DISCOVERY] Coordinate extraction failure: {e}")
        return None

DISCOVERY_DRAFTER_PROMPT = """You are a World-Class Strategic Deal Closer.
Draft a high-touch response email in response to a prospect's interest.

User Company Info:
- Identity: {user_company_name}
- Specialization: {user_company_offerings}
- Context: {user_company_research}

Decision Maker: {dm_name} ({dm_position}) at {target_company}
Prospect's Last Interest: "{last_interest_context}"

{booking_context}

STRICT CONSTRAINTS:
1. NO PLACEHOLDERS. Never use brackets like [Your Name], [Company Name], or [Your Position].
2. NO BRACKETED SIGNATURES. End the email professionally using ONLY the company identity "{user_company_name}".
3. NARRATIVE FLOW: {narrative_instruction}
4. ORGANIC TONE: Speak like a senior executive, not a bot or a junior SDR. No corporate cliché.
5. LENGTH: Keep it under 100 words. Return ONLY the JSON-wrapped response.
6. STRICT FORMATTING: Do NOT use mid-paragraph line breaks. Only use double line-breaks (\n\n) to separate different paragraphs. Each paragraph must be a single, long line of text.
"""

class DiscoveryEmailResponse(BaseModel):
    subject: str = Field(description="The professional subject line")
    body: str = Field(description="The organic email body")

def draft_discovery_request(user_intel: dict, dm_name: str, dm_position: str, target_company: str, last_interest: str, booked_link: str = None):
    """
    Strategic Coordination Synthesis.
    Drafts high-fidelity communication to finalize discovery meeting coordinates.
    Handles both initial interest responses and finalized booking confirmation dispatches.
    """
    structured_llm = llm.with_structured_output(DiscoveryEmailResponse)
    
    booking_context = ""
    narrative_instruction = "Acknowledge their interest and request their preferred timing or a few available slots next week to sync up for a 15-minute discovery call. Mention we will handle the bridge coordinates once they share a time."
    
    if booked_link:
        # Finalized coordinate injection for confirmed sessions
        booking_context = f"MISSION CRITICAL: Coordination success. Discovery slot confirmed. Coordinates: {booked_link}."
        narrative_instruction = f"Inform them that you have successfully SECURED the discovery slot. Provide the confirmed meeting bridge coordinates ({booked_link}) and express that you are looking forward to the alignment session."

    prompt = ChatPromptTemplate.from_template(DISCOVERY_DRAFTER_PROMPT)
    chain = prompt | structured_llm
    
    try:
        logger.info(f"[DISCOVERY] Synthesizing strategic discovery response for {dm_name} at {target_company}...")
        safe_interest = sanitize_for_llm(last_interest, context_limit=3000)
        response = run_openai_guarded(
            "discovery_draft_generation",
            lambda: chain.invoke({
                "user_company_name": user_intel.get("name", ""),
                "user_company_offerings": user_intel.get("offerings", ""),
                "user_company_research": user_intel.get("deep_research", ""),
                "dm_name": dm_name,
                "dm_position": dm_position,
                "target_company": target_company,
                "last_interest_context": safe_interest,
                "booking_context": booking_context,
                "narrative_instruction": narrative_instruction
            }),
            fallback=None,
        )
        return response.model_dump() if response else None
    except Exception as e:
        logger.error(f"[DISCOVERY] Strategic drafting mission failure: {e}")
        return None
