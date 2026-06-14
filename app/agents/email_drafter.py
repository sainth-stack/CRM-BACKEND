# Email Drafting Agent
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from dotenv import load_dotenv
from app.core.logging_config import logger
from app.core.llm_resilience import run_openai_guarded
import re

load_dotenv()

# LLM for email content generation
llm = ChatOpenAI(
    model="gpt-4o-mini", 
    temperature=0,
    top_p=1,
    frequency_penalty=0,
    presence_penalty=0,
    seed=42,
    request_timeout=120
)

FOLLOW_UP_PROMPT = """You are an elite B2B sales ghostwriter for high-stakes follow-up sequences. In NUDGE
mode you plan each email with the MEDDPICC framework, and every new email must be RICHER and more
compelling than EVERY email already in the thread — open a genuinely fresh angle, never repeat one.

MODE: {outreach_mode}
(NUDGE = escalating, research + MEDDPICC-driven persistence. COORDINATION = calendar coordination after a booking failure.)

SENDER:
- Company: {user_company_name}
- Capabilities & business summary: {user_company_research}
- Proof points / outcomes: {sender_proof}
- Differentiators: {sender_advantages}

PROSPECT (recipient):
- Name: {dm_name} | Company: {dm_company}
- Their role in the buying decision (our assessment): {recipient_role_signal}

TARGET INTELLIGENCE (NUDGE mode):
- Research summary: {target_research_summary}
- Growth signals: {growth_hooks}
- Pain indicators: {pain_hooks}
- News / recent triggers: {news_hooks}
- Why now: {opportunity_reason}

MEDDPICC READ (hypotheses, NOT confirmed facts):
- Evidenced need/pain: {need_evidence}
- Pains we solve: {matched_pains}
- Services that map: {matched_services}
- Value / metrics angle: {metrics}
- Likely economic buyer (role): {economic_buyer}
- Likely champion (role): {champion}
- Likely decision criteria: {decision_criteria}

COMMUNICATION THREAD so far (most recent first) — every angle/hook here is ALREADY used; the new email
must open a fresh one and be richer than all of them:
{thread_history}

{progress_context}

ALTERNATIVE AVAILABLE SLOTS:
{alternative_slots_context}

GREETING (BOTH modes, ALWAYS): begin the email with a greeting line "Hi <first name>," using ONLY the
recipient's first name (the first word of "{dm_name}") — on its own line, followed by a blank line, before
the body.

STRATEGY:
- If MODE is 'COORDINATION':
    - If alternative slots are provided: Politely state the requested time was not available. Offer the listed slots as concrete options. Keep tone warm and professional.
    - If no slots available: Apologize that the slot didn't fit, ask them to suggest 2-3 alternative times.
    - DO NOT claim the requested time works. DO NOT promise a calendar invite without a confirmed booking.

- If MODE is 'NUDGE' (MEDDPICC, each step grounded — never invented):
    1. IDENTIFY PAIN: open with a NEW pain/signal NOT already used in the thread above.
    2. CAPABILITY + DECISION CRITERIA: bridge that to the exact sender capability that fits their likely criteria.
    3. METRICS: add a credible proof/outcome or cost-of-inaction — a sender proof point, or a general
       pattern phrased without a hard figure ('teams like yours often…'). NEVER invent a number or attach a
       metric/percentage to a NAMED client unless that exact figure is in the sender's proof points.
    4. ASK calibrated to the recipient's role (decision-owner: business outcome + a short call; champion/
       influencer: make it forwardable, or ask who owns this).
    5. Escalation level {escalation_level}: Early (#1-3) confident, insight-led; Mid (#4-6) more direct,
       stronger; Late (#7-9) build urgency, cost of inaction visible; Final (#10-11) blunt, give an easy out.

STRICT CONSTRAINTS:
1. ABSOLUTELY NO PLACEHOLDERS or bracketed/curly/angle tokens ([Name], {{metric}}, <X>). Fill a real value or omit it.
2. NO FABRICATION: every fact, client name, outcome, and number must come from the data above only.
3. For NUDGE: do NOT reuse any angle, hook, framing, or proof already in the thread.
4. Do NOT state MEDDPICC hypotheses as facts and do NOT expose framework jargon. No "hope this finds you well" filler.
5. Sign off EXACTLY, with a blank line after 'Best regards,' and a single line break between name and company:
   "Best regards,

   {user_name}
   {user_company_name}"
6. 150-220 words; richer and more substantive than the prior emails. Write each paragraph as ONE continuous
   line with NO hard line breaks inside it; separate sections with a single blank line only.
7. Return subject + body.
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
                "sender_proof": _join(user_intel.get("proof_points")),
                "sender_advantages": _join(user_intel.get("competitive_advantages")),
                "dm_name": dm_info.get("name", ""),
                "dm_company": target_company_name,
                "recipient_role_signal": tc_intel.get("recipient_role_signal") or "Role/influence not yet assessed.",
                "target_research_summary": tc_intel.get("research_summary", "") or "N/A",
                "growth_hooks": _join(tc_intel.get("growth_hooks")),
                "pain_hooks": _join(tc_intel.get("pain_hooks")),
                "news_hooks": _join(tc_intel.get("news_hooks")),
                "opportunity_reason": tc_intel.get("opportunity_reason", "") or "N/A",
                "need_evidence": _join(tc_intel.get("need_evidence")),
                "matched_pains": _join(tc_intel.get("matched_pains")),
                "matched_services": _join(tc_intel.get("matched_services")),
                "metrics": _join(tc_intel.get("metrics")),
                "economic_buyer": _join(tc_intel.get("economic_buyer")),
                "champion": _join(tc_intel.get("champion")),
                "decision_criteria": _join(tc_intel.get("decision_criteria")),
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

REMINDER_ESCALATION_PROMPT = """You are an elite B2B sales ghostwriter writing the NEXT no-reply follow-up
using the MEDDPICC framework. The prospect has not replied. This email MUST be RICHER and MORE COMPELLING
than EVERY previous email below — open a genuinely NEW angle none of them used and add more substance that
pulls the prospect's interest toward us.

SENDER:
- Company: {user_company_name}
- Capabilities & business summary: {user_company_research}
- Proof points / outcomes: {sender_proof}
- Differentiators: {sender_advantages}

PROSPECT (recipient):
- Name: {dm_name} | Company: {target_company_name}
- Their role in the buying decision (our assessment): {recipient_role_signal}

TARGET INTELLIGENCE:
- Research summary: {target_research_summary}
- Growth signals: {growth_hooks}
- Pain indicators: {pain_hooks}
- News / recent triggers: {news_hooks}
- Why now: {opportunity_reason}

MEDDPICC READ (hypotheses, NOT confirmed facts):
- Evidenced need/pain: {need_evidence}
- Pains we solve: {matched_pains}
- Services that map: {matched_services}
- Value / metrics angle: {metrics}
- Likely economic buyer (role): {economic_buyer}
- Likely champion (role): {champion}
- Likely decision criteria: {decision_criteria}

ALL PREVIOUS EMAILS WE HAVE ALREADY SENT (do NOT reuse ANY of their angles, hooks, openings, or proof):
{previous_emails}

ESCALATION: {escalation_level}
- Level 1: confident re-engagement from a FRESH pain/value angle; add a concrete proof point.
- Level 2 (final): strongest and most substantive; make the cost of inaction vivid, be direct, give an easy out.

GREETING (ALWAYS): begin with a greeting line "Hi <first name>," using ONLY the recipient's first name
(the first word of "{dm_name}") — on its own line, followed by a blank line, before the body.

MISSION (MEDDPICC, each step grounded — never invented):
1. IDENTIFY PAIN: open with a NEW pain/signal not used in ANY previous email above.
2. CAPABILITY + DECISION CRITERIA: bridge to the exact sender capability that fits their likely criteria.
3. METRICS: add a credible proof/outcome or cost-of-inaction. Use ONLY the sender's stated proof points,
   or a general pattern phrased without a hard figure ('teams like yours often reduce…'). NEVER invent a
   number, percentage, or result, and NEVER attach a metric/percentage to a NAMED client unless that exact
   figure appears verbatim in the sender's proof points. When unsure, stay qualitative — no number.
4. ASK calibrated to the recipient's role (decision-owner: business outcome + a short call; champion/
   influencer: make it forwardable or ask who owns this).

STRICT CONSTRAINTS:
1. ABSOLUTELY NO PLACEHOLDERS or bracketed/curly/angle tokens of ANY kind ([Name], {{metric}}, <X>). Fill a
   real value or omit it — never bracket it.
2. NO FABRICATION: every fact, client name, outcome, and number must come from the sender data or target
   intelligence above. Do not invent percentages or attribute invented results to named clients.
3. Do NOT reuse the angle, hook, framing, or proof of ANY previous email above.
4. Do NOT state MEDDPICC hypotheses as facts (who the buyer is, their decision/paper process) and do NOT
   expose framework jargon. No "hope this finds you well" filler.
5. Sign off EXACTLY, with a blank line after 'Best regards,' and a single line break between name and company:
   "Best regards,

   {user_name}
   {user_company_name}"
6. 150-220 words; richer and more substantive than the prior emails. Write each paragraph as ONE continuous
   line with NO hard line breaks inside it; separate sections with a single blank line only.
7. Return subject + body.
"""

def draft_reminder_escalation_email(
    previous_emails: str,
    user_intel: dict,
    target_company_intel: dict,
    dm_name: str,
    target_company_name: str,
    user_name: str = "Account Manager",
    reminder_number: int = 1,
) -> str:
    """Draft a MEDDPICC-grounded no-reply escalation email that is richer than ALL
    previously sent emails. `previous_emails` is the full history of what we've sent
    (so each round opens a fresh angle); `target_company_intel` carries the MEDDPICC
    scorecard fields + recipient role; `user_intel` carries proof points/differentiators."""
    structured_llm = llm.with_structured_output(EmailDraftResponse)
    prompt = ChatPromptTemplate.from_template(REMINDER_ESCALATION_PROMPT)
    chain = prompt | structured_llm

    escalation_level = f"Level {reminder_number} of 2"
    tci = target_company_intel or {}
    ui = user_intel or {}

    def _join(val):
        if isinstance(val, (list, tuple)):
            return "; ".join(str(v) for v in val if str(v).strip()) or "N/A"
        return str(val) if val else "N/A"

    try:
        logger.info(f"[GHOSTWRITER] Drafting MEDDPICC reminder escalation #{reminder_number} for {dm_name} at {target_company_name}...")
        data = run_openai_guarded(
            "reminder_escalation_generation",
            lambda: chain.invoke({
                "user_company_name": ui.get("company_name", ""),
                "user_company_research": ui.get("deep_research", "") or "N/A",
                "sender_proof": _join(ui.get("proof_points")),
                "sender_advantages": _join(ui.get("competitive_advantages")),
                "dm_name": dm_name,
                "target_company_name": target_company_name,
                "recipient_role_signal": tci.get("recipient_role_signal") or "Role/influence not yet assessed.",
                "target_research_summary": tci.get("research_summary", "") or "N/A",
                "growth_hooks": _join(tci.get("growth_hooks")),
                "pain_hooks": _join(tci.get("pain_hooks")),
                "news_hooks": _join(tci.get("news_hooks")),
                "opportunity_reason": tci.get("opportunity_reason", "") or "N/A",
                "need_evidence": _join(tci.get("need_evidence")),
                "matched_pains": _join(tci.get("matched_pains")),
                "matched_services": _join(tci.get("matched_services")),
                "metrics": _join(tci.get("metrics")),
                "economic_buyer": _join(tci.get("economic_buyer")),
                "champion": _join(tci.get("champion")),
                "decision_criteria": _join(tci.get("decision_criteria")),
                "previous_emails": previous_emails or "(No previous emails on record)",
                "escalation_level": escalation_level,
                "user_name": user_name,
            }),
            fallback=None,
        )
        if data:
            dumped = data.model_dump()
            return clean_email_body(dumped["body"])
        return f"Hi {dm_name}, I wanted to follow up on my previous message.\n\nBest regards,\n\n{user_name}\n{ui.get('company_name', '')}"
    except Exception as e:
        logger.error(f"[GHOSTWRITER] Reminder escalation drafting failure: {e}")
        return f"Hi {dm_name}, I wanted to follow up on my previous message.\n\nBest regards,\n\n{user_name}\n{ui.get('company_name', '')}"

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
