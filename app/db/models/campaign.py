import uuid
import datetime
from datetime import UTC
import enum
from sqlalchemy import Column, String, Text, DateTime, ForeignKey, JSON, Integer, Enum as SQLEnum
from sqlalchemy.orm import relationship, deferred
from .base import Base

class CampaignStatus(enum.Enum):
    INPUT_VALIDATED = "INPUT_VALIDATED"
    PENDING = "PENDING"
    
    # NEW Granular Intelligence Stages (SSoT-First)
    STAGE_1_CSV_TRIMMED = "STAGE_1_CSV_TRIMMED"
    STAGE_2_USER_INTEL_COMPLETE = "STAGE_2_USER_INTEL_COMPLETE"
    STAGE_3_ICP_FILTERED = "STAGE_3_ICP_FILTERED"
    STAGE_4_RESEARCH_COMPLETE = "STAGE_4_RESEARCH_COMPLETE"
    STAGE_5_STAKEHOLDERS_RANKED = "STAGE_5_STAKEHOLDERS_RANKED"
    STAGE_6_DRAFTING_COMPLETE = "STAGE_6_DRAFTING_COMPLETE"

    # Legacy / High-Level Statuses
    RESEARCHING_USER_COMPANY = "RESEARCHING_USER_COMPANY"
    FINDING_TARGET_COMPANIES = "FINDING_TARGET_COMPANIES"
    FINDING_DECISION_MAKERS = "FINDING_DECISION_MAKERS"
    DRAFTING_EMAILS = "DRAFTING_EMAILS"
    COMPLETED = "COMPLETED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS" 
    INTERVENTION_NEEDED = "INTERVENTION_NEEDED" 
    FAILED = "FAILED"
    INACTIVE = "INACTIVE"

class Campaign(Base):
    __tablename__ = "campaigns"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=False)
    prompt = Column(Text, nullable=True)
    input_validation_review = Column(JSON, nullable=True)

    status = Column(SQLEnum(CampaignStatus, values_callable=lambda obj: [e.value for e in obj]), default=CampaignStatus.INPUT_VALIDATED, index=True)
    status_reason = Column(Text, nullable=True) # Operational metadata for terminal states
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(UTC))
    
    # Target company parameters
    target_industry = Column(String, nullable=True)
    target_location = Column(String, nullable=True)
    # SSoT: trimmed batch CSV stored as text. deferred() so it is loaded ONLY when
    # explicitly accessed (the CSV workers) — routine Campaign queries (list, detail,
    # state machine) no longer drag this multi-row blob into memory.
    trimmed_csv_data = deferred(Column(Text, nullable=True))
    target_employee_count = Column(String, nullable=True)
    # Retained (nullable) for backward compatibility / existing rows. No longer
    # written: the trimmed CSV lives only in trimmed_csv_data (DB SSoT), never on
    # local disk.
    csv_file_url = Column(String, nullable=True)
    
    # Operational Telemetry (Lease & Heartbeat System)
    last_heartbeat = Column(DateTime, nullable=True)
    locked_by = Column(String, nullable=True) # ID of the worker currently holding the lease

    # LangGraph workflow engine (Phase 1). workflow_version pins a running
    # campaign to a workflow definition (workflow-versioning constraint);
    # workflow_thread_id is the checkpointer thread key (defaults to id).
    workflow_version = Column(Integer, nullable=False, default=1)
    workflow_thread_id = Column(String, nullable=True)
    
    # Relationships
    owner = relationship("User", back_populates="campaigns")
    user_intel = relationship("UserCompanyIntel", back_populates="campaign", uselist=False, cascade="all, delete-orphan")
    leads = relationship("CampaignLead", back_populates="campaign", cascade="all, delete-orphan")
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
    
    # Enriched Dossier Cluster (The Brand DNA)
    target_customers = Column(JSON, nullable=True)
    competitive_advantages = Column(JSON, nullable=True)
    proof_points = Column(JSON, nullable=True)
    capability_to_pain_map = Column(JSON, nullable=True)
    
    v2_intel = Column(JSON, nullable=True)
    
    campaign = relationship("Campaign", back_populates="user_intel")
