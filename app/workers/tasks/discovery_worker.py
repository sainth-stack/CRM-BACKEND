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
import gc
from datetime import UTC


def predict_prospect_email(name: str, domain: str) -> str:
    """
    Algorithmic Email Prediction Engine.
    Generates a high-probability corporate email address based on verified stakeholder names and company domains.
    """
    if not name or not domain or domain == "unknown":
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
        existing_domains = {c.domain.lower() if c.domain else "" for c in db.query(models.TargetCompany).filter(models.TargetCompany.campaign_id == campaign_id).all()}
        discovered_entities = []
        
        for co in find_target_companies(criteria, offerings_list):
            domain = (co.get("domain") or "").lower()
            
            # Idempotency Guard: Skip redundant entities already secured for this specific campaign
            if domain and domain in existing_domains:
                logger.debug(f"[IDEMPOTENCY] Skipping {co.get('name')} - Domain {domain} already exists for campaign {campaign_id}")
                continue

            score = co.get("similarity_score", 0)
            status = co.get("status", "REJECTED")

            new_co = models.TargetCompany(
                campaign_id=campaign_id,
                name=co.get("name"),
                website=co.get("website"),
                domain=co.get("domain"),
                linkedin=co.get("linkedin"),
                location=co.get("location"),
                company_type=co.get("company_type"),
                employee_count=co.get("employee_count"),
                contact_email="N/A",
                contact_number="N/A",
                deep_research=co.get("deep_research"),
                relevance_score=score,
                relevance_explanation=co.get("score_reason", ""),
                rejection_reason=co.get("rejection_reason"),
                status=status
            )
            discovered_entities.append(new_co)
            if domain: existing_domains.add(domain) # Prevent duplicates within the same research batch

        if discovered_entities:
            # Heartbeat Integrity Gate: Verify campaign still exists before committing
            if not db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first():
                logger.warning(f"[MISSION ABORTED] Campaign {campaign_id} deleted during research cycle. Discarding company discovery results.")
                return
            
            db.add_all(discovered_entities)
            db.commit()
            logger.info(f"[MISSION CONTROL] Performance Success: Batch committed {len(discovered_entities)} companies for campaign {campaign_id}")

        # Final Heartbeat Integrity Check before advancing pipeline
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign:
            logger.warning(f"[MISSION ABORTED] Campaign {campaign_id} lost during discovery synchronization.")
            return

        campaign.status = models.CampaignStatus.FINDING_DECISION_MAKERS
        db.commit()
        find_dms_worker.delay(campaign_id)
    except Exception as e:
        logger.error(f"Operational Failure in Company Discovery for {campaign_id}: {e}", exc_info=True)
        db.rollback()
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

        logger.info(f"[MISSION CONTROL] Processing {len(target_cos)} target entities for stakeholder identification.")

        # IDEMPOTENCY: Identity Anchor Recovery
        existing_dm_identifiers = {
            (dm.target_company_id, dm.name.lower()) 
            for dm in db.query(models.DecisionMaker).filter(models.DecisionMaker.campaign_id == campaign_id).all()
        }

        all_new_dms = []
        for co in target_cos:
            try:
                logger.debug(f"[DM FINDER] Identifying stakeholders at {co.name}")
                dms = find_decision_makers(co.name, co.location)

                for dm in dms:
                    dm_name = dm.get("name") or "Unknown"
                    identifier = (co.id, dm_name.lower())
                    
                    if identifier in existing_dm_identifiers:
                        logger.debug(f"[IDEMPOTENCY] Skipping DM {dm_name} at {co.name} - Identity already secured.")
                        continue

                    score = dm.get("similarity_score", 0)
                    if score >= 70:
                        new_dm = models.DecisionMaker(
                            campaign_id=campaign_id,
                            target_company_id=co.id,
                            name=dm_name,
                            position=dm.get("position"),
                            linkedin=dm.get("linkedin"),
                            relevance_score=score,
                            relevance_explanation=dm.get("score_reason", ""),
                            status="NEW"
                        )
                        email = predict_prospect_email(dm_name, co.domain)
                        new_dm.email = email

                        # External CRM Synchronization (Managed per-entry for error isolation)
                        try:
                            hs_id = hubspot_provider.create_lead(dm, co.name, email=email)
                            if hs_id:
                                new_dm.hubspot_id = hs_id
                                new_dm.status = "SYNCED"
                        except Exception as hs_e:
                            logger.error(f"HubSpot Sync Error for {dm_name}: {hs_e}")

                        all_new_dms.append(new_dm)
                        existing_dm_identifiers.add(identifier) # Prevent duplicates within the same batch

                co.status = "ACTIVE"
            except Exception as proc_e:
                logger.error(f"Error processing stakeholders for company {co.name}: {proc_e}")

            gc.collect()

        if all_new_dms:
            # Heartbeat Integrity Gate: Deletion Safety
            if not db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first():
                logger.warning(f"[MISSION ABORTED] Campaign {campaign_id} deleted during DM identification. Discarding batch.")
                return

            db.add_all(all_new_dms)
            db.commit()
            logger.info(f"[MISSION CONTROL] Successfully synchronized {len(all_new_dms)} stakeholders across {len(target_cos)} entities.")

        # Atomic Status Transition Gate
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign:
            logger.warning(f"[MISSION ABORTED] Final sync failed: Campaign {campaign_id} deleted.")
            return

        campaign.status = models.CampaignStatus.DRAFTING_EMAILS
        db.commit()
        from app.workers.tasks.ghostwriter_worker import draft_emails_worker
        draft_emails_worker.delay(campaign_id)

    except Exception as e:
        logger.error(f"Critical operational error in DM Finder Cluster: {e}", exc_info=True)
        db.rollback()
        self.retry(exc=e)
    finally:
        db.close()
