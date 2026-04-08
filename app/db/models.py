from sqlalchemy import Column, String, Text, DateTime, ForeignKey, JSON, Integer, Boolean, Enum as SQLEnum, create_engine
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
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(UTC))
    
    user = relationship("User", back_populates="oauth_accounts")

class CampaignStatus(str, enum.Enum):
    PENDING = "PENDING"
    RESEARCHING_USER_COMPANY = "RESEARCHING_USER_COMPANY"
    FINDING_TARGET_COMPANIES = "FINDING_TARGET_COMPANIES"
    FINDING_DECISION_MAKERS = "FINDING_DECISION_MAKERS"
    DRAFTING_EMAILS = "DRAFTING_EMAILS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INACTIVE = "INACTIVE"

class Campaign(Base):
    __tablename__ = "campaigns"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=False)
    user_query = Column(Text, nullable=False)
    status = Column(SQLEnum(CampaignStatus), default=CampaignStatus.PENDING, index=True)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(UTC))
    
    # Target company parameters
    target_industry = Column(String, nullable=True)
    target_location = Column(String, nullable=True)
    target_employee_count = Column(String, nullable=True)
    
    # Relationships
    owner = relationship("User", back_populates="campaigns")
    user_intel = relationship("UserCompanyIntel", back_populates="campaign", uselist=False, cascade="all, delete-orphan")
    target_companies = relationship("TargetCompany", back_populates="campaign", cascade="all, delete-orphan")
    dms = relationship("DecisionMaker", back_populates="campaign", cascade="all, delete-orphan")
    drafts = relationship("EmailDraft", back_populates="campaign", cascade="all, delete-orphan")
    logs = relationship("CommunicationLog", back_populates="campaign", cascade="all, delete-orphan")

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
    linkedin = Column(String)
    location = Column(String)
    company_type = Column(String, nullable=True) # Specific sub-vertical (e.g., 'Precision Machining')
    employee_count = Column(String, nullable=True) # Headcount data from research
    contact_email = Column(String)
    contact_number = Column(String)
    deep_research = Column(Text)
    similarity_score = Column(JSON) # Store score and reasoning
    rejection_reason = Column(Text, nullable=True)
    status = Column(String, default="ACTIVE", index=True) # NEW, ACTIVE, DISCOVERY_CALL, TERMINATED, REJECTED
    
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
    reply_intent = Column(String, nullable=True) # POSITIVE, NEUTRAL, NEGATIVE
    linkedin = Column(String)
    similarity_score = Column(JSON) # Store score and reasoning
    hubspot_id = Column(String, nullable=True)
    status = Column(String, default="NEW", index=True) # NEW, SYNCED, DRAFTED, INITIAL_SENT, FOLLOWUP_X_SENT, DISCOVERY_CALL, TERMINATED
    followup_count = Column(Integer, default=0)
    last_message_id = Column(String, nullable=True) # ID of the last email sent
    thread_id = Column(String, nullable=True) # References/In-Reply-To header
    
    # Scheduling Fields (Discovery Subsystem)
    meeting_link = Column(String, nullable=True)
    scheduled_time = Column(DateTime, nullable=True)
    timezone = Column(String, nullable=True)
    scheduling_note = Column(Text, nullable=True) # Reason for slot shift or conflict resolution
    reminder_24h_sent = Column(Boolean, default=False)
    reminder_1h_sent = Column(Boolean, default=False)
    
    campaign = relationship("Campaign", back_populates="dms")
    target_company = relationship("TargetCompany", back_populates="dms")
    drafts = relationship("EmailDraft", back_populates="dm", cascade="all, delete-orphan")
    logs = relationship("CommunicationLog", back_populates="dm", cascade="all, delete-orphan")

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
    
    campaign = relationship("Campaign", back_populates="drafts")
    dm = relationship("DecisionMaker", back_populates="drafts")

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
    
    dm = relationship("DecisionMaker", back_populates="logs")
    campaign = relationship("Campaign", back_populates="logs")
