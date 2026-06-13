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

FOLLOW_UP_PROMPT = """You are an elite B2B sales ghostwriter specializing in high-stakes follow-up sequences.

MODE: {outreach_mode}
(Mode 'NUDGE' = escalating research-driven persistence. Mode 'COORDINATION' = calendar coordination after a booking failure.)

Sender Company:
- Name: {user_company_name}
- Capabilities & Research: {user_company_research}

Prospect:
- Name: {dm_name}
- Company: {dm_company}

Target Company Intelligence (use for NUDGE mode — pick a fresh hook NOT already in the thread):
- Research Summary: {target_research_summary}
- Growth Signals: {growth_hooks}
- Pain Indicators: {pain_hooks}
- News / Recent Triggers: {news_hooks}
- Strategic Opportunity: {opportunity_reason}

Communication Thread (most recent first):
{thread_history}

{progress_context}

ALTERNATIVE AVAILABLE SLOTS:
{alternative_slots_context}

STRATEGY:
- If MODE is 'COORDINATION':
    - If alternative slots are provided: Politely state the requested time was not available. Offer the listed slots as concrete options. Keep tone warm and professional.
    - If no slots available: Apologize that the slot didn't fit, ask them to suggest 2-3 alternative times.
    - DO NOT claim the requested time works. DO NOT promise a calendar invite without a confirmed booking.

- If MODE is 'NUDGE':
    1. OPEN with a NEW signal from the target company intelligence — a growth hook, pain indicator, or news trigger NOT already used in the thread above.
    2. BRIDGE that signal to a specific capability of {user_company_name}.
    3. PROOF: Add a concrete proof point, outcome metric, or cost-of-inaction statement that deepens the case — something specific, not generic.
    4. CLOSE with one direct ask for a 15-minute discovery call.
    5. Apply the escalation level: {escalation_level}
       - Early (#1–3): Confident, insight-led. Fresh angle each email.
       - Mid (#4–6): More direct. Stronger hook. Reference the ongoing thread.
       - Late (#7–9): Build urgency. Make the cost of inaction visible.
       - Final (#10–11): Last outreach. Be direct and blunt. Give them an easy out.

STRICT CONSTRAINTS:
1. NO PLACEHOLDERS. NO "Hope this finds you well". NO generic filler.
2. For NUDGE: Do NOT repeat an angle or hook already used in the thread.
3. Sign off EXACTLY as follows, with a double line break after 'Best regards,' and a single line break between sender name and company name:
   "Best regards,

   {user_name}
   {user_company_name}"
4. 150–220 words total body. This must be longer and more substantive than the initial outreach.
5. Each paragraph is a single continuous string. Double line breaks between sections only.
6. Return ONLY valid JSON with "subject" and "body" fields.
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

def _followup_escalation_level(n: int) -> str:
    if n <= 3:
        return f"Early Stage — Follow-up #{n} of 11. Fresh angle, confident re-engagement."
    elif n <= 6:
        return f"Mid Stage — Follow-up #{n} of 11. More direct, stronger hook, reference thread context."
    elif n <= 9:
        return f"Late Stage — Follow-up #{n} of 11. Build urgency, make the cost of inaction visible."
    else:
        return f"Final Stage — Follow-up #{n} of 11. Last outreach before closing. Direct, blunt, give them an easy out."


def draft_followup_email(user_intel: dict, dm_info: dict, target_company_name: str, thread_history: str, followup_number: int, manual_scheduling: bool = False, alternative_slots: list = None, user_name: str = "Account Manager", target_company_intel: dict = None):
    """Drafts escalating follow-up emails using research data and thread history."""
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

    tc_intel = target_company_intel or {}

    def _join(val):
        if isinstance(val, list):
            return ", ".join(str(v) for v in val) if val else "N/A"
        return str(val) if val else "N/A"

    try:
        logger.info(f"[GHOSTWRITER] Drafting {mode} email for {dm_info.get('name')} (Follow-up #{followup_number})...")
        data = run_openai_guarded(
            "followup_draft_generation",
            lambda: chain.invoke({
                "outreach_mode": mode,
                "user_company_name": user_intel.get("company_name", ""),
                "user_company_research": user_intel.get("deep_research", "") or "N/A",
                "dm_name": dm_info.get("name", ""),
                "dm_company": target_company_name,
                "target_research_summary": tc_intel.get("research_summary", "") or "N/A",
                "growth_hooks": _join(tc_intel.get("growth_hooks")),
                "pain_hooks": _join(tc_intel.get("pain_hooks")),
                "news_hooks": _join(tc_intel.get("news_hooks")),
                "opportunity_reason": tc_intel.get("opportunity_reason", "") or "N/A",
                "thread_history": thread_history,
                "progress_context": progress,
                "escalation_level": _followup_escalation_level(followup_number) if not manual_scheduling else "N/A",
                "alternative_slots_context": alternative_slots_context,
                "user_name": user_name,
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

REMINDER_ESCALATION_PROMPT = """You are an elite B2B sales ghostwriter. A prospect did not reply to the previous email. Your job is to draft a follow-up that is MORE compelling by attacking from a DIFFERENT angle — using fresh intelligence from the target company's research data.

Sender Company:
- Name: {user_company_name}
- Capabilities & Research: {user_company_research}

Prospect:
- Name: {dm_name}
- Company: {target_company_name}

Target Company Intelligence:
- Research Summary: {target_research_summary}
- Growth Signals: {growth_hooks}
- Pain Indicators: {pain_hooks}
- News / Recent Triggers: {news_hooks}
- Strategic Opportunity: {opportunity_reason}

Previous Email Sent (DO NOT reuse the same angle, hook, or opening from this):
{previous_email}

Escalation Level: {escalation_level}
- Level 1: Confident re-engagement. Lead with a fresh insight from the intelligence above. Reinforce why the timing is right.
- Level 2: Final, direct, high-stakes pitch. Be blunt about the missed window. Create genuine urgency — not aggression. This is the last outreach before the conversation closes.

MISSION:
1. OPEN with a specific NEW signal from the target company intelligence — a growth hook, pain indicator, or news trigger NOT already used in the previous email.
2. BRIDGE: Map that signal precisely to a capability of {user_company_name}.
3. PROOF: Add a concrete proof point, outcome, or consequence that makes the case undeniable — a metric, a pattern seen across similar companies, or a specific cost of inaction.
4. CLOSE: One direct, low-friction ask for a 15-minute discovery call.

STRICT CONSTRAINTS:
1. NO PLACEHOLDERS. NO "Hope this finds you well". NO generic filler.
2. Do NOT repeat the angle, hook, or framing from the previous email.
3. Sign off EXACTLY as follows, with a double line break after 'Best regards,' and a single line break between sender name and company name:
   "Best regards,

   {user_name}
   {user_company_name}"
4. 150–220 words total body. This must be longer and more substantive than the initial outreach.
5. Each paragraph is a single continuous string. Use double line breaks between sections only.
6. Return ONLY valid JSON with "subject" and "body" fields.
"""

def draft_reminder_escalation_email(
    previous_email: str,
    user_intel: dict,
    target_company_intel: dict,
    dm_name: str,
    target_company_name: str,
    user_name: str = "Account Manager",
    reminder_number: int = 1,
) -> str:
    """Drafts a research-driven escalation email that hits a fresh angle from target company intel."""
    structured_llm = llm.with_structured_output(EmailDraftResponse)
    prompt = ChatPromptTemplate.from_template(REMINDER_ESCALATION_PROMPT)
    chain = prompt | structured_llm

    escalation_level = f"Level {reminder_number} of 2"
    growth_hooks = target_company_intel.get("growth_hooks") or []
    pain_hooks = target_company_intel.get("pain_hooks") or []
    news_hooks = target_company_intel.get("news_hooks") or []

    def _join(val):
        if isinstance(val, list):
            return ", ".join(str(v) for v in val) if val else "N/A"
        return str(val) if val else "N/A"

    try:
        logger.info(f"[GHOSTWRITER] Drafting reminder escalation #{reminder_number} for {dm_name} at {target_company_name}...")
        data = run_openai_guarded(
            "reminder_escalation_generation",
            lambda: chain.invoke({
                "user_company_name": user_intel.get("company_name", ""),
                "user_company_research": user_intel.get("deep_research", "") or "N/A",
                "dm_name": dm_name,
                "target_company_name": target_company_name,
                "target_research_summary": target_company_intel.get("research_summary", "") or "N/A",
                "growth_hooks": _join(growth_hooks),
                "pain_hooks": _join(pain_hooks),
                "news_hooks": _join(news_hooks),
                "opportunity_reason": target_company_intel.get("opportunity_reason", "") or "N/A",
                "previous_email": previous_email or "(No previous email on record)",
                "escalation_level": escalation_level,
                "user_name": user_name,
            }),
            fallback=None,
        )
        if data:
            dumped = data.model_dump()
            return clean_email_body(dumped["body"])
        return f"Hi {dm_name}, I wanted to follow up on my previous message.\n\nBest regards,\n\n{user_name}\n{user_intel.get('company_name', '')}"
    except Exception as e:
        logger.error(f"[GHOSTWRITER] Reminder escalation drafting failure: {e}")
        return f"Hi {dm_name}, I wanted to follow up on my previous message.\n\nBest regards,\n\n{user_name}\n{user_intel.get('company_name', '')}"

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
