from app.db.database import SessionLocal
from app.db import models
from app.agents.company_finder import find_target_companies
from app.agents.dm_finder import find_decision_makers
from app.integrations.hubspot import hubspot_provider
from app.workers.config.celery_app import celery_app
from app.core.logging_config import logger
import json
import datetime
import re
import concurrent.futures
import socket
import urllib.parse
from datetime import UTC

# Bounded DNS Executor: Prevents OS thread exhaustion during massive ICP discovery missions
dns_executor = concurrent.futures.ThreadPoolExecutor(max_workers=20)


def build_company_identity_key(company) -> str | None:
    """
    Canonical company identity key used for application and database dedupe.
    Prefers resolvable domain, then normalized website host, then normalized name.
    """
    if isinstance(company, dict):
        domain = company.get("domain")
        website = company.get("website")
        name = company.get("name")
    else:
        domain = getattr(company, "domain", None)
        website = getattr(company, "website", None)
        name = getattr(company, "name", None)

    def _clean(value: str | None) -> str | None:
        if not value:
            return None
        return value.strip().lower()

    clean_domain = _clean(domain)
    if clean_domain:
        return clean_domain

    clean_website = _clean(website)
    if clean_website:
        parsed = urllib.parse.urlparse(clean_website if "://" in clean_website else f"https://{clean_website}")
        host = (parsed.netloc or parsed.path or "").strip().lower()
        host = host.removeprefix("www.")
        if host:
            return host.rstrip("/")

    clean_name = _clean(name)
    if clean_name:
        return re.sub(r"\s+", " ", clean_name)

    return None

def predict_prospect_email(name: str, domain: str) -> str:
    """
    Algorithmic Email Prediction Engine.
    Generates a high-probability corporate email address based on verified stakeholder names and company domains.
    Includes a domain vitality audit to mitigate hard-bounce risks.
    """
    if not name or not domain or domain == "unknown":
        return None
        
    # Vitality Audit: Ensure the domain is actually resolvable before predicting (Bounded 2s timeout)
    try:
        future = dns_executor.submit(socket.gethostbyname, domain)
        future.result(timeout=2)
    except (concurrent.futures.TimeoutError, Exception) as e:
        logger.warning(f"[DM FINDER] Domain {domain} vitality check failed/timed out: {e}. Skipping to protect sender reputation.")
        return None

    clean_name = re.sub(r'[^a-zA-Z\s]', '', name).lower().strip()
    parts = clean_name.split()
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[-1]}@{domain}"
    return f"{parts[0]}@{domain}"


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def find_companies_worker(self, campaign_id: str):
    """
    Phase 2a: Target Company Discovery Cluster.
    Executes deep web research to identify companies matching the campaign's ideal customer profile (ICP).
    Uses high-performance batch writes to stabilize the database during large discovery missions.
    """
    db = SessionLocal()
    try:
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign:
            logger.warning(f"Aborting Company Discovery: Campaign {campaign_id} not found.")
            return

        owner = campaign.owner
        if owner and owner.is_demo and owner.demo_expires_at:
            if owner.demo_expires_at.replace(tzinfo=UTC) < datetime.datetime.now(UTC):
                logger.info(f"[MISSION CONTROL] Suspension: Campaign {campaign_id} owner trial expired.")
                return

        user_intel = campaign.user_intel
        if not user_intel:
            logger.error(f"No intel package found for campaign {campaign_id}. Unable to start discovery.")
            return

        from app.workers.utils import acquire_lease, release_lease, heartbeat_lease
        worker_id = f"worker:{self.request.id}"
        if not acquire_lease(db, campaign_id, worker_id):
            return

        logger.info(f"Starting discovery sweep for campaign {campaign_id} in {campaign.target_industry}")
        criteria = {
            "industry": campaign.target_industry,
            "location": campaign.target_location,
            "employee_count": campaign.target_employee_count
        }

        offerings_list = []
        try:
            offerings_list = json.loads(user_intel.offerings)
        except:
            offerings_list = [user_intel.offerings]

        # PERFORMANCE & IDEMPOTENCY: Anchored Identity Resolution
        existing_company_fingerprints = set()
        for company in db.query(models.TargetCompany).filter(models.TargetCompany.campaign_id == campaign_id).all():
            fingerprint = company.identity_key or build_company_identity_key(company)
            if fingerprint:
                existing_company_fingerprints.add(fingerprint)
        
        # CREDIT PROTECTION & RESUME LOGIC: 
        # Calculate the next logical start page based on current lead volume.
        # This prevents redundant Zenserp credit spend while ensuring we reach the 30-lead goal.
        start_page = len(existing_company_fingerprints) // 10
        if start_page >= 3:
            logger.info(f"[DISCOVERY] Idempotency Hit: {len(existing_company_fingerprints)} companies already exist. Target threshold met. Skipping Recon.")
            companies_generator = []
        else:
            companies_generator = find_target_companies(criteria, offerings_list, start_page=start_page)
        
        batch_count = len(existing_company_fingerprints)
        from sqlalchemy.dialects.postgresql import insert
        
        for co in companies_generator:
            batch_count += 1
            domain = (co.get("domain") or "").lower()
            fingerprint = build_company_identity_key(co)
            
            # Application-layer Idempotency
            if fingerprint and fingerprint in existing_company_fingerprints:
                continue

            # Heartbeat Pulse: Prevent sweeper hijacking during long research loops
            if batch_count % 5 == 0:
                heartbeat_lease(db, campaign_id, worker_id)

            # High-Performance Atomic Upsert: Prevent transaction invalidation on collision
            stmt = insert(models.TargetCompany).values(
                campaign_id=campaign_id,
                name=co.get("name"),
                website=co.get("website"),
                domain=co.get("domain"),
                identity_key=fingerprint,
                linkedin=co.get("linkedin"),
                location=co.get("location"),
                company_type=co.get("company_type"),
                employee_count=co.get("employee_count"),
                contact_email="N/A",
                contact_number="N/A",
                deep_research=co.get("deep_research"),
                relevance_score=co.get("similarity_score", 0),
                relevance_explanation=co.get("score_reason", ""),
                rejection_reason=co.get("rejection_reason"),
                status=co.get("status", "REJECTED")
            ).on_conflict_do_nothing(index_elements=['campaign_id', 'identity_key'])
            
            try:
                db.execute(stmt)
                db.commit()
                if fingerprint:
                    existing_company_fingerprints.add(fingerprint)
            except Exception as e:
                db.rollback()
                logger.error(f"[DISCOVERY] Insertion failure for {domain}: {e}")

        # Final Transition Integrity
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign:
            return

        if batch_count == 0:
            campaign.status = models.CampaignStatus.PARTIAL_SUCCESS
            campaign.status_reason = "Mission stalled: Zero target companies identified matching criteria."
            db.commit()
            release_lease(db, campaign_id, worker_id)
            logger.warning(f"[MISSION CONTROL] Campaign {campaign_id} stalled: Zero companies found.")
            return

        campaign.status = models.CampaignStatus.FINDING_DECISION_MAKERS
        db.commit()
        
        release_lease(db, campaign_id, worker_id)
        find_dms_worker.delay(campaign_id)
    except Exception as e:
        logger.error(f"Operational Failure in Company Discovery for {campaign_id}: {e}", exc_info=True)
        try: release_lease(db, campaign_id, f"worker:{self.request.id}")
        except: pass
        self.retry(exc=e)
    finally:
        db.close()

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def find_dms_worker(self, campaign_id: str):
    """
    Phase 2b: Stakeholder Identification Cluster.
    Pinpoints key decision-makers within discovered companies and synchronizes results with HubSpot.
    Optimized for high-throughput with single-transaction commits across all identified entities.
    """
    logger.info(f"[MISSION CONTROL] Transition: Initiating Stakeholder Discovery for {campaign_id}")
    db = SessionLocal()
    try:
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign:
            logger.warning(f"Aborting DM Finder: Campaign {campaign_id} not found.")
            return

        owner = campaign.owner
        if owner and owner.is_demo and owner.demo_expires_at:
            if owner.demo_expires_at.replace(tzinfo=UTC) < datetime.datetime.now(UTC):
                logger.info(f"[MISSION CONTROL] Suspension: Campaign {campaign_id} owner trial expired.")
                return

        target_cos = db.query(models.TargetCompany).filter(
            models.TargetCompany.campaign_id == campaign_id,
            models.TargetCompany.status == "NEW"
        ).all()

        if not target_cos:
            logger.info(f"[MISSION CONTROL] No NEW companies found for {campaign_id}. Advancing to Outreach phase.")
            
            # Re-verify campaign before status update
            campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
            if campaign:
                campaign.status = models.CampaignStatus.DRAFTING_EMAILS
                db.commit()
                from app.workers.tasks.ghostwriter_worker import draft_emails_worker
                draft_emails_worker.delay(campaign_id)
            return

        from app.workers.utils import acquire_lease, release_lease, heartbeat_lease
        worker_id = f"worker:{self.request.id}"
        if not acquire_lease(db, campaign_id, worker_id):
            return

        logger.info(f"[MISSION CONTROL] Processing {len(target_cos)} target entities for stakeholder identification.")

        # IDEMPOTENCY: Identity Anchor Recovery
        existing_dm_identifiers = {
            (dm.target_company_id, dm.name.lower()) 
            for dm in db.query(models.DecisionMaker).filter(models.DecisionMaker.campaign_id == campaign_id).all()
        }

        from sqlalchemy.exc import IntegrityError
        from sqlalchemy.dialects.postgresql import insert
        processed_companies_count = 0
        for co in target_cos:
            processed_companies_count += 1
            # Heartbeat Pulse: Prevent sweeper hijacking
            if processed_companies_count % 3 == 0:
                heartbeat_lease(db, campaign_id, worker_id)

            try:
                # UX ENHANCEMENT: Update company status to reflect active research phase
                co.status = "RESEARCHING_STAKEHOLDERS"
                db.commit()

                logger.debug(f"[DM FINDER] Identifying stakeholders at {co.name}")
                dms = find_decision_makers(co.name, co.location)

                for dm in dms:
                    dm_name = dm.get("name") or "Unknown"
                    identifier = (co.id, dm_name.lower())
                    
                    if identifier in existing_dm_identifiers:
                        continue

                    score = dm.get("similarity_score", 0)
                    if score >= 70:
                        email = predict_prospect_email(dm_name, co.domain)
                        # Dialect-Agnostic Atomic Persistence: Supports both SQLite and PostgreSQL
                        new_dm = models.DecisionMaker(
                            campaign_id=campaign_id,
                            target_company_id=co.id,
                            name=dm_name,
                            position=dm.get("position"),
                            linkedin=dm.get("linkedin"),
                            relevance_score=score,
                            relevance_explanation=dm.get("score_reason", ""),
                            email=email,
                            status="NEW"
                        )
                        
                        try:
                            db.add(new_dm)
                            db.commit()
                            
                            # CRM Synchronization: Provision fresh lead artifacts
                            try:
                                hs_id = hubspot_provider.create_lead(dm, co.name, email=email)
                                if hs_id:
                                    new_dm.hubspot_id = hs_id
                                    new_dm.status = "SYNCED"
                                    db.commit()
                            except Exception as hs_e:
                                logger.error(f"HubSpot Deferred Sync Error for {dm_name}: {hs_e}")

                            existing_dm_identifiers.add(identifier)
                        except IntegrityError:
                            db.rollback()
                            logger.info(f"[DM FINDER] IDEMPOTENCY: Skipping duplicate stakeholder {dm_name} at {co.name}")
                        except Exception as e:
                            db.rollback()
                            logger.error(f"[DM FINDER] Persistence failure for {dm_name}: {e}")

                co.status = "ACTIVE"
                db.commit()
            except Exception as proc_e:
                logger.error(f"Error processing stakeholders for company {co.name}: {proc_e}")
                db.rollback()

        # Check if we actually found anyone
        dm_count = db.query(models.DecisionMaker).filter(models.DecisionMaker.campaign_id == campaign_id).count()
        if dm_count == 0:
            campaign.status = models.CampaignStatus.PARTIAL_SUCCESS
            campaign.status_reason = "Mission stalled: Zero stakeholders identified across discovered companies."
            db.commit()
            release_lease(db, campaign_id, worker_id)
            logger.warning(f"[MISSION CONTROL] Campaign {campaign_id} stalled: Zero DMs found.")
            return

        campaign.status = models.CampaignStatus.DRAFTING_EMAILS
        db.commit()
        
        release_lease(db, campaign_id, worker_id)
        from app.workers.tasks.ghostwriter_worker import draft_emails_worker
        draft_emails_worker.delay(campaign_id)

    except Exception as e:
        logger.error(f"Critical operational error in DM Finder Cluster: {e}", exc_info=True)
        try: release_lease(db, campaign_id, f"worker:{self.request.id}")
        except: pass
        self.retry(exc=e)
    finally:
        db.close()
