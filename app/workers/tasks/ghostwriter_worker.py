"""
Phase 3: Email Ghostwriting Worker
Celery task that drafts personalized outreach emails for all discovered DMs.
Also contains follow-up and discovery call drafting helpers.
"""
from app.db.database import SessionLocal
from app.db import models
from app.agents.email_drafter import draft_personalized_email, draft_followup_email, draft_nudge_email
from app.agents.discovery_agent import draft_discovery_request
from app.workers.config.celery_app import celery_app
import json
import datetime
import gc
from datetime import UTC


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def draft_emails_worker(self, campaign_id: str):
    """Phase 3: AI-powered personalized email drafting for all validated stakeholders."""
    db = SessionLocal()
    try:
        print(f"[MISSION CONTROL] Initiating Email Ghostwriting for campaign {campaign_id}...")
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign or not campaign.user_intel:
            print(f"Aborting Email Drafting: Missing campaign or intel for {campaign_id}")
            return

        # Temporal Boundary Check
        owner = campaign.owner
        if owner and owner.is_demo and owner.demo_expires_at:
            if owner.demo_expires_at.replace(tzinfo=UTC) < datetime.datetime.now(UTC):
                print(f"[MISSION CONTROL] Suspension: Campaign {campaign_id} owner trial expired.")
                return

        user_intel_raw = campaign.user_intel
        offerings = []
        try:
            offerings = json.loads(user_intel_raw.offerings)
            if not isinstance(offerings, list):
                offerings = [str(offerings)]
        except:
            offerings = [str(user_intel_raw.offerings)]

        user_intel = {
            "company_name": user_intel_raw.company_name,
            "moto": user_intel_raw.motto or "N/A",
            "offerings": offerings,
            "deep_research": user_intel_raw.deep_research
        }

        dms = db.query(models.DecisionMaker).filter(models.DecisionMaker.campaign_id == campaign_id).all()
        print(f"Drafting for {len(dms)} validated stakeholders.")

        for dm in dms:
            # Skip if already drafted
            if db.query(models.EmailDraft).filter(models.EmailDraft.decision_maker_id == dm.id).first():
                continue

            target_co = db.query(models.TargetCompany).filter(models.TargetCompany.id == dm.target_company_id).first()
            if not target_co: continue

            try:
                print(f"[GHOSTWRITER] Creating personalized draft for {dm.name} at {target_co.name}...")
                draft_data = draft_personalized_email(
                    user_intel,
                    {"name": dm.name, "position": dm.position},
                    target_co.name,
                    target_co.deep_research
                )
                if draft_data:
                    new_draft = models.EmailDraft(
                        campaign_id=campaign_id,
                        decision_maker_id=dm.id,
                        subject=draft_data.get("subject"),
                        body=draft_data.get("body"),
                        status="DRAFTED"
                    )
                    db.add(new_draft)
                    dm.status = "DRAFTED"
                    db.commit()
                    print(f"[GHOSTWRITER] Success: Draft saved for {dm.name}")
                else:
                    print(f"[GHOSTWRITER] Warning: Agent returned empty draft for {dm.name}")
            except Exception as draft_e:
                print(f"Failure drafting email for {dm.name}: {draft_e}")
                db.rollback()

            gc.collect()

        campaign.status = models.CampaignStatus.COMPLETED
        db.commit()
        print(f"[MISSION CONTROL] Campaign {campaign_id} fully deployed and completed.")
    except Exception as e:
        print(f"Error in Email Drafter: {e}")
        db.rollback()
        self.retry(exc=e)
    finally:
        db.close()


def draft_followup_worker(dm_id: str):
    """Drafts a persistent follow-up when intent is Neutral."""
    db = SessionLocal()
    try:
        dm = db.query(models.DecisionMaker).filter(models.DecisionMaker.id == dm_id).first()
        if not dm: return

        campaign = dm.campaign
        user_intel = {
            "company_name": campaign.user_intel.company_name,
            "deep_research": campaign.user_intel.deep_research
        }

        logs = db.query(models.CommunicationLog).filter(
            models.CommunicationLog.dm_id == dm.id
        ).order_by(models.CommunicationLog.received_at.desc()).limit(5).all()

        history_text = "\n".join([f"{log.direction}: {log.body}" for log in logs])
        dm.followup_count += 1

        draft_data = draft_followup_email(
            user_intel=user_intel,
            dm_info={"name": dm.name},
            target_company_name=dm.target_company.name,
            thread_history=history_text,
            followup_number=dm.followup_count
        )

        if draft_data:
            new_draft = models.EmailDraft(
                campaign_id=campaign.id,
                decision_maker_id=dm.id,
                subject=draft_data["subject"],
                body=draft_data["body"],
                status="DRAFTED"
            )
            db.add(new_draft)
            dm.status = f"FOLLOWUP_{dm.followup_count}_DRAFTED"
            db.commit()
            print(f"[FOLLOW-UP] Persistence triggered for {dm.name} (#{dm.followup_count})")
    finally:
        db.close()


def draft_discovery_worker(dm_id: str, db=None, is_auto_booking: bool = False):
    """Drafts the initial discovery call request."""
    should_close = False
    if db is None:
        db = SessionLocal()
        should_close = True
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

        last_reply = db.query(models.CommunicationLog).filter(
            models.CommunicationLog.dm_id == dm.id,
            models.CommunicationLog.direction == "RECEIVED"
        ).order_by(models.CommunicationLog.received_at.desc()).first()

        draft = draft_discovery_request(
            user_intel=user_intel,
            dm_name=dm.name,
            dm_position=dm.position,
            target_company=dm.target_company.name,
            last_interest=last_reply.body if last_reply else "Interest in AI solutions",
            booked_link=dm.meeting_link if is_auto_booking else None
        )

        if draft:
            new_draft = models.EmailDraft(
                campaign_id=campaign.id,
                decision_maker_id=dm.id,
                subject=draft["subject"],
                body=draft["body"],
                status="DRAFTED"
            )
            db.add(new_draft)
            dm.status = "DISCOVERY_CALL"
            if should_close:
                db.commit()
            print(f"[DISCOVERY] Draft created for {dm.name} and status updated to DISCOVERY_CALL")
    except Exception as e:
        if should_close: db.rollback()
        raise e
    finally:
        if should_close:
            db.close()
