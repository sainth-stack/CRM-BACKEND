# Email Drafting Agent
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from dotenv import load_dotenv
from app.core.logging_config import logger
from app.core.llm_resilience import run_openai_guarded
import re

load_dotenv()

# Specialized LLM for high-fidelity content generation
llm = ChatOpenAI(
    model="gpt-4o-mini", 
    temperature=0,
    top_p=1,
    frequency_penalty=0,
    presence_penalty=0,
    seed=42,
    request_timeout=120
)

EMAIL_DRAFTER_PROMPT = """You are a World-Class Ghostwriter for high-stakes cold outreach.
Your task is to draft a hyper-personalized email for the decision maker below.

User Company Context (The Sender):
- Brand Name: {user_company_name}
- Mission/Motto: {user_company_moto}
- Core Offerings: {user_company_offerings}
- Specialized Capability Map: {user_company_research}

Target Decision Maker:
- Name: {dm_name}
- Position: {dm_position}
- Company: {dm_company}

Strategic Intelligence (THE WHY NOW):
- Master Research Summary: {research_summary}
- Growth Hooks: {growth_hooks}
- Pain Hooks: {pain_hooks}
- News Hooks: {news_hooks}
- Business Opportunity Reasoning: {opportunity_reason}

NARRATIVE STRATEGY:
1. THE HOOK: Lead immediately with a specific fact from the 'Strategic Intelligence'. Anchor the email in their current reality (Expansion, Pain, or News).
2. THE BRIDGE: Connect the hook to one of the {user_company_name}'s 'Core Offerings'. 
3. THE PROOF: Briefly mention how {user_company_name} specifically solves the problem or supports the growth mentioned in the hook.
4. THE ASK: A low-friction "Discovery Call" request.

STRICT CONSTRAINTS:
1. NO PLACEHOLDERS. (No [Name], [Company Name], etc.). 
2. NO generic openers like "Hope this finds you well" or "I was doing research".
3. Sign off exactly as follows, with a double line break after 'Best regards,' and a single line break between the sender name and the company name:
   "Best regards,
   
   {user_name}
   {user_company_name}"
4. Tone: Senior, direct, and insight-driven. Avoid "marketing-speak".
5. Paragraph Formatting: Each paragraph must be a single continuous string. Use double line breaks (\\n\\n) between sections.
6. Length: Under 150 words.
"""

FOLLOW_UP_PROMPT = """You are a World-Class Persistence Ghostwriter.
Your task is to draft a short, convincing follow-up email.

MODE: {outreach_mode}
(Mode 'NUDGE' = persistence on a thread. Mode 'COORDINATION' = booking attempt failed, the requested slot was not available in our calendar. We need to offer specific alternative slots or request them.)

Our Company Info:
- Name: {user_company_name}
- Research: {user_company_research}

Decision Maker Info:
- Name: {dm_name}
- Company: {dm_company}

Communication History (Most recent first):
{thread_history}

{progress_context}

ALTERNATIVE AVAILABLE SLOTS:
{alternative_slots_context}

STRATEGY:
- Be polite and professional.
- If MODE is 'COORDINATION':
    - If alternative slots are provided: Politely state that the specific time they requested was not available on our calendar. Offer the listed alternative slots as concrete options for them to choose from. Keep the tone warm and professional.
    - If alternative slots are NOT provided (the list says 'None available'): Politely apologize that the requested slot didn't fit our current calendar, and ask them to suggest 2-3 alternative times that work for them, or share a link to check a common time.
    - DO NOT claim the requested time 'works perfectly'. DO NOT say you will send a calendar invite if no booking was confirmed.
- If MODE is 'NUDGE': Directly address the prospect's previous neutral points. Re-iterate strategic match.
- The goal is a "Discovery Call".

STRICT CONSTRAINTS:
1. NO PLACEHOLDERS.
2. Sign off exactly as follows, with a double line break after 'Best regards,' and a single line break between the sender name and the company name:
   "Best regards,
   
   {user_name}
   {user_company_name}"
3. Keep length under 120 words.
4. STRICT FORMATTING: Do NOT use mid-paragraph line breaks. Each paragraph must be a single continuous string. Only use double line breaks (\\n\\n) to separate sections.
5. Return ONLY the JSON with subject and body.
"""

def clean_email_body(body: str) -> str:
    """Cleans single line breaks inside paragraphs to prevent jagged wrapping, while preserving signature spacing."""
    if not body:
        return ""
    body = body.replace("\r\n", "\n")
    body = re.sub(r'\n{3,}', '\n\n', body)
    
    # 1. Look for signature block starting with a signoff keyword at the end
    signoff_pattern = r'\n+(best regards|best|sincerely|regards|thanks|warmly|cheers|best wishes),?\s*\n\s*(.+)$'
    match = re.search(signoff_pattern, body, re.IGNORECASE | re.DOTALL)
    
    signature_block = ""
    if match:
        full_match = match.group(0)
        body = body[:match.start()]
        sign_off = match.group(1)
        sig_lines = match.group(2).strip()
        signature_block = f"\n\n{sign_off.capitalize()},\n\n{sig_lines}"
        
    # 2. Clean main paragraphs
    parts = body.split('\n\n')
    cleaned_parts = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        cleaned_part = re.sub(r'(?<!\n)\n(?!\n)', ' ', part)
        cleaned_parts.append(cleaned_part)
        
    cleaned_body = '\n\n'.join(cleaned_parts).strip()
    
    # 3. Re-append protected signature block
    if signature_block:
        cleaned_body += signature_block
        
    return cleaned_body


from pydantic import BaseModel, Field

class EmailDraftResponse(BaseModel):
    subject: str = Field(description="The personalized email subject line")
    body: str = Field(description="The full, hyper-personalized email body")

def draft_personalized_email(user_intel: dict, dm_info: dict, target_company_name: str, intel_hooks: dict, user_name: str = "Account Manager"):
    """Generates a hyper-personalized outreach draft for a stakeholder using V2 hooks."""
    structured_llm = llm.with_structured_output(EmailDraftResponse)
    prompt = ChatPromptTemplate.from_template(EMAIL_DRAFTER_PROMPT)
    chain = prompt | structured_llm
    
    try:
        logger.info(f"[GHOSTWRITER] Drafting high-fidelity outreach for {dm_info.get('name')} at {target_company_name}...")
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
                "research_summary": intel_hooks.get("research_summary", ""),
                "growth_hooks": ", ".join(intel_hooks.get("growth_hooks", [])),
                "pain_hooks": ", ".join(intel_hooks.get("pain_hooks", [])),
                "news_hooks": ", ".join(intel_hooks.get("news_hooks", [])),
                "opportunity_reason": intel_hooks.get("opportunity_reason", ""),
                "user_name": user_name
            }),
            fallback=None,
        )
        if data:
            dumped = data.model_dump()
            dumped["body"] = clean_email_body(dumped["body"])
            return dumped
        return None
    except Exception as e:
        logger.error(f"[GHOSTWRITER] Outreach drafting critical failure: {e}", exc_info=True)
        return None

def draft_followup_email(user_intel: dict, dm_info: dict, target_company_name: str, thread_history: str, followup_number: int, manual_scheduling: bool = False, alternative_slots: list = None, user_name: str = "Account Manager"):
    """Drafts contextual follow-up emails based on previous thread history."""
    structured_llm = llm.with_structured_output(EmailDraftResponse)
    prompt = ChatPromptTemplate.from_template(FOLLOW_UP_PROMPT)
    chain = prompt | structured_llm
    
    mode = "COORDINATION" if manual_scheduling else "NUDGE"
    progress = f"Current Progress: This is Follow-up #{followup_number} of 11." if not manual_scheduling else "Context: Manual scheduling coordination required — booking attempt failed or slot was unavailable."

    if manual_scheduling and alternative_slots:
        slots_formatted = "\n".join([f"- {s}" for s in alternative_slots])
        alternative_slots_context = f"The following slots are confirmed available in our calendar. Offer these to the prospect:\n{slots_formatted}"
    elif manual_scheduling:
        alternative_slots_context = "None available — ask the prospect to suggest 2-3 times that work for them."
    else:
        alternative_slots_context = "N/A (NUDGE mode — no slot coordination needed)"

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
                "progress_context": progress,
                "alternative_slots_context": alternative_slots_context,
                "user_name": user_name
            }),
            fallback=None,
        )
        if data:
            dumped = data.model_dump()
            dumped["body"] = clean_email_body(dumped["body"])
            return dumped
        return None
    except Exception as e:
        logger.error(f"[GHOSTWRITER] Follow-up drafting failure: {e}")
        return None

class NudgeDraftResponse(BaseModel):
    subject: str = Field(description="The short nudge subject")
    body: str = Field(description="The short nudge body")

def draft_nudge_email(dm_name: str, user_company_name: str, user_name: str = "Account Manager"):
    """Generates short inbox nudges for non-responsive prospects."""
    structured_llm = llm.with_structured_output(NudgeDraftResponse)
    prompt = ChatPromptTemplate.from_template("""
    You are a polite business assistant. 
    Draft a extremely short (under 30 words) nudge email for {dm_name}.
    Context: You sent an email from {user_company_name} 2 days ago and haven't heard back.
    Goal: Politely bring the conversation to the top of their inbox.
    
    STRICT CONSTRAINTS:
    1. NO PLACEHOLDERS.
    2. Sign off exactly as follows, with a double line break after 'Best regards,' and a single line break between the sender name and the company name:
       "Best regards,
       
       {user_name}
       {user_company_name}"
    4. STRICT FORMATTING: No mid-paragraph breaks. Just a single block of text for the body.
    5. Return ONLY the JSON.
    """)
    chain = prompt | structured_llm
    try:
        logger.info(f"[GHOSTWRITER] Generating inbox nudge for {dm_name}...")
        response = run_openai_guarded(
            "nudge_generation",
            lambda: chain.invoke({"dm_name": dm_name, "user_company_name": user_company_name, "user_name": user_name}),
            fallback=None,
        )
        if response and hasattr(response, 'body'):
            return clean_email_body(response.body)
        return f"Hi {dm_name}, just bringing this to the top of your inbox.\n\nBest regards,\n\n{user_name}\n{user_company_name}"
    except Exception as e:
        logger.error(f"[GHOSTWRITER] Nudge generation compromised: {e}")
        return f"Hi {dm_name}, just bringing this to the top of your inbox.\n\nBest regards,\n\n{user_name}\n{user_company_name}"

def draft_discovery_request(user_intel: dict, dm_name: str, dm_position: str, target_company: str, last_interest: str, booked_link: str = None, user_real_name: str = "Account Manager"):
    """
    Discovery Protocol: Interest-to-Call Conversion Agent.
    Drafts the formal request for a discovery call based on captured interest.
    """
    structured_llm = llm.with_structured_output(EmailDraftResponse)
    
    prompt_text = """You are a Senior Strategic Coordinator.
    Your task is to draft a short, formal Discovery Call request.
    
    Company Context (Our Side):
    - Name: {user_name}
    - Services: {user_offerings}
    - Research: {user_research}
    
    Target Decision Maker:
    - Name: {dm_name}
    - Position: {dm_position}
    - Company: {target_company}
    
    Previous Interest Context:
    - Interest Signal: {last_interest}
    
    STRATEGY:
    - Acknowledge their interest briefly.
    - {booking_context}
    
    STRICT CONSTRAINTS:
    1. NO PLACEHOLDERS.
    2. Sign off exactly as follows, with a double line break after 'Best regards,' and a single line break between the sender name and the company name:
       "Best regards,
       
       {user_real_name}
       {user_name}"
    3. Keep it under 80 words.
    4. Never ask the prospect to share "2-3 time slots" or "multiple slots". Strictly ask them for a single convenient timeslot or time to connect (e.g. "Please let me know a convenient time for you").
    5. STRICT FORMATTING: Do NOT use mid-paragraph line breaks or random newlines. Each paragraph must be a single continuous string. Only use double line breaks (\\n\\n) to separate sections.
    """
    
    booking_context = f"Since you've shown interest, please pick a slot here to synchronize: {booked_link}" if booked_link else "I would love to coordinate a brief 15-minute discovery call so we can properly align our solutions with your organization's specific needs. Please let me know a convenient time for you to connect, and I will gladly adjust my schedule to accommodate."

    prompt = ChatPromptTemplate.from_template(prompt_text)
    chain = prompt | structured_llm
    
    try:
        logger.info(f"[GHOSTWRITER] Drafting Discovery Protocol for {dm_name} at {target_company}...")
        data = run_openai_guarded(
            "discovery_draft_generation",
            lambda: chain.invoke({
                "user_name": user_intel.get("name", ""),
                "user_offerings": user_intel.get("offerings", ""),
                "user_research": user_intel.get("deep_research", ""),
                "dm_name": dm_name,
                "dm_position": dm_position,
                "target_company": target_company,
                "last_interest": last_interest,
                "booking_context": booking_context,
                "user_real_name": user_real_name
            }),
            fallback=None,
        )
        if data:
            dumped = data.model_dump()
            dumped["body"] = clean_email_body(dumped["body"])
            return dumped
        return None
    except Exception as e:
        logger.error(f"[GHOSTWRITER] Discovery drafting failure: {e}")
        return None
