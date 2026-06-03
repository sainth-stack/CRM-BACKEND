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
from app.workers.utils import heartbeat_lease
from app.services.company_validation_service import CompanyValidationService
from app.services.stakeholder_service import StakeholderRankingService
from app.services.user_intel_service import UserIntelService
from app.db.database import SessionLocal

# Bounded DNS Executor for Vitality Audits
dns_executor = concurrent.futures.ThreadPoolExecutor(max_workers=20)

from app.services.csv_service import CSVProcessingService

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



    def stage_1_csv_trimming(self, db: Session, campaign_id: str, csv_content: str):
        """
        STAGE 1: CSV Trimming & High-Fidelity Mapping
        Saves a trimmed version of the CSV and updates campaign status.
        """
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        csv_svc = CSVProcessingService()
        
        # 1. Process and Trim
        contacts_map, unique_cos = csv_svc.process_csv_content(
            csv_content.encode('utf-8'), 
            campaign.target_location, 
            campaign.target_industry, 
            campaign.target_employee_count, 
            campaign_id, 
            db
        )
        
        # 2. Persist the Trimmed State (For now, we store unique_cos in metadata or a file)
        # We also need to save the "Trimmed CSV" back to disk
        # (Assuming CSVProcessingService handles the internal mapping)
        
        campaign.status = models.CampaignStatus.STAGE_1_CSV_TRIMMED
        db.commit()
        logger.info(f"✅ [STAGE 1] CSV Trimmed & Status Updated for {campaign_id}")
        return unique_cos

    async def stage_3_icp_filtering(self, db: Session, campaign_id: str, unique_cos: dict):
        """
        STAGE 3: ICP Filtering (AI Gatekeeper)
        Runs soft-signal checks and saves TargetCompany records.
        """
        # 1. Fetch Brand DNA (Short Read Session)
        temp_db = SessionLocal()
        try:
            campaign = temp_db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
            ui_record = campaign.user_intel
            user_intel_dict = {
                "services": json.loads(ui_record.offerings) if ui_record.offerings else [],
                "target_customers": ui_record.target_customers or [],
                "competitive_advantages": ui_record.competitive_advantages or []
            }
            campaign_metadata = {
                "target_location": campaign.target_location,
                "target_employee_count": campaign.target_employee_count
            }
        finally:
            temp_db.close()

        # 2. Run long LLM validation holding ZERO DB connections
        validator = CompanyValidationService()
        import asyncio

        STAGE3_CHUNK_SIZE = 10   # companies committed per transaction
        # Bounded fan-out: on a small instance, 10 concurrent (Tavily + LLM)
        # pipelines spike memory enough to trigger the OOM-killer and burst past
        # OpenAI/Tavily rate limits. 4 keeps memory flat and rate steady.
        semaphore = asyncio.Semaphore(4)

        async def validate_one(domain, co_data):
            async with semaphore:
                try:
                    res = await validator.validate_company(co_data, user_intel_dict, campaign_metadata)
                    return domain, res
                except Exception as e:
                    logger.error(f"Validation failed for {domain}: {e}")
                    return domain, {"status": "ERROR", "reasoning": str(e)}

        # 3. Process in chunks: validate a chunk, commit it (upsert), release memory.
        items = list(unique_cos.items())
        total = len(items)
        logger.info(f"🚦 [STAGE 3] Filtering {total} companies for {campaign_id} in chunks of {STAGE3_CHUNK_SIZE}.")

        for start in range(0, total, STAGE3_CHUNK_SIZE):
            chunk = items[start:start + STAGE3_CHUNK_SIZE]
            results = await asyncio.gather(*[validate_one(domain, data) for domain, data in chunk])
            self._persist_stage3_chunk(campaign_id, results, unique_cos)
            logger.info(f"   ↳ [STAGE 3] Committed {min(start + STAGE3_CHUNK_SIZE, total)}/{total} for {campaign_id}.")
            del results

        # 4. Mark stage complete once every chunk has landed.
        temp_db = SessionLocal()
        try:
            campaign = temp_db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
            if campaign:
                campaign.status = models.CampaignStatus.STAGE_3_ICP_FILTERED
                temp_db.commit()
        finally:
            temp_db.close()
        logger.info(f"✅ [STAGE 3] ICP Filtering Complete for {campaign_id}")

    def _persist_stage3_chunk(self, campaign_id: str, results: list, unique_cos: dict):
        """Upsert one chunk of ICP-filter results in a single short transaction.

        Upsert keeps the stage idempotent: a retry re-validates rows but updates
        in place instead of raising UniqueViolation on (campaign_id, domain).
        Each row is isolated via SAVEPOINT so one bad row doesn't sink the chunk.
        """
        temp_db = SessionLocal()
        try:
            for domain, res in results:
                try:
                    with temp_db.begin_nested():  # SAVEPOINT per company
                        status = res.get('status', 'REJECTED')
                        co_extra = unique_cos.get(domain, {})

                        existing_co = temp_db.query(models.TargetCompany).filter(
                            models.TargetCompany.campaign_id == campaign_id,
                            models.TargetCompany.domain == domain
                        ).first()

                        if existing_co:
                            existing_co.status = status
                            existing_co.relevance_score = res.get('relevance_score', 0)
                            existing_co.relevance_explanation = res.get('reasoning', 'Updated via AI Gatekeeper.')
                            existing_co.icp_research_context = res.get('icp_context')

                            existing_co.location = co_extra.get('location')
                            existing_co.company_type = co_extra.get('industry')
                            existing_co.employee_count = co_extra.get('size')
                            existing_co.revenue_range = co_extra.get('revenue')
                            existing_co.linkedin = co_extra.get('linkedin')
                            existing_co.linkedin_id = co_extra.get('linkedin_id')
                            existing_co.description = co_extra.get('description')
                            existing_co.website = co_extra.get('website')

                            if status == "RESEARCH_COMPLETE":
                                existing_co.v2_intel = res.get('hooks', {})
                            logger.info(f"[IDEMPOTENCY] Updated existing company: {domain}")
                        else:
                            new_co = models.TargetCompany(
                                campaign_id=campaign_id,
                                name=co_extra.get('company_name_cleaned') or co_extra.get('name', domain),
                                domain=domain,
                                status=status,
                                relevance_score=res.get('relevance_score', 0),
                                relevance_explanation=res.get('reasoning', 'Filtered via AI Gatekeeper.'),
                                icp_research_context=res.get('icp_context'),

                                location=co_extra.get('location'),
                                company_type=co_extra.get('industry'),
                                employee_count=co_extra.get('size'),
                                revenue_range=co_extra.get('revenue'),
                                linkedin=co_extra.get('linkedin'),
                                linkedin_id=co_extra.get('linkedin_id'),
                                description=co_extra.get('description'),
                                website=co_extra.get('website'),

                                v2_intel=res.get('hooks', {}) if status == "RESEARCH_COMPLETE" else {}
                            )
                            temp_db.add(new_co)
                except Exception as row_err:
                    logger.error(f"❌ [STAGE 3] Skipped company {domain}: {row_err}")
            temp_db.commit()
        except Exception as e:
            temp_db.rollback()
            logger.error(f"❌ [STAGE 3] Error committing chunk: {e}")
            raise e
        finally:
            temp_db.close()

    async def stage_4_deep_research(self, db: Session, campaign_id: str, target_domains: list = None):
        """
        STAGE 4: Deep Research Swarm
        Mobilizes research agents for ACCEPTED companies.

        Scaling design (OOM-safe): companies are researched and committed in
        bounded CHUNKS rather than fanning out all rows and holding every
        result in memory until a single final commit. Peak memory is therefore
        capped at one chunk regardless of campaign size. Because the read query
        only selects status == "ACCEPTED", any company committed as
        RESEARCH_COMPLETE is auto-excluded from a later retry — so an OOM-kill
        or crash mid-run resumes from the last committed chunk instead of
        redoing the whole campaign.
        """
        import asyncio

        STAGE4_CHUNK_SIZE = 10   # companies committed per transaction
        STAGE4_CONCURRENCY = 3   # in-flight Tavily + LLM pipelines (heaviest stage)

        # 1. Fetch Brand DNA & Target Companies (Short Read Session)
        temp_db = SessionLocal()
        try:
            campaign = temp_db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
            ui_record = campaign.user_intel
            user_intel_dict = {
                "capability_to_pain_map": ui_record.capability_to_pain_map or [],
                "proof_points": ui_record.proof_points or [],
                "competitive_advantages": ui_record.competitive_advantages or []
            }

            query = temp_db.query(models.TargetCompany).filter(
                models.TargetCompany.campaign_id == campaign_id,
                models.TargetCompany.status == "ACCEPTED"
            )
            if target_domains:
                query = query.filter(models.TargetCompany.domain.in_(target_domains))

            accepted_cos_data = [
                {
                    "id": co.id,
                    "name": co.name,
                    "domain": co.domain,
                    "description": co.description
                }
                for co in query.all()
            ]
        finally:
            temp_db.close()

        if not accepted_cos_data:
            logger.warning(f"No ACCEPTED companies for {campaign_id}. Skipping Stage 4.")
            self._mark_stage4_complete(campaign_id)
            return

        # 2. Run long LLM research holding ZERO DB connections, throttled by semaphore.
        validator = CompanyValidationService()
        semaphore = asyncio.Semaphore(STAGE4_CONCURRENCY)

        async def research_one(co):
            async with semaphore:
                try:
                    # raw Tavily results are intentionally discarded: they are
                    # never read downstream and are the single biggest memory hog.
                    swarm_data, _raw = await validator.deep_research_swarm(
                        co["domain"],
                        co["name"],
                        user_intel_dict,
                        existing_description=co["description"]
                    )
                    return co["id"], swarm_data
                except Exception as e:
                    logger.error(f"Research failed for {co['domain']}: {e}")
                    return co["id"], None

        total = len(accepted_cos_data)
        logger.info(f"🔬 [STAGE 4] Researching {total} companies for {campaign_id} in chunks of {STAGE4_CHUNK_SIZE}.")

        # 3. Process in chunks: gather a chunk, commit it, release its memory, repeat.
        for start in range(0, total, STAGE4_CHUNK_SIZE):
            chunk = accepted_cos_data[start:start + STAGE4_CHUNK_SIZE]
            results = await asyncio.gather(*[research_one(co) for co in chunk])
            self._persist_stage4_chunk(campaign_id, results)
            logger.info(f"   ↳ [STAGE 4] Committed {min(start + STAGE4_CHUNK_SIZE, total)}/{total} for {campaign_id}.")
            del results  # free the chunk before scheduling the next one

        # 4. Mark campaign complete once every chunk has landed.
        self._mark_stage4_complete(campaign_id)
        logger.info(f"✅ [STAGE 4] Deep Research Complete for {campaign_id}")

    def _persist_stage4_chunk(self, campaign_id: str, results: list):
        """Persist one chunk of deep-research results in a single short transaction.

        Each row update is isolated via SAVEPOINT so a single malformed dossier
        is skipped rather than rolling back the whole chunk.
        """
        temp_db = SessionLocal()
        try:
            for co_id, swarm_data in results:
                if not swarm_data:
                    continue
                try:
                    with temp_db.begin_nested():  # SAVEPOINT per company
                        co = temp_db.query(models.TargetCompany).filter(models.TargetCompany.id == co_id).first()
                        if not co:
                            continue
                        # High-Fidelity Separate Field Persistence
                        co.relevance_score = swarm_data.get('relevance_score', co.relevance_score)
                        co.relevance_explanation = swarm_data.get('reasoning', co.relevance_explanation)
                        co.opportunity_reason = swarm_data.get('business_opportunity_reason', '')
                        co.matched_pains = swarm_data.get('matched_pains', [])
                        co.matched_services = swarm_data.get('matched_services', [])
                        co.growth_hooks = swarm_data.get('growth_hooks', [])
                        co.pain_hooks = swarm_data.get('pain_hooks', [])
                        co.news_hooks = swarm_data.get('news_hooks', [])
                        co.research_summary = swarm_data.get('executive_summary', '')

                        # Lean blob: only the structured LLM dossier (small).
                        # raw_swarm_results is dropped — it was never read anywhere.
                        co.v2_intel = swarm_data
                        co.status = "RESEARCH_COMPLETE"
                except Exception as row_err:
                    logger.error(f"❌ [STAGE 4] Skipped company {co_id}: {row_err}")
            temp_db.commit()
        except Exception as e:
            temp_db.rollback()
            logger.error(f"❌ [STAGE 4] Error committing chunk: {e}")
            raise e
        finally:
            temp_db.close()

    def _mark_stage4_complete(self, campaign_id: str):
        """Flip campaign status to STAGE_4_RESEARCH_COMPLETE in a short session."""
        temp_db = SessionLocal()
        try:
            campaign = temp_db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
            if campaign:
                campaign.status = models.CampaignStatus.STAGE_4_RESEARCH_COMPLETE
                temp_db.commit()
        finally:
            temp_db.close()

    async def stage_5_stakeholder_ranking(self, db: Session, campaign_id: str, contacts_map: dict):
        """
        STAGE 5: Strategic Stakeholder Ranking
        Selects Top 4 and applies Primary-First email logic using AI.
        """
        # 1. Fetch Researched Companies & Brand DNA (Short Read Session)
        temp_db = SessionLocal()
        try:
            campaign = temp_db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
            ui_record = campaign.user_intel
            user_intel_dict = {
                "target_customers": ui_record.target_customers or [],
                "capability_to_pain_map": ui_record.capability_to_pain_map or [],
                "competitive_advantages": ui_record.competitive_advantages or []
            }

            researched_cos_data = [
                {
                    "id": co.id,
                    "domain": co.domain,
                    "name": co.name,
                    "research_summary": co.research_summary
                }
                for co in temp_db.query(models.TargetCompany).filter(
                    models.TargetCompany.campaign_id == campaign_id,
                    models.TargetCompany.status == "RESEARCH_COMPLETE"
                ).all()
            ]
        finally:
            temp_db.close()

        STAGE5_CHUNK_SIZE = 10   # companies committed per transaction
        total = len(researched_cos_data)
        logger.info(f"🔍 [STAGE 5] Ranking stakeholders for {total} researched companies in chunks of {STAGE5_CHUNK_SIZE}.")

        # 2. Process companies in chunks; rank (sequential, ZERO DB held), then commit each chunk.
        # Resumability: each committed company flips to STAKEHOLDERS_IDENTIFIED, so the
        # read query (status == "RESEARCH_COMPLETE") won't re-select it on a retry.
        ranking_svc = StakeholderRankingService()

        def get_fuzzy(data, targets, default=None):
            for k in data.keys():
                norm_k = k.lower().replace(" ", "").replace("_", "").replace("-", "")
                for t in targets:
                    norm_t = t.lower().replace(" ", "").replace("_", "").replace("-", "")
                    if norm_k == norm_t:
                        return data.get(k)
            return default

        for start in range(0, total, STAGE5_CHUNK_SIZE):
            chunk = researched_cos_data[start:start + STAGE5_CHUNK_SIZE]
            dms_to_create = []
            company_status_updates = []

            for co in chunk:
                domain_key = (co["domain"] or "").strip().lower()
                csv_prospects = contacts_map.get(domain_key, [])
                if not csv_prospects:
                    for k in contacts_map.keys():
                        if k.strip().lower() == domain_key:
                            csv_prospects = contacts_map[k]
                            break

                if not csv_prospects:
                    logger.debug(f"⏭️ [STAGE 5] No prospects found in CSV for {domain_key}. Skipping.")
                    # Still mark processed so a retry doesn't re-rank an empty company.
                    company_status_updates.append(co["id"])
                    continue

                prospects_to_process = []
                if len(csv_prospects) <= 4:
                    logger.info(f"⏭️ [STAGE 5] Low prospect count ({len(csv_prospects)}). Applying Management-Level Auto-Pass.")
                    mgmt_keywords = r'director|manager|vp|vice president|chief|head|lead|senior|principal|partner|owner'
                    for p in csv_prospects:
                        title = ""
                        for key in p.keys():
                            if key.lower() in ['title', 'position']:
                                title = str(p.get(key) or "").lower()
                                break
                        if re.search(mgmt_keywords, title):
                            p['strategic_score'] = 100
                            p['strategic_reasoning'] = "Auto-passed via Management Gate."
                            prospects_to_process.append(p)
                else:
                    research_context = co["research_summary"] or ""
                    try:
                        ranked_prospects = await ranking_svc.rank_stakeholders_with_ai(csv_prospects, user_intel_dict, research_context)
                        prospects_to_process = ranked_prospects[:4]
                    except Exception as e:
                        logger.error(f"Failed to rank stakeholders with AI for {domain_key}: {e}")
                        prospects_to_process = csv_prospects[:4]

                logger.info(f"   - Processing {len(csv_prospects)} candidates for {co['domain']}...")
                for p in prospects_to_process:
                    target_email = get_fuzzy(p, ["primary email", "email"])
                    is_verified = True if target_email else False

                    if not target_email:
                        for i in range(1, 11):
                            val_status = str(get_fuzzy(p, [f"email {i} validation", f"email_{i}_validation"]) or "").lower()
                            if val_status in ['valid', 'accept all', 'deliverable']:
                                target_email = get_fuzzy(p, [f"email {i}", f"email_{i}"])
                                is_verified = True
                                break

                    if not target_email:
                        logger.debug(f"   - Skipping {get_fuzzy(p, ['contact full name', 'name'])}: No valid email found.")
                        continue

                    dms_to_create.append({
                        "target_company_id": co["id"],
                        "name": get_fuzzy(p, ["contact full name", "name"], "Unknown"),
                        "position": get_fuzzy(p, ["title", "position"], "Stakeholder"),
                        "seniority": get_fuzzy(p, ["seniority"]),
                        "location": get_fuzzy(p, ["contact location", "location"]),
                        "email": target_email,
                        "phone": get_fuzzy(p, ["contact phone 1", "phone"]),
                        "company_phone": get_fuzzy(p, ["company phone 1", "company phone"]),
                        "linkedin": get_fuzzy(p, ["contact li profile url", "linkedin"]),
                        "time_in_role": get_fuzzy(p, ["time in role"]),
                        "time_at_company": get_fuzzy(p, ["time at company"]),
                        "is_email_verified": is_verified,
                        "relevance_score": p.get("strategic_score", 0),
                        "relevance_explanation": p.get("strategic_reasoning") or "Identified via SToT matching."
                    })

                company_status_updates.append(co["id"])

            self._persist_stage5_chunk(campaign_id, dms_to_create, company_status_updates)
            logger.info(f"   ↳ [STAGE 5] Committed {min(start + STAGE5_CHUNK_SIZE, total)}/{total} for {campaign_id}.")
            del dms_to_create, company_status_updates

        # 3. Mark stage complete once every chunk has landed.
        temp_db = SessionLocal()
        try:
            campaign = temp_db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
            if campaign:
                campaign.status = models.CampaignStatus.STAGE_5_STAKEHOLDERS_RANKED
                temp_db.commit()
        finally:
            temp_db.close()
        logger.info(f"✅ [STAGE 5] Stakeholder Ranking Complete for {campaign_id}")

    def _persist_stage5_chunk(self, campaign_id: str, dms_to_create: list, company_status_updates: list):
        """Persist one chunk of decision makers + company status in a single short transaction.

        Upsert keeps the stage idempotent (no UniqueViolation on campaign_id+email on
        retry). Each DM is isolated via SAVEPOINT so one bad row doesn't sink the chunk.
        """
        temp_db = SessionLocal()
        try:
            total_dms = 0
            for dm_data in dms_to_create:
                try:
                    with temp_db.begin_nested():  # SAVEPOINT per decision maker
                        existing_dm = temp_db.query(models.DecisionMaker).filter(
                            models.DecisionMaker.campaign_id == campaign_id,
                            models.DecisionMaker.email == dm_data["email"]
                        ).first()

                        if existing_dm:
                            existing_dm.name = dm_data["name"]
                            existing_dm.position = dm_data["position"]
                            existing_dm.seniority = dm_data["seniority"]
                            existing_dm.location = dm_data["location"]
                            existing_dm.phone = dm_data["phone"]
                            existing_dm.company_phone = dm_data["company_phone"]
                            existing_dm.linkedin = dm_data["linkedin"]
                            existing_dm.time_in_role = dm_data["time_in_role"]
                            existing_dm.time_at_company = dm_data["time_at_company"]
                            existing_dm.is_email_verified = dm_data["is_email_verified"]
                            existing_dm.relevance_score = dm_data["relevance_score"]
                            existing_dm.relevance_explanation = dm_data["relevance_explanation"]
                            logger.info(f"[IDEMPOTENCY] Updated existing decision maker: {dm_data['email']}")
                        else:
                            new_dm = models.DecisionMaker(
                                campaign_id=campaign_id,
                                target_company_id=dm_data["target_company_id"],
                                name=dm_data["name"],
                                position=dm_data["position"],
                                seniority=dm_data["seniority"],
                                location=dm_data["location"],
                                email=dm_data["email"],
                                phone=dm_data["phone"],
                                company_phone=dm_data["company_phone"],
                                linkedin=dm_data["linkedin"],
                                time_in_role=dm_data["time_in_role"],
                                time_at_company=dm_data["time_at_company"],
                                is_email_verified=dm_data["is_email_verified"],
                                relevance_score=dm_data["relevance_score"],
                                relevance_explanation=dm_data["relevance_explanation"],
                                status="NEW",
                                state=models.ProspectState.NEW
                            )
                            temp_db.add(new_dm)
                            total_dms += 1
                except Exception as row_err:
                    logger.error(f"❌ [STAGE 5] Skipped DM {dm_data.get('email')}: {row_err}")

            for co_id in company_status_updates:
                co = temp_db.query(models.TargetCompany).filter(models.TargetCompany.id == co_id).first()
                if co:
                    co.status = "STAKEHOLDERS_IDENTIFIED"

            temp_db.commit()
            logger.info(f"   ↳ [STAGE 5] Chunk committed. New DMs: {total_dms}")
        except Exception as e:
            temp_db.rollback()
            logger.error(f"❌ [STAGE 5] Error committing chunk: {e}")
            raise e
        finally:
            temp_db.close()

    def process_state_machine(self, db: Session, campaign_id: str, csv_content: str = None):
        """
        The Indestructible Orchestrator (Sync V3).
        Checks status and executes the next logical stage based on SSoT presence.
        """
        campaign = db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
        if not campaign: return

        logger.info(f"🔄 [State-Machine] Resuming Campaign {campaign_id} at Status: {campaign.status}")

        # New Gated Architecture (Phase 1):
        # A: Input Validation
        # B: CSV Trimming
        # C: User Intel
        # D: ICP Filtering (Wait for B & C)
        
        val_review = campaign.input_validation_review
        # Support both schemas: Agent B (is_valid) and Stage 1 Review (overall.status)
        if val_review:
            is_valid_flag = val_review.get('is_valid')
            status_val = val_review.get('overall', {}).get('status')
            
            val_done = (is_valid_flag is True) or (status_val == "success")
            val_failed = (is_valid_flag is False) or (status_val == "needs_clarification")
        else:
            val_done = False
            val_failed = False
        
        csv_done = (campaign.trimmed_csv_data is not None) or (campaign.csv_file_url is not None)
        intel_done = campaign.user_intel is not None and campaign.user_intel.v2_intel is not None
        
        logger.info(f"📊 [State-Machine] Snapshot for {campaign_id}:")
        logger.info(f"   - Validation: {val_done} (Failed: {val_failed}) | Data: {val_review is not None}")
        logger.info(f"   - CSV SSoT: {csv_done}")
        logger.info(f"   - User Intel: {intel_done}")

        # Intervention Logic: If Validation failed, stop and notify
        if val_failed and campaign.status != "INTERVENTION_NEEDED":
            logger.warning(f"🛑 [State-Machine] Validation Failed for {campaign_id}. Halting workflow.")
            campaign.status = "INTERVENTION_NEEDED"
            db.commit()
            return

        if campaign.status in [models.CampaignStatus.PENDING, models.CampaignStatus.INPUT_VALIDATED, models.CampaignStatus.STAGE_1_CSV_TRIMMED, models.CampaignStatus.STAGE_2_USER_INTEL_COMPLETE, models.CampaignStatus.RESEARCHING_USER_COMPANY, "INTERVENTION_NEEDED"]:
            from app.workers.tasks.intel_worker import validate_input_worker, research_user_company_worker, process_csv_worker
            
            # 1. Track A & B start immediately
            if not val_done and not val_failed:
                validate_input_worker.delay(campaign_id)
            if not csv_done and not val_failed:
                process_csv_worker.delay(campaign_id)
            
            # 2. Track C (User Intel) waits for A (Validation)
            # Gate: Only trigger if A is done AND C hasn't started/finished yet
            # Track C: User Intel (Brand Research)
            if val_done and not intel_done:
                # If not currently locked, or in a different status, trigger research
                if not campaign.locked_by or campaign.status != models.CampaignStatus.RESEARCHING_USER_COMPANY:
                    logger.info(f"🟢 [State-Machine] A is done. Triggering/Resuming Track C (User Intel) for {campaign_id}")
                    campaign.status = models.CampaignStatus.RESEARCHING_USER_COMPANY
                    db.commit()
                    research_user_company_worker.delay(campaign_id)
                else:
                    logger.debug(f"⏳ [State-Machine] Track C already active for {campaign_id} (Locked by: {campaign.locked_by})")
            
            # Barrier: Wait for B & C to be complete before starting Stage 3
            if not (csv_done and intel_done):
                logger.info(f"[State-Machine] Progress Tracking -> A(Val): {val_done}, B(CSV): {csv_done}, C(Intel): {intel_done}. Waiting for B & C sync.")
                return

            if csv_done and intel_done and campaign.status in [models.CampaignStatus.RESEARCHING_USER_COMPANY, models.CampaignStatus.STAGE_1_CSV_TRIMMED, models.CampaignStatus.STAGE_2_USER_INTEL_COMPLETE]:
                logger.info(f"🚀 [State-Machine] Track B & C complete. Advancing to Stage 3: ICP Filtering.")
                campaign.status = models.CampaignStatus.STAGE_2_USER_INTEL_COMPLETE
                db.commit()
                
                from app.workers.tasks.discovery_worker import find_companies_worker
                find_companies_worker.delay(campaign_id)
                return

        # STAGE 3 -> STAGE 4 Transition
        if campaign.status == models.CampaignStatus.STAGE_3_ICP_FILTERED:
             logger.info(f"🚀 [State-Machine] Stage 3 (ICP) Complete. Triggering Stage 4: Deep Research.")
             # We move status to a transition state or trigger worker directly if worker handles status
             from app.workers.tasks.discovery_worker import deep_research_worker
             deep_research_worker.delay(campaign_id)
             return

        # STAGE 4 -> STAGE 5 Transition
        if campaign.status == models.CampaignStatus.STAGE_4_RESEARCH_COMPLETE:
             logger.info(f"🚀 [State-Machine] Stage 4 (Research) Complete. Triggering Stage 5: Stakeholder Ranking.")
             from app.workers.tasks.discovery_worker import find_dms_worker
             find_dms_worker.delay(campaign_id)
             return

        # STAGE 5 -> STAGE 6 Transition
        if campaign.status == models.CampaignStatus.STAGE_5_STAKEHOLDERS_RANKED:
            logger.info(f"🏁 [State-Machine] Stage 5 (Ranking) Complete. Triggering Final Stage: Email Drafting.")
            from app.workers.tasks.ghostwriter_worker import draft_emails_worker
            draft_emails_worker.delay(campaign_id)
            return


campaign_service = CampaignService()
