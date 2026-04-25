import datetime
from datetime import UTC

from sqlalchemy.orm import Session

from app.db import models


RETRY_WINDOW_DAYS = 90
DISCOVERY_HOLD_WINDOW_HOURS = 96


def utcnow_naive() -> datetime.datetime:
    return datetime.datetime.now(UTC).replace(tzinfo=None)


def clear_hold_fields(dm: models.DecisionMaker) -> None:
    dm.hold_source_dm_id = None
    dm.held_at = None
    dm.hold_release_at = None
    dm.pre_hold_state = None
    dm.pre_hold_status = None
    dm.pre_hold_next_action_at = None


def _normalize_reason(reason: str | models.ProspectTerminationReason | None) -> str | None:
    if isinstance(reason, models.ProspectTerminationReason):
        return reason.value
    return reason


def record_transition(
    db: Session,
    dm: models.DecisionMaker,
    *,
    from_state: models.ProspectState | None,
    from_status: str | None,
    reason: str | models.ProspectTerminationReason | None = None,
    actor: str = "system",
    metadata: dict | None = None,
) -> None:
    db.add(
        models.ProspectLifecycleTransition(
            campaign_id=dm.campaign_id,
            dm_id=dm.id,
            from_state=from_state,
            to_state=dm.state,
            from_status=from_status,
            to_status=dm.status,
            reason=_normalize_reason(reason),
            actor=actor,
            transition_metadata=metadata,
        )
    )


def transition_prospect(
    db: Session,
    dm: models.DecisionMaker,
    *,
    state: models.ProspectState | None = None,
    status: str | None = None,
    reason: str | models.ProspectTerminationReason | None = None,
    actor: str = "system",
    metadata: dict | None = None,
) -> bool:
    previous_state = dm.state
    previous_status = dm.status

    if state is not None:
        dm.state = state
    if status is not None:
        dm.status = status

    changed = previous_state != dm.state or previous_status != dm.status or metadata is not None or reason is not None
    if changed:
        record_transition(
            db,
            dm,
            from_state=previous_state,
            from_status=previous_status,
            reason=reason,
            actor=actor,
            metadata=metadata,
        )
    return changed


def terminate_prospect(
    db: Session,
    dm: models.DecisionMaker,
    reason: models.ProspectTerminationReason,
    *,
    retryable: bool = True,
    now: datetime.datetime | None = None,
    actor: str = "system",
    metadata: dict | None = None,
) -> None:
    now = now or utcnow_naive()
    transition_prospect(
        db,
        dm,
        state=models.ProspectState.TERMINATED,
        status="TERMINATED",
        reason=reason,
        actor=actor,
        metadata=metadata,
    )
    dm.next_action_at = None
    dm.termination_reason = reason
    dm.retry_after = now + datetime.timedelta(days=RETRY_WINDOW_DAYS) if retryable else None
    clear_hold_fields(dm)


def hold_company_siblings(db: Session, active_dm: models.DecisionMaker) -> list[models.DecisionMaker]:
    now = utcnow_naive()
    siblings = db.query(models.DecisionMaker).filter(
        models.DecisionMaker.target_company_id == active_dm.target_company_id,
        models.DecisionMaker.id != active_dm.id,
    ).all()

    held = []
    for sibling in siblings:
        if sibling.state in [
            models.ProspectState.TERMINATED,
            models.ProspectState.MEETING_BOOKED,
            models.ProspectState.ON_HOLD,
        ]:
            continue

        sibling.pre_hold_state = sibling.state or models.ProspectState.NEW
        sibling.pre_hold_status = sibling.status or sibling.pre_hold_state.value
        sibling.pre_hold_next_action_at = sibling.next_action_at
        sibling.hold_source_dm_id = active_dm.id
        sibling.held_at = now
        sibling.hold_release_at = None
        sibling.next_action_at = None
        transition_prospect(
            db,
            sibling,
            state=models.ProspectState.ON_HOLD,
            status=models.ProspectState.ON_HOLD.value,
            reason="COMPANY_HOLD",
            metadata={"hold_source_dm_id": active_dm.id},
        )
        sibling.termination_reason = None
        sibling.retry_after = None
        held.append(sibling)

    return held


def arm_company_hold_window(
    db: Session,
    active_dm: models.DecisionMaker,
    *,
    sent_at: datetime.datetime | None = None,
) -> None:
    sent_at = sent_at or utcnow_naive()
    release_at = sent_at + datetime.timedelta(hours=DISCOVERY_HOLD_WINDOW_HOURS)

    held_siblings = db.query(models.DecisionMaker).filter(
        models.DecisionMaker.target_company_id == active_dm.target_company_id,
        models.DecisionMaker.hold_source_dm_id == active_dm.id,
        models.DecisionMaker.state == models.ProspectState.ON_HOLD,
    ).all()

    for sibling in held_siblings:
        sibling.hold_release_at = release_at


def restore_held_company_siblings(db: Session, active_dm: models.DecisionMaker) -> list[models.DecisionMaker]:
    held_siblings = db.query(models.DecisionMaker).filter(
        models.DecisionMaker.target_company_id == active_dm.target_company_id,
        models.DecisionMaker.hold_source_dm_id == active_dm.id,
        models.DecisionMaker.state == models.ProspectState.ON_HOLD,
    ).all()

    restored = []
    for sibling in held_siblings:
        restored_state = sibling.pre_hold_state or models.ProspectState.NEW
        restored_status = sibling.pre_hold_status or restored_state.value
        transition_prospect(
            db,
            sibling,
            state=restored_state,
            status=restored_status,
            reason="HOLD_RELEASED",
            metadata={"hold_source_dm_id": active_dm.id},
        )
        sibling.next_action_at = sibling.pre_hold_next_action_at
        sibling.termination_reason = None
        sibling.retry_after = None
        clear_hold_fields(sibling)
        restored.append(sibling)

    return restored


def terminate_held_company_siblings(db: Session, active_dm: models.DecisionMaker) -> list[models.DecisionMaker]:
    held_siblings = db.query(models.DecisionMaker).filter(
        models.DecisionMaker.target_company_id == active_dm.target_company_id,
        models.DecisionMaker.hold_source_dm_id == active_dm.id,
        models.DecisionMaker.state == models.ProspectState.ON_HOLD,
    ).all()

    terminated = []
    now = utcnow_naive()
    for sibling in held_siblings:
        terminate_prospect(
            db,
            sibling,
            models.ProspectTerminationReason.INTERNAL_LEAD_SECURED,
            retryable=False,
            now=now,
            metadata={"hold_source_dm_id": active_dm.id},
        )
        terminated.append(sibling)

    return terminated


def terminate_company_siblings(
    db: Session,
    active_dm: models.DecisionMaker,
    *,
    actor: str = "system",
) -> list[models.DecisionMaker]:
    siblings = db.query(models.DecisionMaker).filter(
        models.DecisionMaker.target_company_id == active_dm.target_company_id,
        models.DecisionMaker.id != active_dm.id,
    ).all()

    terminated = []
    now = utcnow_naive()
    for sibling in siblings:
        if sibling.state == models.ProspectState.MEETING_BOOKED:
            continue
        if sibling.state == models.ProspectState.TERMINATED and (
            sibling.termination_reason == models.ProspectTerminationReason.INTERNAL_LEAD_SECURED
        ):
            continue

        terminate_prospect(
            db,
            sibling,
            models.ProspectTerminationReason.INTERNAL_LEAD_SECURED,
            retryable=False,
            now=now,
            actor=actor,
            metadata={"booked_dm_id": active_dm.id},
        )
        terminated.append(sibling)

    return terminated


def reactivate_due_prospects(db: Session) -> list[models.DecisionMaker]:
    now = utcnow_naive()
    prospects = db.query(models.DecisionMaker).filter(
        models.DecisionMaker.state == models.ProspectState.TERMINATED,
        models.DecisionMaker.retry_after != None,
        models.DecisionMaker.retry_after <= now,
    ).all()

    reactivated = []
    for prospect in prospects:
        transition_prospect(
            db,
            prospect,
            state=models.ProspectState.NEUTRAL,
            status="REACTIVATED",
            reason="REACTIVATED",
        )
        prospect.next_action_at = now
        prospect.retry_after = None
        prospect.termination_reason = None
        prospect.reminder_count = 0
        prospect.followup_count = 0
        prospect.reply_intent = None
        prospect.intent_last = None
        clear_hold_fields(prospect)
        reactivated.append(prospect)

    return reactivated
