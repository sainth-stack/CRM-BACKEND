"""ICP qualification + deep-research — MEDDPICC engine with enrichment_v2 support.

Evidence comes from enrichment_v2 (6-source multi-layer pipeline) or from a site
crawl + enrichment LLM call (eval/legacy path, via precomputed_context).

Architecture:
  Pre-filter  — LeadFitScreener (upstream, CSV-only; not called here).
  Call 1      — enrichment_v2.enrich_company()  → CompanyEnrichment (v2 schema)
                OR legacy: site crawl → _enrich() → CompanyResearchProfile (v1).
  Call 2      — Reasoning model validates the profile against sender + campaign:
                ICPMeddpiccJudgment (MEDDPICC role gate + operational_overlap LLM,
                scale_readiness + signal_breadth computed deterministically).
  Decision    — _decide(): binary ACCEPTED / REJECTED vs ICP_ACCEPT_THRESHOLD.

Globally unbiased:
  - No hardcoded industry, geography, or sector rubric.
  - All scoring is relative to the sender's actual profile.
  - operational_overlap is the only LLM-graded number; scale + breadth are
    computed from firmographics/lists to give consistent per-company spread.
"""
from __future__ import annotations

import json
import re
import logging
from typing import List, Literal

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.llm import get_chat_llm

logger = logging.getLogger("CompanyValidationService")


# ===========================================================================
# Pydantic schemas
# ===========================================================================

class CompanyResearchProfile(BaseModel):
    """Sender-agnostic research profile — produced by the eval/legacy enrichment path
    (raw site crawl → LLM). Only `executive_summary` distinguishes v1 from v2."""
    actual_business: str = Field(description="One sentence: the company's primary business.")
    products_services: List[str] = Field(default_factory=list)
    markets_served: List[str] = Field(default_factory=list)
    scale_operations: str = Field(description="Operational scale from the evidence.")
    evidenced_signals: List[str] = Field(default_factory=list)
    growth_hooks: List[str] = Field(default_factory=list)
    pain_hooks: List[str] = Field(default_factory=list)
    news_hooks: List[str] = Field(default_factory=list)
    executive_summary: str = Field(description="3-4 paragraphs grounded in evidence.")


class ICPMeddpiccJudgment(BaseModel):
    """Reasoning-model output — MEDDPICC ICP qualification against THIS sender.

    operator_fit is SYSTEM-COMPUTED from the LLM's operational_overlap plus two
    deterministic sub-scores (scale_readiness, signal_breadth). The LLM MUST output
    0 for scale_readiness, signal_breadth, and operator_fit — the system fills them.
    """

    # ── Role gate ────────────────────────────────────────────────────────────
    target_role: Literal["operator_end_user", "solution_vendor_overlap", "out_of_domain"] = Field(
        description=(
            "Classify the target against the BUYER PROFILE and COMPETITOR PROFILE derived from "
            "the SENDER PROFILE. "
            "'operator_end_user' = the target RUNS the kind of operations the sender's offering "
            "serves — it is a genuine buyer candidate. Illustrative examples of 'runs operations': "
            "manufactures physical goods of ANY kind, operates production lines or equipment fleets, "
            "runs clinics/hospitals, operates retail stores or distribution, runs financial back-office "
            "processes. A company that MAKES physical products of any category is an operator even if "
            "its product looks unrelated to the sender — what matters is whether its OPERATIONS match "
            "the derived BUYER PROFILE. When evidence is genuinely ambiguous, lean operator_end_user. "
            "'solution_vendor_overlap' = the target's OWN core product matches the derived COMPETITOR "
            "PROFILE — it sells a directly-competing solution in the SAME category the sender sells. "
            "Companies that merely make physical equipment in an adjacent vertical are NOT competitors. "
            "'out_of_domain' = the target matches NEITHER profile — pure service firms, consultancies, "
            "pure SaaS/software, pure financial services, pure resellers/distributors that buy-and-resell "
            "without making anything, or companies whose entire value chain is non-physical. Choose "
            "out_of_domain ONLY when there is NO production signal at all. When in doubt between "
            "operator and out_of_domain, choose operator."
        )
    )
    role_reason: str = Field(description="One sentence: what the target does and why that role.")

    # ── Operational fit — ONLY operational_overlap is LLM-scored ────────────
    operational_overlap: int = Field(
        description=(
            "0-45 (operators only; 0 for other roles): how DIRECTLY the target's core operations "
            "match the sender's buyer profile and its derived operational signals, inferred from "
            "what the company does/makes/serves. "
            "38-45 = textbook match to the central signal; "
            "25-37 = clear match; 12-24 = partial/adjacent overlap; 1-11 = loose. "
            "DIFFERENTIATE — avoid round-number defaults; two companies should rarely tie. "
            "Never lower this score because pain is not explicitly stated in the profile."
        )
    )
    scale_readiness: int = Field(
        default=0,
        description="SYSTEM-COMPUTED from firmographics — output 0; the system fills this."
    )
    signal_breadth: int = Field(
        default=0,
        description="SYSTEM-COMPUTED from matched pains/services — output 0; the system fills this."
    )
    operator_fit: int = Field(
        default=0,
        description="SYSTEM-COMPUTED (operational_overlap + scale_readiness + signal_breadth) — output 0."
    )

    # ── Corroborating evidence (bonus signals — absence is NEUTRAL, never a penalty) ─
    has_evidenced_need: bool = Field(
        description=(
            "True ONLY if the research profile explicitly shows a problem/need/initiative the "
            "SENDER'S offering addresses (derive from sender's capability->pain map). "
            "A POSITIVE BONUS only — when false the score is NOT penalised (marketing websites "
            "describe capabilities, not problems). Do NOT force it true."
        )
    )
    need_evidence: str = Field(
        description="The exact evidenced need (quote/paraphrase). 'None evidenced' when false."
    )
    has_evidenced_precondition: bool = Field(
        description=(
            "True ONLY if the profile shows the PRECONDITION making the sender's solution usable "
            "(the operations, assets, data, or scale the offering requires). POSITIVE BONUS only — "
            "absence is neutral, never a penalty."
        )
    )
    precondition_evidence: str = Field(
        description="The exact evidenced precondition (quote/paraphrase). 'None evidenced' when false."
    )
    evidence_confidence: int = Field(description="0-100: share of assessment from EXPLICIT profile facts.")

    # ── MEDDPICC informational fields ────────────────────────────────────────
    metrics: str = Field(
        description="Value hypothesis tied to the evidenced need, or 'unclear — no evidenced need'."
    )
    economic_buyer: str = Field(
        description=(
            "Budget-owner ROLE/TITLE appropriate to the buyer profile and campaign objective. "
            "Derive from the sender's offering and the target's operations. "
            "NEVER a named individual unless that exact name is in the evidence."
        )
    )
    champion: str = Field(description="Likely internal CHAMPION role (not a person unless named).")
    decision_criteria: str = Field(description="Likely evaluation criteria.")
    decision_process: str = Field(description="Start with 'NEEDS DISCOVERY:' then a concrete note.")
    paper_process: str = Field(description="Start with 'NEEDS DISCOVERY:' then a concrete note.")
    discovery_checklist: List[str] = Field(
        default_factory=list,
        description="3-5 specific things to confirm on the first call."
    )

    # ── Sender-relative enrichment (for cold email drafting) ─────────────────
    business_opportunity_reason: str = Field(
        description=(
            "The 'why now', anchored to ONE specific fact from the profile. "
            "State plainly if no concrete trigger was found."
        )
    )
    matched_pains: List[str] = Field(
        default_factory=list,
        description="Specific target pains the sender solves (grounded in the profile)."
    )
    matched_services: List[str] = Field(
        default_factory=list,
        description="Sender services that map to those pains."
    )
    overall_reasoning: str = Field(
        description=(
            "A professional 2-3 sentence FACTUAL assessment for a sales team. "
            "Ground it in what the company actually does/operates and why that matches (or does "
            "not match) the sender's target-customer profile. "
            "FORBIDDEN: any sentence referencing missing or unstated information. Never write "
            "phrases like 'absence of challenges', 'no specific needs were stated', 'limits "
            "confidence', 'despite the absence', 'not explicitly stated', 'further exploration "
            "needed', or 'may not be a fit'. Marketing websites describe capabilities, not "
            "problems — treating absence of stated problems as a caveat is WRONG. "
            "Write only affirmative, evidence-grounded sentences. "
            "Do NOT state or imply accept/reject. Use the exact sender name."
        )
    )
    confidence: int = Field(description="0-100 confidence given the quality/quantity of evidence.")


# ===========================================================================
# Prompts
# ===========================================================================

# Call 1 (eval/legacy path only): raw web text → sender-agnostic profile (v1).
_ENRICH_PROMPT = ChatPromptTemplate.from_template(
    """You are a meticulous B2B research analyst. Read the RAW website content for a company and distil it
into a STRUCTURED, FACTUAL research profile. Use ONLY what is present in the evidence — never invent,
assume, or generalise. This profile is SENDER-AGNOSTIC: do NOT judge fit to any seller; just capture what
the company is and does, as richly and accurately as the evidence allows.

COMPANY:
- Name: {name}
- Provided description: {csv_desc}

RAW WEBSITE EVIDENCE (the company's own site):
\"\"\"{raw_evidence}\"\"\"

Ground EVERY field strictly in the evidence above. BANNED hedge words: may, might, could, likely,
potential, possibly. Lists must contain specific, verifiable items drawn from the evidence (or [] if
none). If the evidence is thin, capture only what is grounded and say so in the summary."""
)


# Call 2 (per company): MEDDPICC validation against sender profile + campaign.
#
# Three-part message structure for prompt caching:
#   SystemMessage  — static rules that never change (cached across ALL campaigns)
#   HumanMessage 1 — campaign-level inputs (same for every company in this run,
#                    cached after the first company in the batch)
#   HumanMessage 2 — per-company target record (changes every call)
#
# This replaces the old ChatPromptTemplate.from_template approach which put
# everything in one user message, preventing any cache hits.

_VALIDATE_SYSTEM = """\
You are a STRICT B2B ICP analyst qualifying an OUTBOUND lead before any contact. Your #1 job is to
PREVENT false positives and stay fully grounded. Quote facts from the RESEARCH PROFILE; never invent
needs, names, or firmographics. Everything sender-related must be derived from THIS sender — no
fixed-industry assumptions.

NOTE: Firmographic pre-filtering (industry / location / employee-size) was handled upstream by
LeadFitScreener. Only companies that cleared that screen reach you. Do NOT re-evaluate firmographics —
focus entirely on MEDDPICC qualification and sender-relative enrichment.

==================== PART A — DERIVE SENDER CONTEXT ====================
From the SENDER PROFILE only, define:
  (i)   BUYER PROFILE     — a genuine end-user customer of this sender;
  (ii)  COMPETITOR PROFILE — a company selling a directly-overlapping solution;
  (iii) THE NEED          — the specific problem(s) the sender's offering solves;
  (iv)  THE PRECONDITION  — what a company must have/do for the sender's solution to be USABLE.

Then UNPACK the BUYER PROFILE into 3-5 concrete OPERATIONAL SIGNALS derived ENTIRELY from THIS sender's
offerings and capability→pain map (not from any fixed industry list). Each signal is one specific
operational characteristic a company would have to make the sender's offering applicable.

A target qualifies as operator_end_user when the profile shows it matches AT LEAST ONE derived signal —
even if its headline product category looks unrelated to the sender at first glance.

==================== PART B — MEDDPICC QUALIFICATION ====================
HOW TO READ THE EVIDENCE: the RESEARCH PROFILE is distilled from the target's own MARKETING website
(and supplementary sources), which describes what the company DOES, MAKES, SELLS, and SERVES — its
capabilities — and almost NEVER states internal problems or gaps. Judge fit by INFERENCE from what the
company does: if its operations match the derived BUYER PROFILE, it is a fit, REGARDLESS of whether any
pain is spelled out. The ABSENCE of stated need/problem is NOT evidence against fit and must NOT lower
the fit grade.

PRODUCTION OPERATIONS RULE — APPLY THIS BEFORE CLASSIFYING ROLE:
STEP 1 — Look for a production signal in TWO places:
  A) In the text: "manufacturer", "manufactures", "produces", "production", "fabrication",
     "assembly", "machining", "finishing", "foundry", "mill", "plant", "factory", "welding",
     "casting", "extrusion", "designs and builds", "designs and manufactures".
  B) In the PRODUCTS LIST: if the company's listed products are TANGIBLE PHYSICAL ITEMS —
     devices, components, instruments, hardware, sensors, systems that are shipped, installed,
     and maintained (as opposed to software, services, or consulting) — then the company MAKES
     those physical products and therefore RUNS production operations.
     HOW TO APPLY STEP 1B: ask "Can these products be held in one's hand, shipped in a box,
     installed on a wall or machine, and physically fail or wear out?" If YES → physical product
     → production signal present. If the products are clearly intangible (software, platforms,
     APIs, consulting deliverables) → no production signal from this step.
STEP 2 — If ANY production signal (from STEP 1A or 1B) is present, the company RUNS its own
operations and is ALWAYS operator_end_user — NEVER out_of_domain — regardless of end market.
The sector the hardware serves (security, medical, aerospace, agriculture, etc.) does NOT change
this classification. A company making physical security sensors is the same kind of operator as
a company making industrial components.
STEP 3 — Choose out_of_domain ONLY when there is NO production signal from EITHER step: pure
service firms, consultancies, SaaS/software, financial services, or pure resellers that resell
without making anything of their own.

Score target_role, role_reason, and operational_overlap using the company's RESEARCH PROFILE judged in
light of the campaign objective. Fill scale_readiness, signal_breadth, operator_fit with 0 — system
computes them.

has_evidenced_need + need_evidence: TRUE only if THE NEED (iii) happens to be EXPLICIT in the profile.
Setting it false does NOT reduce the score. Do NOT force it true.
has_evidenced_precondition + precondition_evidence: TRUE only if THE PRECONDITION (iv) is EXPLICIT.
Same rule — absence is neutral, never a penalty.

evidence_confidence, metrics, economic_buyer, champion, decision_criteria, decision_process,
paper_process, discovery_checklist: fill these from the profile + sender context.

==================== PART C — SENDER-RELATIVE ENRICHMENT ====================
business_opportunity_reason: the 'why now' anchored to ONE specific fact in the profile. State plainly
if no concrete trigger was found.
matched_pains / matched_services: the sender pains/services that apply to what the profile shows.
overall_reasoning: professional 2-3 sentence FACTUAL assessment using the exact sender name from the
campaign inputs. Ground in what the company DOES. FORBIDDEN: any sentence about missing/unstated
information. No scores, field names, booleans, or verdict labels. No accept/reject recommendation.
confidence: 0-100.\
"""

_VALIDATE_CAMPAIGN = """\
==================== CAMPAIGN-LEVEL INPUTS (same for every company in this run) ====================

SENDER PROFILE — who is doing the outreach:
- Company: {sender_name}
- Offerings: {sender_offerings}
- Sells to (their ICP): {sender_customers}
- Differentiators: {sender_advantages}
- Capability → customer-pain map: {sender_capmap}
- Proof points: {sender_proof}
- Business summary: {sender_research}

CAMPAIGN OBJECTIVE — the user's own words: {campaign_prompt}

APPROVAL THRESHOLD: {threshold}/100. A company is ACCEPTED when its fit score reaches this threshold.
Calibrate operational_overlap HONESTLY against this bar — never inflate to clear it, never deflate a
genuine fit.\
"""

_VALIDATE_TARGET = """\
==================== TARGET RECORD (the ONLY per-company input) ====================
TARGET COMPANY:
- Name: {name}
- RESEARCH PROFILE (THE evidence — quote from it):
\"\"\"{profile}\"\"\"\
"""


# ===========================================================================
# Service
# ===========================================================================

class CompanyValidationService:
    """ICP qualification via MEDDPICC + enrichment_v2.

    Call 1: enrichment_v2.enrich_company() → v2 profile (or legacy site crawl → v1).
    Call 2: reasoning model → ICPMeddpiccJudgment.
    Decision: deterministic _decide() → ACCEPTED / REJECTED.

    Globally unbiased — all scoring is relative to the sender's actual profile.
    No hardcoded industry, geography, or sector rules.
    """

    def __init__(self):
        self.enrichment_llm = get_chat_llm("enrichment", timeout=120)
        self.reasoning_llm  = get_chat_llm("reasoning",  timeout=60)

    # ------------------------------------------------------------------ #
    # Utilities                                                           #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _as_text(value, empty="Any") -> str:
        if value is None:
            return empty
        if isinstance(value, (list, tuple)):
            items = [str(v).strip() for v in value if str(v).strip()]
            return ", ".join(items) if items else empty
        text = str(value).strip()
        if not text:
            return empty
        parts = [p.strip() for p in re.split(r"[,;\n]+", text) if p.strip()]
        return ", ".join(parts) if parts else empty

    _ENRICH_INPUT_CHARS = 12000

    # ------------------------------------------------------------------ #
    # Profile cache                                                       #
    # ------------------------------------------------------------------ #
    def prefetch_icp_research(self, domains: list[str]) -> dict[str, dict]:
        """Batch-load cached research profiles (removes per-company N+1 DB queries).

        Accepts both v2 profiles (_schema="v2" + overview) and legacy v1 profiles
        (executive_summary). Returns {domain: profile_dict}."""
        import datetime as _dt
        from app.db.database import SessionLocal
        from app.db import models

        domains = [d for d in {d for d in domains if d}]
        if not domains:
            return {}
        cutoff = _dt.datetime.utcnow() - _dt.timedelta(days=settings.ICP_PROFILE_FRESHNESS_DAYS)
        out: dict[str, dict] = {}
        db = SessionLocal()
        try:
            rows = (
                db.query(models.TargetCompany.domain, models.TargetCompany.icp_research_context)
                .filter(
                    models.TargetCompany.domain.in_(domains),
                    models.TargetCompany.icp_research_context != None,  # noqa: E711
                    models.TargetCompany.updated_at >= cutoff,
                )
                .order_by(models.TargetCompany.updated_at.desc())
                .all()
            )
            for d, ctx in rows:
                if d in out or not ctx:
                    continue
                try:
                    obj = json.loads(ctx)
                    if not isinstance(obj, dict):
                        continue
                    # Accept v2 schema
                    if obj.get("_schema") == "v2" and obj.get("overview"):
                        out[d] = obj
                    # Accept legacy v1 schema
                    elif obj.get("executive_summary"):
                        out[d] = obj
                except (ValueError, TypeError):
                    continue
        finally:
            db.close()
        return out

    # ------------------------------------------------------------------ #
    # Profile schema detection + rendering                                #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _profile_is_v2(profile: dict) -> bool:
        return isinstance(profile, dict) and profile.get("_schema") == "v2"

    @staticmethod
    def _synthesize_summary_v2(profile: dict) -> str:
        """Build a research_summary string from v2 fields (v2 has no executive_summary)."""
        parts: list[str] = []
        overview = (profile.get("overview") or "").strip()
        if overview and overview.lower() != "unknown":
            parts.append(overview)
        ps = profile.get("products_services") or []
        if ps:
            parts.append("Products/services: " + "; ".join(str(v) for v in ps[:5]))
        tc = (profile.get("target_customers") or "").strip()
        if tc and tc.lower() not in ("unknown", "not stated", ""):
            parts.append(f"Customers: {tc}")
        gs = profile.get("growth_signals") or []
        if gs:
            parts.append("Growth: " + "; ".join(str(v) for v in gs[:3]))
        re_ = profile.get("recent_events") or []
        if re_:
            parts.append("Recent: " + "; ".join(str(v) for v in re_[:3]))
        ki = profile.get("key_initiatives") or []
        if ki:
            parts.append("Initiatives: " + "; ".join(str(v) for v in ki[:2]))
        return "\n".join(parts) if parts else "N/A"

    @staticmethod
    def _render_profile_v1(profile: dict, summary_chars: int = 600) -> str:
        """Render a v1 CompanyResearchProfile dict for the validation prompt."""
        def _lst(key):
            vals = profile.get(key) or []
            return "; ".join(str(v) for v in vals) if vals else "None stated"
        summary = (profile.get("executive_summary") or "").strip()
        if summary_chars and len(summary) > summary_chars:
            summary = summary[:summary_chars].rsplit(" ", 1)[0] + " ..."
        summary_block = f"\nSummary (excerpt):\n{summary}" if summary_chars and summary else ""
        return (
            f"Actual business: {profile.get('actual_business') or 'Unknown'}\n"
            f"Products/services: {_lst('products_services')}\n"
            f"Markets served: {_lst('markets_served')}\n"
            f"Scale & operations: {profile.get('scale_operations') or 'Not stated'}\n"
            f"Evidenced signals: {_lst('evidenced_signals')}\n"
            f"Stated challenges/pains: {_lst('pain_hooks')}\n"
            f"Recent items: {_lst('news_hooks')}\n"
            f"{summary_block}"
        )

    @staticmethod
    def _render_profile_v2(profile: dict, csv_desc: str = "") -> str:
        """Render a v2 CompanyEnrichment dict for the validation prompt.

        All 13 enrichment_v2 fields are surfaced so the reasoning model can ground
        every dimension of the MEDDPICC judgment in factual evidence.

        The CSV description is ALWAYS appended (not only when enrichment is thin)
        because it often contains production/manufacturing language that the company's
        marketing website omits or buries in vague "provider of solutions" phrasing.
        The model uses it as supplementary context alongside the enrichment data.
        """
        def _lst(key: str) -> str:
            vals = profile.get(key) or []
            if isinstance(vals, list):
                return "; ".join(str(v) for v in vals) if vals else "None stated"
            return str(vals) if vals else "None stated"

        overview = (profile.get("overview") or "").strip()

        lines = [
            f"Overview: {overview or 'Unknown'}",
            f"Products/services: {_lst('products_services')}",
            f"Target customers: {profile.get('target_customers') or 'Not stated'}",
            f"Notable clients/partners: {_lst('notable_customers_partners')}",
            f"Technologies in use: {_lst('technologies')}",
            f"Certifications: {_lst('certifications')}",
            f"Company history: {_lst('company_history')}",
            f"Key initiatives: {_lst('key_initiatives')}",
            f"Growth signals: {_lst('growth_signals')}",
            f"Recent events: {_lst('recent_events')}",
            f"Hiring signals: {_lst('hiring_signals')}",
            f"Awards/recognition: {_lst('awards_recognition')}",
            f"Pain points/challenges: {_lst('pain_points')}",
        ]

        # Always include CSV description — web enrichment often uses vague "provider/
        # solutions" language even for clear manufacturers; the CSV description from the
        # data provider tends to be more direct about what the company actually produces.
        if csv_desc and csv_desc.strip():
            lines.append(
                f"\nCSV DESCRIPTION (direct data-provider summary — use when the above "
                f"is vague or contradicts this):\n{csv_desc.strip()[:600]}"
            )

        return "\n".join(lines)

    def _render_profile(self, profile: dict, csv_desc: str = "") -> str:
        """Dispatch to v1 or v2 renderer."""
        if self._profile_is_v2(profile):
            return self._render_profile_v2(profile, csv_desc=csv_desc)
        return self._render_profile_v1(profile)

    # ------------------------------------------------------------------ #
    # Deterministic sub-scores                                            #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _revenue_score(revenue: str) -> int:
        """0-12 from a revenue-range string (uses UPPER bound). Blank → neutral 3."""
        t = (revenue or "").lower().replace(",", "").replace("$", "")
        if not t.strip():
            return 3
        vals = []
        for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([bmk])?", t):
            if not num:
                continue
            mult = {"b": 1_000_000_000, "m": 1_000_000, "k": 1_000}.get(unit, 1)
            vals.append(float(num) * mult)
        if not vals:
            return 3
        v = max(vals)
        for thr, sc in ((1e9, 12), (5e8, 11), (1e8, 9), (5e7, 7), (2e7, 5), (5e6, 4), (1e6, 2)):
            if v >= thr:
                return sc
        return 1

    @classmethod
    def _headcount_score(cls, size: str) -> int:
        """0-8 from an employee-size string (uses UPPER bound). Blank → neutral 2."""
        ranges = cls._parse_ranges(size or "")
        if not ranges:
            return 2
        hi = max(h if h != float("inf") else l for l, h in ranges)
        for thr, sc in ((5000, 8), (1000, 7), (500, 6), (200, 5), (50, 4), (10, 3)):
            if hi >= thr:
                return sc
        return 2

    @staticmethod
    def _age_score(founded_year: str) -> int:
        """0-3 company-maturity score from a founded year/date. Blank → neutral 1."""
        if not founded_year:
            return 1
        m = re.search(r"(19|20)\d{2}", str(founded_year))
        if not m:
            return 1
        import datetime as _dt
        age = _dt.datetime.utcnow().year - int(m.group(0))
        for thr, sc in ((50, 3), (20, 2), (3, 1)):
            if age >= thr:
                return sc
        return 1

    @staticmethod
    def _parse_ranges(text: str):
        """Parse employee-count ranges from free text → [(low, high)]; high=inf for '5000+'."""
        if not text:
            return []
        t = text.lower().replace(",", "")
        ranges = []
        for mt in re.finditer(r"(\d+)\s*\+", t):
            ranges.append((int(mt.group(1)), float("inf")))
        for mt in re.finditer(r"(\d+)\s*(?:-|to|–|—|−)\s*(\d+)", t):
            a, b = int(mt.group(1)), int(mt.group(2))
            ranges.append((min(a, b), max(a, b)))
        if not ranges:
            nums = [int(x) for x in re.findall(r"\d+", t)]
            if len(nums) == 1:
                ranges.append((nums[0], nums[0]))
            elif len(nums) >= 2:
                ranges.append((min(nums), max(nums)))
        return ranges

    @classmethod
    def _compute_scale_readiness(cls, company_data: dict, profile: dict) -> int:
        """0-30 from real firmographic + profile facts.

        revenue (0-12) + headcount (0-8) + market_breadth (0-4)
        + products (0-3) + age (0-3) = 30.

        For v2 profiles, target_customers (string) is parsed for market breadth;
        products_services list is used for product count.
        For v1 profiles, markets_served list is used directly.
        """
        score = cls._revenue_score(
            company_data.get("annual_revenue") or company_data.get("revenue")
        )
        score += cls._headcount_score(
            company_data.get("staff_count") or company_data.get("size")
        )

        if CompanyValidationService._profile_is_v2(profile):
            # Parse target_customers string for market breadth (split by comma/semicolon)
            tc_raw = (profile.get("target_customers") or "").strip()
            if tc_raw and tc_raw.lower() not in ("unknown", "not stated", "n/a", ""):
                tc_parts = [p.strip() for p in re.split(r"[,;]", tc_raw) if p.strip()]
                score += min(4, len(tc_parts)) if tc_parts else 1
        else:
            score += min(4, len(profile.get("markets_served") or []))

        score += min(3, len(profile.get("products_services") or []))
        score += cls._age_score(company_data.get("founded_year"))
        return max(0, min(30, score))

    @staticmethod
    def _compute_signal_breadth(m: ICPMeddpiccJudgment) -> int:
        """0-25 from how many sender pains/services the target matched.

        LLM lists vary per company (reliable extraction), so the derived count
        spreads naturally where a direct 0-25 grade would cluster.
        """
        n = len(m.matched_pains or []) + len(m.matched_services or [])
        return max(0, min(25, n * 4))

    # ── Bonus for corroborating evidence (positive signals only) ─────────────
    _NEED_BONUS        = 8
    _PRECONDITION_BONUS = 7

    @classmethod
    def _overall_score(cls, m: ICPMeddpiccJudgment) -> int | None:
        """Continuous 0-100 fit score for genuine operators, or None for gated roles.

        Base = operator_fit (computed by system: operational_overlap + scale + breadth).
        Bonus = +8 for evidenced need, +7 for evidenced precondition (positive only).
        Absence of evidenced need/precondition is NEUTRAL — never penalised.
        """
        if m.target_role != "operator_end_user":
            return None
        base = max(0, min(100, int(m.operator_fit or 0)))
        bonus = 0
        if m.has_evidenced_need:
            bonus += cls._NEED_BONUS
        if m.has_evidenced_precondition:
            bonus += cls._PRECONDITION_BONUS
        return min(100, base + bonus)

    # ------------------------------------------------------------------ #
    # Production-signal role override                                     #
    # ------------------------------------------------------------------ #
    _PRODUCTION_RE = re.compile(
        # Production action verbs and operational nouns
        r"manufactur|produces|production|fabricat|machining|\bassembly\b|\bassembles\b"
        r"|foundry|\bmill\b|\bplant\b|factory|injection mold|stamping|casting|extrusion"
        r"|fabrication|\bwelding\b|\bmoulding\b|designs and builds|engineers and manufactur"
        r"|builds and ships|designs and manufactur",
        re.IGNORECASE,
    )
    _SENDER_OPERATOR_RE = re.compile(
        r"manufactur|equipment|production|maintenance|operational|asset|downtime|industrial"
        r"|\bplant\b|machinery|operator|factory|oem|supply chain|logistics",
        re.IGNORECASE,
    )

    @classmethod
    def _profile_has_production(cls, profile: dict, csv_desc: str = "") -> bool:
        """True when the profile or CSV description contains a production signal.

        Checks all available text fields because enrichment_v2 often captures the
        marketing-site framing ("provider of solutions") rather than the operational
        framing ("manufactures X"). The CSV description from the data provider is
        typically more direct and is included as a fallback signal source.
        """
        if cls._profile_is_v2(profile):
            blob = (
                (profile.get("overview") or "") + " " +
                "; ".join(str(v) for v in (profile.get("products_services") or [])) + " " +
                "; ".join(str(v) for v in (profile.get("company_history") or [])) + " " +
                "; ".join(str(v) for v in (profile.get("key_initiatives") or []))
            )
        else:
            blob = (
                " ".join(str(profile.get(k) or "") for k in ("actual_business", "scale_operations"))
                + " "
                + "; ".join(str(v) for v in (profile.get("products_services") or []))
                + " "
                + "; ".join(str(v) for v in (profile.get("evidenced_signals") or []))
            )
        # Always include CSV description — it often has plainer manufacturing language
        if csv_desc:
            blob += " " + csv_desc
        return bool(cls._PRODUCTION_RE.search(blob))

    @classmethod
    def _sender_serves_operators(cls, sender_offerings: str, sender_customers: str, sender_capmap: str) -> bool:
        return bool(cls._SENDER_OPERATOR_RE.search(
            f"{sender_offerings} {sender_customers} {sender_capmap}"
        ))

    # ------------------------------------------------------------------ #
    # Hedge scrubber                                                      #
    # ------------------------------------------------------------------ #
    _HEDGE_RE = re.compile(
        r"absence of|not (?:explicitly )?stated"
        r"|no (?:specific|explicit) (?:challenge|need|pain|problem)"
        r"|limits? (?:the ability|confidence)|despite the absence|further exploration"
        r"|without explicit|no (?:stated|mentioned) (?:challenge|need|pain)",
        re.IGNORECASE,
    )

    @classmethod
    def _scrub_hedges(cls, text: str) -> str:
        if not text:
            return text
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        kept = [s for s in sentences if s.strip() and not cls._HEDGE_RE.search(s)]
        if not kept:
            return sentences[0].strip() if sentences else text
        return " ".join(kept).strip()

    # ------------------------------------------------------------------ #
    # Decision + result builder                                           #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _format_reasoning(m: ICPMeddpiccJudgment, verdict: str) -> str:
        base = (m.overall_reasoning or "").strip()
        if base and base[-1] not in ".!?":
            base += "."
        if verdict == "ACCEPT":
            note = "On balance the company qualifies for outreach as a genuine operational fit for the sender's offering."
            if m.has_evidenced_need and (m.need_evidence or "").strip().lower() not in ("", "none evidenced"):
                note += f" A relevant need is evidenced: {m.need_evidence.strip()}"
        else:
            if m.target_role == "solution_vendor_overlap":
                note = (
                    "It does not qualify: the company appears to sell a solution that competes with "
                    "the sender's, rather than being a prospective customer."
                )
            elif m.target_role == "out_of_domain":
                note = (
                    "It does not qualify: its operations fall outside the domain the sender's "
                    "offering serves."
                )
            else:
                note = (
                    "It does not qualify: its operations are too weak a match for the sender's "
                    "target-customer profile to meet the qualification bar."
                )
        return f"{base} {note}".strip()

    def _decide(self, m: ICPMeddpiccJudgment, profile: dict) -> dict:
        """Deterministic binary verdict + enrichment payload.

        ACCEPTED when score >= ICP_ACCEPT_THRESHOLD and role is operator_end_user.
        All other cases: REJECTED.
        Research dossier is attached to every verdict.
        """
        threshold = settings.ICP_ACCEPT_THRESHOLD
        ov = self._overall_score(m)
        score = ov if ov is not None else 0

        verdict = "ACCEPT" if score >= threshold else "REJECT"
        status  = "ACCEPTED" if verdict == "ACCEPT" else "REJECTED"
        reasoning = self._format_reasoning(m, verdict)

        # Map profile fields → canonical output keys (v2 and v1 both produce same keys)
        if self._profile_is_v2(profile):
            growth_hooks     = list(profile.get("growth_signals") or [])
            pain_hooks       = list(profile.get("pain_points") or [])
            news_hooks       = (
                list(profile.get("recent_events") or [])
                + list(profile.get("awards_recognition") or [])
                + list(profile.get("key_initiatives") or [])
            )
            research_summary = self._synthesize_summary_v2(profile)
        else:
            growth_hooks     = list(profile.get("growth_hooks") or [])
            pain_hooks       = list(profile.get("pain_hooks") or [])
            news_hooks       = list(profile.get("news_hooks") or [])
            research_summary = profile.get("executive_summary", "")

        hooks = {
            "relevance_score":             score,
            "reasoning":                   m.overall_reasoning,
            "business_opportunity_reason": m.business_opportunity_reason,
            "matched_pains":               m.matched_pains,
            "matched_services":            m.matched_services,
            "growth_hooks":                growth_hooks,
            "pain_hooks":                  pain_hooks,
            "news_hooks":                  news_hooks,
            "executive_summary":           research_summary,
            "research_profile":            profile,
            "meddpicc":                    m.model_dump(),
        }

        return {
            "status":                      status,
            "relevance_score":             score,
            "reasoning":                   reasoning[:900],
            "icp_context":                 json.dumps(profile),
            "business_opportunity_reason": m.business_opportunity_reason,
            "matched_pains":               m.matched_pains,
            "matched_services":            m.matched_services,
            "growth_hooks":                growth_hooks,
            "pain_hooks":                  pain_hooks,
            "news_hooks":                  news_hooks,
            "research_summary":            research_summary,
            "hooks":                       hooks,
        }

    # ------------------------------------------------------------------ #
    # Call 1 — legacy/eval path: site crawl → v1 profile                 #
    # ------------------------------------------------------------------ #
    async def _gather_raw_evidence(self, domain: str) -> str:
        from app.integrations.site_extractor import extract_site
        if not domain:
            return ""
        try:
            ext = await extract_site(domain, max_pages=5, per_page_chars=7000)
            if ext.ok and ext.combined_text.strip():
                return ext.combined_text
            logger.warning(f"[ICP-RESEARCH] No site content for {domain}: {ext.error}")
        except Exception as e:
            logger.warning(f"[ICP-RESEARCH] Extraction failed for {domain}: {e}")
        return ""

    async def _enrich(self, name: str, csv_desc: str, raw_evidence: str) -> dict:
        """Call 1 (eval/legacy path) — distil raw web text into a v1 profile dict."""
        from app.core.logging_config import agent_label_var
        blob = (
            f"CSV DESCRIPTION: {csv_desc or 'N/A'}\n\n"
            f"COMPANY WEBSITE TEXT:\n{raw_evidence or 'No website content retrieved.'}"
        )
        chain = _ENRICH_PROMPT | self.enrichment_llm.with_structured_output(CompanyResearchProfile)
        _ag_tok = agent_label_var.set("icp_enrich")
        try:
            profile: CompanyResearchProfile = await chain.ainvoke({
                "name":         name or "the company",
                "csv_desc":     (csv_desc or "")[:1500],
                "raw_evidence": blob[: self._ENRICH_INPUT_CHARS],
            })
        finally:
            agent_label_var.reset(_ag_tok)
        return profile.model_dump()

    # ------------------------------------------------------------------ #
    # Call 2 — MEDDPICC validation (reasoning model)                     #
    # ------------------------------------------------------------------ #
    async def _validate(
        self,
        name: str,
        rendered_profile: str,
        sender_name: str,
        sender_offerings: str,
        sender_customers: str,
        sender_advantages: str,
        sender_capmap: str,
        sender_proof: str,
        sender_research: str,
        campaign_prompt: str,
    ) -> ICPMeddpiccJudgment:
        from langchain_core.messages import SystemMessage, HumanMessage
        from app.core.logging_config import agent_label_var

        chain = self.reasoning_llm.with_structured_output(ICPMeddpiccJudgment)

        # SystemMessage — static rules, cached across all campaigns.
        system_msg = SystemMessage(content=_VALIDATE_SYSTEM)

        # HumanMessage 1 — campaign-level inputs, cached within a campaign run.
        campaign_msg = HumanMessage(content=_VALIDATE_CAMPAIGN.format(
            sender_name=sender_name,
            sender_offerings=sender_offerings,
            sender_customers=sender_customers,
            sender_advantages=sender_advantages,
            sender_capmap=sender_capmap[:2_000],
            sender_proof=sender_proof,
            sender_research=(sender_research or "N/A")[:3_000],
            threshold=settings.ICP_ACCEPT_THRESHOLD,
            campaign_prompt=campaign_prompt[:1_500],
        ))

        # HumanMessage 2 — per-company target record, changes every call.
        # Profile cap kept at 8K: ICP-critical fields (growth_signals, hiring_signals,
        # pain_points) render last in _render_profile_v2 and would be silently truncated
        # at 4K for data-rich companies, directly hurting scoring accuracy.
        target_msg = HumanMessage(content=_VALIDATE_TARGET.format(
            name=name,
            profile=rendered_profile[:8_000],
        ))

        _ag_tok = agent_label_var.set("icp_validate")
        try:
            result: ICPMeddpiccJudgment = await chain.ainvoke(
                [system_msg, campaign_msg, target_msg]
            )
        finally:
            agent_label_var.reset(_ag_tok)
        return result

    # ------------------------------------------------------------------ #
    # Main entry point                                                    #
    # ------------------------------------------------------------------ #
    async def qualify_and_enrich(
        self,
        company_data: dict,
        user_intel: dict,
        campaign_metadata: dict = None,
        research_cache: dict | None = None,
        return_context: bool = False,
        precomputed_context=None,
        client=None,
        search_hints: list[str] | None = None,
    ) -> dict:
        """Single-pass ICP qualification + enrichment for one target company.

        Returns a dict with: status, relevance_score, reasoning, icp_context,
        and enrichment hooks for the email drafting stage.
        Dossier is attached to every verdict — both accepted and rejected rows
        are fully displayable in the UI.
        """
        from app.core.logging_config import company_domain_var

        campaign_metadata = campaign_metadata or {}
        domain = company_data.get("domain") or company_data.get("website")
        name   = company_data.get("name")
        _dom_tok = company_domain_var.set(domain or "unknown")

        csv_desc        = company_data.get("description") or ""
        campaign_prompt = self._as_text(
            campaign_metadata.get("prompt"), empty="(no specific objective provided)"
        )

        sender_name      = user_intel.get("company_name") or user_intel.get("name") or "the sender"
        sender_offerings = self._as_text(
            user_intel.get("services") or user_intel.get("offerings") or user_intel.get("core_offerings")
        )
        sender_customers  = self._as_text(user_intel.get("target_customers"))
        sender_advantages = self._as_text(user_intel.get("competitive_advantages"))
        sender_capmap     = str(user_intel.get("capability_to_pain_map") or [])[:2000]
        sender_proof      = str(user_intel.get("proof_points") or [])[:1000]
        sender_research   = self._as_text(user_intel.get("deep_research"), empty="N/A")

        # ── Enrichment ────────────────────────────────────────────────────────
        profile = research_cache.get(domain) if research_cache else None
        if profile:
            logger.info(f"[ICP-CACHE] Reusing profile for {domain}.")
        else:
            if precomputed_context is not None:
                # Eval/iteration hook: caller supplies raw site text → v1 enrich.
                raw = (
                    precomputed_context[0]
                    if isinstance(precomputed_context, (tuple, list))
                    else precomputed_context
                )
                try:
                    profile = await self._enrich(name, csv_desc, raw)
                except Exception as e:
                    logger.error(f"[ICP] Enrichment (Call 1) failed for {domain}: {e}")
                    profile = None
                raw = None
            else:
                # Production path: enrichment_v2 multi-layer pipeline.
                try:
                    from app.integrations.enrichment_v2 import enrich_company as _enrich_v2
                    profile = await _enrich_v2(
                        name, domain, client=client, search_hints=search_hints or []
                    )
                    if profile:
                        logger.info(
                            f"[ICP] V2 enrichment OK for {domain} "
                            f"(status={profile.get('_status')})"
                        )
                    else:
                        logger.warning(f"[ICP] V2 enrichment returned no profile for {domain}.")
                except Exception as e:
                    logger.warning(f"[ICP] V2 enrichment error for {domain}: {e}")
                    profile = None

        if not profile:
            company_domain_var.reset(_dom_tok)
            err = {
                "status": "REJECTED",
                "relevance_score": 0,
                "operational_fit": 0,
                "readiness": 0,
                "reasoning": "No usable research could be gathered for this company.",
                "icp_context": None,
                "_llm_error": True,
            }
            if return_context:
                err["_eval_context"] = ""
            return err

        rendered = self._render_profile(profile, csv_desc=csv_desc)

        # ── MEDDPICC validation (Call 2) ─────────────────────────────────────
        try:
            m: ICPMeddpiccJudgment = await self._validate(
                name=name,
                rendered_profile=rendered,
                sender_name=sender_name,
                sender_offerings=sender_offerings,
                sender_customers=sender_customers,
                sender_advantages=sender_advantages,
                sender_capmap=sender_capmap,
                sender_proof=sender_proof,
                sender_research=sender_research,
                campaign_prompt=campaign_prompt,
            )
        except Exception as e:
            company_domain_var.reset(_dom_tok)
            logger.error(f"[ICP] Validation (Call 2) failed for {domain}: {e}")
            # Map profile fields to canonical output keys even on error
            if self._profile_is_v2(profile):
                _rs = self._synthesize_summary_v2(profile)
                _gh = list(profile.get("growth_signals") or [])
                _ph = list(profile.get("pain_points") or [])
                _nh = list(profile.get("recent_events") or [])
            else:
                _rs = profile.get("executive_summary", "")
                _gh = list(profile.get("growth_hooks") or [])
                _ph = list(profile.get("pain_hooks") or [])
                _nh = list(profile.get("news_hooks") or [])
            err = {
                "status": "REJECTED",
                "relevance_score": 0,
                "reasoning": f"ICP evaluation error: {e}",
                "icp_context": json.dumps(profile),
                "research_summary": _rs,
                "growth_hooks": _gh,
                "pain_hooks": _ph,
                "news_hooks": _nh,
                "hooks": {"research_profile": profile},
                "_llm_error": True,
            }
            if return_context:
                err["_eval_context"] = rendered[:9000]
            return err

        m.overall_reasoning = self._scrub_hedges(m.overall_reasoning)

        # ── Production-signal role rescue ─────────────────────────────────────
        # Corrects the common mis-classification where the LLM picks out_of_domain
        # because the company's END MARKET (e.g. "eye care", "agriculture") looks
        # unrelated to the sender, but the company itself RUNS production operations.
        # Double-gated to avoid false accepts: only fires when BOTH the profile shows
        # production signals AND the sender serves operators.
        if (
            m.target_role == "out_of_domain"
            and self._profile_has_production(profile, csv_desc=csv_desc)
            and self._sender_serves_operators(sender_offerings, sender_customers, sender_capmap)
        ):
            logger.info(
                f"[ICP] Production override for {domain}: out_of_domain → operator_end_user "
                f"(profile shows production + sender serves operators)."
            )
            m.target_role = "operator_end_user"
            m.role_reason = (m.role_reason or "") + " [System: confirmed production operations.]"
            # Conservative 'clear match' baseline; real differentiation still comes
            # from the computed scale + breadth sub-scores.
            m.operational_overlap = max(int(m.operational_overlap or 0), 24)

        # ── Compute deterministic sub-scores ─────────────────────────────────
        if m.target_role == "operator_end_user":
            m.scale_readiness = self._compute_scale_readiness(company_data, profile)
            m.signal_breadth  = self._compute_signal_breadth(m)
            m.operator_fit    = (
                max(0, min(45, int(m.operational_overlap or 0)))
                + m.scale_readiness
                + m.signal_breadth
            )
        else:
            m.operational_overlap = 0
            m.scale_readiness     = 0
            m.signal_breadth      = 0
            m.operator_fit        = 0

        company_domain_var.reset(_dom_tok)

        result = self._decide(m, profile)
        if return_context:
            result["_eval_context"] = rendered[:9000]
        return result
