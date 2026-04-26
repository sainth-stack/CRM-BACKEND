import json
import re
import socket
import datetime
import concurrent.futures
from typing import List, Set
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert
from app.db import models
from app.core.logging_config import logger
from app.integrations.hubspot import hubspot_provider
from app.agents.company_finder import find_target_companies
from app.agents.dm_finder import find_decision_makers
from app.workers.utils import heartbeat_lease

# Bounded DNS Executor for Vitality Audits
dns_executor = concurrent.futures.ThreadPoolExecutor(max_workers=20)

class CampaignService:
    """
    Central service for campaign research, company discovery, 
    and stakeholder identification.
    """

    @staticmethod
    def build_company_identity_key(co_data: dict) -> str | None:
        """Generates a canonical identity key for company deduplication."""
        def _clean(v): return v.strip().lower() if v else None
        
        domain = _clean(co_data.get("domain"))
        if domain: return domain
        
        website = _clean(co_data.get("website"))
        if website:
            from urllib.parse import urlparse
            parsed = urlparse(website if "://" in website else f"https://{website}")
            host = (parsed.netloc or parsed.path or "").strip().lower().removeprefix("www.")
            if host: return host.rstrip("/")
            
        name = _clean(co_data.get("name"))
        if name: return re.sub(r"\s+", " ", name)
        return None

    @staticmethod
    def predict_prospect_email(name: str, domain: str) -> str | None:
        """Algorithmic email prediction with domain vitality audit."""
        if not name or not domain or domain == "unknown":
            return None
        
        try:
            future = dns_executor.submit(socket.gethostbyname, domain)
            future.result(timeout=2)
        except:
            return None

        clean_name = re.sub(r'[^a-zA-Z\s]', '', name).lower().strip()
        parts = clean_name.split()
        if len(parts) >= 2:
            return f"{parts[0]}.{parts[-1]}@{domain}"
        return f"{parts[0]}@{domain}"

    def run_company_discovery(self, db: Session, campaign_id: str, worker_id: str):
        """Executes targeted company research and identity resolution."""
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign: return 0

        # Idempotency: Identity Anchor Recovery
        existing_fingerprints = {
            c.identity_key for c in db.query(models.TargetCompany)
            .filter(models.TargetCompany.campaign_id == campaign_id).all() if c.identity_key
        }

        # Resume logic: Start from appropriate search page
        start_page = len(existing_fingerprints) // 10
        if start_page >= 3:
            logger.info(f"[CAMPAIGN] Threshold met for {campaign_id}. Skipping search.")
            return 0

        # Load offerings context
        try: offerings = json.loads(campaign.user_intel.offerings)
        except: offerings = [campaign.user_intel.offerings]

        criteria = {
            "industry": campaign.target_industry,
            "location": campaign.target_location,
            "employee_count": campaign.target_employee_count
        }

        companies_generator = find_target_companies(criteria, offerings, start_page=start_page)
        
        count = 0
        for co in companies_generator:
            count += 1
            fingerprint = self.build_company_identity_key(co)
            if fingerprint in existing_fingerprints: continue

            if count % 5 == 0: heartbeat_lease(db, campaign_id, worker_id)

            # Atomic Upsert logic
            stmt = insert(models.TargetCompany).values(
                campaign_id=campaign_id,
                name=co.get("name"),
                website=co.get("website"),
                domain=co.get("domain"),
                identity_key=fingerprint,
                location=co.get("location"),
                status=co.get("status", "REJECTED"),
                relevance_score=co.get("similarity_score", 0),
                relevance_explanation=co.get("score_reason", ""),
                deep_research=co.get("deep_research")
            ).on_conflict_do_nothing(index_elements=['campaign_id', 'identity_key'])
            
            db.execute(stmt)
            db.commit()
            if fingerprint: existing_fingerprints.add(fingerprint)

        return count

    def identify_stakeholders(self, db: Session, campaign_id: str, worker_id: str):
        """Orchestrates stakeholder identification and CRM synchronization."""
        target_cos = db.query(models.TargetCompany).filter(
            models.TargetCompany.campaign_id == campaign_id,
            models.TargetCompany.status == "NEW"
        ).all()

        existing_dm_keys = {
            (dm.target_company_id, dm.name.lower())
            for dm in db.query(models.DecisionMaker).filter(models.DecisionMaker.campaign_id == campaign_id).all()
        }

        total_dms = 0
        for i, co in enumerate(target_cos):
            if i % 3 == 0: heartbeat_lease(db, campaign_id, worker_id)
            
            co.status = "RESEARCHING_STAKEHOLDERS"
            db.commit()

            try:
                dms = find_decision_makers(co.name, co.location)
                for dm_data in dms:
                    name = dm_data.get("name", "Unknown")
                    if (co.id, name.lower()) in existing_dm_keys: continue
                    
                    score = dm_data.get("similarity_score", 0)
                    if score < 70: continue

                    email = self.predict_prospect_email(name, co.domain)
                    new_dm = models.DecisionMaker(
                        campaign_id=campaign_id,
                        target_company_id=co.id,
                        name=name,
                        position=dm_data.get("position"),
                        relevance_score=score,
                        email=email,
                        status="NEW",
                        state=models.ProspectState.NEW
                    )
                    db.add(new_dm)
                    db.flush()
                    hubspot_provider.sync_decision_maker(new_dm.id)
                    total_dms += 1
                
                co.status = "STAKEHOLDERS_IDENTIFIED"
                db.commit()
            except Exception as e:
                logger.error(f"[CAMPAIGN] DM identification failed for {co.name}: {e}")
                db.rollback()

        return total_dms

campaign_service = CampaignService()
