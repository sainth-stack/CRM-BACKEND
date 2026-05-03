import os
from hubspot import HubSpot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, SimplePublicObjectInput
from dotenv import load_dotenv
from app.core.logging_config import logger

load_dotenv()

HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN")

class HubSpotProvider:
    """
    Enterprise CRM Integration Layer.
    Synchronizes discovered leads and communication states with HubSpot CRM to maintain a professional source of truth.
    """
    def __init__(self):
        self.client = HubSpot(access_token=HUBSPOT_ACCESS_TOKEN) if HUBSPOT_ACCESS_TOKEN else None
        # Valid HubSpot Lead Statuses for this client's portal
        self.STATUS_MAP = {
            "Initial Email Sent": "ATTEMPTED_TO_CONTACT",
            "REMINDER_1_SENT Sent": "ATTEMPTED_TO_CONTACT",
            "REMINDER_2_SENT Sent": "ATTEMPTED_TO_CONTACT",
            "Reply Received": "IN_PROGRESS",
            "Neutral Reply": "IN_PROGRESS",
            "Discovery Invitation Sent": "CONNECTED",
            "Discovery Reminder Sent": "CONNECTED",
            "Discovery Expired": "IN_PROGRESS",
            "On Hold (Peer Engaged)": "CONNECTED",
            "Hold Released": "IN_PROGRESS",
            "Reactivated": "IN_PROGRESS",
            "DISCOVERY_CALL": "CONNECTED",
            "Discovery Protocol": "CONNECTED",
            "Meeting Booked": "OPEN_DEAL",
            "Booked": "OPEN_DEAL",
            "Terminated": "UNQUALIFIED",
            "Terminated (System Timeout)": "UNQUALIFIED",
            "Terminated (Discovery Timeout)": "UNQUALIFIED",
            "Terminated (Negative Reply)": "UNQUALIFIED",
            "Terminated (Invalid Schedule)": "UNQUALIFIED",
            "Terminated (Internal Lead Secured)": "UNQUALIFIED",
            "Terminated (Exhausted Threshold)": "UNQUALIFIED",
            "Rejected": "UNQUALIFIED",
            "NEW": "NEW"
        }

    def create_lead(self, dm: dict, company_name: str, email: str = None):
        """
        CRM Lead Provisioning.
        Creates a new contact record in HubSpot for a validated stakeholder and returns the HubSpot Unique Identifier.
        """
        if not self.client:
            logger.warning("[HUBSPOT] Integration disabled: Null access token.")
            return None
        
        try:
            properties = {
                "email": email,
                "firstname": dm.get("name", "").split(" ")[0],
                "lastname": dm.get("name", "").split(" ")[-1] if " " in dm.get("name", "") else "",
                "jobtitle": dm.get("position", ""),
                "company": company_name,
                "lifecyclestage": "lead",
                "hs_lead_status": "NEW"
            }
            # Normalize properties
            properties = {k: v for k, v in properties.items() if v is not None}
            
            simple_public_object_input_for_create = SimplePublicObjectInputForCreate(properties=properties)
            api_response = self.client.crm.contacts.basic_api.create(simple_public_object_input_for_create=simple_public_object_input_for_create)
            logger.info(f"[HUBSPOT] Lead Created: {dm.get('name')} | ID: {api_response.id}")
            return api_response.id
        except Exception as e:
            logger.error(f"[HUBSPOT] Contact creation critical failure: {e}")
            return None

    def update_lead_status(self, hubspot_id: str, status: str):
        """
        CRM Lifecycle Management.
        Synchronizes the outreach protocol state with the HubSpot 'Lead Status' property.
        """
        if not self.client or not hubspot_id:
            logger.debug("[HUBSPOT] Skipping status update: Client not configured or null ID.")
            return
        try:
            # Map human-readable outreach state to HubSpot system enumeration
            hs_status = self.STATUS_MAP.get(status, "IN_PROGRESS")
            properties = {"hs_lead_status": hs_status}
            
            simple_public_object_input = SimplePublicObjectInput(properties=properties)
            self.client.crm.contacts.basic_api.update(contact_id=hubspot_id, simple_public_object_input=simple_public_object_input)
            logger.info(f"[HUBSPOT] Status Synchronized: {hubspot_id} -> {hs_status} (State: {status})")
        except Exception as e:
            logger.error(f"[HUBSPOT] Status synchronization error: {e}")

    def sync_decision_maker(self, dm_id: str):
        """
        Synchronizes a specific decision maker to HubSpot.
        """
        if not self.client:
            return None
        from app.db.database import SessionLocal
        from app.db import models
        db = SessionLocal()
        try:
            dm = db.query(models.DecisionMaker).filter(models.DecisionMaker.id == dm_id).first()
            if dm and not dm.hubspot_id:
                co = db.query(models.TargetCompany).filter(models.TargetCompany.id == dm.target_company_id).first()
                co_name = co.name if co else "Unknown"
                hs_id = self.create_lead(
                    dm={"name": dm.name, "position": dm.position},
                    company_name=co_name,
                    email=dm.email
                )
                if hs_id:
                    dm.hubspot_id = hs_id
                    db.commit()
                    return hs_id
        except Exception as e:
            logger.error(f"[HUBSPOT] Error syncing decision maker {dm_id}: {e}")
            db.rollback()
        finally:
            db.close()
        return None

hubspot_provider = HubSpotProvider()
