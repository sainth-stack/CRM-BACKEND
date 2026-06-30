import datetime

from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.logging_config import logger
from app.db import models
from app.services.asset_service import get_asset_download_url, get_org_brochure, get_org_usecases
from app.workers.lifecycle import transition_prospect


MAX_SILENCE_REMINDERS = 6
REMINDER_DRAFT_INDEX_BASE = 100
REMINDER_STATES = {
    1: models.ProspectState.REMINDER_1_SENT,
    2: models.ProspectState.REMINDER_2_SENT,
    3: models.ProspectState.REMINDER_3_SENT,
    4: models.ProspectState.REMINDER_4_SENT,
    5: models.ProspectState.REMINDER_5_SENT,
    6: models.ProspectState.REMINDER_6_SENT,
}
INACTIVITY_STATES = {
    models.ProspectState.INITIAL_SENT,
    models.ProspectState.FOLLOWUP_ACTIVE,
    *REMINDER_STATES.values(),
}


def reminder_draft_index(reminder_number: int) -> int:
    return REMINDER_DRAFT_INDEX_BASE + reminder_number


def reminder_number_from_draft_index(index: int | None) -> int | None:
    if index is None:
        return None
    number = index - REMINDER_DRAFT_INDEX_BASE
    if 1 <= number <= MAX_SILENCE_REMINDERS:
        return number
    return None


def get_reminder_state(reminder_number: int):
    return REMINDER_STATES[reminder_number]


def _owner_scope(dm) -> tuple[str | None, str | None]:
    owner = dm.campaign.owner if dm.campaign else None
    if not owner:
        return None, None
    return owner.organization_id, owner.id


def _existing_pending_reminder_draft(db, dm_id: str, reminder_number: int):
    return (
        db.query(models.EmailDraft)
        .filter(
            models.EmailDraft.decision_maker_id == dm_id,
            models.EmailDraft.draft_type == "REMINDER",
            models.EmailDraft.followup_index == reminder_draft_index(reminder_number),
            models.EmailDraft.status != "SENT",
        )
        .first()
    )


def resolve_next_reminder_slot(db, dm) -> dict | None:
    """Return the next sendable reminder slot, skipping missing asset slots."""
    org_id, owner_user_id = _owner_scope(dm)
    brochure = get_org_brochure(db, org_id, owner_user_id=owner_user_id)
    usecases = get_org_usecases(db, org_id, limit=3, owner_user_id=owner_user_id)
    skipped = []

    for number in range((dm.reminder_count or 0) + 1, MAX_SILENCE_REMINDERS + 1):
        if _existing_pending_reminder_draft(db, dm.id, number):
            return {"number": number, "kind": "pending", "asset": None, "skipped": skipped}
        if number == 1:
            return {"number": number, "kind": "ai_intro", "asset": None, "skipped": skipped}
        if number == 2:
            if brochure:
                return {"number": number, "kind": "brochure", "asset": brochure, "skipped": skipped}
            skipped.append(number)
            continue
        if number in (3, 4, 5):
            usecase_index = number - 3
            if len(usecases) > usecase_index:
                return {"number": number, "kind": "usecase", "asset": usecases[usecase_index], "skipped": skipped}
            skipped.append(number)
            continue
        if number == 6:
            return {"number": number, "kind": "ai_soft_close", "asset": None, "skipped": skipped}
    return None


def _sent_history(db, dm):
    logs = (
        db.query(models.CommunicationLog)
        .filter(models.CommunicationLog.dm_id == dm.id)
        .order_by(models.CommunicationLog.received_at.asc())
        .all()
    )
    sent = [log for log in logs if log.direction == "SENT"]
    replies = [log for log in logs if log.direction == "RECEIVED"]
    previous_emails = "\n\n---\n\n".join(
        f"[Email {i + 1}] Subject: {(log.subject or '')}\n{(log.body or '')}"
        for i, log in enumerate(sent)
    )
    previous_replies = "\n\n---\n\n".join(
        f"[Reply {i + 1}] {(log.body or '')}"
        for i, log in enumerate(replies)
    ) or None
    # Root (first-ever) subject, not the most recent — the most recent may
    # already carry a "Re:" prefix, which would otherwise double up.
    root_subject = sent[0].subject if sent else None
    return previous_emails, previous_replies, root_subject


def _target_company_intel(dm) -> dict:
    tc = dm.target_company
    meddpicc = (tc.v2_intel or {}).get("meddpicc", {}) if (tc and tc.v2_intel) else {}
    return {
        "research_summary": (tc.research_summary or "") if tc else "",
        "growth_hooks": (tc.growth_hooks or []) if tc else [],
        "pain_hooks": (tc.pain_hooks or []) if tc else [],
        "news_hooks": (tc.news_hooks or []) if tc else [],
        "opportunity_reason": (tc.opportunity_reason or "") if tc else "",
        "matched_pains": (tc.matched_pains or []) if tc else [],
        "matched_services": (tc.matched_services or []) if tc else [],
        "metrics": meddpicc.get("metrics", ""),
        "economic_buyer": meddpicc.get("economic_buyer", ""),
        "champion": meddpicc.get("champion", ""),
        "decision_criteria": meddpicc.get("decision_criteria", ""),
        "need_evidence": meddpicc.get("need_evidence", ""),
        "recipient_role_signal": dm.relevance_explanation or "",
    }


def _user_name(dm) -> str:
    owner = dm.campaign.owner if dm.campaign else None
    if owner and owner.full_name:
        return owner.full_name
    if owner and owner.email:
        return owner.email.split("@")[0]
    return "Account Manager"


def create_next_reminder_draft_for_review(db, dm, *, actor: str = "orchestrator") -> dict:
    """Create the next reminder draft. Does not queue or send email."""
    slot = resolve_next_reminder_slot(db, dm)
    if not slot:
        return {"status": "exhausted", "skipped": []}
    reminder_number = slot["number"]
    existing = _existing_pending_reminder_draft(db, dm.id, reminder_number)
    if existing:
        dm.next_action_at = None
        return {"status": "existing", "draft": existing, "reminder_number": reminder_number, "skipped": slot["skipped"]}
    if slot["kind"] == "pending":
        dm.next_action_at = None
        return {"status": "existing", "draft": None, "reminder_number": reminder_number, "skipped": slot["skipped"]}

    from app.agents.email_drafter import draft_asset_link_email, draft_reminder_escalation_email, reply_subject

    campaign = dm.campaign
    ui = campaign.user_intel
    tc = dm.target_company
    user_name = _user_name(dm)
    previous_emails, previous_replies, root_subject = _sent_history(db, dm)
    subject = reply_subject(root_subject)

    if slot["kind"] in ("ai_intro", "ai_soft_close"):
        body = draft_reminder_escalation_email(
            previous_emails=previous_emails,
            previous_replies=previous_replies,
            user_intel={
                "company_name": ui.company_name,
                "deep_research": ui.deep_research or "",
                "proof_points": ui.proof_points or [],
                "competitive_advantages": ui.competitive_advantages or [],
            },
            target_company_intel=_target_company_intel(dm),
            dm_name=dm.name,
            target_company_name=tc.name if tc else "",
            user_name=user_name,
            reminder_number=1 if slot["kind"] == "ai_intro" else 2,
        )
    else:
        draft = draft_asset_link_email(
            asset=slot["asset"],
            asset_url=get_asset_download_url(slot["asset"].id),
            dm_name=dm.name,
            target_company_name=tc.name if tc else "",
            sender_name=user_name,
            user_company_name=ui.company_name,
            content_kind=slot["kind"],
            pain_hint=(tc.matched_pains or tc.pain_hooks or tc.opportunity_reason) if tc else None,
        )
        subject = reply_subject(root_subject) if root_subject else draft["subject"]
        body = draft["body"]

    draft = models.EmailDraft(
        campaign_id=campaign.id,
        decision_maker_id=dm.id,
        subject=subject,
        body=body,
        status="DRAFTED",
        is_approved=False,
        followup_index=reminder_draft_index(reminder_number),
        draft_type="REMINDER",
        dispatch_state="REQUIRES_REVIEW",
    )
    db.add(draft)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = _existing_pending_reminder_draft(db, dm.id, reminder_number)
        if existing:
            return {"status": "existing", "draft": existing, "reminder_number": reminder_number, "skipped": slot["skipped"]}
        raise

    skipped_count = len(slot["skipped"])
    transition_prospect(
        db,
        dm,
        state=dm.state,
        status=f"REMINDER_{reminder_number}_DRAFTED",
        reason="REMINDER_DRAFTED",
        actor=actor,
        metadata={
            "draft_type": "REMINDER",
            "draft_id": draft.id,
            "reminder_count": reminder_number,
            "reminder_kind": slot["kind"],
            "skipped_reminders": slot["skipped"],
            "requires_review": True,
        },
    )
    dm.next_action_at = None
    if skipped_count:
        logger.info("[REMINDER] Skipped missing reminder slots %s for %s", slot["skipped"], dm.name)
    return {"status": "drafted", "draft": draft, "reminder_number": reminder_number, "skipped": slot["skipped"]}


def mark_reminder_sent_effects(db, draft, dm, *, message_id: str, sent_at: datetime.datetime) -> None:
    number = reminder_number_from_draft_index(draft.followup_index)
    if not number:
        return
    dm.reminder_count = number
    transition_prospect(
        db,
        dm,
        state=get_reminder_state(number),
        status=f"REMINDER_{number}_SENT",
        reason="REMINDER_SENT",
        actor="user",
        metadata={
            "draft_type": "REMINDER",
            "draft_id": draft.id,
            "message_id": message_id,
            "reminder_count": number,
        },
    )
    dm.next_action_at = sent_at + datetime.timedelta(minutes=settings.NUDGE_FOLLOWUP_DELAY_MINUTES)
