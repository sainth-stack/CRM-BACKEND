import logging
from app.db import models
from app.core.resilience import retry_with_backoff

logger = logging.getLogger(__name__)

class DraftingService:
    # Stateless: the actual LLMs live in the email sub-graph (app.agents.email_graph),
    # routed via app.core.llm. No per-instance client is needed.

    @retry_with_backoff(max_attempts=3, base_delay_sec=2.0, max_delay_sec=15.0)
    async def agenerate_draft_set(self, db, dm_id: str):
        from app.db.database import SessionLocal
        # 1. Gather Context Cluster in a short-lived read transaction
        temp_db = SessionLocal()
        try:
            dm = temp_db.query(models.DecisionMaker).filter(models.DecisionMaker.id == dm_id).first()
            if not dm: return None
            
            target_co = dm.target_company
            campaign = dm.campaign
            user_intel = campaign.user_intel
            
            if not user_intel: return None

            sender_name = user_intel.company_name
            sender_services = user_intel.offerings

            # Fetch campaign owner name
            user_name = campaign.owner.full_name if campaign.owner else None
            if not user_name:
                user_name = campaign.owner.email.split('@')[0] if campaign.owner and campaign.owner.email else "Account Manager"

            prospect_name = dm.name
            target_company_name = target_co.name
            prospect_role = dm.position or "Executive"
            prospect_seniority = dm.seniority or "Management"
            research_summary = target_co.research_summary or "N/A"
            growth_hooks = ", ".join(target_co.growth_hooks or [])
            pain_hooks = ", ".join(target_co.pain_hooks or [])
            news_hooks = ", ".join(target_co.news_hooks or [])
            opportunity_reason = target_co.opportunity_reason or ""
            objective = campaign.prompt or "Win a discovery call with the right stakeholder."

            def _join(v):
                if isinstance(v, (list, tuple)):
                    return "; ".join(str(x) for x in v if str(x).strip()) or "N/A"
                return str(v) if v else "N/A"

            # need_evidence from MEDDPICC scorecard — primary pain input.
            # Dropped: metrics, economic_buyer, champion, decision_criteria — all drove
            # the capability-bridge paragraph removed in the pain-first redesign.
            # Dropped: sender_map (capability_to_pain_map JSON) — no longer in the prompt.
            meddpicc = (target_co.v2_intel or {}).get("meddpicc", {}) if target_co.v2_intel else {}
            need_evidence = _join(meddpicc.get("need_evidence"))
            matched_pains = _join(target_co.matched_pains or meddpicc.get("matched_pains"))
            matched_services = _join(target_co.matched_services or meddpicc.get("matched_services"))
            recipient_role_signal = dm.relevance_explanation or "Role/influence not yet assessed."
            sender_proof = _join(user_intel.proof_points)
            sender_advantages = _join(user_intel.competitive_advantages)

        finally:
            temp_db.close()

        # 2. Run the Draft -> Critique -> Refine sub-graph (Strategist/Writer/Critic).
        from app.agents.email_graph import get_email_graph, MAX_ATTEMPTS
        from app.agents.email_drafter import clean_email_body
        from app.services.observability_service import ObservabilityService

        first_name = prospect_name.split(' ')[0] if prospect_name and ' ' in prospect_name else (prospect_name or "there")

        ctx = {
            "sender_name":          sender_name,
            "sender_services":      sender_services,
            "user_name":            user_name,
            "prospect_name":        prospect_name,
            "prospect_first_name":  first_name,
            "target_company":       target_company_name,
            "prospect_role":        prospect_role,
            "prospect_seniority":   prospect_seniority,
            "research_summary":     research_summary,
            "growth_hooks":         growth_hooks,
            "pain_hooks":           pain_hooks,
            "news_hooks":           news_hooks,
            "opportunity_reason":   opportunity_reason,
            "objective":            objective,
            "need_evidence":        need_evidence,
            "matched_pains":        matched_pains,
            "matched_services":     matched_services,
            "recipient_role_signal": recipient_role_signal,
            "sender_proof":         sender_proof,
            "sender_advantages":    sender_advantages,
        }

        from app.core.logging_config import agent_label_var, company_domain_var
        _dom = target_co.domain if target_co and hasattr(target_co, "domain") else None
        _dom_tok = company_domain_var.set(_dom or target_company_name)
        _ag_tok  = agent_label_var.set("email_draft")
        try:
            with ObservabilityService.track_latency("gpt_draft_generation"):
                final_state = await get_email_graph().ainvoke(
                    {"ctx": ctx, "attempts": 0, "max_attempts": MAX_ATTEMPTS}
                )

            draft = final_state.get("draft")
            strategy = final_state.get("strategy") or {}
            if not draft or not draft.get("body"):
                logger.warning(f"[DRAFT] Sub-graph produced no draft for {target_company_name}.")
                return None

            critique = final_state.get("critique") or {}
            logger.info(
                f"[DRAFT] {target_company_name}/{prospect_name}: "
                f"attempts={final_state.get('attempts')} verdict={critique.get('verdict')} "
                f"score={critique.get('score')}"
            )

            cleaned_body = clean_email_body(draft["body"])
            final_drafts = {"primary": {"subject": draft["subject"], "body": cleaned_body}}

            # Compatibility object expected by the ghostwriter worker.
            class CompatibilityDraftSet:
                def __init__(self, variants, strategic, pain, hook):
                    self.variants = variants
                    self.strategic_observation = strategic
                    self.pain_hypothesis = pain
                    self.personalization_hook = hook

            return CompatibilityDraftSet(
                final_drafts,
                strategy.get("strategic_observation", ""),
                strategy.get("pain_hypothesis", ""),
                strategy.get("hook", ""),
            )
        except Exception as e:
            logger.error(f"Drafting error for {target_company_name}: {e}")
            return None
        finally:
            agent_label_var.reset(_ag_tok)
            company_domain_var.reset(_dom_tok)
