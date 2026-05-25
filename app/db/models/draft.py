import uuid
import datetime
from datetime import UTC
from sqlalchemy import Column, String, Text, DateTime, ForeignKey, JSON, Integer, Boolean, Enum as SQLEnum, UniqueConstraint
from sqlalchemy.orm import relationship
from .base import Base
from .prospect import ProspectState

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
    scheduled_at = Column(DateTime, nullable=True, index=True)
    dispatch_started_at = Column(DateTime, nullable=True)
    dispatch_completed_at = Column(DateTime, nullable=True)
    dispatch_error = Column(Text, nullable=True)

    # V2 Intelligence Upgrades
    variants = Column(JSON, nullable=True) # Stores the 3 high-impact variants
    strategic_observation = Column(Text, nullable=True)
    pain_hypothesis = Column(Text, nullable=True)
    personalization_hook = Column(Text, nullable=True)
    
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
    state = Column(String, default="IDLE", index=True)  # IDLE, QUEUED, IN_PROGRESS, SENT, FAILED, REQUIRES_REVIEW
    message_id = Column(String, nullable=True)
    thread_id = Column(String, nullable=True)
    scheduled_at = Column(DateTime, nullable=True, index=True)
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
    from_state = Column(SQLEnum(ProspectState, values_callable=lambda obj: [e.value for e in obj]), nullable=True)
    to_state = Column(SQLEnum(ProspectState, values_callable=lambda obj: [e.value for e in obj]), nullable=True, index=True)
    from_status = Column(String, nullable=True)
    to_status = Column(String, nullable=True)
    reason = Column(String, nullable=True, index=True)
    actor = Column(String, nullable=False, default="system")
    transition_metadata = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(UTC), index=True)

    campaign = relationship("Campaign", back_populates="transitions")
    dm = relationship("DecisionMaker", back_populates="transitions")
