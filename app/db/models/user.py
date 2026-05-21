import uuid
import datetime
from datetime import UTC
import enum
from sqlalchemy import Column, String, Text, DateTime, ForeignKey, Integer, Boolean, Enum as SQLEnum, UniqueConstraint
from sqlalchemy.orm import relationship
from .base import Base

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
    role = Column(SQLEnum(UserRole, values_callable=lambda obj: [e.value for e in obj]), default=UserRole.USER, index=True)
    user_limit = Column(Integer, default=0) # Total users an admin can create
    created_by_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    
    is_demo = Column(Boolean, default=False)
    demo_expires_at = Column(DateTime, nullable=True)
    signup_source = Column(String, nullable=True) # demo, manual, organic
    has_used_trial_quota = Column(Boolean, default=False)
    created_at = Column(DateTime, default=lambda: datetime.datetime.now(UTC))

    # Business Identity Profile (used to personalize AI-generated email drafts)
    full_name = Column(String, nullable=True)
    company_name = Column(String, nullable=True)
    role_title = Column(String, nullable=True)

    # Cal.com OAuth & Scheduling Settings
    cal_access_token = Column(String, nullable=True)
    cal_refresh_token = Column(String, nullable=True)
    cal_token_expires_at = Column(DateTime, nullable=True)
    cal_event_type_id = Column(Integer, nullable=True)
    cal_timezone = Column(String, default="UTC")
    
    campaigns = relationship("Campaign", back_populates="owner", cascade="all, delete-orphan")
    oauth_accounts = relationship("OAuthAccount", back_populates="user", cascade="all, delete-orphan")
    
    # Self-referential relationship for administrative lineage
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
    access_token = Column(Text, nullable=True)
    token_expiry = Column(DateTime, nullable=True)
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
