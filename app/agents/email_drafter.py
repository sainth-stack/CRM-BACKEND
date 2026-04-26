# Email Drafting Agent
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from dotenv import load_dotenv
from app.core.logging_config import logger
from app.core.llm_resilience import run_openai_guarded

load_dotenv()

# Specialized LLM for high-fidelity content generation
llm = ChatOpenAI(
    model="gpt-4o-mini", 
    temperature=0,
    top_p=1,
    frequency_penalty=0,
    presence_penalty=0,
    seed=42,
    request_timeout=25
)

EMAIL_DRAFTER_PROMPT = """You are a World-Class Ghostwriter for cold outreach.
Your task is to draft a hyper-personalized email for the decision maker below.

User Company Info:
- Name: {user_company_name}
- Moto: {user_company_moto}
- Offerings: {user_company_offerings}
- Research: {user_company_research}

Decision Maker Info:
- Name: {dm_name}
- Position: {dm_position}
- Company: {dm_company}
- Target Company Research: {target_company_research}

STRICT CONSTRAINTS:
1. NO PLACEHOLDERS (like [Name], [Company Name], [Your Name], [Your Name], etc.). If a piece of data is missing, adapt the narrative to avoid mentioning it. 
2. DO NOT include bracketed signature blocks. End the email professionally using the "{user_company_name}" name only.
3. The tone must be professional but empathetic. No corporate jargon.
4. Reference specific facts from the Target Company Research (e.g., recent news, specialized products).
5. Explain how {user_company_name}'s offerings directly help the target company.
6. Provide a clear, compelling subject line.
7. Keep length under 200 words.
9. STRICT FORMATTING: Do NOT use mid-paragraph line breaks or manual line-wrapping. Each paragraph must be a single continuous string of text. Only use double line breaks (\n\n) to separate the greeting, different paragraphs, and the professional sign-off.
10. Return ONLY the drafted email in the specified JSON format.
"""

FOLLOW_UP_PROMPT = """You are a World-Class Persistence Ghostwriter.
Your task is to draft a short, convincing follow-up email.

MODE: {outreach_mode}
(Mode 'NUDGE' = persistence on a thread. Mode 'COORDINATION' = auto-booking failed, need to ask for their time manually).

Our Company Info:
- Name: {user_company_name}
- Research: {user_company_research}

Decision Maker Info:
- Name: {dm_name}
- Company: {dm_company}

Communication History (Most recent first):
{thread_history}

{progress_context}

STRATEGY:
- Be polite and professional.
    - If MODE is 'COORDINATION': Politely ask them to share 2-3 time slots that work for them next week for a 15-min discovery call.
- If MODE is 'NUDGE': Directly address the prospect's previous neutral points. Re-iterate strategic match.
- The goal is a "Discovery Call".

STRICT CONSTRAINTS:
1. NO PLACEHOLDERS.
2. NO bracketed signatures. Just the company name "{user_company_name}".
3. Keep length under 100 words.
4. STRICT FORMATTING: Do NOT use mid-paragraph line breaks. Each paragraph must be a single continuous string. Only use double line breaks (\n\n) to separate sections.
5. Return ONLY the JSON with subject and body.
"""

from pydantic import BaseModel, Field

class EmailDraftResponse(BaseModel):
    subject: str = Field(description="The personalized email subject line")
    body: str = Field(description="The full, hyper-personalized email body")

def draft_personalized_email(user_intel: dict, dm_info: dict, target_company_name: str, target_company_research: str):
    """Generates a hyper-personalized outreach draft for a stakeholder."""
    structured_llm = llm.with_structured_output(EmailDraftResponse)
    prompt = ChatPromptTemplate.from_template(EMAIL_DRAFTER_PROMPT)
    chain = prompt | structured_llm
    
    try:
        logger.info(f"[GHOSTWRITER] Drafting initial outreach for stakeholders at {target_company_name}...")
        data = run_openai_guarded(
            "email_draft_generation",
            lambda: chain.invoke({
                "user_company_name": user_intel.get("company_name", ""),
                "user_company_moto": user_intel.get("moto", ""),
                "user_company_offerings": ", ".join(user_intel.get("offerings", [])),
                "user_company_research": user_intel.get("deep_research", ""),
                "dm_name": dm_info.get("name", ""),
                "dm_position": dm_info.get("position", ""),
                "dm_company": target_company_name,
                "target_company_research": target_company_research
            }),
            fallback=None,
        )
        return data.model_dump() if data else None
    except Exception as e:
        logger.error(f"[GHOSTWRITER] Outreach drafting critical failure: {e}", exc_info=True)
        return None

def draft_followup_email(user_intel: dict, dm_info: dict, target_company_name: str, thread_history: str, followup_number: int, manual_scheduling: bool = False):
    """Drafts contextual follow-up emails based on previous thread history."""
    structured_llm = llm.with_structured_output(EmailDraftResponse)
    prompt = ChatPromptTemplate.from_template(FOLLOW_UP_PROMPT)
    chain = prompt | structured_llm
    
    mode = "COORDINATION" if manual_scheduling else "NUDGE"
    progress = f"Current Progress: This is Follow-up #{followup_number} of 11." if not manual_scheduling else "Context: Manual scheduling request."

    try:
        logger.info(f"[GHOSTWRITER] Drafting {mode} email for {dm_info.get('name')} (Followup #{followup_number})...")
        data = run_openai_guarded(
            "followup_draft_generation",
            lambda: chain.invoke({
                "outreach_mode": mode,
                "user_company_name": user_intel.get("company_name", ""),
                "user_company_research": user_intel.get("deep_research", ""),
                "dm_name": dm_info.get("name", ""),
                "dm_company": target_company_name,
                "thread_history": thread_history,
                "followup_number": followup_number,
                "progress_context": progress
            }),
            fallback=None,
        )
        return data.model_dump() if data else None
    except Exception as e:
        logger.error(f"[GHOSTWRITER] Follow-up drafting failure: {e}")
        return None

class NudgeDraftResponse(BaseModel):
    subject: str = Field(description="The short nudge subject")
    body: str = Field(description="The short nudge body")

def draft_nudge_email(dm_name: str, user_company_name: str):
    """Generates short inbox nudges for non-responsive prospects."""
    structured_llm = llm.with_structured_output(NudgeDraftResponse)
    prompt = ChatPromptTemplate.from_template("""
    You are a polite business assistant. 
    Draft a extremely short (under 30 words) nudge email for {dm_name}.
    Context: You sent an email from {user_company_name} 2 days ago and haven't heard back.
    Goal: Politely bring the conversation to the top of their inbox.
    
    STRICT CONSTRAINTS:
    1. NO PLACEHOLDERS.
    2. NO bracketed signatures. Just the company name "{user_company_name}".
    4. STRICT FORMATTING: No mid-paragraph breaks. Just a single block of text for the body.
5. Return ONLY the JSON.
    """)
    chain = prompt | structured_llm
    try:
        logger.info(f"[GHOSTWRITER] Generating inbox nudge for {dm_name}...")
        response = run_openai_guarded(
            "nudge_generation",
            lambda: chain.invoke({"dm_name": dm_name, "user_company_name": user_company_name}),
            fallback=None,
        )
        if not response:
            return f"Hi {dm_name}, just bringing this to the top of your inbox. Best, {user_company_name}"
        return response.body if hasattr(response, 'body') else f"Hi {dm_name}, just bringing this to the top of your inbox. Best, {user_company_name}"
    except Exception as e:
        logger.error(f"[GHOSTWRITER] Nudge generation compromised: {e}")
        return f"Hi {dm_name}, just bringing this to the top of your inbox. Best, {user_company_name}"
