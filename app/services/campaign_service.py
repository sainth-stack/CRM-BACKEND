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
from app.core.config import settings
from app.core.sanitizer import strip_null_bytes
from app.core.circuit_breaker import circuit_is_open, record_circuit_failure, CircuitOpenError
from app.core.llm_resilience import OPENAI_CIRCUIT
from app.services.company_validation_service import CompanyValidationService
from app.services.stakeholder_service import StakeholderRankingService
from app.services.user_intel_service import UserIntelService
from app.db.database import SessionLocal

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



    def _pulse_heartbeat(self, campaign_id: str, worker_id: str | None) -> None:
        """Refresh the campaign lease heartbeat from inside a long stage loop.

        Long stages (3/4/5) can run for many minutes. Without a heartbeat the
        stuck-campaign sweeper treats them as dead (last_heartbeat is NULL/stale),
        force-releases their stage lock, and re-dispatches the stage — the ICP
        resurrection loop. Pulsing after each chunk keeps the lease fresh so the
        sweeper only resurrects genuinely dead campaigns. Best-effort: a failed
        pulse must never crash the stage.
        """
        if not worker_id:
            return
        hb = SessionLocal()
        try:
            heartbeat_lease(hb, campaign_id, worker_id)
        except Exception:
            pass
        finally:
            hb.close()

    def _fetch_judged_icp_domains(self, campaign_id: str) -> set:
        """Domains already given a terminal ICP verdict for this campaign (any
        status except ERROR). Used by Stage 3 for resume-awareness so a restart
        skips already-decided companies. Best-effort: on error, return empty so
        the stage simply reprocesses (correct, just not optimised)."""
        db = SessionLocal()
        try:
            rows = (
                db.query(models.TargetCompany.domain)
                .filter(
                    models.TargetCompany.campaign_id == campaign_id,
                    models.TargetCompany.status != "ERROR",
                )
                .all()
            )
            return {r[0] for r in rows if r[0]}
        except Exception:
            return set()
        finally:
            db.close()

    async def stage_3_icp_filtering(self, db: Session, campaign_id: str, unique_cos: dict, worker_id: str = None):
        """
        STAGE 3+4 (MERGED): ICP Qualification + Deep Research in one pass.

        Each company gets a single combined agent call (website-evidence company
        intel feeding one LLM) that returns the accept/reject
        verdict AND the full research dossier. The dossier is persisted for EVERY
        company (accepted or rejected) — only the verdict differs — so there is
        no separate deep-research stage. This halves the per-company round-trips and
        removes an entire Celery stage (the t2.medium latency/CPU win).
        """
        # 1. Fetch the sender profile (short read session).
        temp_db = SessionLocal()
        try:
            campaign = temp_db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
            ui_record = campaign.user_intel
            # Full sender profile so the ICP gate judges against the real sender,
            # not a hardcoded profile.
            user_intel_dict = {
                "company_name": ui_record.company_name,
                "services": json.loads(ui_record.offerings) if ui_record.offerings else [],
                "target_customers": ui_record.target_customers or [],
                "competitive_advantages": ui_record.competitive_advantages or [],
                "capability_to_pain_map": ui_record.capability_to_pain_map or [],
                "proof_points": ui_record.proof_points or [],
                "deep_research": ui_record.deep_research or "",
            }
            # All of the user's campaign filters reach the gate (target_industry and
            # the objective prompt were previously dropped — the industry filter was
            # silently ignored).
            campaign_metadata = {
                "target_industry": campaign.target_industry,
                "target_location": campaign.target_location,
                "target_employee_count": campaign.target_employee_count,
                "prompt": campaign.prompt,
            }
        finally:
            temp_db.close()

        # 2. Run long LLM validation holding ZERO DB connections
        validator = CompanyValidationService()
        import asyncio

        STAGE3_COMMIT_BATCH = 10   # companies persisted per DB transaction
        # Concurrency (companies in flight) is deliberately DECOUPLED from the commit
        # batch size. A streaming worker pool keeps ICP_CONCURRENCY companies running
        # at all times: the moment one finishes, the freed slot picks up the next, so a
        # single slow site / slow LLM call can no longer stall a whole batch (the old
        # per-chunk `gather` waited for the slowest of 10 before starting the next 10).
        # Verdicts are identical — only scheduling changes. Tune width via ICP_CONCURRENCY.
        semaphore = asyncio.Semaphore(settings.ICP_CONCURRENCY)

        # In-memory circuit flag. We check Redis (circuit_is_open) only at batch
        # boundaries — NOT once per company — so the per-company path stays free of a
        # synchronous Redis round-trip that would stall the event loop. validate_one
        # reads this flag (late-bound) and skips cheaply once it flips.
        circuit_open = [circuit_is_open(OPENAI_CIRCUIT)[0]]

        async def validate_one(domain, co_data, research_cache):
            async with semaphore:
                # Fail-fast: once the provider circuit is open, drain the remaining
                # queued companies cheaply instead of burning each one's retry backoff.
                # They stay unjudged and are resumed on the rescheduled run.
                if circuit_open[0]:
                    return domain, {"_circuit_skipped": True}
                try:
                    res = await validator.qualify_and_enrich(co_data, user_intel_dict, campaign_metadata, research_cache=research_cache)
                    return domain, res
                except Exception as e:
                    logger.error(f"Qualify+enrich failed for {domain}: {e}")
                    return domain, {"status": "ERROR", "reasoning": str(e), "_llm_error": True}

        # 3. Build the work list + resume-awareness: skip companies already given a
        # terminal ICP verdict for THIS campaign (any status except ERROR) so a
        # restart/retry/resurrection does not re-pay LLM + crawl for work already done.
        # ERROR rows are kept so transient failures are re-judged. This changes only
        # *what is recomputed*, never the qualification result for any company.
        items = list(unique_cos.items())
        judged = self._fetch_judged_icp_domains(campaign_id)
        if judged:
            before = len(items)
            items = [(d, data) for d, data in items if d not in judged]
            logger.info(
                f"♻️ [STAGE 3] Resume for {campaign_id}: {before - len(items)} already judged, "
                f"{len(items)} remaining."
            )

        total = len(items)
        logger.info(
            f"🚦 [STAGE 3] Filtering {total} companies for {campaign_id} "
            f"(streaming pool, concurrency={settings.ICP_CONCURRENCY}, commit batch {STAGE3_COMMIT_BATCH})."
        )

        # One batched cache read for ALL remaining domains (was once per chunk): any
        # already-known, still-fresh domain skips crawl + enrich.
        research_cache = validator.prefetch_icp_research([d for d, _ in items])

        def _commit_batch(batch: list) -> None:
            """Persist one batch, pulse the lease heartbeat, and run outage detection.
            A batch where EVERY company hit an LLM/transport error trips the breaker
            (keyed off the non-persisted `_llm_error` flag, never a legit REJECTED)."""
            self._persist_stage3_chunk(campaign_id, batch, unique_cos)
            self._pulse_heartbeat(campaign_id, worker_id)
            logger.info(f"   ↳ [STAGE 3] Committed {processed}/{total} for {campaign_id}.")
            if all(r.get("_llm_error") for _, r in batch):
                record_circuit_failure(OPENAI_CIRCUIT, "Stage 3 batch fully errored")
            # Refresh the in-memory flag once per batch; once open, remaining queued
            # companies skip cheaply and we reschedule after the loop.
            if not circuit_open[0] and circuit_is_open(OPENAI_CIRCUIT)[0]:
                circuit_open[0] = True

        # 4. Streaming fan-out: every company is launched behind the shared semaphore
        #    (so at most ICP_CONCURRENCY run at once) and results are handled AS THEY
        #    COMPLETE, persisted every STAGE3_COMMIT_BATCH. Completed tasks are released
        #    each loop so only the in-flight working set stays in memory.
        pending = {
            asyncio.create_task(validate_one(domain, data, research_cache))
            for domain, data in items
        }
        buffer: list = []
        processed = 0
        skipped = 0
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for fut in done:
                    domain, res = fut.result()
                    if res.get("_circuit_skipped"):
                        skipped += 1
                        continue
                    buffer.append((domain, res))
                    processed += 1
                    if len(buffer) >= STAGE3_COMMIT_BATCH:
                        _commit_batch(buffer)
                        buffer = []
                del done
            if buffer:  # flush the final partial batch
                _commit_batch(buffer)
                buffer = []
        finally:
            for t in pending:
                t.cancel()

        # If the provider circuit opened mid-run we deliberately skipped the remaining
        # companies; reschedule so the resume-aware filter finishes them next pass.
        if skipped:
            logger.warning(
                f"[STAGE 3] Provider circuit open: {skipped} companies deferred for {campaign_id}. Rescheduling."
            )
            raise CircuitOpenError("OpenAI circuit open during Stage 3 ICP filtering")

        # 4. Mark stage complete once every chunk has landed. Because qualification
        # AND research happened together, accepted companies are already
        # RESEARCH_COMPLETE — advance the campaign straight to STAGE_4 so the state
        # machine routes to stakeholder ranking (the separate deep-research stage is
        # now a no-op).
        temp_db = SessionLocal()
        try:
            campaign = temp_db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
            if campaign:
                campaign.status = models.CampaignStatus.STAGE_4_RESEARCH_COMPLETE
                temp_db.commit()
        finally:
            temp_db.close()
        logger.info(f"✅ [STAGE 3+4] Combined ICP Qualification + Research Complete for {campaign_id}")

    @staticmethod
    def _apply_enrichment(co, res: dict) -> None:
        """Write the combined-agent research dossier onto a TargetCompany row.

        Applied to EVERY company regardless of verdict (accepted or rejected):
        the merged ICP+research agent grounds this dossier in the same web evidence
        for all of them, so every row is displayable. The fields default to empty
        only when the agent hard-failed or had no evidence to work from. The verdict
        status (ACCEPTED / REJECTED) gates *pipeline progression*, not whether
        research is stored."""
        co.relevance_score = res.get('relevance_score', co.relevance_score)
        co.opportunity_reason = res.get('business_opportunity_reason', '') or ''
        co.matched_pains = res.get('matched_pains', []) or []
        co.matched_services = res.get('matched_services', []) or []
        co.growth_hooks = res.get('growth_hooks', []) or []
        co.pain_hooks = res.get('pain_hooks', []) or []
        co.news_hooks = res.get('news_hooks', []) or []
        co.research_summary = res.get('research_summary', '') or ''
        co.v2_intel = res.get('hooks', {}) or {}

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
                        # Scrub NUL bytes (0x00) from scraped/CSV text — Postgres TEXT
                        # rejects them and would drop the whole row otherwise.
                        res = strip_null_bytes(res)
                        status = res.get('status', 'REJECTED')
                        co_extra = strip_null_bytes(unique_cos.get(domain, {}))

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

                            # Persist the research dossier for EVERY verdict (accepted
                            # or rejected). The combined agent produces it for all
                            # companies; only a hard LLM failure or a web-scrape miss
                            # leaves it empty. Keeping it for rejects lets the UI show
                            # why a lead was filtered, not just that it was.
                            self._apply_enrichment(existing_co, res)
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
                            )
                            # Research dossier persisted for every verdict (see above).
                            self._apply_enrichment(new_co, res)
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

    async def stage_5_stakeholder_ranking(self, db: Session, campaign_id: str, worker_id: str = None):
        """
        STAGE 5: Stakeholder ranking.
        Selects the top 4 and applies primary-first email logic using AI.

        Contacts are read per-chunk from campaign_leads (not held as one big
        contacts_map), so memory stays flat regardless of batch size.
        """
        from app.services import lead_repository
        # 1. Fetch researched companies + sender profile (short read session).
        temp_db = SessionLocal()
        try:
            campaign = temp_db.query(models.Campaign).filter(models.Campaign.id == campaign_id).first()
            ui_record = campaign.user_intel
            user_intel_dict = {
                "target_customers": ui_record.target_customers or [],
                "capability_to_pain_map": ui_record.capability_to_pain_map or [],
                "competitive_advantages": ui_record.competitive_advantages or []
            }
            # Campaign objective steers persona selection ("achieve the prompt").
            objective = campaign.prompt or ""

            researched_cos_data = [
                {
                    "id": co.id,
                    "domain": co.domain,
                    "name": co.name,
                    "research_summary": co.research_summary,
                    "industry": co.company_type,   # drives the dynamic buying-committee in ranking
                }
                for co in temp_db.query(models.TargetCompany).filter(
                    models.TargetCompany.campaign_id == campaign_id,
                    models.TargetCompany.status == "ACCEPTED"
                ).all()
            ]
        finally:
            temp_db.close()

        STAGE5_COMMIT_BATCH = 10   # companies persisted per DB transaction
        total = len(researched_cos_data)
        logger.info(
            f"🔍 [STAGE 5] Ranking stakeholders for {total} researched companies "
            f"(streaming pool, concurrency={settings.STAGE5_CONCURRENCY}, commit batch {STAGE5_COMMIT_BATCH})."
        )

        # Resumability: each committed company flips to STAKEHOLDERS_IDENTIFIED, so the
        # read query (status == "ACCEPTED") won't re-select it on a retry.
        ranking_svc = StakeholderRankingService()
        import asyncio
        from app.core.scheduler import geocode_timezone
        # Per-run cache so each distinct contact location is geocoded only once
        # (Nominatim is rate-limited). Shared across the whole Stage 5 run.
        tz_cache: dict = {}

        def get_fuzzy(data, targets, default=None):
            for k in data.keys():
                norm_k = k.lower().replace(" ", "").replace("_", "").replace("-", "")
                for t in targets:
                    norm_t = t.lower().replace(" ", "").replace("_", "").replace("-", "")
                    if norm_k == norm_t:
                        return data.get(k)
            return default

        # Preload every accepted company's contacts in ONE short session (was once per
        # chunk). domains in campaign_leads and TargetCompany.domain are both normalized
        # (lowercased), so an exact match reproduces the old case-insensitive lookup.
        cdb = SessionLocal()
        try:
            all_contacts = lead_repository.load_contacts_for_domains(
                cdb, campaign_id, [co["domain"] for co in researched_cos_data]
            )
        finally:
            cdb.close()

        # Concurrency (companies in flight) is DECOUPLED from the commit batch size: a
        # streaming pool keeps STAGE5_CONCURRENCY rankings running at all times so one
        # slow AI persona-fit call no longer stalls a whole batch (the old per-chunk
        # `gather` waited for the slowest of 10). Ranking holds ZERO DB connections;
        # DM-building (geocode via the shared tz_cache) runs serially in the result
        # handler so Nominatim stays rate-limited and the cache stays race-free.
        rank_sem = asyncio.Semaphore(settings.STAGE5_CONCURRENCY)

        async def _rank_company(co):
            domain_key = (co["domain"] or "").strip().lower()
            prospects = all_contacts.get(domain_key, [])
            if not prospects:
                return co, []
            research_context = co["research_summary"] or ""
            async with rank_sem:
                try:
                    ranked = await ranking_svc.rank_stakeholders_with_ai(
                        prospects, user_intel_dict, research_context,
                        objective=objective, industry=co.get("industry") or "",
                    )
                    return co, ranked[:4]
                except Exception as e:
                    logger.error(f"Failed to rank stakeholders with AI for {domain_key}: {e}")
                    return co, prospects[:4]

        pending = {asyncio.create_task(_rank_company(co)) for co in researched_cos_data}
        dms_to_create: list = []
        company_status_updates: list = []
        processed = 0

        def _commit_batch() -> None:
            self._persist_stage5_chunk(campaign_id, dms_to_create, company_status_updates)
            self._pulse_heartbeat(campaign_id, worker_id)
            logger.info(f"   ↳ [STAGE 5] Committed {processed}/{total} for {campaign_id}.")

        # Streaming fan-out: handle each ranking AS IT COMPLETES, persisting every
        # STAGE5_COMMIT_BATCH companies. Completed tasks are released each loop.
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for fut in done:
                    co, prospects_to_process = fut.result()
                    for p in prospects_to_process:
                        # Reachability is decided solely by the Primary Email now; the
                        # per-slot email-validation columns were removed.
                        target_email = get_fuzzy(p, ["primary email", "email"])
                        is_verified = bool(target_email)
                        if not target_email:
                            continue

                        # Eagerly resolve & persist the prospect timezone now (geocoded
                        # from the contact location, company location as fallback) so it
                        # is no longer NULL waiting for the first dispatch.
                        contact_loc = get_fuzzy(p, ["contact location", "location"])
                        tz_location = contact_loc or get_fuzzy(p, ["company location"])
                        resolved_tz = geocode_timezone(tz_location, tz_cache)

                        dms_to_create.append({
                            "target_company_id": co["id"],
                            "name": get_fuzzy(p, ["contact full name", "name"], "Unknown"),
                            "position": get_fuzzy(p, ["title", "position"], "Stakeholder"),
                            "seniority": get_fuzzy(p, ["seniority"]),
                            "location": contact_loc,
                            "display_timezone": resolved_tz,
                            "email": target_email,
                            "phone": get_fuzzy(p, ["contact mobile phone", "contact phone 1", "phone"]),
                            "company_phone": get_fuzzy(p, ["company phone 1", "company phone"]),
                            "linkedin": get_fuzzy(p, ["contact li profile url", "linkedin"]),
                            "time_in_role": get_fuzzy(p, ["time in role"]),
                            "time_at_company": get_fuzzy(p, ["time at company"]),
                            "is_email_verified": is_verified,
                            "relevance_score": p.get("strategic_score", 0),
                            "relevance_explanation": p.get("strategic_reasoning") or "Identified via SToT matching."
                        })

                    company_status_updates.append(co["id"])
                    processed += 1
                    if len(company_status_updates) >= STAGE5_COMMIT_BATCH:
                        _commit_batch()
                        dms_to_create = []
                        company_status_updates = []
                del done
            if company_status_updates or dms_to_create:  # flush final partial batch
                _commit_batch()
                dms_to_create = []
                company_status_updates = []
        finally:
            for t in pending:
                t.cancel()

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
                        # Scrub NUL bytes from CSV-derived contact fields (Postgres-safe).
                        dm_data = strip_null_bytes(dm_data)
                        existing_dm = temp_db.query(models.DecisionMaker).filter(
                            models.DecisionMaker.campaign_id == campaign_id,
                            models.DecisionMaker.email == dm_data["email"]
                        ).first()

                        if existing_dm:
                            existing_dm.name = dm_data["name"]
                            existing_dm.position = dm_data["position"]
                            existing_dm.seniority = dm_data["seniority"]
                            existing_dm.location = dm_data["location"]
                            # Only overwrite a resolved tz; never wipe a known-good
                            # value with None on an idempotent re-run.
                            if dm_data.get("display_timezone"):
                                existing_dm.display_timezone = dm_data["display_timezone"]
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
                                display_timezone=dm_data.get("display_timezone"),
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

    def process_state_machine(self, db: Session, campaign_id: str):
        """
        Campaign stage orchestrator (chokepoint).

        Delegates to the LangGraph workflow engine, which owns explicit,
        checkpointed stage transitions. The runner already falls back to an
        ephemeral graph if the checkpointer is unavailable, so routing always
        proceeds.
        """
        from app.workflow.runner import advance_workflow
        return advance_workflow(campaign_id)


campaign_service = CampaignService()
