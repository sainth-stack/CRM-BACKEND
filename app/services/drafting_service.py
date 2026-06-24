import logging
import datetime
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
            objective = campaign.prompt or "Win a discovery call with the right stakeholder."

            def _join(v):
                if isinstance(v, (list, tuple)):
                    return "; ".join(str(x) for x in v if str(x).strip()) or "N/A"
                return str(v) if v else "N/A"

            def _fmt_list(items) -> str:
                """Format a list as a bullet list for the strategist prompt.
                Preserves each signal on its own line so the LLM can evaluate them individually."""
                if not items:
                    return "None available"
                return "\n".join(f"- {item}" for item in items if str(item).strip())

            meddpicc = (target_co.v2_intel or {}).get("meddpicc", {}) if target_co.v2_intel else {}
            need_evidence   = _join(meddpicc.get("need_evidence"))
            matched_pains   = _join(target_co.matched_pains or meddpicc.get("matched_pains"))
            matched_services= _join(target_co.matched_services or meddpicc.get("matched_services"))
            recipient_role_signal = dm.relevance_explanation or "Role/influence not yet assessed."
            sender_proof    = _join(user_intel.proof_points)
            sender_advantages = _join(user_intel.competitive_advantages)

            # Pass signals as formatted bullet lists so the strategist can evaluate
            # each item independently (freshness filter, hook priority, etc.)
            news_hooks_fmt   = _fmt_list(target_co.news_hooks)
            growth_hooks_fmt = _fmt_list(target_co.growth_hooks)
            pain_hooks_fmt   = _fmt_list(target_co.pain_hooks)

            # Runtime context — current month used by the strategist for freshness calculation
            current_month = datetime.datetime.now().strftime("%Y-%m")

        finally:
            temp_db.close()

        # 2. Run the Strategist -> Writer -> Validate sub-graph.
        from app.agents.email_graph import get_email_graph, MAX_ATTEMPTS
        from app.agents.email_drafter import clean_email_body
        from app.services.observability_service import ObservabilityService

        first_name = prospect_name.split(' ')[0] if prospect_name and ' ' in prospect_name else (prospect_name or "there")

        ctx = {
            # Sender
            "sender_name":           sender_name,
            "sender_services":       sender_services,
            "sender_proof":          sender_proof,
            "sender_advantages":     sender_advantages,
            # Prospect
            "user_name":             user_name,
            "prospect_name":         prospect_name,
            "prospect_first_name":   first_name,
            "prospect_role":         prospect_role,
            "prospect_seniority":    prospect_seniority,
            "recipient_role_signal": recipient_role_signal,
            # Company
            "target_company":        target_company_name,
            "research_summary":      research_summary,
            "description":           target_co.description or "N/A",
            "company_type":          target_co.company_type or "N/A",
            "employee_count":        target_co.employee_count or "N/A",
            "location":              target_co.location or "N/A",
            # Signals — formatted bullet lists (strategist evaluates each individually)
            "news_hooks":            news_hooks_fmt,
            "growth_hooks":          growth_hooks_fmt,
            "pain_hooks":            pain_hooks_fmt,
            # MEDDPICC
            "need_evidence":         need_evidence,
            "matched_pains":         matched_pains,
            "matched_services":      matched_services,
            # Runtime
            "current_month":         current_month,
            "objective":             objective,
        }

        from app.core.logging_config import agent_label_var, company_domain_var
        from app.agents.sequence_graph import get_sequence_graph, SEQ_MAX_ATTEMPTS
        _dom = target_co.domain if target_co and hasattr(target_co, "domain") else None
        _dom_tok = company_domain_var.set(_dom or target_company_name)
        _ag_tok  = agent_label_var.set("email_sequence")
        try:
            with ObservabilityService.track_latency("gpt_sequence_generation"):
                final_state = await get_sequence_graph().ainvoke({
                    "ctx":          ctx,
                    "attempts":     0,
                    "max_attempts": SEQ_MAX_ATTEMPTS,
                })

            plan     = final_state.get("plan") or {}
            email1   = final_state.get("email1") or {}
            critiques = final_state.get("critiques") or {}

            if not email1.get("body"):
                logger.warning(f"[SEQUENCE] Sub-graph produced no Email 1 for {target_company_name}.")
                return None

            verdicts = {k: v.get("verdict") for k, v in critiques.items()}
            logger.info(
                f"[SEQUENCE] {target_company_name}/{prospect_name}: "
                f"attempts={final_state.get('write_attempts')} verdicts={verdicts} "
                f"hook_type={plan.get('e1_hook_type')} persona={plan.get('e1_persona_focus')}"
            )

            # Only the initial email is drafted upfront.
            # Follow-ups are drafted on-demand after intent classification.
            sequence = [
                {
                    "subject":        email1["subject"],
                    "body":           clean_email_body(email1["body"]),
                    "draft_type":     "INITIAL",
                    "followup_index": 0,
                },
            ]

            class SequenceDraftSet:
                def __init__(self, seq, strategic, pain, hook):
                    self.sequence  = seq
                    # Primary variant = Email 1 (backward compat with ghostwriter_worker)
                    self.variants  = {"primary": {"subject": seq[0]["subject"], "body": seq[0]["body"]}}
                    self.strategic_observation = strategic
                    self.pain_hypothesis       = pain
                    self.personalization_hook  = hook

            return SequenceDraftSet(
                sequence,
                plan.get("e1_primary_pain", ""),
                plan.get("e1_primary_pain", ""),
                plan.get("e1_hook_value", ""),
            )
        except Exception as e:
            logger.error(f"Sequence drafting error for {target_company_name}: {e}", exc_info=True)
            return None
        finally:
            agent_label_var.reset(_ag_tok)
            company_domain_var.reset(_dom_tok)
