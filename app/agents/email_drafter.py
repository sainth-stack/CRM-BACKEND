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

FOLLOW_UP_PROMPT = """You are an elite B2B sales ghostwriter writing Follow-up #{followup_number} of 2 in an outreach sequence.

MODE: {outreach_mode}
(NUDGE = follow-up nudge; COORDINATION = calendar coordination after a booking failure.)

SENDER:
- Company: {user_company_name}
- Capabilities & business summary: {user_company_research}
- Proof points / outcomes: {sender_proof}
- Differentiators: {sender_advantages}

PROSPECT (recipient):
- Name: {dm_name} | Company: {dm_company}
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

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EMAILS WE HAVE SENT (oldest → newest):
{sent_history}

PROSPECT REPLIES RECEIVED:
{prospect_replies}

LATEST PROSPECT REPLY:
{latest_prospect_reply}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

{progress_context}

ALTERNATIVE AVAILABLE SLOTS:
{alternative_slots_context}

GREETING (BOTH modes, ALWAYS): begin with "Hi <first name>," using ONLY the first word of "{dm_name}" —
on its own line, followed by a blank line.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRATEGY — apply the section that matches Follow-up #{followup_number}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

If MODE is 'COORDINATION':
  - If alternative slots are provided: politely state the requested time was unavailable and offer the listed slots.
  - If no slots: apologise and ask the prospect to suggest 2-3 times.
  - DO NOT claim any time works. DO NOT promise a calendar invite without a confirmed booking.

═══ FOLLOW-UP #1 — Sender Introduction ═══════════════

  PRIMARY GOAL: introduce {user_company_name} to the prospect.
  The initial email established the pain context. This email answers:
  "Who is {user_company_name} and how exactly do they solve that?"

  Para 1 — PROSPECT CONTEXT (2-3 sentences):
    Combine whatever is available from the two sources below.

    Source A — Fresh intel hooks (if any):
      Use any item from growth_hooks, pain_hooks, news_hooks, need_evidence, or matched_pains
      not already in sent_history. Frame as a forward-looking observation. Never assert as fact.

    Source B — Prospect reply (if latest_prospect_reply is not "N/A"):
      Acknowledge briefly (1 sentence, non-sycophantic). Address their concern directly.
      If "not now" — acknowledge timing, then pivot without dismissing.

    If BOTH empty: write a one-sentence continuity callback to the pain theme from the initial email.
    DO NOT mention {user_company_name} in Para 1.

  Para 2 — SENDER INTRODUCTION (2-3 sentences):
    Introduce {user_company_name}: what they are and the specific capability that maps to Para 1's pain.
    Name {user_company_name} explicitly. Be concrete about what you do and for whom.
    NEVER mention {user_company_name}'s location, headquarters, city, or country — irrelevant to the prospect.

  Para 3 — PROOF + CTA (2 sentences):
    Sentence 1: one proof point from sender_proof relevant to the pain. Qualitative if proof is empty.
    Sentence 2: ONE curiosity question — no meeting ask.
      Good: "How are you currently handling X?" / "Is that on your radar this year?"
    NEVER: "does this resonate?" / "let's schedule" / "I'd love to connect."

═══ FOLLOW-UP #2 — Reply-Aware Soft Close ════════════

  This is the LAST email in the sequence. Tone: warm, respectful, zero pressure.
  Write EXACTLY 2 paragraphs — no more, no less.
  Never announce "this is my last email/follow-up" — convey finality through tone only.

  ── CASE A: latest_prospect_reply is NOT "N/A" (prospect HAS replied) ──

  Para 1 — REPLY RESPONSE (2-3 sentences):
    Open by directly engaging with what the prospect said in their last reply.
    If they asked a question: answer it briefly and honestly using only data available above.
      If the answer is not in the data, acknowledge the question and suggest the easiest path to clarity.
    If they raised an objection or gave context: acknowledge it genuinely and address it in 1 sentence.
    If they said "not now" / "we're handling it internally": respect that; no pushback, no pivot pitch.
    Show you heard them. Do NOT parrot their words back verbatim.
    ABSOLUTELY NO product capabilities, proof points, or feature names in Para 1.
    DO NOT mention {user_company_name} in Para 1.

  Para 2 — GRACEFUL EXIT (2-3 sentences, pure closing — zero pitch):
    This paragraph must contain ZERO product information, capabilities, or proof points.
    NEVER mention {user_company_name}'s location, headquarters, city, or country — irrelevant to a soft close.
    Convey finality through tone, not by announcing it.
    Acceptable phrasing: "I'll leave it here for now", "I won't keep filling your inbox",
    "I'll step back and give you space", "I'll let you take it from here" — choose what fits naturally.
    State warmly you're available if circumstances change or they want to revisit.
    Leave one low-friction open door: "you know where to find me" / "feel free to reach out whenever".
    End on a genuinely warm, human note — wish them well on whatever they shared in their reply.

  ── CASE B: latest_prospect_reply IS "N/A" (no reply received) ──

  Para 1 — CONTINUITY (1-2 sentences):
    Briefly reference the specific pain or theme from the previous emails —
    not a copy of any line already sent, just a natural callback showing you remember the context.
    ABSOLUTELY NO product capabilities, proof points, or feature names in Para 1.
    DO NOT mention {user_company_name} in Para 1.

  Para 2 — GRACEFUL EXIT (2-3 sentences, pure closing — zero pitch):
    This paragraph must contain ZERO product information, capabilities, or proof points.
    NEVER mention {user_company_name}'s location, headquarters, city, or country — irrelevant to a soft close.
    Convey finality through tone, not by announcing it.
    Do NOT reference "not hearing back" or imply the prospect ignored you.
    Acceptable phrasing: "I'll leave it here for now", "I won't keep filling your inbox",
    "I'll step back", "I'll let you take it from here" — choose what fits naturally.
    State warmly you're available if priorities shift or they want to explore it later.
    Leave one low-friction open door.

  SIGN-OFF (mandatory — always the last lines of the email, no exceptions):
Best regards,

{user_name}
{user_company_name}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRICT CONSTRAINTS (both follow-up numbers)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. NO PLACEHOLDERS of any kind ([Name], {{metric}}, <X>). Fill or omit.
2. NO FABRICATION: every fact and number must come from the data above only.
3. Never reuse the exact opening line or hook from any previously sent email.
4. INFERENCE RULE: a reply does not confirm any pain asserted in previous emails.
5. BANNED openers: "I wanted to follow up", "Just following up", "Touching base", "Checking in",
   "Hope this finds you", "I wanted to reach out", "As I mentioned".
6. ZERO PERFORMANCE CLAIMS unless verbatim in sender_proof.
7. NEVER mention {user_company_name}'s location, headquarters, city, or country anywhere in the email,
   even if present in user_company_research. It is irrelevant to the prospect.
8. No framework jargon. Write like a human.
9. 80-130 words for Follow-up #2 (shorter is fine for a soft close). 110-160 words for Follow-up #1.
   Each paragraph ONE continuous line — no internal line breaks.
10. Return subject + body.
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
    body: str = Field(
        description=(
            "The full email body including greeting, all paragraphs, and the complete sign-off. "
            "The sign-off MUST always be the last lines, exactly as: "
            "'Best regards,\\n\\n<sender name>\\n<company name>'. "
            "Never omit the sign-off."
        )
    )

def _followup_escalation_level(n: int) -> str:
    if n == 1:
        return "#1 of 2 — insight-led, engagement question only, no meeting ask."
    return "#2 of 2 (final) — more direct, can ask for a short call or suggest a specific next step."


def draft_followup_email(
    user_intel: dict,
    dm_info: dict,
    target_company_name: str,
    thread_history: str,         # kept for backward compat; prefer sent_history + prospect_replies
    followup_number: int,
    manual_scheduling: bool = False,
    alternative_slots: list = None,
    user_name: str = "Account Manager",
    target_company_intel: dict = None,
    sent_history: str = None,           # all emails WE sent, oldest→newest
    prospect_replies: str = None,       # all prospect replies, oldest→newest
    latest_prospect_reply: str = None,  # the specific reply that triggered this follow-up
):
    """Drafts a context-aware follow-up that analyses previous sent emails and prospect replies."""
    structured_llm = llm.with_structured_output(EmailDraftResponse)
    prompt = ChatPromptTemplate.from_template(FOLLOW_UP_PROMPT)
    chain = prompt | structured_llm

    mode = "COORDINATION" if manual_scheduling else "NUDGE"
    progress = (
        f"Current Progress: This is Follow-up #{followup_number} of 2."
        if not manual_scheduling
        else "Context: Manual scheduling coordination required — booking attempt failed or slot was unavailable."
    )

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

    # Resolve conversation history fields.
    # If new fields are provided use them; fall back to legacy thread_history.
    resolved_sent_history = sent_history or thread_history or "(No emails sent yet)"
    resolved_prospect_replies = prospect_replies or "N/A (no replies received)"
    resolved_latest_reply = latest_prospect_reply or "N/A"

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
                "sent_history": resolved_sent_history,
                "prospect_replies": resolved_prospect_replies,
                "latest_prospect_reply": resolved_latest_reply,
                "progress_context": progress,
                "followup_number": followup_number,
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

REMINDER_ESCALATION_PROMPT = """You are an elite B2B sales ghostwriter writing Reminder #{reminder_number} in an outreach sequence.
The prospect has NOT replied to any email sent so far. There is no reply to acknowledge.

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

MEDDPICC:
- Evidenced need/pain: {need_evidence}
- Pains we solve: {matched_pains}
- Services that map: {matched_services}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ALL EMAILS WE HAVE SENT (oldest → newest):
{previous_emails}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

GREETING (ALWAYS): begin with "Hi <first name>," using ONLY the first word of "{dm_name}" —
on its own line, followed by a blank line.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRATEGY — apply the section that matches Reminder #{reminder_number}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

═══ REMINDER #1 — Sender Introduction ════════════════

  PRIMARY GOAL: introduce {user_company_name} using fresh intel the prospect hasn't seen yet.

  Para 1 — PROSPECT CONTEXT (2-3 sentences):
    Use any item from growth_hooks, pain_hooks, news_hooks, need_evidence, or matched_pains
    not already in previous_emails. Frame as a forward-looking observation. Never assert as fact.
    If NO fresh hooks exist: one-sentence continuity callback to the pain theme of prior emails.
    INFERENCE RULE: silence does NOT confirm any pain claimed in previous emails.
    DO NOT mention {user_company_name} in Para 1.

  Para 2 — SENDER INTRODUCTION (2-3 sentences):
    Introduce {user_company_name}: what they are and the specific capability that maps to Para 1's pain.
    Name {user_company_name} explicitly. Be concrete about what you do and for whom.

  Para 3 — PROOF + CTA (2 sentences):
    Sentence 1: one proof point from sender_proof relevant to the pain. Qualitative if proof is empty.
    Sentence 2: ONE curiosity question — no meeting ask.
      Good: "How are you currently handling X?" / "Is that on your radar this year?"
    NEVER: "does this resonate?" / "let's schedule" / "I'd love to connect."

═══ REMINDER #2 — Final / Soft Close ═════════════════

  This is the LAST email in the sequence. Tone: warm, respectful, zero pressure.

  Para 1 — CONTINUITY (1-2 sentences):
    Briefly reference the specific pain or theme discussed across the previous emails —
    not a copy of any line already sent, just a natural callback showing you remember the context.
    DO NOT mention {user_company_name} in Para 1.

  Para 2 — SOFT CLOSE / OPEN DOOR (3-4 sentences):
    Politely acknowledge that you have reached out a few times without hearing back.
    Express no hard feelings — make clear this is the last note and there is zero pressure.
    State warmly that your door remains open if their situation or priorities change.
    Leave them with a genuine, low-friction way to re-engage if the time is right.
    Do NOT pitch again. Do NOT ask for a meeting. Do NOT summarise your product.

SIGN-OFF (mandatory — always the last lines of the email, no exceptions):
Best regards,

{user_name}
{user_company_name}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STRICT CONSTRAINTS:
1. NO PLACEHOLDERS of any kind. Fill or omit.
2. NO FABRICATION: every fact and number must come from the data above only.
3. Never reuse the exact opening line or hook from any previous email.
4. BANNED openers: "I wanted to follow up", "Just following up", "Touching base",
   "Checking in", "Hope this finds you", "As I mentioned", "I wanted to reach out".
5. ZERO PERFORMANCE CLAIMS unless verbatim in sender_proof. If empty, stay qualitative.
6. No framework jargon. Write like a human.
7. 80-130 words for Reminder #2 (shorter is fine for a soft close). 110-160 words for Reminder #1.
   Each paragraph ONE continuous line — no internal line breaks.
8. Return subject + body.
"""

def draft_reminder_escalation_email(
    previous_emails: str,
    user_intel: dict,
    target_company_intel: dict,
    dm_name: str,
    target_company_name: str,
    user_name: str = "Account Manager",
    reminder_number: int = 1,
    previous_replies: str = None,   # kept for call-site compat; not used in prompt
) -> str:
    """Draft a no-reply reminder that introduces the sender company using fresh hooks.

    previous_emails: all SENT emails oldest→newest (avoids angle repetition).
    previous_replies: ignored — reminders are sent only when the prospect has not replied.
    """
    structured_llm = llm.with_structured_output(EmailDraftResponse)
    prompt = ChatPromptTemplate.from_template(REMINDER_ESCALATION_PROMPT)
    chain = prompt | structured_llm

    tci = target_company_intel or {}
    ui = user_intel or {}

    def _join(val):
        if isinstance(val, (list, tuple)):
            return "; ".join(str(v) for v in val if str(v).strip()) or "N/A"
        return str(val) if val else "N/A"

    try:
        logger.info(f"[GHOSTWRITER] Drafting reminder #{reminder_number} for {dm_name} at {target_company_name}...")
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
                "previous_emails": previous_emails or "(No previous emails on record)",
                "reminder_number": reminder_number,
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
