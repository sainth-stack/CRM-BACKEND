from app.db import models
from app.workers.config.celery_app import celery_app
from app.workers.utils import acquire_lease, release_lease, db_session
from app.core.logging_config import logger

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def find_companies_worker(self, campaign_id: str):
    """
    STAGE 3+4 (MERGED): ICP Qualification + Deep Research in one pass.
    Runs the combined qualify-and-enrich agent, then advances the campaign
    straight to STAGE_4_RESEARCH_COMPLETE (no separate deep-research stage).
    """
    from app.services.campaign_service import campaign_service
    from app.core.security import acquire_lock, release_lock
    from app.core.circuit_breaker import CircuitOpenError

    worker_id = self.request.id
    lock_key = f"campaign_stage3:{campaign_id}"
    if not acquire_lock(lock_key, ttl=1200): return

    # CRITICAL: the lock MUST be released even if this task crashes / is OOM-killed
    # mid-run, otherwise the campaign stays wedged until the TTL expires and every
    # retry/sweep no-ops on the held lock. try/finally guarantees release.
    try:
        # Claim the DB lease (sets locked_by + heartbeat). Stage 3 then pulses the
        # heartbeat after each chunk so the stuck-campaign sweeper does NOT treat
        # this actively-running stage as dead and re-dispatch it (the ICP loop).
        with db_session() as db:
            if not acquire_lease(db, campaign_id, worker_id):
                return
        # 1. Load normalized companies from campaign_leads (chunked). Back-compat:
        # an old campaign still on the legacy blob is normalized once, then loaded.
        from app.services import lead_repository
        with db_session() as db:
            campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
            if not campaign:
                return
            if not lead_repository.has_leads(db, campaign_id) and campaign.trimmed_csv_data:
                lead_repository.normalize_csv_to_leads(db, campaign_id, campaign.trimmed_csv_data)
                db.commit()
            unique_cos = lead_repository.load_unique_cos(db, campaign_id)

        if unique_cos:
            # 2. Stage 3 logic runs with ZERO database sessions held
            import asyncio
            try:
                asyncio.run(campaign_service.stage_3_icp_filtering(None, campaign_id, unique_cos, worker_id=worker_id))
            except CircuitOpenError as e:
                # OpenAI provider outage — do NOT mark STAGE_3 complete. Reschedule;
                # the resume-aware filter means the retry skips already-judged companies.
                logger.warning(f"[STAGE 3] Provider outage for {campaign_id}: {e}. Rescheduling.")
                raise self.retry(exc=e, countdown=300)

            # 3. Short write transaction to update campaign status. Qualification +
            # research happened together, so the campaign is already research-complete.
            with db_session() as db:
                campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
                if campaign:
                    campaign.status = models.CampaignStatus.STAGE_4_RESEARCH_COMPLETE
                    db.commit()

            # Release the lease and lock *before* triggering the state machine
            # to prevent a race condition where the next worker runs and finds the lease still held.
            with db_session() as db:
                release_lease(db, campaign_id, worker_id)
            release_lock(lock_key)

            with db_session() as db:
                logger.info(f"✅ [STAGE 3+4] Combined ICP + Research Complete for {campaign_id}. Triggering State Machine.")
                campaign_service.process_state_machine(db, campaign_id)
    finally:
        # Idempotent cleanup in case of exceptions before the manual release
        with db_session() as db:
            release_lease(db, campaign_id, worker_id)
        release_lock(lock_key)

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def find_dms_worker(self, campaign_id: str):
    """
    STAGE 5: Stakeholder ranking
    """
    from app.services.campaign_service import campaign_service
    from app.core.security import acquire_lock, release_lock

    worker_id = self.request.id
    lock_key = f"campaign_stage5:{campaign_id}"
    if not acquire_lock(lock_key, ttl=1200): return

    try:
        # Claim the DB lease so the stuck-campaign sweeper doesn't resurrect this
        # stage mid-run. Stage 5 pulses the heartbeat per chunk.
        with db_session() as db:
            if not acquire_lease(db, campaign_id, worker_id):
                return
        # 1. Load contacts from campaign_leads. Back-compat: normalize an old blob.
        from app.services import lead_repository
        with db_session() as db:
            campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
            if not campaign:
                return
            if not lead_repository.has_leads(db, campaign_id) and campaign.trimmed_csv_data:
                lead_repository.normalize_csv_to_leads(db, campaign_id, campaign.trimmed_csv_data)
                db.commit()
            has = lead_repository.has_leads(db, campaign_id)

        if has:
            # 2. Stage 5 reads its contacts per-chunk from campaign_leads itself.
            import asyncio
            asyncio.run(campaign_service.stage_5_stakeholder_ranking(None, campaign_id, worker_id=worker_id))

            # 3. Short write transaction to update status
            with db_session() as db:
                campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
                if campaign:
                    campaign.status = models.CampaignStatus.STAGE_5_STAKEHOLDERS_RANKED
                    db.commit()

            # Release the lease and lock *before* triggering the state machine
            # to prevent a race condition where the next worker runs and finds the lease still held.
            with db_session() as db:
                release_lease(db, campaign_id, worker_id)
            release_lock(lock_key)

            with db_session() as db:
                logger.info(f"✅ [STAGE 5] Stakeholder Ranking Complete for {campaign_id}. Triggering State Machine.")
                campaign_service.process_state_machine(db, campaign_id)
    finally:
        with db_session() as db:
            release_lease(db, campaign_id, worker_id)
        release_lock(lock_key)
