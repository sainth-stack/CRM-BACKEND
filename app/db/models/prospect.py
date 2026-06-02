import uuid
import datetime
from datetime import UTC
import enum
from sqlalchemy import Column, String, Text, DateTime, ForeignKey, JSON, Integer, Boolean, Enum as SQLEnum, UniqueConstraint
from sqlalchemy.orm import relationship
from .base import Base

class ProspectState(enum.Enum):
    NEW = "NEW"
    DRAFTED = "DRAFTED"
    INITIAL_SENT = "INITIAL_SENT"
    WAITING_FOR_REPLY = "WAITING_FOR_REPLY"
    DISCOVERY_EXPIRED = "DISCOVERY_EXPIRED"
    REMINDER_1_SENT = "REMINDER_1_SENT"
    REMINDER_2_SENT = "REMINDER_2_SENT"
    FOLLOWUP_ACTIVE = "FOLLOWUP_ACTIVE"
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NEUTRAL = "NEUTRAL"
    DISCOVERY_CALL = "DISCOVERY_CALL"
    MEETING_BOOKED = "MEETING_BOOKED"
    ON_HOLD = "ON_HOLD"
    TERMINATED = "TERMINATED"

class ProspectTerminationReason(enum.Enum):
    NEGATIVE_REPLY = "NEGATIVE_REPLY"
    NO_RESPONSE = "NO_RESPONSE"
    FOLLOWUP_EXHAUSTED = "FOLLOWUP_EXHAUSTED"
    DISCOVERY_TIMEOUT = "DISCOVERY_TIMEOUT"
    INVALID_SCHEDULE = "INVALID_SCHEDULE"
    INTERNAL_LEAD_SECURED = "INTERNAL_LEAD_SECURED"
    MANUAL = "MANUAL"

class TargetCompany(Base):
    __tablename__ = "target_companies"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id = Column(String, ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    name = Column(String, nullable=False)
    website = Column(String)
    domain = Column(String, nullable=True)
    identity_key = Column(String, nullable=True, index=True)
    linkedin = Column(String)
    linkedin_id = Column(String, nullable=True)
    location = Column(String)
    website = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    company_type = Column(String, nullable=True) # Specific sub-vertical (e.g., 'Precision Machining')
    employee_count = Column(String, nullable=True) # Headcount data from research
    revenue_range = Column(String, nullable=True)
    contact_email = Column(String)
    contact_number = Column(String)
    deep_research = Column(Text)
    v2_intel = Column(JSON, nullable=True)
    relevance_score = Column(Integer, default=0, index=True) # Anchored numeric quality index
    relevance_explanation = Column(Text, nullable=True) # Strategic reasoning for the score
    rejection_reason = Column(Text, nullable=True)
    matched_pains = Column(JSON, nullable=True)
    matched_services = Column(JSON, nullable=True)
    opportunity_reason = Column(Text, nullable=True)
    growth_hooks = Column(JSON, nullable=True)
    pain_hooks = Column(JSON, nullable=True)
    news_hooks = Column(JSON, nullable=True)
    research_summary = Column(Text, nullable=True)
    icp_research_context = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.datetime.now(datetime.timezone.utc), onupdate=lambda: datetime.datetime.now(datetime.timezone.utc))

    status = Column(String, default="ACTIVE", index=True) # NEW, ACTIVE, DISCOVERY_CALL, TERMINATED, REJECTED
    
    __table_args__ = (
        UniqueConstraint('campaign_id', 'identity_key', name='_campaign_identity_uc'),
        UniqueConstraint('campaign_id', 'domain', name='_campaign_domain_uc'),
    )
    
    campaign = relationship("Campaign", back_populates="target_companies")
    dms = relationship("DecisionMaker", back_populates="target_company", cascade="all, delete-orphan")

class DecisionMaker(Base):
    __tablename__ = "decision_makers"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id = Column(String, ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    target_company_id = Column(String, ForeignKey("target_companies.id", ondelete="CASCADE"), index=True)
    name = Column(String, nullable=False)
    position = Column(String)
    seniority = Column(String, nullable=True)
    location = Column(String, nullable=True)
    email = Column(String)
    phone = Column(String, nullable=True)
    company_phone = Column(String, nullable=True)
    is_email_verified = Column(Boolean, default=False) # Distinguish predicted vs verified coordinates
    reply_intent = Column(String, nullable=True) # POSITIVE, NEUTRAL, NEGATIVE
    linkedin = Column(String)
    time_in_role = Column(String, nullable=True)
    time_at_company = Column(String, nullable=True)
    relevance_score = Column(Integer, default=0, index=True) # Operational lead quality score
    relevance_explanation = Column(Text, nullable=True) # Agentic reasoning for coordinate selection
    hubspot_id = Column(String, nullable=True)
    status = Column(String, default="NEW") # Legacy string status for compatibility
    state = Column(SQLEnum(ProspectState, values_callable=lambda obj: [e.value for e in obj]), default=ProspectState.NEW, index=True)
    termination_reason = Column(SQLEnum(ProspectTerminationReason, values_callable=lambda obj: [e.value for e in obj]), nullable=True, index=True)
    
    # Temporal & Behavioral Engine Coordinates
    last_sent_at = Column(DateTime, nullable=True)
    last_reply_at = Column(DateTime, nullable=True)
    next_action_at = Column(DateTime, nullable=True, index=True) # Key for Orchestrator dispatch
    retry_after = Column(DateTime, nullable=True, index=True) # For 3-month reactivation
    
    reminder_count = Column(Integer, default=0) # 0-2
    followup_count = Column(Integer, default=0) # 0-11
    intent_last = Column(String, nullable=True)

    # Hold/Resume Controls for company-level routing
    hold_source_dm_id = Column(String, nullable=True, index=True)
    held_at = Column(DateTime, nullable=True)
    hold_release_at = Column(DateTime, nullable=True, index=True)
    pre_hold_state = Column(SQLEnum(ProspectState, values_callable=lambda obj: [e.value for e in obj]), nullable=True)
    pre_hold_status = Column(String, nullable=True)
    pre_hold_next_action_at = Column(DateTime, nullable=True)

    last_message_id = Column(String, nullable=True)
    thread_id = Column(String, nullable=True, index=True)
    
    # Meeting & Reminder Synchronization
    scheduled_time_utc = Column(DateTime, nullable=True) # Canonical UTC coordinate
    display_timezone = Column(String, nullable=True, default=None) # NULL = not resolved yet; set by scheduler on first dispatch
    meeting_link = Column(String, nullable=True)
    scheduling_note = Column(Text, nullable=True)
    reminder_24h_sent = Column(Boolean, default=False)
    reminder_1h_sent = Column(Boolean, default=False)
    
    # Audit & Recovery
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(UTC))
    
    __table_args__ = (
        UniqueConstraint('target_company_id', 'name', name='_company_dm_name_uc'),
        UniqueConstraint('campaign_id', 'email', name='_campaign_dm_email_uc'),
    )
    
    campaign = relationship("Campaign", back_populates="dms")
    target_company = relationship("TargetCompany", back_populates="dms")
    drafts = relationship("EmailDraft", back_populates="dm", cascade="all, delete-orphan")
    logs = relationship("CommunicationLog", back_populates="dm", cascade="all, delete-orphan")
    transitions = relationship("ProspectLifecycleTransition", back_populates="dm", cascade="all, delete-orphan")
    outbound_dispatches = relationship("OutboundDispatch", back_populates="dm", cascade="all, delete-orphan")
