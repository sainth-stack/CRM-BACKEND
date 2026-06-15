"""ICP qualification + deep-research (merged Stage 3+4).

Evidence comes from the company's OWN website via the bounded
curl_cffi + trafilatura extractor (`app.integrations.site_extractor.extract_site`).
Qualification follows the stabilized MEDDPICC engine from the root ICP tool:

  * ROLE GATE  — operator_end_user (genuine buyer) | solution_vendor_overlap
                 (competitor/partner) | out_of_domain (unrelated). Only genuine
                 operators can ACCEPT.
  * TWO EVIDENCE FLOORS — an evidenced NEED the sender solves AND an evidenced
                 PRECONDITION that makes the sender's solution usable. Both derived
                 from the sender profile (sender/industry-agnostic). Matching the
                 buyer profile alone is NOT enough.
  * FIRMOGRAPHIC FILTERS — the campaign's explicit industry / location / employee-
                 size requirements are KEPT as hard disqualifiers (as before).

A single LLM call returns the verdict AND the research dossier (hooks/summary)
for EVERY company — the dossier is grounded in the same web evidence regardless of
verdict, so both accepted and rejected rows are displayable. The verdict is binary
(ACCEPTED when the score clears the ICP threshold, else REJECTED). Accepted companies
flow straight into stakeholder ranking; there is no separate research stage.
"""
import json
import re
import logging
from typing import List, Literal

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.llm import get_chat_llm
from app.integrations.site_extractor import extract_site

logger = logging.getLogger("CompanyValidationService")


# --------------------------------------------------------------------------- #
# Structured output                                                            #
# --------------------------------------------------------------------------- #
class DimensionVerdict(BaseModel):
    verdict: Literal["pass", "fail", "unknown"] = Field(
        description="pass = clearly satisfies the requirement; fail = clearly contradicts it; unknown = insufficient evidence."
    )
    evidence: str = Field(description="One sentence citing the specific evidence behind the verdict.")


class CompanyResearchProfile(BaseModel):
    """Structured company research distilled from the raw website crawl (ICP Call 1,
    enrichment model). SENDER-AGNOSTIC — pure facts about the target — so it can be
    cached and reused across campaigns/senders. The raw crawl is dropped right after
    this object is produced; only this structured profile is stored and reused."""
    actual_business: str = Field(description="One sentence: the company's ACTUAL primary business — what it makes / does / operates — read from the evidence, independent of any supplied industry label.")
    products_services: List[str] = Field(default_factory=list, description="The company's core products and/or services as named in the evidence.")
    markets_served: List[str] = Field(default_factory=list, description="Industries, customer types, and regions the company serves, per the evidence.")
    scale_operations: str = Field(description="Operational scale from the evidence: facilities, locations, headcount, revenue, certifications, named technologies/equipment, production capabilities. 'Not stated' if absent.")
    evidenced_signals: List[str] = Field(default_factory=list, description="3-8 SPECIFIC facts quoted/closely paraphrased from the evidence (what they make/operate/serve, scale, customers, named tech). Not generic.")
    growth_hooks: List[str] = Field(default_factory=list, description="Concrete VERIFIABLE facts about the company (products, sectors/markets, scale, named tech, expansion). [] if the evidence is empty.")
    pain_hooks: List[str] = Field(default_factory=list, description="ONLY challenges/problems/needs the evidence EXPLICITLY states about this company. Do NOT infer from what they do. [] if none stated.")
    news_hooks: List[str] = Field(default_factory=list, description="ONLY recent items in the evidence explicitly about this company. [] if none.")
    executive_summary: str = Field(description=(
        "A focused research dossier (aim for 3-4 tight paragraphs / 900-1600 chars when the evidence "
        "supports it) where EVERY claim traces to the provided evidence. Cover, in prose: (1) what the "
        "company makes/does and its core products/services; (2) the markets, industries, and customer "
        "types it serves; (3) scale and operations — facilities, locations, headcount, revenue, "
        "certifications, named technologies; (4) any recent developments or notable operational traits. "
        "Be concise and concrete — do NOT pad. If evidence is thin, write only what is grounded and say so."))


class ICPMeddpiccJudgment(BaseModel):
    """ICP validation verdict (Call 2, reasoning model). Judges the structured research
    profile + CSV firmographics against the SENDER's ICP using MEDDPICC. Sees the
    distilled profile, NOT the raw web text."""
    # ---- Firmographic filters (the campaign's explicit requirements; KEPT) ----
    industry_match: DimensionVerdict = Field(description="Does the company's ACTUAL business (from the research profile) fall UNDER any campaign target industry (supercategory containment, not exact-label match)?")
    location_match: DimensionVerdict = Field(description="Company HQ/operating region vs the campaign's target locations.")
    size_match: DimensionVerdict = Field(description="Company employee size vs the campaign's target size.")

    # ---- MEDDPICC role gate (primary; derived from the sender via BUYER/COMPETITOR profiles) ----
    target_role: Literal["operator_end_user", "solution_vendor_overlap", "out_of_domain"] = Field(
        description=(
            "Classify the target against the BUYER PROFILE and COMPETITOR PROFILE you derived from the SENDER "
            "PROFILE. The category names below illustrate the GENERAL CONCEPT across industries — they are not a "
            "fixed list of allowed categories; the actual buyer/competitor profiles come ENTIRELY from THIS sender. "
            "'operator_end_user' = the target matches the derived BUYER PROFILE — it RUNS the kind of operations "
            "the sender's offering acts on (illustrative cross-industry examples of what counts as 'runs "
            "operations': manufactures physical goods of ANY kind, runs production lines or equipment fleets, "
            "operates clinics or hospital networks, operates retail stores or distribution, runs financial-services "
            "back-office processes, etc.). A company that MAKES physical products of any category is an operator "
            "even if its product looks unrelated to the sender at first glance — what matters is whether its "
            "operations match the derived buyer profile, NOT whether its product category equals the sender's. "
            "'solution_vendor_overlap' = the target's OWN core product matches the derived COMPETITOR PROFILE — it "
            "sells a directly-competing solution in the SAME category the sender sells. Companies that merely make "
            "or operate in an adjacent vertical are NOT competitors — they are operators. "
            "'out_of_domain' = the target matches NEITHER profile."))
    role_reason: str = Field(description="One sentence: what the target does, and why that role relative to the sender.")
    # ---- Operator fit, scored as THREE independent sub-factors (operators only; else 0). ----
    # These are summed by the system into operator_fit (0-100). Scoring three separate
    # axes forces real differentiation between companies instead of stamping a single
    # round default — a large multi-site OEM matching many signals should clearly outscore
    # a small single-process shop. INFER each from what the company does; never require a
    # stated problem.
    operational_overlap: int = Field(description="0-45 (operators only; else 0): how DIRECTLY the target's core operations match the sender's buyer profile and derived operational signals. 38-45 = textbook match to the central signal; 25-37 = clear match; 12-24 = partial/adjacent overlap; 1-11 = loose. Spread this — do NOT default to a round number; two different companies should rarely get the same value.")
    scale_readiness: int = Field(default=0, description="SYSTEM-COMPUTED from firmographics — do NOT score this, just output 0.")
    signal_breadth: int = Field(default=0, description="SYSTEM-COMPUTED from matched pains/services — do NOT score this, just output 0.")
    operator_fit: int = Field(default=0, description="SYSTEM-COMPUTED (operational_overlap + computed scale + computed breadth) — output 0; the system fills it.")

    # ---- Corroborating evidence signals (BONUS only — absence is neutral) ----
    has_evidenced_need: bool = Field(description="True if the research profile shows a problem/need/initiative the SENDER'S offering addresses (derive from the sender's capability->pain map). A POSITIVE BONUS signal only — when false the score is NOT penalised (websites rarely state needs). Do not force it true: 'uses technology' alone is not a need.")
    need_evidence: str = Field(description="The exact evidenced need (quote/paraphrase from the profile). 'None evidenced' when has_evidenced_need is false.")
    has_evidenced_precondition: bool = Field(description="True if the profile shows the PRECONDITION that makes the sender's solution usable (the operations, assets, data, scale, or context the offering requires). A POSITIVE BONUS signal only — when false the score is NOT penalised. Derive from the sender's offering.")
    precondition_evidence: str = Field(description="The exact evidenced precondition (quote/paraphrase). 'None evidenced' when has_evidenced_precondition is false.")
    evidence_confidence: int = Field(description="0-100: share of the assessment read from EXPLICIT profile facts vs inference.")

    # ---- MEDDPICC informational ----
    metrics: str = Field(description="A value hypothesis tied to the evidenced need (or 'unclear — no evidenced need').")
    economic_buyer: str = Field(description="A budget-owner ROLE/TITLE appropriate to the derived BUYER PROFILE and the campaign objective for this target — derive the function from the sender's offering and the target's operations, do NOT default to a fixed role. NEVER a named individual unless that exact name is in the evidence.")
    champion: str = Field(description="A likely internal CHAMPION role (not a person unless named in evidence).")
    decision_criteria: str = Field(description="Likely evaluation criteria.")
    decision_process: str = Field(description="Start with 'NEEDS DISCOVERY:' then a concrete one-line note.")
    paper_process: str = Field(description="Start with 'NEEDS DISCOVERY:' then a concrete one-line note.")
    discovery_checklist: List[str] = Field(default_factory=list, description="3-5 specific things to confirm on the first call.")

    # ---- Sender-relative enrichment (for draft generation) ----
    business_opportunity_reason: str = Field(description="The 'why now', anchored to ONE specific fact from the profile. State plainly if none found.")
    matched_pains: List[str] = Field(default_factory=list, description="Specific target pains the sender solves (grounded in the profile).")
    matched_services: List[str] = Field(default_factory=list, description="Sender services that map to those pains.")

    overall_reasoning: str = Field(description=(
        "A professional 2-3 sentence FACTUAL assessment, in polished business English, of this target's "
        "fit for the sender's ICP — written as if briefing a sales team. Ground it in the MEDDPICC read: "
        "name what the company actually does/operates and why that matches (or does not match) the "
        "sender's target-customer profile. Reason from operations — do NOT comment on whether a pain is "
        "'evidenced' or note the absence of stated problems. "
        "Be specific and cite evidenced facts. Do NOT state or imply an accept/reject decision, do NOT "
        "recommend whether to pursue, and do NOT hedge with 'may not be a fit / needs further "
        "exploration' — the system decides qualification separately from this text. STRICTLY no internal "
        "jargon, field names, scores, booleans, verdict labels, or bracketed metadata. Use the exact "
        "sender name."))
    confidence: int = Field(description="0-100 confidence given the quality/quantity of evidence.")


# --------------------------------------------------------------------------- #
# Call 1 — ENRICHMENT: raw web content -> structured, sender-agnostic profile   #
# --------------------------------------------------------------------------- #
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
none). If the evidence is thin, capture only what is grounded and say so in the summary — do NOT pad with
invented facts. `actual_business` must reflect what the evidence shows the company does, independent of
any provided label."""
)


# --------------------------------------------------------------------------- #
# Call 2 — VALIDATION: structured profile + sender + filters -> MEDDPICC verdict#
# --------------------------------------------------------------------------- #
_VALIDATE_PROMPT = ChatPromptTemplate.from_template(
    """You are a STRICT B2B ICP analyst qualifying an OUTBOUND lead before any contact. Your #1 job is to
PREVENT false positives and stay fully grounded. Quote facts from the RESEARCH PROFILE; never invent
needs, names, or firmographics. Everything sender-related must be derived from THIS sender — no
fixed-industry assumptions.

SENDER PROFILE (who is doing the outreach):
- Company: {sender_name}
- Offerings: {sender_offerings}
- Sells to (their ICP): {sender_customers}
- Differentiators: {sender_advantages}
- Capability -> customer-pain map: {sender_capmap}
- Proof points: {sender_proof}
- Business summary: {sender_research}

APPROVAL THRESHOLD: {threshold}/100. A company is ACCEPTED when its fit score reaches this threshold and
no firmographic requirement is violated. Calibrate operator_fit and the evidence floors HONESTLY against
this bar — never inflate a score to clear it, never deflate a genuine fit.

==================== PART A — FIRMOGRAPHIC FILTERS (structural gate) ====================
Judge these THREE checks STRICTLY against the CSV FIRMOGRAPHIC FIELDS below — compare target industry to
the CSV Industry, target location to the CSV Location, target size to the CSV Employee size. IGNORE the
RESEARCH PROFILE entirely for PART A: it may mention a parent company's headcount, extra regions, or
activities that are NOT this record's firmographics, and using it causes wrong fails. Do NOT use the
campaign objective here either.

USER-SPECIFIED REQUIREMENTS (may contain one or more values each):
- Target industries: {target_industry}
- Target locations: {target_loc}
- Target employee size: {target_size}

CSV FIRMOGRAPHIC FIELDS (the ONLY source for PART A):
- Industry: {csv_industry}
- Location: {csv_loc}
- Employee size: {csv_size}

For each, return verdict (pass | fail | unknown) + one-sentence evidence (cite the CSV field):
1. industry_match (compare the CSV Industry to the target industries): the question is ONLY "does the CSV
   industry fall UNDER any target industry?" — NOT "does it exactly equal the input". Treat each target
   industry as a broad SUPERCATEGORY that includes all its sub-sectors, specialisations and verticals. The
   bar for FAIL is INTENTIONALLY HIGH — FAIL hard-disqualifies, so use it ONLY when the CSV industry is
   CLEARLY in a different supercategory than EVERY target. When the CSV label is ambiguous, return UNKNOWN
   (does NOT disqualify), never FAIL. Procedure (judge the CSV industry label only):
   - If "Any" -> PASS.
   - If the CSV industry is IDENTICAL to any target -> PASS.
   - If the CSV industry is a SUB-SECTOR, SPECIALISATION, or VERTICAL inside any target supercategory ->
     PASS. The illustrative cross-industry examples below show the supercategory→sub-sector relationship —
     they are NOT a fixed list; derive the inclusion from general knowledge of how the target industry is
     structured:
       * "Manufacturing" is satisfied by any sub-sector that makes/produces physical goods — for
         example: Electronics Manufacturing, Electrical/Electronic Manufacturing, Industrial Automation,
         Machinery, Mechanical/Industrial Engineering, Materials/Composites, Textiles production,
         Dairy/Food equipment, Nanotechnology, Consumer Electronics (the makers, not retailers), etc.
       * "Healthcare" is satisfied by Pharma, Medical Devices, Biotech, Clinical Services, etc.
       * "Financial Services" is satisfied by Banking, Insurance, Fintech, Asset Management, etc.
       * "Technology / Software" is satisfied by SaaS, Enterprise Software, Cloud, Cybersecurity, etc.
   - If the CSV label is AMBIGUOUS between supercategories (e.g. "Consumer Electronics", "Logistics",
     "Dairy", "Technology") -> UNKNOWN. Do NOT guess; UNKNOWN does not disqualify.
   - Return FAIL ONLY when the CSV industry is UNAMBIGUOUSLY in a DIFFERENT supercategory than EVERY target
     (e.g. a recruitment agency, a coffee shop, or a law firm against a "Manufacturing" filter). Against a
     "Manufacturing" target, ANY label that denotes making / producing / fabricating / engineering physical
     goods is a PASS, NOT a fail — this explicitly INCLUDES "Textiles", "Industrial", "Industrial
     Automation", "Machinery", "Electrical/Electronic Manufacturing", "Composites", "Materials", and the
     like (textile *manufacturing* is manufacturing). Reserve FAIL for pure non-physical services with no
     production at all. If the label could plausibly involve making physical goods, return UNKNOWN, never
     FAIL.
2. location_match and 3. size_match: the SYSTEM validates these two deterministically from the CSV fields
   AFTER your response, so do NOT spend effort on them — just return verdict "unknown" with evidence
   "validated separately by the system" for both. (industry_match above IS yours to decide.)

==================== PART B — MEDDPICC ICP QUALIFICATION (sender-fit) ====================
This is where the CAMPAIGN OBJECTIVE and the RESEARCH PROFILE are used.

HOW TO READ THE EVIDENCE (critical): the RESEARCH PROFILE is distilled from the target's own MARKETING
website, which describes what the company DOES, MAKES, SELLS, and SERVES — its capabilities — and almost
NEVER states the company's internal problems, gaps, or needs. Therefore judge fit by INFERENCE from what
the company does: if its operations match the sender's buyer profile, it is a fit, REGARDLESS of whether
any pain is spelled out. The ABSENCE of a stated need or problem is NOT evidence against fit and must NOT
lower the fit grade. Reserve low fit grades for companies whose operations genuinely do not match the
buyer profile — not for companies that simply did not advertise a weakness.

CAMPAIGN OBJECTIVE (the user's own words — the target should help satisfy this): {campaign_prompt}

TARGET COMPANY:
- Name: {name}
- RESEARCH PROFILE (distilled from the company's own website — treat this as THE evidence; quote from it):
\"\"\"{profile}\"\"\"

First, from the SENDER PROFILE ONLY, define:
  (i)   BUYER PROFILE — a genuine end-user customer of this sender;
  (ii)  COMPETITOR PROFILE — a company selling a directly-overlapping solution;
  (iii) THE NEED — the specific problem(s) the sender's offering solves (from the capability->pain map);
  (iv)  THE PRECONDITION — what a company must have/do for the sender's solution to be USABLE.

Then UNPACK the BUYER PROFILE into 3-5 concrete OPERATIONAL SIGNALS derived from THIS sender's offerings
and capability->pain map (signals come ENTIRELY from the sender data — do NOT use any fixed industry/
product list). Each signal is one specific operational characteristic a company would have to make the
sender's offering applicable — e.g. a specific kind of asset operated, a specific kind of data
generated, a specific kind of process run, a specific kind of customer served — written in the sender's
own terms. Treat these signals as the practical checklist for what counts as the BUYER PROFILE.

A target qualifies as operator_end_user when the research profile shows it matches AT LEAST ONE of those
derived signals — even if its headline product category looks unrelated to the sender at first glance.
Then ground EVERY field in the RESEARCH PROFILE, judged in light of the campaign objective:
- target_role — classify against the BUYER PROFILE (i) and COMPETITOR PROFILE (ii) you derived above
  from THIS sender (+ role_reason). The cross-industry examples below illustrate the GENERAL CONCEPT —
  they are not a fixed list; the actual profiles come ENTIRELY from this sender's data:
  * operator_end_user: the target matches the derived BUYER PROFILE — it RUNS the kind of operations
    the sender's offering acts on (illustrative cross-industry examples of what 'runs operations' looks
    like: manufactures physical goods of ANY kind — electronics, batteries, machinery, robots, vehicles,
    materials, safety equipment, medical devices, food/dairy equipment, textiles, plasma tools, etc.;
    operates clinics/hospitals; runs retail stores or distribution networks; operates financial-services
    back-office processes; etc.). A company that MAKES physical products of any category is an operator
    even if its product looks unrelated to the sender — what matters is whether its OPERATIONS match the
    derived buyer profile, NOT whether its product category equals the sender's customers.
  * solution_vendor_overlap: the target's OWN core product matches the derived COMPETITOR PROFILE — it
    sells a directly-competing solution in the SAME category the sender sells to the SAME buyers. A
    company that merely makes physical equipment in an adjacent vertical is NOT a competitor — it is an
    operator.
  * out_of_domain: the target matches NEITHER profile — its operations do not match the BUYER PROFILE
    AND its product does not match the COMPETITOR PROFILE.
  Do NOT default to out_of_domain just because the target's product category differs from the sender's
  typical customers; the decisive question is whether the target's OPERATIONS match the derived BUYER
  PROFILE.
- operational_overlap (0-45, operators only; else 0) — the ONLY fit number you score. Judge how DIRECTLY
  the target's core operations match the BUYER PROFILE and its derived signals, inferred from what the
  company does/makes/serves: 38-45 textbook match to the central signal; 25-37 clear match; 12-24
  partial/adjacent overlap; 1-11 loose. DIFFERENTIATE — avoid round-number defaults; two different
  companies should rarely get the same value. Never lower it because no pain is stated.
  (scale_readiness, signal_breadth and operator_fit are computed by the system from firmographics and the
  matched pains/services — output 0 for all three.)
- has_evidenced_need + need_evidence: a CORROBORATING BONUS only. TRUE if THE NEED (iii) happens to be
  explicit in the profile; otherwise false + 'None evidenced'. Setting it false does NOT reduce the score
  — it just means no bonus. Do NOT force it true, and do NOT downgrade operator_fit when it is false.
- has_evidenced_precondition + precondition_evidence: a CORROBORATING BONUS only, same rule — TRUE if THE
  PRECONDITION (iv) is explicit; false otherwise, with no penalty.
- evidence_confidence (0-100). metrics (value hypothesis tied to the evidenced need, else 'unclear — no
  evidenced need'). economic_buyer / champion: a ROLE/TITLE only — NEVER a named person unless that exact
  name is in the evidence. decision_criteria. decision_process / paper_process: each start 'NEEDS
  DISCOVERY:' + a concrete note. discovery_checklist (3-5).

==================== PART C — SENDER-RELATIVE ENRICHMENT (for the cold email) ====================
CRITICAL GROUNDING: the opportunity reason and matched pains/services MUST trace to a SPECIFIC fact in
the RESEARCH PROFILE; never generic, assumed, or invented. BANNED hedge words: may, might, could, likely,
potential, possibly, "can lead to". (Company facts/hooks are already captured in the profile — here you
only map them to THIS sender.)
- business_opportunity_reason: the 'why now' anchored to ONE specific fact in the profile, or state
  plainly that no concrete trigger was found.
- matched_pains / matched_services: the sender pains/services that apply to what the profile shows.

Finally: overall_reasoning — a professional, polished 2-3 sentence FACTUAL assessment (use the exact
sender name {sender_name}) of the target's MEDDPICC fit: what it does/operates and why that matches (or
does not match) {sender_name}'s target-customer profile. Reason from operations; do NOT comment on
whether a pain is 'evidenced' or remark on the absence of stated problems. Do NOT state or imply
accept/reject, do NOT recommend whether to pursue, and do NOT hedge ('may not be a fit', 'needs
exploration') — qualification is decided
separately. Write it for a human sales reader — NO scores, field names, booleans, verdict labels, or
bracketed metadata. Then confidence (0-100)."""
)


class CompanyValidationService:
    """ICP qualification + deep-research (MEDDPICC, website-evidence only).

    Judges each target against the sender's actual profile and the campaign's own
    requirements (industry / location / size / objective).
    No hardcoded sender profile or industry rubric, no paid search API.
    """

    def __init__(self):
        # Call 1 (enrichment): cheap/fast model distils the raw crawl into a profile.
        self.enrichment_llm = get_chat_llm("enrichment", timeout=120)
        # Call 2 (ICP validation): quality-critical judgment on the reasoning model.
        self.reasoning_llm = get_chat_llm("reasoning", timeout=60)

    # ----------------------------------------------------------------------- #
    # Helpers                                                                  #
    # ----------------------------------------------------------------------- #
    @staticmethod
    def _as_text(value, empty="Any") -> str:
        """Render a list/str field for the prompt; '' / None / [] -> 'Any'.
        Comma/semicolon/newline-separated strings are normalised to a ', '-joined list."""
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

    # Max chars of raw crawl fed to the enrichment model. Trimmed 24K -> 12K to cut
    # input tokens (TPM is the latency governor): a marketing site's identity — what
    # it makes/does/serves, its scale and markets — lives in the homepage/about/
    # products pages, which the extractor ranks first and which fit well under 12K.
    # The trimmed tail is blog/case-study filler that does not change the structured
    # profile (and therefore does not change the ICP verdict or score).
    _ENRICH_INPUT_CHARS = 12000

    def prefetch_icp_research(self, domains: list[str]) -> dict[str, dict]:
        """Batch-load cached STRUCTURED research profiles for many domains in ONE query
        (removes the per-company N+1 and lets a known domain skip crawl + enrichment).
        Returns {domain: profile_dict}. Raw web text is never stored, so only parsable
        structured profiles are returned."""
        import json
        from app.db.database import SessionLocal
        from app.db import models

        domains = [d for d in {d for d in domains if d}]
        if not domains:
            return {}
        # Freshness window: only reuse a profile refreshed within N days; older ones
        # are excluded so the domain gets re-crawled + re-enriched (never permanently stale).
        import datetime as _dt
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
                    if isinstance(obj, dict) and obj.get("executive_summary"):
                        out[d] = obj  # latest valid structured profile (rows desc by updated_at)
                except (ValueError, TypeError):
                    continue  # legacy raw-text context -> ignore; will re-enrich
        finally:
            db.close()
        return out

    async def _gather_raw_evidence(self, domain: str) -> str:
        """Crawl the company's OWN site (homepage + high-value pages), bounded. Returns
        the raw combined text — used ONCE by enrichment, then dropped (never stored)."""
        site_text = ""
        if domain:
            try:
                ext = await extract_site(domain, max_pages=5, per_page_chars=7000)
                if ext.ok and ext.combined_text.strip():
                    site_text = ext.combined_text
                else:
                    logger.warning(f"[ICP-RESEARCH] No website content for {domain}: {ext.error}")
            except Exception as e:
                logger.warning(f"[ICP-RESEARCH] Extraction failed for {domain}: {e}")
        return site_text

    async def _enrich(self, name: str, csv_desc: str, raw_evidence: str) -> dict:
        """CALL 1 — distil raw web text into a structured, sender-agnostic profile dict.
        The raw text is passed in and dropped by the caller right after this returns."""
        blob = (
            f"CSV DESCRIPTION: {csv_desc or 'N/A'}\n\n"
            f"COMPANY WEBSITE TEXT:\n{raw_evidence or 'No website content retrieved.'}"
        )
        chain = _ENRICH_PROMPT | self.enrichment_llm.with_structured_output(CompanyResearchProfile)
        profile: CompanyResearchProfile = await chain.ainvoke({
            "name": name or "the company",
            "csv_desc": (csv_desc or "")[:1500],
            "raw_evidence": blob[: self._ENRICH_INPUT_CHARS],
        })
        return profile.model_dump()

    @staticmethod
    def _render_profile(profile: dict, summary_chars: int = 600) -> str:
        """Render a structured profile dict into readable text for the validation prompt.

        The ICP verdict + score are driven by the STRUCTURED facts below (actual
        business, products, markets, scale, signals, pains) — NOT by the prose
        dossier. So the long `executive_summary` is truncated to `summary_chars` for
        the validation LLM to cut input tokens, while the FULL summary is still stored
        and used by the drafting stage. Pass summary_chars<=0 to omit it entirely."""
        def _lst(key):
            vals = profile.get(key) or []
            return "; ".join(str(v) for v in vals) if vals else "None stated"
        summary = (profile.get("executive_summary") or "").strip()
        if summary_chars and len(summary) > summary_chars:
            summary = summary[:summary_chars].rsplit(" ", 1)[0] + " …"
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

    # ----------------------------------------------------------------------- #
    # MEDDPICC scoring (from the stabilized root ICP engine)                   #
    # ----------------------------------------------------------------------- #
    # ---- Deterministic sub-scores (option A): computed from hard facts, not LLM grades. --- #
    # gpt-class judges cannot reliably produce DIFFERENTIATED 0-N numbers — they stamp a
    # "safe middle" on every company (observed: scale=20 for a single-site shop AND an
    # $11B multinational). So the two objective axes are computed in code from facts the
    # LLM is good at EXTRACTING (revenue, headcount, markets, products, matched pains/
    # services). Only operational_overlap (a genuine qualitative judgment) stays on the LLM.
    @staticmethod
    def _revenue_score(revenue: str) -> int:
        """0-12 from a revenue-range string (uses the UPPER bound). Blank -> neutral 3."""
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
        """0-8 from an employee-size string (uses the UPPER bound). Blank -> neutral 2."""
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
        """0-3 company-maturity score from a founded year/date. Blank -> neutral 1."""
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

    @classmethod
    def _compute_scale_readiness(cls, company_data: dict, profile: dict) -> int:
        """0-30 from real firmographic + profile facts. Prefers the EXACT headcount /
        revenue figures (continuous -> finer spread) and falls back to the coarse bands
        when the exact fields are absent. Spreads naturally because these facts genuinely
        differ company to company.

          revenue 0-12 + headcount 0-8 + markets 0-4 + products 0-3 + age 0-3 = 30."""
        score = cls._revenue_score(company_data.get("annual_revenue") or company_data.get("revenue"))
        score += cls._headcount_score(company_data.get("staff_count") or company_data.get("size"))
        score += min(4, len(profile.get("markets_served") or []))
        score += min(3, len(profile.get("products_services") or []))
        score += cls._age_score(company_data.get("founded_year"))
        return max(0, min(30, score))

    # ---- Deterministic industry classification from SIC / NAICS codes ---------- #
    # Code -> canonical supercategory + the keywords a user's target-industry string
    # might use for it. A code match forces industry_match=PASS (a precise, positive
    # override of the LLM's label-guessing); it NEVER forces a FAIL, so it can only add
    # precision, not new false negatives.
    _SUPERCATEGORY_KEYWORDS = {
        "Manufacturing": ("manufactur", "industrial", "factory", "production", "oem", "fabricat"),
        "Healthcare": ("health", "medical", "pharma", "clinical", "biotech", "life science"),
        "Financial Services": ("financ", "bank", "insurance", "fintech", "asset manage", "capital"),
        "Technology": ("tech", "software", "saas", "it ", "information", "computer", "digital", "internet"),
        "Construction": ("construct", "building", "engineering & construction"),
        "Retail": ("retail", "ecommerce", "e-commerce", "consumer goods"),
        "Wholesale": ("wholesale", "distribut"),
        "Transportation": ("transport", "logistics", "freight", "supply chain"),
        "Energy": ("energy", "oil", "gas", "utilit", "power"),
        "Agriculture": ("agricultur", "farming", "forestry"),
    }

    @staticmethod
    def _sic_supercategory(sic: str) -> str | None:
        m = re.match(r"\s*(\d{2})", str(sic or ""))
        if not m:
            return None
        d = int(m.group(1))
        if 1 <= d <= 9:   return "Agriculture"
        if 10 <= d <= 14: return "Energy"
        if 15 <= d <= 17: return "Construction"
        if 20 <= d <= 39: return "Manufacturing"
        if 40 <= d <= 49: return "Transportation"
        if 50 <= d <= 51: return "Wholesale"
        if 52 <= d <= 59: return "Retail"
        if 60 <= d <= 67: return "Financial Services"
        return None

    @staticmethod
    def _naics_supercategory(naics: str) -> str | None:
        m = re.match(r"\s*(\d{2})", str(naics or ""))
        if not m:
            return None
        d = int(m.group(1))
        return {
            11: "Agriculture", 21: "Energy", 22: "Energy", 23: "Construction",
            31: "Manufacturing", 32: "Manufacturing", 33: "Manufacturing",
            42: "Wholesale", 44: "Retail", 45: "Retail", 48: "Transportation",
            49: "Transportation", 51: "Technology", 52: "Financial Services",
            53: "Financial Services", 54: "Technology", 62: "Healthcare",
        }.get(d)

    @classmethod
    def _deterministic_industry_pass(cls, sic: str, naics: str, target_industry: str) -> str | None:
        """Return the matched supercategory when the company's SIC/NAICS code falls under
        a target industry; else None. Positive-only (never fails a company)."""
        target = (target_industry or "").lower()
        if not target or target == "any":
            return None
        for cat in (cls._naics_supercategory(naics), cls._sic_supercategory(sic)):
            if not cat:
                continue
            kws = cls._SUPERCATEGORY_KEYWORDS.get(cat, ())
            if cat.lower() in target or any(k.strip() in target for k in kws):
                return cat
        return None

    @staticmethod
    def _compute_signal_breadth(m: "ICPMeddpiccJudgment") -> int:
        """0-25 from how many sender pains/services the target matched. These LLM LISTS
        vary per company (the LLM is reliable at producing them), so the derived number
        spreads where a direct 0-25 grade collapsed."""
        n = len(m.matched_pains or []) + len(m.matched_services or [])
        return max(0, min(25, n * 4))

    # Bonus added when an explicit need / precondition IS stated in the evidence.
    # These are POSITIVE signals only — their absence is neutral, never a penalty,
    # because marketing websites describe capabilities, not internal problems, so a
    # company rarely "confesses" a need or precondition even when it is a perfect fit.
    _NEED_BONUS = 8
    _PRECONDITION_BONUS = 7

    @classmethod
    def _overall_score(cls, m: ICPMeddpiccJudgment):
        """Continuous 0-100 fit score for genuine operators, or None for role-gated
        companies (vendors / out-of-domain) where a fit score is meaningless.

        The score is the LLM's graded `operator_fit` — an INFERENCE of how well the
        target's actual operations match THIS sender's ideal-customer profile, read
        from what the company does/makes/serves (the only thing a marketing site
        reliably exposes). An explicitly stated need or precondition then ADDS a small
        bonus on top (a positive corroborating signal); their ABSENCE is neutral and
        never discounts the score. This is the key fix for the old multiplicative
        floors, which pinned almost every company to ~0.70 of its fit (websites never
        state pains) and clustered all scores in a narrow band at the threshold."""
        if m.target_role != "operator_end_user":
            return None
        base = max(0, min(100, int(m.operator_fit or 0)))
        bonus = 0
        if m.has_evidenced_need:
            bonus += cls._NEED_BONUS
        if m.has_evidenced_precondition:
            bonus += cls._PRECONDITION_BONUS
        return min(100, base + bonus)

    # ----------------------------------------------------------------------- #
    # Deterministic firmographic matching — LOCATION + SIZE (CSV fields only)   #
    #                                                                          #
    # These two are validated in code against the campaign targets vs the       #
    # company's CSV fields ONLY, never the research profile. The model could     #
    # not reliably ignore headcounts/locations that appear in the research text, #
    # which caused same-size companies to fail. This is a parameterised          #
    # comparison driven entirely by the inputs — no fixed company/industry       #
    # outcome is baked in. (industry stays on the model: supercategory reasoning #
    # like "Machinery is Manufacturing" can't be coded without a hardcoded       #
    # taxonomy, which is disallowed.) Country-name variants below are the only   #
    # reference data, used purely to normalise spelling.                         #
    # ----------------------------------------------------------------------- #
    _COUNTRY_ALIASES = (
        {"usa", "us", "united states", "united states of america", "america"},
        {"uk", "united kingdom", "great britain", "britain", "england", "scotland",
         "wales", "northern ireland", "gb", "gbr"},
        {"uae", "united arab emirates"},
        {"south korea", "korea", "republic of korea"},
        {"czechia", "czech republic"},
    )

    @staticmethod
    def _blank(v: str) -> bool:
        return (v or "").strip().lower() in ("", "n/a", "na", "none", "null", "unknown", "any")

    @staticmethod
    def _parse_ranges(text: str):
        """Numeric employee ranges from a free-text size string -> [(low, high)]; high=inf for '5000+'."""
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
    def _match_size(cls, csv_size: str, target_size: str):
        """('pass'|'fail'|'unknown', evidence) — numeric range overlap on the CSV field."""
        if cls._blank(target_size):
            return "pass", "No employee-size requirement was specified."
        if cls._blank(csv_size):
            return "unknown", "The CSV employee size is not stated."
        comp = cls._parse_ranges(csv_size)
        tgt = []
        for part in re.split(r"[,/;|]| or ", target_size.lower()):
            tgt += cls._parse_ranges(part)
        if not comp:
            return "unknown", f"CSV employee size '{csv_size}' could not be read as a number."
        if not tgt:
            return "unknown", f"Target size '{target_size}' could not be read as a range."
        for cl, ch in comp:
            for tl, th in tgt:
                if cl <= th and tl <= ch:
                    return "pass", f"CSV size ({csv_size}) falls within the target band ({target_size})."
        return "fail", f"CSV size ({csv_size}) is outside the target band ({target_size})."

    @classmethod
    def _loc_groups(cls, text: str):
        t = " " + re.sub(r"[^a-z ]", " ", (text or "").lower()) + " "
        t = re.sub(r"\s+", " ", t)
        return ({i for i, al in enumerate(cls._COUNTRY_ALIASES) if any(f" {a} " in t for a in al)}, t)

    @classmethod
    def _match_location(cls, csv_loc: str, target_loc: str):
        """('pass'|'fail'|'unknown', evidence) — country containment on the CSV field."""
        if cls._blank(target_loc):
            return "pass", "No location requirement was specified."
        if cls._blank(csv_loc):
            return "unknown", "The CSV location is not stated."
        comp_g, comp_t = cls._loc_groups(csv_loc)
        tgt_g, _ = cls._loc_groups(target_loc)
        if comp_g & tgt_g:
            return "pass", f"CSV location ({csv_loc}) is within the target region ({target_loc})."
        for part in re.split(r"[,/;|]| or ", target_loc.lower()):
            p = re.sub(r"[^a-z ]", " ", part).strip()
            if len(p) >= 3 and f" {p} " in comp_t:
                return "pass", f"CSV location ({csv_loc}) matches the target ({part.strip()})."
        if tgt_g or comp_g:
            return "fail", f"CSV location ({csv_loc}) is outside the target region ({target_loc})."
        return "unknown", f"Could not place CSV location ({csv_loc}) against the target ({target_loc})."

    # ----------------------------------------------------------------------- #
    # Combined ICP-qualification + deep-research agent (single stage)          #
    # ----------------------------------------------------------------------- #
    async def qualify_and_enrich(self, company_data: dict, user_intel: dict,
                                 campaign_metadata: dict = None, research_cache: dict | None = None,
                                 return_context: bool = False,
                                 precomputed_context=None) -> dict:
        """Single-pass ICP qualification (MEDDPICC, firmographic filters) + research
        enrichment. The enrichment dossier is returned for every verdict; the status
        field carries the verdict ('ACCEPTED' when the score clears the threshold, else 'REJECTED')."""
        campaign_metadata = campaign_metadata or {}
        domain = company_data.get("domain") or company_data.get("website")
        name = company_data.get("name")

        # Prefer the structured country for deterministic location matching; the free
        # location string remains the prompt-facing label.
        csv_loc = company_data.get("location") or "N/A"
        csv_country = company_data.get("country") or csv_loc
        csv_size = company_data.get("size") or "N/A"
        csv_industry = company_data.get("industry") or "N/A"
        csv_sic = company_data.get("sic_code") or ""
        csv_naics = company_data.get("naics_code") or ""
        csv_desc = company_data.get("description") or ""

        # Campaign requirements (the user's explicit filters for THIS campaign).
        target_industry = self._as_text(campaign_metadata.get("target_industry"))
        target_loc = self._as_text(campaign_metadata.get("target_location"))
        target_size = self._as_text(campaign_metadata.get("target_employee_count"))
        campaign_prompt = self._as_text(campaign_metadata.get("prompt"), empty="(no specific objective provided)")

        # Sender profile (built dynamically — no hardcoded values).
        sender_name = user_intel.get("company_name") or user_intel.get("name") or "the sender"
        sender_offerings = self._as_text(user_intel.get("services") or user_intel.get("offerings") or user_intel.get("core_offerings"))
        sender_customers = self._as_text(user_intel.get("target_customers"))
        sender_advantages = self._as_text(user_intel.get("competitive_advantages"))
        sender_capmap = str(user_intel.get("capability_to_pain_map") or [])[:2000]
        sender_proof = str(user_intel.get("proof_points") or [])[:1000]
        sender_research = self._as_text(user_intel.get("deep_research"), empty="N/A")

        # ---- CALL 1: structured research profile (reuse cache, else crawl + enrich) ----
        profile = research_cache.get(domain) if research_cache else None
        if profile:
            logger.info(f"[ICP-CACHE] Reusing structured research profile for {domain}.")
        else:
            if precomputed_context is not None:
                # Eval/iteration hook: caller supplied raw evidence text to enrich.
                raw = precomputed_context[0] if isinstance(precomputed_context, (tuple, list)) else precomputed_context
            else:
                raw = await self._gather_raw_evidence(domain)
            try:
                profile = await self._enrich(name, csv_desc, raw)
            except Exception as e:
                logger.error(f"[ICP] Enrichment (Call 1) failed for {domain}: {e}")
                profile = None
            raw = None  # raw web text dropped from RAM here; never persisted.

        if not profile:
            err = {
                "status": "REJECTED",
                "relevance_score": 0,
                "reasoning": "No usable website research could be gathered for this company.",
                "icp_context": None,
                "_llm_error": True,
            }
            if return_context:
                err["_eval_context"] = ""
            return err

        rendered = self._render_profile(profile)

        # ---- CALL 2: ICP MEDDPICC validation on the structured profile ----
        chain = _VALIDATE_PROMPT | self.reasoning_llm.with_structured_output(ICPMeddpiccJudgment)
        try:
            res: ICPMeddpiccJudgment = await chain.ainvoke({
                "sender_name": sender_name,
                "sender_offerings": sender_offerings,
                "sender_customers": sender_customers,
                "sender_advantages": sender_advantages,
                "sender_capmap": sender_capmap,
                "sender_proof": sender_proof,
                "sender_research": (sender_research or "N/A")[:3000],
                "threshold": settings.ICP_ACCEPT_THRESHOLD,
                "target_industry": target_industry,
                "target_loc": target_loc,
                "target_size": target_size,
                "csv_industry": csv_industry,
                "csv_loc": csv_loc,
                "csv_size": csv_size,
                "campaign_prompt": campaign_prompt[:1500],
                "name": name,
                "profile": rendered[:8000],
            })
        except Exception as e:
            # Fail-closed: a failed judgment is REJECTED. `_llm_error` is the transient,
            # non-persisted signal used only by Stage 3's provider-outage detector. The
            # profile is still persisted so the research isn't lost.
            logger.error(f"[ICP] Validation (Call 2) failed for {domain}: {e}")
            err = {
                "status": "REJECTED",
                "relevance_score": 0,
                "reasoning": f"ICP evaluation error: {e}",
                "icp_context": json.dumps(profile),
                "research_summary": profile.get("executive_summary", ""),
                "growth_hooks": profile.get("growth_hooks", []),
                "pain_hooks": profile.get("pain_hooks", []),
                "news_hooks": profile.get("news_hooks", []),
                "hooks": {"research_profile": profile},
                "_llm_error": True,
            }
            if return_context:
                err["_eval_context"] = rendered[:9000]
            return err

        # operator_fit = LLM operational_overlap (qualitative) + the two DETERMINISTIC
        # sub-scores computed from facts. The LLM number clustered when scored directly;
        # computing scale + breadth from real firmographics/lists restores per-company
        # spread. Operators only; gated roles score 0.
        if res.target_role == "operator_end_user":
            res.scale_readiness = self._compute_scale_readiness(company_data, profile)
            res.signal_breadth = self._compute_signal_breadth(res)
            res.operator_fit = (
                max(0, min(45, int(res.operational_overlap or 0)))
                + res.scale_readiness
                + res.signal_breadth
            )
        else:
            res.operational_overlap = 0
            res.scale_readiness = 0
            res.signal_breadth = 0
            res.operator_fit = 0

        # Location + size are validated deterministically against the CSV fields ONLY
        # (overriding the model, which wavered between the CSV field and headcounts/
        # locations mentioned in the research profile). Location uses the structured
        # country when present (cleaner than the free-text blob).
        lv, le = self._match_location(csv_country, target_loc)
        sv, se = self._match_size(csv_size, target_size)
        res.location_match = DimensionVerdict(verdict=lv, evidence=le)
        res.size_match = DimensionVerdict(verdict=sv, evidence=se)

        # Deterministic industry PASS from SIC/NAICS codes (precise, positive-only). When
        # the company's official code falls under a target industry, trust it over the
        # LLM's label-guessing — this is what makes "Textiles" (NAICS 313) correctly read
        # as Manufacturing instead of being hard-failed. Never forces a FAIL.
        matched_cat = self._deterministic_industry_pass(csv_sic, csv_naics, target_industry)
        if matched_cat:
            res.industry_match = DimensionVerdict(
                verdict="pass",
                evidence=(f"SIC/NAICS code ({csv_sic or csv_naics}) classifies this company under "
                          f"{matched_cat}, which falls within the target industry."),
            )

        # Industry-gate safety net: the model sometimes hard-FAILS a CSV industry that is
        # really a manufacturing sub-sector (e.g. "Textiles", "Industrial") while ALSO
        # classifying the company as a genuine operator that runs the operations the sender
        # serves. Those two judgments are contradictory: a clear different-supercategory
        # company could not be an operator_end_user for this sender. When they conflict,
        # trust the role read and soften the FAIL to UNKNOWN (which does not disqualify) so
        # a true manufacturer is not silently dropped on a mislabelled industry string.
        if res.industry_match.verdict == "fail" and res.target_role == "operator_end_user":
            logger.info(
                f"[ICP] Softening industry FAIL->UNKNOWN for {domain}: model classified it as "
                f"operator_end_user, so the CSV industry '{csv_industry}' is not a clear "
                f"different-supercategory mismatch."
            )
            res.industry_match = DimensionVerdict(
                verdict="unknown",
                evidence=(f"CSV industry '{csv_industry}' is ambiguous against the target, but the "
                          f"company operates as a genuine end-user in the sender's domain."),
            )

        result = self._decide(res, profile)
        if return_context:
            result["_eval_context"] = rendered[:9000]
        return result

    @staticmethod
    def _format_reasoning(m: "ICPMeddpiccJudgment", verdict: str, score: int,
                          threshold: int, firmo_fail: bool) -> str:
        """Assemble the user-facing decision rationale as clean, professional prose.

        Combines the LLM's analyst assessment (overall_reasoning) with a deterministic,
        plain-English statement of WHY the company was accepted/rejected, derived from
        the structured verdict — no debug syntax, scores-as-jargon, or field dumps. The
        raw scorecard lives in `meddpicc` (internal) for audit, never in this string."""
        base = (m.overall_reasoning or "").strip()
        if base and base[-1] not in ".!?":
            base += "."

        if verdict == "ACCEPT":
            note = "On balance the company qualifies for outreach as a genuine operational fit for the sender's offering."
            if m.has_evidenced_need and (m.need_evidence or "").strip().lower() not in ("", "none evidenced"):
                note += f" A relevant need is evidenced: {m.need_evidence.strip()}"
        else:
            if firmo_fail:
                fails = [
                    (label, dim) for label, dim in (
                        ("industry", m.industry_match),
                        ("location", m.location_match),
                        ("size", m.size_match),
                    ) if dim.verdict == "fail"
                ]
                detail = "; ".join(dim.evidence.strip() for _, dim in fails if (dim.evidence or '').strip())
                crit = ", ".join(label for label, _ in fails)
                note = f"It does not qualify: it falls outside the campaign's {crit} requirement."
                if detail:
                    note += f" {detail}"
            elif m.target_role == "solution_vendor_overlap":
                note = ("It does not qualify: the company appears to sell a solution that competes with the "
                        "sender's, rather than being a prospective customer.")
            elif m.target_role == "out_of_domain":
                note = ("It does not qualify: its operations fall outside the domain the sender's offering "
                        "serves.")
            else:
                note = ("It does not qualify: its operations are too weak a match for the sender's "
                        "target-customer profile to meet the qualification bar for this sender.")

        return f"{base} {note}".strip()

    def _decide(self, m: ICPMeddpiccJudgment, profile: dict) -> dict:
        """Programmatic, hallucination-free binary decision + enrichment payload.

        Faithful to the root MEDDPICC tool, with the campaign's firmographic filters
        kept as hard disqualifiers:
          * ACCEPT  — genuine operator whose score (need+precondition floor applied)
                      is >= the ICP threshold AND no firmographic fail.
          * REJECT  — everything else: a firmographic violation, a gated role
                      (out_of_domain / solution_vendor_overlap, which score 0), or a
                      sub-threshold operator.
        Statuses map: ACCEPT -> ACCEPTED (proceeds to stakeholder ranking + drafting),
        REJECT -> REJECTED. The research dossier is attached to both.
        """
        threshold = settings.ICP_ACCEPT_THRESHOLD
        ov = self._overall_score(m)  # int for operators, None for gated roles
        score = ov if ov is not None else 0

        firmo_fail = (
            m.industry_match.verdict == "fail"
            or m.location_match.verdict == "fail"
            or m.size_match.verdict == "fail"
        )

        # Binary verdict — no REVIEW band. A company is ACCEPTED iff it clears the
        # ICP threshold and has no explicit firmographic violation; everything else
        # is REJECTED. Gated roles (out_of_domain / solution_vendor_overlap) score 0
        # via _overall_score, so they fall below threshold and reject naturally.
        if not firmo_fail and score >= threshold:
            verdict = "ACCEPT"
        else:
            verdict = "REJECT"

        status = {"ACCEPT": "ACCEPTED", "REJECT": "REJECTED"}[verdict]

        reasoning = self._format_reasoning(m, verdict, score, threshold, firmo_fail)

        # Research fields come from the Call-1 profile (company facts); sender-relative
        # fields come from the Call-2 judgment.
        hooks = {
            "relevance_score": score,
            "reasoning": m.overall_reasoning,
            "business_opportunity_reason": m.business_opportunity_reason,
            "matched_pains": m.matched_pains,
            "matched_services": m.matched_services,
            "growth_hooks": profile.get("growth_hooks", []),
            "pain_hooks": profile.get("pain_hooks", []),
            "news_hooks": profile.get("news_hooks", []),
            "executive_summary": profile.get("executive_summary", ""),
            "research_profile": profile,
            # MEDDPICC scorecard (useful for the UI / human review of borderline leads).
            "meddpicc": m.model_dump(),
        }

        return {
            "status": status,
            "relevance_score": score,
            "reasoning": reasoning[:800],
            # Structured research profile (JSON) — this is what gets stored; raw web
            # text is never persisted.
            "icp_context": json.dumps(profile),
            # Enrichment fields (persisted for every verdict).
            "business_opportunity_reason": m.business_opportunity_reason,
            "matched_pains": m.matched_pains,
            "matched_services": m.matched_services,
            "growth_hooks": profile.get("growth_hooks", []),
            "pain_hooks": profile.get("pain_hooks", []),
            "news_hooks": profile.get("news_hooks", []),
            "research_summary": profile.get("executive_summary", ""),
            "hooks": hooks,
        }
