from sqlalchemy import Column, String, Text, DateTime, ForeignKey, JSON, Integer, Boolean, Enum as SQLEnum, create_engine, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
import datetime
from datetime import UTC
import uuid
import enum

Base = declarative_base()

class UserRole(enum.Enum):
    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"
    USER = "user"

class User(Base):
    __tablename__ = "users"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    otp_code = Column(String, nullable=True)
    otp_expiry = Column(DateTime, nullable=True)
    provider = Column(String, nullable=True) # google, microsoft
    provider_user_id = Column(String, nullable=True)
    role = Column(SQLEnum(UserRole), default=UserRole.USER, index=True)
    user_limit = Column(Integer, default=0) # Total users an admin can create
    created_by_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    
    is_demo = Column(Boolean, default=False)
    demo_expires_at = Column(DateTime, nullable=True)
    signup_source = Column(String, nullable=True) # demo, manual, organic
    has_used_trial_quota = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(UTC))
    
    campaigns = relationship("Campaign", back_populates="owner", cascade="all, delete-orphan")
    oauth_accounts = relationship("OAuthAccount", back_populates="user", cascade="all, delete-orphan")
    
    # Self-referential relationship for administrative lineage (Cascade Delete Handled)
    creator = relationship("User", remote_side=[id], backref="managed_accounts")

class AdministrativeLog(Base):
    __tablename__ = "administrative_logs"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    actor_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"))
    target_id = Column(String, nullable=True)
    action = Column(String) # PROVISION, DECOMMISSION, QUOTA_CHANGE, REPLACE
    details = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(UTC))
    
    actor = relationship("User", foreign_keys=[actor_id])

class OAuthAccount(Base):
    __tablename__ = "oauth_accounts"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"))
    provider = Column(String)
    email_address = Column(String) # Email of the connected mailbox
    encrypted_refresh_token = Column(Text, nullable=False)
    mailbox_health_status = Column(String, default="UNKNOWN", index=True)
    mailbox_last_checked_at = Column(DateTime, nullable=True)
    mailbox_last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint('user_id', 'provider', name='_user_provider_oauth_uc'),
    )
    
    user = relationship("User", back_populates="oauth_accounts")

class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash = Column(String, unique=True, index=True)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(UTC))
    expires_at = Column(DateTime, index=True)
    is_revoked = Column(Boolean, default=False)
    
    user = relationship("User", backref="refresh_tokens")

class CampaignStatus(str, enum.Enum):
    PENDING = "PENDING"
    RESEARCHING_USER_COMPANY = "RESEARCHING_USER_COMPANY"
    FINDING_TARGET_COMPANIES = "FINDING_TARGET_COMPANIES"
    FINDING_DECISION_MAKERS = "FINDING_DECISION_MAKERS"
    DRAFTING_EMAILS = "DRAFTING_EMAILS"
    COMPLETED = "COMPLETED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS" # Some targets missed but others drafted
    INTERVENTION_NEEDED = "INTERVENTION_NEEDED" # Mission stalled due to data/retry limits
    FAILED = "FAILED"
    INACTIVE = "INACTIVE"

class ProspectState(str, enum.Enum):
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


class ProspectTerminationReason(str, enum.Enum):
    NEGATIVE_REPLY = "NEGATIVE_REPLY"
    NO_RESPONSE = "NO_RESPONSE"
    FOLLOWUP_EXHAUSTED = "FOLLOWUP_EXHAUSTED"
    DISCOVERY_TIMEOUT = "DISCOVERY_TIMEOUT"
    INVALID_SCHEDULE = "INVALID_SCHEDULE"
    INTERNAL_LEAD_SECURED = "INTERNAL_LEAD_SECURED"
    MANUAL = "MANUAL"

class Campaign(Base):
    __tablename__ = "campaigns"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=False)
    user_query = Column(Text, nullable=False)
    status = Column(SQLEnum(CampaignStatus), default=CampaignStatus.PENDING, index=True)
    status_reason = Column(Text, nullable=True) # Operational metadata for terminal states
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(UTC))
    
    # Target company parameters
    target_industry = Column(String, nullable=True)
    target_location = Column(String, nullable=True)
    target_employee_count = Column(String, nullable=True)
    
    # Operational Telemetry (Lease & Heartbeat System)
    # Used to prevent duplicate work during worker clusters or server restarts.
    last_heartbeat = Column(DateTime, nullable=True)
    locked_by = Column(String, nullable=True) # ID of the worker currently holding the lease
    
    # Relationships
    owner = relationship("User", back_populates="campaigns")
    user_intel = relationship("UserCompanyIntel", back_populates="campaign", uselist=False, cascade="all, delete-orphan")
    target_companies = relationship("TargetCompany", back_populates="campaign", cascade="all, delete-orphan")
    dms = relationship("DecisionMaker", back_populates="campaign", cascade="all, delete-orphan")
    drafts = relationship("EmailDraft", back_populates="campaign", cascade="all, delete-orphan")
    logs = relationship("CommunicationLog", back_populates="campaign", cascade="all, delete-orphan")
    transitions = relationship("ProspectLifecycleTransition", back_populates="campaign", cascade="all, delete-orphan")
    outbound_dispatches = relationship("OutboundDispatch", back_populates="campaign", cascade="all, delete-orphan")

class UserCompanyIntel(Base):
    __tablename__ = "user_company_intel"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id = Column(String, ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    company_name = Column(String, nullable=False)
    website = Column(String)
    motto = Column(Text)
    offerings = Column(Text)
    deep_research = Column(Text)
    
    campaign = relationship("Campaign", back_populates="user_intel")

class TargetCompany(Base):
    __tablename__ = "target_companies"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id = Column(String, ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    name = Column(String, nullable=False)
    website = Column(String)
    domain = Column(String, nullable=True)
    identity_key = Column(String, nullable=True, index=True)
    linkedin = Column(String)
    location = Column(String)
    company_type = Column(String, nullable=True) # Specific sub-vertical (e.g., 'Precision Machining')
    employee_count = Column(String, nullable=True) # Headcount data from research
    contact_email = Column(String)
    contact_number = Column(String)
    deep_research = Column(Text)
    relevance_score = Column(Integer, default=0, index=True) # Anchored numeric quality index
    relevance_explanation = Column(Text, nullable=True) # Strategic reasoning for the score
    rejection_reason = Column(Text, nullable=True)
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
    email = Column(String)
    is_email_verified = Column(Boolean, default=False) # Distinguish predicted vs verified coordinates
    reply_intent = Column(String, nullable=True) # POSITIVE, NEUTRAL, NEGATIVE
    linkedin = Column(String)
    relevance_score = Column(Integer, default=0, index=True) # Operational lead quality score
    relevance_explanation = Column(Text, nullable=True) # Agentic reasoning for coordinate selection
    hubspot_id = Column(String, nullable=True)
    status = Column(String, default="NEW") # Legacy string status for compatibility
    state = Column(SQLEnum(ProspectState), default=ProspectState.NEW, index=True)
    termination_reason = Column(SQLEnum(ProspectTerminationReason), nullable=True, index=True)
    
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
    pre_hold_state = Column(SQLEnum(ProspectState), nullable=True)
    pre_hold_status = Column(String, nullable=True)
    pre_hold_next_action_at = Column(DateTime, nullable=True)

    last_message_id = Column(String, nullable=True)
    thread_id = Column(String, nullable=True, index=True)
    
    # Meeting & Reminder Synchronization
    scheduled_time_utc = Column(DateTime, nullable=True) # Canonical UTC coordinate
    display_timezone = Column(String, default="UTC") # Prospect's local timezone
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

class EmailDraft(Base):
    __tablename__ = "email_drafts"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id = Column(String, ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    decision_maker_id = Column(String, ForeignKey("decision_makers.id", ondelete="CASCADE"), index=True)
    subject = Column(String)
    body = Column(Text)
    status = Column(String, default="DRAFTED", index=True) # DRAFTED, APPROVED, SENT
    is_approved = Column(Boolean, default=False)
    message_id = Column(String, nullable=True) # To track once sent
    sent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(UTC))
    followup_index = Column(Integer, default=0) # 0 for initial, 1-11 for follow-ups
    draft_type = Column(String, default="INITIAL") # INITIAL, FOLLOWUP, REMINDER, DISCOVERY
    dispatch_state = Column(String, default="IDLE", index=True) # IDLE, QUEUED, IN_PROGRESS, SENT, FAILED, REQUIRES_REVIEW
    dispatch_started_at = Column(DateTime, nullable=True)
    dispatch_completed_at = Column(DateTime, nullable=True)
    dispatch_error = Column(Text, nullable=True)
    
    __table_args__ = (
        UniqueConstraint('decision_maker_id', 'followup_index', name='_dm_followup_uc'),
    )
    
    campaign = relationship("Campaign", back_populates="drafts")
    dm = relationship("DecisionMaker", back_populates="drafts")


class OutboundDispatch(Base):
    __tablename__ = "outbound_dispatches"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id = Column(String, ForeignKey("campaigns.id", ondelete="CASCADE"), index=True, nullable=False)
    dm_id = Column(String, ForeignKey("decision_makers.id", ondelete="CASCADE"), index=True, nullable=False)
    draft_id = Column(String, ForeignKey("email_drafts.id", ondelete="SET NULL"), index=True, nullable=True)
    action_type = Column(String, nullable=False, index=True)
    dispatch_key = Column(String, nullable=False, index=True)
    state = Column(String, default="IDLE", index=True)  # IDLE, IN_PROGRESS, SENT, FAILED, REQUIRES_REVIEW
    message_id = Column(String, nullable=True)
    thread_id = Column(String, nullable=True)
    dispatch_started_at = Column(DateTime, nullable=True)
    dispatch_completed_at = Column(DateTime, nullable=True)
    dispatch_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(UTC), index=True)

    __table_args__ = (
        UniqueConstraint("dispatch_key", name="_outbound_dispatch_key_uc"),
    )

    campaign = relationship("Campaign", back_populates="outbound_dispatches")
    dm = relationship("DecisionMaker", back_populates="outbound_dispatches")

class CommunicationLog(Base):
    __tablename__ = "communication_logs"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id = Column(String, ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    dm_id = Column(String, ForeignKey("decision_makers.id", ondelete="CASCADE"), index=True)
    direction = Column(String, index=True) # SENT, RECEIVED
    subject = Column(String)
    body = Column(Text)
    message_id = Column(String) # For threading match
    received_at = Column(DateTime, default=lambda: datetime.datetime.now(UTC))

    __table_args__ = (
        UniqueConstraint('message_id', 'direction', name='_communication_message_direction_uc'),
    )
    
    dm = relationship("DecisionMaker", back_populates="logs")
    campaign = relationship("Campaign", back_populates="logs")


class ProspectLifecycleTransition(Base):
    __tablename__ = "prospect_lifecycle_transitions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    campaign_id = Column(String, ForeignKey("campaigns.id", ondelete="CASCADE"), index=True, nullable=False)
    dm_id = Column(String, ForeignKey("decision_makers.id", ondelete="CASCADE"), index=True, nullable=False)
    from_state = Column(SQLEnum(ProspectState), nullable=True)
    to_state = Column(SQLEnum(ProspectState), nullable=True, index=True)
    from_status = Column(String, nullable=True)
    to_status = Column(String, nullable=True)
    reason = Column(String, nullable=True, index=True)
    actor = Column(String, nullable=False, default="system")
    transition_metadata = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(UTC), index=True)

    campaign = relationship("Campaign", back_populates="transitions")
    dm = relationship("DecisionMaker", back_populates="transitions")
