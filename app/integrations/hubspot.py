from app.core.logging_config import logger

class HubSpotProvider:
    """
    Mock/No-Op HubSpot Integration Layer.
    Retains the method signatures to prevent breaking any main business logic or imports.
    """
    def __init__(self):
        self.STATUS_MAP = {}

    def create_lead(self, dm: dict, company_name: str, email: str = None):
        logger.info(f"[HUBSPOT-MOCK] Skipping lead creation for: {dm.get('name')}")
        return None

    def update_lead_status(self, hubspot_id: str, status: str):
        logger.debug(f"[HUBSPOT-MOCK] Skipping status update for lead ID {hubspot_id} -> {status}")
        return

    def sync_decision_maker(self, dm_id: str):
        logger.info(f"[HUBSPOT-MOCK] Skipping decision maker sync for ID: {dm_id}")
        return None

hubspot_provider = HubSpotProvider()
