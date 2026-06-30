from app.db import models
from app.agents.email_drafter import draft_followup_email, draft_discovery_request, reply_subject
from app.services.drafting_service import DraftingService
from app.workers.config.celery_app import celery_app
from app.core.logging_config import logger
from sqlalchemy.exc import IntegrityError
import json
import datetime
import asyncio
from datetime import UTC
from app.core.config import settings
from app.workers.lifecycle import transition_prospect
from app.workers.utils import heartbeat_lease, db_session


DISCOVERY_DRAFT_START_INDEX = 100


def _next_discovery_draft_index(db, dm_id: str) -> int:
    existing_indices = [
        draft.followup_index
        for draft in db.query(models.EmailDraft).filter(
            models.EmailDraft.decision_maker_id == dm_id,
            models.EmailDraft.draft_type == "DISCOVERY",
        ).all()
        if draft.followup_index is not None
    ]
    if not existing_indices:
        return DISCOVERY_DRAFT_START_INDEX
    return max(max(existing_indices), DISCOVERY_DRAFT_START_INDEX - 1) + 1


from app.services.observability_service import ObservabilityService
from sqlalchemy.orm import joinedload

def _save_drafts_batch(campaign_id: str, drafts_batch: list):
    """
    Issue 1: Batch-commits a collection of drafts to the database at once.
    This significantly reduces transaction overhead, locks, and connection counts.
    """
    with db_session() as db:
        try:
            # Idempotency Gate (batched): one query for all DMs in this batch that
            # already have an initial draft, instead of one query per draft.
            batch_dm_ids = [dm_id for dm_id, _ in drafts_batch]
            existing_initial = {
                row[0]
                for row in db.query(models.EmailDraft.decision_maker_id).filter(
                    models.EmailDraft.decision_maker_id.in_(batch_dm_ids),
                    models.EmailDraft.followup_index == 0,
                ).all()
            }

            for dm_id, draft_set in drafts_batch:
                if dm_id in existing_initial:
                    logger.info(f"[BATCH IDEMPOTENCY] Initial email draft already exists for DM {dm_id}. Skipping insertion.")
                    continue

                dm = db.query(models.DecisionMaker).filter(models.DecisionMaker.id == dm_id).first()
                if not dm:
                    continue

                # Sequence path: save the initial email only (follow-ups drafted on-demand)
                if hasattr(draft_set, "sequence") and draft_set.sequence:
                    for email_data in draft_set.sequence:
                        email_draft = models.EmailDraft(
                            campaign_id=campaign_id,
                            decision_maker_id=dm.id,
                            subject=email_data["subject"],
                            body=email_data["body"],
                            status="DRAFTED",
                            followup_index=email_data["followup_index"],
                            draft_type=email_data["draft_type"],
                            variants=draft_set.variants if email_data["followup_index"] == 0 else None,
                            strategic_observation=draft_set.strategic_observation if email_data["followup_index"] == 0 else None,
                            pain_hypothesis=draft_set.pain_hypothesis if email_data["followup_index"] == 0 else None,
                            personalization_hook=draft_set.personalization_hook if email_data["followup_index"] == 0 else None,
                        )
                        db.add(email_draft)
                else:
                    # Legacy single-email path
                    primary_variant = draft_set.variants.get("primary", list(draft_set.variants.values())[0])
                    email_draft = models.EmailDraft(
                        campaign_id=campaign_id,
                        decision_maker_id=dm.id,
                        subject=primary_variant.get("subject"),
                        body=primary_variant.get("body"),
                        status="DRAFTED",
                        followup_index=0,
                        draft_type="INITIAL",
                        variants=draft_set.variants,
                        strategic_observation=draft_set.strategic_observation,
                        pain_hypothesis=draft_set.pain_hypothesis,
                        personalization_hook=draft_set.personalization_hook,
                    )
                    db.add(email_draft)

                transition_prospect(
                    db, dm,
                    state=models.ProspectState.DRAFTED,
                    status="DRAFTED",
                    reason="INITIAL_DRAFTED",
                    actor="ghostwriter",
                    metadata={"draft_type": "SEQUENCE" if hasattr(draft_set, "sequence") else "INITIAL", "v2_enhanced": True},
                )
            db.commit()
            logger.info(f"[BATCH] Committed batch of {len(drafts_batch)} drafts.")
        except Exception as e:
            db.rollback()
            logger.error(f"[BATCH] Failed to commit drafts batch: {e}")


def _get_pending_prospects(db, campaign_id: str, lock_key: str) -> list[dict]:
    dm_data_list = []
    campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
    if not campaign or not campaign.user_intel:
        from app.core.security import release_lock
        release_lock(lock_key)
        return []

    # Temporal Boundary Check: Security gate for trial accounts
    owner = campaign.owner
    if owner and owner.is_demo and owner.demo_expires_at:
        if owner.demo_expires_at.replace(tzinfo=UTC) < datetime.datetime.now(UTC):
            logger.info(f"[CAMPAIGN] Campaign {campaign_id} trial expired.")
            from app.core.security import release_lock
            release_lock(lock_key)
            return []

    # Batch the "already has a draft?" check into ONE query instead of one per DM
    # (the previous per-DM existence query was an N+1 across the whole campaign).
    drafted_dm_ids = {
        row[0]
        for row in db.query(models.EmailDraft.decision_maker_id)
        .filter(models.EmailDraft.campaign_id == campaign_id)
        .distinct()
        .all()
    }

    # Issue 5: Paginate dataset queries to keep memory utilization flat
    PAGE_SIZE = 500
    offset = 0
    while True:
        dms_page = (db.query(models.DecisionMaker)
                    .options(joinedload(models.DecisionMaker.target_company))
                    .filter(models.DecisionMaker.campaign_id == campaign_id)
                    .order_by(models.DecisionMaker.id)
                    .limit(PAGE_SIZE)
                    .offset(offset)
                    .all())
        if not dms_page:
            break

        for dm in dms_page:
            # Issue 5: Filter out pre-drafted items early to avoid massive object overhead
            if dm.id not in drafted_dm_ids:
                dm_data_list.append({
                    "id": dm.id,
                    "name": dm.name,
                    "target_company_id": dm.target_company_id,
                    "target_company_name": dm.target_company.name if dm.target_company else None,
                    "domain": dm.target_company.domain if dm.target_company else None,
                })
        offset += PAGE_SIZE
    return dm_data_list


def _update_campaign_draft_status(db, campaign_id: str, lock_key: str) -> None:
    from app.core.security import release_lock
    try:
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if campaign:
            expected_count = db.query(models.DecisionMaker.id).filter(models.DecisionMaker.campaign_id == campaign_id).count()
            drafted_count = db.query(models.EmailDraft.id).filter(models.EmailDraft.campaign_id == campaign_id).count()

            if expected_count == 0:
                campaign.status = models.CampaignStatus.PARTIAL_SUCCESS
                campaign.status_reason = "No stakeholders were identified."
                logger.warning(f"[CAMPAIGN] Campaign {campaign_id} stalled: No stakeholders identified.")
            elif drafted_count >= expected_count:
                campaign.status = models.CampaignStatus.COMPLETED
                campaign.status_reason = f"Generated {drafted_count}/{expected_count} drafts."
                logger.info(f"[CAMPAIGN] Success: Generated {drafted_count}/{expected_count} drafts for {campaign_id}.")
            elif drafted_count > 0:
                campaign.status = models.CampaignStatus.PARTIAL_SUCCESS
                campaign.status_reason = f"Partial Success: Only {drafted_count}/{expected_count} drafts could be generated."
                logger.warning(f"[CAMPAIGN] Partial Success: Generated {drafted_count}/{expected_count} drafts for {campaign_id}.")
            else:
                campaign.status = models.CampaignStatus.FAILED
                campaign.status_reason = "No drafts could be generated for the identified stakeholders."
                logger.error(f"[CAMPAIGN] No drafts generated: 0/{expected_count} for {campaign_id}.")
            db.commit()
    finally:
        release_lock(lock_key)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
@ObservabilityService.track_celery_task("draft_emails_worker")
def draft_emails_worker(self, campaign_id: str):
    """
    Phase 3: Personalization and drafting worker.
    Generates personalized outreach content for all stakeholders identified in previous phases.
    """
    worker_id = self.request.id
    from app.core.security import acquire_lock, release_lock
    from app.workers.utils import acquire_lease, release_lease

    lock_key = f"campaign_stage6:{campaign_id}"
    if not acquire_lock(lock_key, ttl=3600): return

    try:
        # Claim the DB lease so the heartbeat pulses below actually persist
        # (heartbeat_lease only updates rows where locked_by == worker_id) and the
        # stuck-campaign sweeper won't resurrect this drafting run mid-flight.
        with db_session() as db_lease:
            if not acquire_lease(db_lease, campaign_id, worker_id):
                release_lock(lock_key)
                return

        try:
            logger.info(f"[CAMPAIGN] Initiating draft generation for campaign {campaign_id}")
            
            # 1. Fetch active target list in a paginated, short read session
            dm_data_list = []
            with db_session() as db:
                dm_data_list = _get_pending_prospects(db, campaign_id, lock_key)

            if not dm_data_list:
                return

            logger.info(f"[GHOSTWRITER] Generating content for {len(dm_data_list)} prospects.")
            drafter = DraftingService()
            COMMIT_BATCH = 10   # prospects persisted per DB transaction
            DRAFT_CONCURRENCY = settings.STAGE6_CONCURRENCY

            def _heartbeat():
                # Session-safe lease pulse so the stuck-campaign sweeper won't resurrect
                # this drafting run mid-flight.
                with db_session() as db_heartbeat:
                    try:
                        heartbeat_lease(db_heartbeat, campaign_id, worker_id)
                    except Exception:
                        pass

            async def _draft_all(items):
                """Stream every prospect through ONE bounded pool (DRAFT_CONCURRENCY in
                flight at all times), persisting drafts every COMMIT_BATCH as they
                complete. Concurrency is DECOUPLED from the commit batch, so a single
                slow 3-5 call draft no longer stalls a whole batch (the old per-batch
                `gather` waited for the slowest of 10 before starting the next 10).
                Each draft runs with ZERO DB sessions held during the LLM work; the
                generated drafts are identical — only scheduling changes."""
                sem = asyncio.Semaphore(DRAFT_CONCURRENCY)

                async def _one(item):
                    async with sem:
                        try:
                            ds = await drafter.agenerate_draft_set(None, item["id"])
                            return item["id"], ds
                        except Exception as draft_e:
                            logger.error(f"Drafting Failure for {item['name']}: {draft_e}")
                            return item["id"], None

                total = len(items)
                pending = {asyncio.create_task(_one(it)) for it in items}
                buffer = []
                processed = 0
                since_commit = 0
                try:
                    while pending:
                        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                        for fut in done:
                            dm_id, ds = fut.result()
                            processed += 1
                            since_commit += 1
                            if ds and getattr(ds, "variants", None):
                                buffer.append((dm_id, ds))
                            if since_commit >= COMMIT_BATCH:
                                if buffer:
                                    _save_drafts_batch(campaign_id, buffer)
                                    buffer = []
                                _heartbeat()
                                logger.info(f"   ↳ [GHOSTWRITER] Drafted {processed}/{total} for {campaign_id}.")
                                since_commit = 0
                        del done
                    if buffer:  # flush the final partial batch
                        _save_drafts_batch(campaign_id, buffer)
                        buffer = []
                        _heartbeat()
                        logger.info(f"   ↳ [GHOSTWRITER] Drafted {processed}/{total} for {campaign_id}.")
                finally:
                    for t in pending:
                        t.cancel()

            # Single event loop streams ALL prospects (was one asyncio.run per batch).
            asyncio.run(_draft_all(dm_data_list))

            # Final Campaign Status Update in a short write session
            with db_session() as db:
                _update_campaign_draft_status(db, campaign_id, lock_key)
        finally:
            # Release the DB lease
            with db_session() as db_rel:
                release_lease(db_rel, campaign_id, worker_id)
    finally:
        # Ensure lock is released (idempotent, protects against early exits)
        release_lock(lock_key)


def draft_followup_worker(dm_id: str, db=None, manual_scheduling: bool = False, alternative_slots: list = None):
    """
    Draft and schedule a follow-up nudge.
    Also handles manual-coordination fallbacks when auto-booking fails.
    """
    db_ctx = None
    if db is None:
        db_ctx = db_session()
        db = db_ctx.__enter__()
    try:
        dm = db.query(models.DecisionMaker).filter(models.DecisionMaker.id == dm_id).first()
        if not dm: return

        campaign = dm.campaign
        ui = campaign.user_intel
        user_intel = {
            "company_name": ui.company_name,
            "deep_research": ui.deep_research,
            "proof_points": ui.proof_points or [],
            "competitive_advantages": ui.competitive_advantages or [],
        }

        tc = dm.target_company
        meddpicc = (tc.v2_intel or {}).get("meddpicc", {}) if (tc and tc.v2_intel) else {}
        target_company_intel = {
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

        # Separate sent emails and prospect replies so the drafter can reason about each clearly.
        all_logs = (
            db.query(models.CommunicationLog)
            .filter(models.CommunicationLog.dm_id == dm.id)
            .order_by(models.CommunicationLog.received_at.asc())
            .all()
        )
        sent_logs     = [l for l in all_logs if l.direction == "SENT"]
        received_logs = [l for l in all_logs if l.direction == "RECEIVED"]

        # Format sent history: labelled list oldest→newest
        sent_history = "\n\n".join(
            f"[Email {i + 1}] Subject: {(l.subject or '(no subject)')}\n{(l.body or '')}"
            for i, l in enumerate(sent_logs)
        ) or "(No emails sent yet)"

        # Format prospect replies oldest→newest
        prospect_replies = "\n\n---\n\n".join(
            f"[Reply {i + 1}] {(l.body or '')}"
            for i, l in enumerate(received_logs)
        ) or "N/A"

        # The reply that triggered this follow-up is the most recent one received
        latest_reply = received_logs[-1].body if received_logs else None

        if not manual_scheduling:
            dm.followup_count += 1
            nudge_num = dm.followup_count
        else:
            nudge_num = 0  # Coordination request

        user_name = campaign.owner.full_name if campaign.owner else None
        if not user_name:
            user_name = campaign.owner.email.split('@')[0] if campaign.owner and campaign.owner.email else "Account Manager"

        draft_data = draft_followup_email(
            user_intel=user_intel,
            dm_info={"name": dm.name},
            target_company_name=tc.name if tc else "",
            thread_history="",           # superseded by explicit fields below
            followup_number=nudge_num,
            manual_scheduling=manual_scheduling,
            alternative_slots=alternative_slots,
            user_name=user_name,
            target_company_intel=target_company_intel,
            sent_history=sent_history,
            prospect_replies=prospect_replies,
            latest_prospect_reply=latest_reply,
        )

        if draft_data:
            from sqlalchemy.exc import IntegrityError
            try:
                draft_index = _next_discovery_draft_index(db, dm.id) if manual_scheduling else dm.followup_count
                coordination_dispatch_state = "REQUIRES_REVIEW" if manual_scheduling else "IDLE"
                new_draft = models.EmailDraft(
                    campaign_id=campaign.id,
                    decision_maker_id=dm.id,
                    subject=reply_subject(sent_logs[0].subject) if sent_logs else draft_data["subject"],
                    body=draft_data["body"],
                    status="DRAFTED",
                    followup_index=draft_index,
                    draft_type="FOLLOWUP" if not manual_scheduling else "DISCOVERY",
                    dispatch_state=coordination_dispatch_state
                )
                db.add(new_draft)
                
                if manual_scheduling:
                    if alternative_slots:
                        slot_list = ", ".join(alternative_slots)
                        dm.scheduling_note = (
                            f"[ACTION REQUIRED] Prospect requested a slot outside your available calendar hours. "
                            f"Alternative slots suggested in draft: {slot_list}. "
                            f"Review and send the coordination email manually."
                        )
                    else:
                        dm.scheduling_note = (
                            "[ACTION REQUIRED] Booking failed and no alternative slots could be fetched from Cal.com. "
                            "Prospect is requesting a call - please check your Cal.com calendar and coordinate manually."
                        )
                    logger.info(f"[DISCOVERY] Coordination draft flagged REQUIRES_REVIEW for {dm.name}")
                
                if manual_scheduling:
                    transition_prospect(
                        db,
                        dm,
                        state=models.ProspectState.DISCOVERY_CALL,
                        status="COORDINATION_DRAFTED",
                        reason="DISCOVERY_DRAFTED",
                        actor="ghostwriter",
                        metadata={"draft_type": "DISCOVERY", "draft_index": draft_index, "requires_review": True},
                    )
                    dm.next_action_at = None
                else:
                    transition_prospect(
                        db,
                        dm,
                        state=models.ProspectState.FOLLOWUP_ACTIVE,
                        status=f"FOLLOWUP_{dm.followup_count}_DRAFTED",
                        reason="FOLLOWUP_DRAFTED",
                        actor="ghostwriter",
                        metadata={"draft_type": "FOLLOWUP", "followup_index": dm.followup_count},
                    )
                    dm.next_action_at = None

                dm.termination_reason = None
                dm.retry_after = None
                    
                if db_ctx:
                    db.commit()
                else:
                    db.flush()
                logger.info(f"[FOLLOW-UP] Persistence triggered for {dm.name} | Index: {draft_index} | Mode: {'ManualCoord' if manual_scheduling else 'Nudge'}")
            except IntegrityError:
                if db_ctx:
                    db.rollback()
                # dm is expired after rollback; use the captured dm_id/draft_index, not the ORM object.
                logger.info(f"[IDEMPOTENCY] Benign Collision: Follow-up {draft_index} for DM {dm_id} already exists.")
    finally:
        if db_ctx:
            db_ctx.__exit__(None, None, None)


def draft_discovery_worker(dm_id: str, db=None, is_auto_booking: bool = False):
    """
    Draft the discovery-call request after positive-intent classification or auto-booking.
    """
    db_ctx = None
    if db is None:
        db_ctx = db_session()
        db = db_ctx.__enter__()
    try:
        dm = db.query(models.DecisionMaker).filter(models.DecisionMaker.id == dm_id).first()
        if not dm: return

        campaign = dm.campaign
        user_intel_obj = campaign.user_intel
        offerings = []
        try:
            offerings = json.loads(user_intel_obj.offerings)
            if not isinstance(offerings, list):
                offerings = [str(offerings)]
        except:
            offerings = [str(user_intel_obj.offerings)]

        user_intel = {
            "name": user_intel_obj.company_name,
            "offerings": ", ".join(offerings) if offerings else "AI-driven professional solutions",
            "deep_research": user_intel_obj.deep_research
        }

        user_real_name = campaign.owner.full_name if campaign.owner else None
        if not user_real_name:
            user_real_name = campaign.owner.email.split('@')[0] if campaign.owner and campaign.owner.email else "Account Manager"

        last_reply = db.query(models.CommunicationLog).filter(
            models.CommunicationLog.dm_id == dm.id,
            models.CommunicationLog.direction == "RECEIVED"
        ).order_by(models.CommunicationLog.received_at.desc()).first()

        first_sent = db.query(models.CommunicationLog).filter(
            models.CommunicationLog.dm_id == dm.id,
            models.CommunicationLog.direction == "SENT"
        ).order_by(models.CommunicationLog.received_at.asc()).first()

        draft = draft_discovery_request(
            user_intel=user_intel,
            dm_name=dm.name,
            dm_position=dm.position,
            target_company=dm.target_company.name,
            last_interest=last_reply.body if last_reply else "Interest in AI solutions",
            booked_link=dm.meeting_link if is_auto_booking else None,
            user_real_name=user_real_name
        )

        if draft:
            try:
                new_draft = models.EmailDraft(
                    campaign_id=campaign.id,
                    decision_maker_id=dm.id,
                    subject=reply_subject(first_sent.subject) if first_sent else draft["subject"],
                    body=draft["body"],
                    status="DRAFTED",
                    followup_index=_next_discovery_draft_index(db, dm.id),
                    draft_type="DISCOVERY"
                )
                db.add(new_draft)
                transition_prospect(
                    db,
                    dm,
                    state=models.ProspectState.DISCOVERY_CALL,
                    status="DISCOVERY_CALL",
                    reason="DISCOVERY_DRAFTED",
                    actor="ghostwriter",
                    metadata={"draft_type": "DISCOVERY", "is_auto_booking": is_auto_booking},
                )
                dm.next_action_at = None
                dm.termination_reason = None
                dm.retry_after = None
                if db_ctx:
                    db.commit()
                logger.info(f"[DISCOVERY] Draft saved for {dm.name} (DISCOVERY_CALL)")
            except IntegrityError:
                if db_ctx: db.rollback()
                logger.info(f"[IDEMPOTENCY] Discovery draft already exists for {dm.name}")
    except Exception as e:
        if db_ctx: db.rollback()
        logger.error(f"Discovery draft failed for DM {dm_id}: {e}", exc_info=True)
        raise e
    finally:
        if db_ctx:
            db_ctx.__exit__(None, None, None)
