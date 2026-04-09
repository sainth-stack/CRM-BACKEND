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
    """
    db = SessionLocal()
    try:
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign:
            logger.warning(f"Aborting Company Discovery: Campaign {campaign_id} not found.")
            return

        # Temporal Boundary Check: Security gate for demo accounts
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

        # Incremental Store: Commit each company as it is found for real-time UI updates
        for co in find_target_companies(criteria, offerings_list):
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
                similarity_score={"score": score, "reason": co.get("score_reason", "")},
                rejection_reason=co.get("rejection_reason"),
                status=status
            )
            db.add(new_co)
            db.commit()
            logger.info(f"Incremental Discovery: Saved {co.get('name')} ({status} | Score: {score})")

        # Advance pipeline to DM finding
        campaign.status = models.CampaignStatus.FINDING_DECISION_MAKERS
        db.commit()
        find_dms_worker.delay(campaign_id)
        logger.info(f"[MISSION CONTROL] Company Discovery Complete for {campaign_id}. Dispatched Stakeholder Identification.")
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
    """
    logger.info(f"[MISSION CONTROL] Transition: Initiating Stakeholder Discovery for {campaign_id}")
    db = SessionLocal()
    try:
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign:
            logger.warning(f"Aborting DM Finder: Campaign {campaign_id} not found.")
            return

        # Temporal Boundary Check
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
            campaign.status = models.CampaignStatus.DRAFTING_EMAILS
            db.commit()
            from app.workers.tasks.ghostwriter_worker import draft_emails_worker
            draft_emails_worker.delay(campaign_id)
            return

        logger.info(f"[MISSION CONTROL] Processing {len(target_cos)} target entities for stakeholder identification.")

        for co in target_cos:
            try:
                logger.debug(f"[DM FINDER] Identifying stakeholders at {co.name}")
                dms = find_decision_makers(co.name, co.location)

                with SessionLocal() as local_db:
                    saved_count = 0
                    for dm in dms:
                        score = dm.get("similarity_score", 0)
                        if score >= 70:
                            new_dm = models.DecisionMaker(
                                campaign_id=campaign_id,
                                target_company_id=co.id,
                                name=dm.get("name"),
                                position=dm.get("position"),
                                linkedin=dm.get("linkedin"),
                                similarity_score={"score": score, "reason": dm.get("score_reason", "")},
                                status="NEW"
                            )
                            local_db.add(new_dm)
                            local_db.flush()

                            email = predict_prospect_email(dm.get("name"), co.domain)
                            new_dm.email = email

                            try:
                                hs_id = hubspot_provider.create_lead(dm, co.name, email=email)
                                if hs_id:
                                    new_dm.hubspot_id = hs_id
                                    new_dm.status = "SYNCED"
                            except Exception as hs_e:
                                logger.error(f"HubSpot Sync Error for {dm.get('name')}: {hs_e}")

                            saved_count += 1

                    local_db.commit()
                    logger.info(f"[DM FINDER] Secured {saved_count} validated stakeholders for {co.name}.")

                co.status = "ACTIVE"
                db.commit()

            except Exception as proc_e:
                logger.error(f"Error processing stakeholders for company {co.name}: {proc_e}")

            # Force memory cleanup during heavy agent iterations
            gc.collect()

        logger.info(f"[MISSION CONTROL] DM Discovery complete for {campaign_id}. Launching Outreach Generation.")
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
