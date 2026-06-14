import os
from fastapi import APIRouter, Depends, HTTPException, status, Body, Request, Query
from sqlalchemy.orm import Session
from sqlalchemy.orm import aliased
from typing import Dict, Any
from pydantic import BaseModel, EmailStr, Field, field_validator
import datetime
from datetime import UTC, timedelta
import random
from app.db import models
from app.db.database import get_db
from app.core.security import (
    create_access_token, 
    create_refresh_token, 
    get_current_user, 
    decrypt_token,
    encrypt_token, 
    verify_password, 
    get_password_hash,
    revoke_sessions,
    hash_token,
    REFRESH_TOKEN_EXPIRE_DAYS
)
from app.core.token_service import TokenService
from app.core.email_service import email_service
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from app.core.logging_config import logger

router = APIRouter(prefix="/auth", tags=["Authentication"])

# Module-level limiter (shares state with main app via Cloud Redis)
limiter = Limiter(key_func=get_remote_address, storage_uri=os.getenv("REDIS_URL"))


def _lock_query(query):
    bind = query.session.bind
    if bind is not None and bind.dialect.name != "sqlite":
        return query.with_for_update()
    return query

# Resolve FRONTEND_URL dynamically to avoid hardcoded localhost in email setup links
FRONTEND_URL = os.getenv("FRONTEND_URL")
if not FRONTEND_URL:
    env_origins = os.getenv("ALLOWED_ORIGINS")
    if env_origins:
        first_origin = env_origins.split(",")[0].strip().strip('"').strip("'")
        if first_origin:
            FRONTEND_URL = first_origin
if not FRONTEND_URL:
    FRONTEND_URL = "http://localhost:5173"

def _client_ip(request: Request) -> str | None:
    """Resolve client IP, honoring the proxy chain (Render terminates TLS upstream)."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def record_login_session(db: Session, request: Request, user: "models.User") -> None:
    """
    Login Audit Anchor.
    Writes a LoginSession row capturing IP + device for the admin audit trail.
    Best-effort: a failure here must never block authentication.
    """
    try:
        session = models.LoginSession(
            user_id=user.id,
            user_email=user.email,
            org_id=user.organization_id,
            ip_address=_client_ip(request),
            device_info=request.headers.get("user-agent"),
            login_time=datetime.datetime.now(UTC),
            status="active",
        )
        db.add(session)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"[AUDIT] Failed to record login session for {user.email}: {e}")


def build_identity_context(db: Session, user: "models.User") -> dict:
    """Resolve the tenant / organization / permission context for a user."""
    from app.core.rbac import get_user_permissions
    org = db.query(models.Organization).filter(models.Organization.id == user.organization_id).first() if user.organization_id else None
    tenant = db.query(models.Tenant).filter(models.Tenant.id == user.tenant_id).first() if user.tenant_id else None
    return {
        "tenant": {
            "tenant_id": tenant.id, "tenant_name": tenant.name,
            "tenant_type": tenant.type, "tenant_timeout": tenant.timeout,
        } if tenant else None,
        "organization": {
            "organization_id": org.id, "organization_name": org.name,
            "logo_url": org.logo_url,
        } if org else None,
        "permissions": get_user_permissions(db, user),
    }


def issue_refresh_token(db: Session, user_id: str) -> str:
    """Generate a JWT refresh token and persist its SHA-256 hash in the database."""
    token = create_refresh_token(data={"sub": user_id})
    expires_at = datetime.datetime.now(UTC) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    
    db_token = models.RefreshToken(
        user_id=user_id,
        token_hash=hash_token(token),
        expires_at=expires_at
    )
    db.add(db_token)
    db.commit()
    return token

class LoginRequest(BaseModel):
    email: str
    password: str

class RefreshRequest(BaseModel):
    refresh_token: str

class VerifyOTPRequest(BaseModel):
    email: str
    otp: str

@router.post("/login")
@limiter.limit("10/minute")  # rate limit: 10 attempts/minute per IP
def login(request: Request, credentials: LoginRequest = Body(...), db: Session = Depends(get_db)):
    """Email/password sign-in. Verifies credentials and issues a JWT session."""
    user = db.query(models.User).filter(models.User.email == credentials.email).first()
    if not user or not user.hashed_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    if not verify_password(credentials.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Activation Gate: an administrator may disable an identity without deleting it.
    if user.is_active is False:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been deactivated. Please contact your administrator.",
        )

    # Issue the stateless access token (JWT) + refresh token.
    access_token = create_access_token(data={
        "sub": user.id,
        "role": user.role.value
    })
    refresh_token = issue_refresh_token(db, user.id)

    # Login audit + multi-tenant identity context
    record_login_session(db, request, user)
    identity = build_identity_context(db, user)

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "email": user.email,
            "role": user.role.value,
            "is_demo": user.is_demo,
            "demo_expires_at": user.demo_expires_at.isoformat() if user.demo_expires_at else None,
            "user_limit": user.user_limit,
            "provider": user.provider,
            "is_active": user.is_active,
            "tenant": identity["tenant"],
            "organization": identity["organization"],
            "permissions": identity["permissions"],
        }
    }

@router.post("/demo/signup")
@limiter.limit("5/minute")   # rate limit: 5 demo signups/minute per IP
def demo_signup(request: Request, credentials: LoginRequest = Body(...), db: Session = Depends(get_db)):
    """Create a temporary 5-day trial account and email a verification code (OTP)."""
    existing_user = db.query(models.User).filter(models.User.email == credentials.email).first()
    if existing_user:
        # Return a generic success message so the response can't be used to tell
        # whether an email is already registered (prevents account enumeration).
        return {"message": "If this is a new account, check your email for the verification code."}
    
    # Generate 6-digit cryptographically secure OTP for initial verification
    import secrets
    otp = str(secrets.randbelow(900000) + 100000)

    user = models.User(
        email=credentials.email,
        hashed_password=get_password_hash(credentials.password),
        is_demo=True,
        signup_source="demo",
        otp_code=get_password_hash(otp),
        otp_expiry=datetime.datetime.now(UTC) + timedelta(minutes=15)
    )
    db.add(user)
    db.commit()
    
    try:
        email_service.send_verification_email(user.email, otp)
    except Exception as e:
        logger.error(f"[DEMO] Verification email failed to send: {e}")
        # Non-fatal: the account is created; the user can request the code again.

    return {"message": "Verification code sent. Please check your email."}

@router.post("/demo/verify")
def verify_demo_otp(request: Request, payload: VerifyOTPRequest, db: Session = Depends(get_db)):
    """Validate the OTP and activate the account's 5-day trial window."""
    user = db.query(models.User).filter(models.User.email == payload.email).first()
    if not user or not user.otp_code or not verify_password(payload.otp, user.otp_code):
        raise HTTPException(status_code=400, detail="Invalid verification code.")
    
    if not user.otp_expiry or user.otp_expiry.replace(tzinfo=UTC) < datetime.datetime.now(UTC):
        raise HTTPException(status_code=400, detail="Verification code has expired.")
    
    # Start the 5-day trial window.
    user.demo_expires_at = datetime.datetime.now(UTC) + timedelta(days=5)
    user.otp_code = None
    user.otp_expiry = None
    db.commit()

    # Issue the initial session tokens.
    access_token = create_access_token(data={
        "sub": user.id, 
        "role": user.role.value
    })
    refresh_token = issue_refresh_token(db, user.id)
    
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "email": user.email,
            "role": user.role.value,
            "is_demo": user.is_demo,
            "demo_expires_at": user.demo_expires_at,
            "user_limit": user.user_limit
        }
    }



class ForgotPasswordRequest(BaseModel):
    email: str

@router.post("/forgot-password")
@limiter.limit("5/minute")   # Enumeration shield: 5 reset requests/minute per IP
def forgot_password(request: Request, payload: ForgotPasswordRequest = Body(...), db: Session = Depends(get_db)):
    """Email a 6-digit verification code to authorize a password reset."""
    user = db.query(models.User).filter(models.User.email == payload.email).first()
    if not user:
        # Return success even if the account doesn't exist (prevents enumeration).
        return {"message": "If this email is registered, a verification code has been sent to it."}

    # Accounts that were provisioned but never activated cannot self-serve recovery.
    if not user.hashed_password:
        raise HTTPException(
            status_code=400,
            detail="Account not yet activated. Please use the setup link from your welcome email or contact your administrator."
        )

    # Generate a 6-digit cryptographically secure OTP.
    import secrets
    otp = str(secrets.randbelow(900000) + 100000)
    user.otp_code = get_password_hash(otp)
    user.otp_expiry = datetime.datetime.now(UTC) + timedelta(minutes=10)
    db.commit()

    try:
        email_service.send_verification_email(user.email, otp)
    except Exception as e:
        logger.error(f"[AUTH] Verification email failed to send: {e}")
        raise HTTPException(status_code=500, detail="Failed to send verification email.")

    return {"message": "A verification code has been sent to your email."}

@router.post("/verify-otp")
@limiter.limit("10/minute")
def verify_otp(request: Request, payload: VerifyOTPRequest, db: Session = Depends(get_db)):
    """Validate the verification code and issue a short-lived password-reset token."""
    user = db.query(models.User).filter(models.User.email == payload.email).first()
    if not user or not user.otp_code or not verify_password(payload.otp, user.otp_code):
        raise HTTPException(status_code=400, detail="Invalid verification code.")

    # Time-based validation
    if not user.otp_expiry or user.otp_expiry.replace(tzinfo=UTC) < datetime.datetime.now(UTC):
        raise HTTPException(status_code=400, detail="Verification code has expired.")

    # Short-lived token that authorizes the reset-password endpoint (prevents
    # calling it directly without first proving the OTP).
    reset_token = create_access_token(data={"sub": user.id, "purpose": "password_reset"}, expires_delta=timedelta(minutes=15))
    
    return {"reset_token": reset_token}

class ResetPasswordRequest(BaseModel):
    new_password: str
    confirm_password: str
    reset_token: str

@router.post("/reset-password")
@limiter.limit("10/minute")
def reset_password(request: Request, payload: ResetPasswordRequest, db: Session = Depends(get_db)):
    """Set a new password and revoke all of the user's existing sessions."""
    if payload.new_password != payload.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match.")

    try:
        from app.core.security import SECRET_KEY, ALGORITHM, _get_revocation_time
        import jwt
        decoded_payload = jwt.decode(payload.reset_token, SECRET_KEY, algorithms=[ALGORITHM])
        purpose = decoded_payload.get("purpose")
        if purpose not in ["password_reset", "account_onboarding"]:
            raise HTTPException(status_code=401, detail="Unauthorized reset session.")
        
        user_id = decoded_payload.get("sub")
        iat = decoded_payload.get("iat")

        # Distributed revocation check: reject tokens issued before a global logout.
        if iat is not None:
            revocation_time = _get_revocation_time(user_id)
            if revocation_time and iat < revocation_time:
                raise HTTPException(status_code=401, detail="Security link has expired or been used.")
                
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Reset session expired or invalid.")

    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Account not found.")

    # Onboarding links are single-use: reject if the account is already activated.
    if purpose == "account_onboarding" and user.hashed_password:
        raise HTTPException(status_code=400, detail="This activation link has already been used.")

    # Prepare secure hashed credentials. We only commit after session revocation
    # succeeds so the endpoint cannot return failure after persisting the password.
    user.hashed_password = get_password_hash(payload.new_password)
    user.otp_code = None # Invalidate OTP after success
    user.otp_expiry = None
    db.flush()
    
    # Revoke all of the user's existing sessions (JWTs) globally.
    revoke_sessions(user.id)
    db.commit()

    return {"message": "Password updated. You can now sign in."}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str

@router.post("/change-password")
@limiter.limit("5/minute")
def change_password(
    request: Request,
    payload: ChangePasswordRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Let an authenticated user change their own password."""
    if payload.new_password != payload.confirm_password:
        raise HTTPException(status_code=400, detail="New passwords do not match.")
        
    if not current_user.hashed_password:
        raise HTTPException(
            status_code=400,
            detail="Current credentials invalid. Please contact administrator or use password recovery."
        )
        
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect current password.")

    current_user.hashed_password = get_password_hash(payload.new_password)
    db.commit()
    return {"message": "Password updated successfully."}


@router.post("/refresh")
@limiter.limit("15/minute")
def refresh_token(request: Request, payload: RefreshRequest, db: Session = Depends(get_db)):
    """Exchange a valid refresh token for a new access token (with rotation)."""
    try:
        from app.core.security import SECRET_KEY, ALGORITHM
        import jwt
        decoded_payload = jwt.decode(payload.refresh_token, SECRET_KEY, algorithms=[ALGORITHM])
        
        if decoded_payload.get("type") != "refresh":
            raise HTTPException(status_code=401, detail="Invalid token type.")
            
        user_id = decoded_payload.get("sub")
        iat = decoded_payload.get("iat")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token payload.")
            
        # Distributed revocation check so refresh tokens also honor global logouts.
        if iat is not None:
            from app.core.security import _get_revocation_time
            revocation_time = _get_revocation_time(user_id)
            if revocation_time and iat < revocation_time:
                raise HTTPException(status_code=401, detail="Refresh session has been revoked.")

        user = db.query(models.User).filter(models.User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=401, detail="Account no longer exists.")

        # Verify the token hash still exists and is active in the DB. This protects
        # against leaked JWT signatures and allows per-token revocation.
        t_hash = hash_token(payload.refresh_token)
        db_token = db.query(models.RefreshToken).filter(
            models.RefreshToken.token_hash == t_hash,
            models.RefreshToken.is_revoked == False
        ).first()

        if not db_token:
            # A revoked token presented here may indicate a replay attack;
            # in production we could trigger a global revocation for the user.
            logger.warning(f"[SECURITY] REPLAY ATTACK SUSPECTED for user {user_id}. Revoked token presented.")
            raise HTTPException(status_code=401, detail="Session expired or already rotated.")

        # Temporal Boundary Check
        if db_token.expires_at.replace(tzinfo=UTC) < datetime.datetime.now(UTC):
            db_token.is_revoked = True
            db.commit()
            raise HTTPException(status_code=401, detail="Refresh session has expired.")

        # Token rotation: invalidate the old token and issue a fresh one
        # This ensures that a stolen refresh token is only useful until the user's next refresh.
        db_token.is_revoked = True
        db.commit()
        
        new_refresh_token = issue_refresh_token(db, user.id)

        # Issue a fresh access token.
        access_token = create_access_token(data={
            "sub": user.id,
            "role": user.role.value
        })
        
        return {
            "access_token": access_token,
            "refresh_token": new_refresh_token,
            "token_type": "bearer"
        }
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Refresh session expired. Please log in again.")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid refresh session.")

@router.post("/logout")
def logout(request: Request, payload: RefreshRequest, db: Session = Depends(get_db)):
    """Revoke the given refresh token and end the user's active sessions."""
    t_hash = hash_token(payload.refresh_token)
    db_token = db.query(models.RefreshToken).filter(
        models.RefreshToken.token_hash == t_hash
    ).first()

    if db_token:
        db_token.is_revoked = True
        # Also revoke this user's access tokens globally as a standard sign-out.
        revoke_sessions(db_token.user_id)

        # Close the most recent open login-audit session for this user.
        open_session = db.query(models.LoginSession).filter(
            models.LoginSession.user_id == db_token.user_id,
            models.LoginSession.status == "active"
        ).order_by(models.LoginSession.login_time.desc()).first()
        if open_session:
            now = datetime.datetime.now(UTC)
            open_session.logout_time = now
            if open_session.login_time:
                login_aware = open_session.login_time if open_session.login_time.tzinfo else open_session.login_time.replace(tzinfo=UTC)
                open_session.duration_minutes = round((now - login_aware).total_seconds() / 60.0, 2)
            open_session.status = "ended"

        db.commit()

    return {"message": "Session terminated successfully."}

@router.get("/me")
def get_me(current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Return the authenticated user's profile, role, permissions and trial status."""
    from app.core.config import settings
    oauth_account = db.query(models.OAuthAccount).filter(models.OAuthAccount.user_id == current_user.id).first()
    has_mailbox = oauth_account is not None
    has_calendar = current_user.cal_refresh_token is not None
    is_expired = False
    if current_user.is_demo and current_user.demo_expires_at:
        is_expired = current_user.demo_expires_at.replace(tzinfo=UTC) < datetime.datetime.now(UTC)
        
    identity = build_identity_context(db, current_user)
    return {
        "id": current_user.id,
        "email": current_user.email,
        "role": current_user.role.value,
        "is_active": current_user.is_active,
        "tenant": identity["tenant"],
        "organization": identity["organization"],
        "permissions": identity["permissions"],
        "is_demo": current_user.is_demo,
        "demo_expires_at": current_user.demo_expires_at,
        "is_expired": is_expired,
        "user_limit": current_user.user_limit,
        "has_used_trial_quota": current_user.has_used_trial_quota,
        "has_mailbox": has_mailbox,
        "has_calendar": has_calendar,
        "provider": current_user.provider,
        "created_at": current_user.created_at,
        "mailbox_health": {
            "status": oauth_account.mailbox_health_status if oauth_account else "NOT_CONNECTED",
            "last_checked_at": oauth_account.mailbox_last_checked_at if oauth_account else None,
            "last_error": oauth_account.mailbox_last_error if oauth_account else None,
            "email_address": oauth_account.email_address if oauth_account else None,
        },
        "full_name": current_user.full_name,
        "company_name": current_user.company_name,
        "role_title": current_user.role_title,
        "profile_complete": bool(current_user.full_name and current_user.company_name and current_user.role_title),
    }

# --- Business profile ---
# Used downstream by the Ghostwriter agent to personalize AI-generated email drafts.

class BusinessProfileUpdate(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=120)
    company_name: str = Field(..., min_length=1, max_length=160)
    role_title: str = Field(..., min_length=1, max_length=120)

    @field_validator("full_name", "company_name", "role_title")
    @classmethod
    def _strip_and_require(cls, v: str) -> str:
        cleaned = (v or "").strip()
        if not cleaned:
            raise ValueError("Field cannot be empty or whitespace.")
        return cleaned


@router.get("/profile")
def get_business_profile(current_user: models.User = Depends(get_current_user)):
    """
    Business Identity Retrieval.
    Returns the authenticated user's business profile fields used for AI email personalization.
    The email is sourced from the authenticated session and is non-editable.
    """
    return {
        "email": current_user.email,
        "full_name": current_user.full_name,
        "company_name": current_user.company_name,
        "role_title": current_user.role_title,
    }


@router.put("/profile")
def update_business_profile(
    payload: BusinessProfileUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Save the authenticated user's business profile. Email is taken from the
    session, never from the request body."""
    current_user.full_name = payload.full_name
    current_user.company_name = payload.company_name
    current_user.role_title = payload.role_title
    db.commit()
    db.refresh(current_user)

    return {
        "message": "Business profile saved successfully.",
        "profile": {
            "email": current_user.email,
            "full_name": current_user.full_name,
            "company_name": current_user.company_name,
            "role_title": current_user.role_title,
        },
    }

# --- Super Admin management endpoints ---

class AdminProvisionRequest(BaseModel):
    email: str
    user_limit: int = 5

@router.post("/sovereign/admins")
def provision_admin(
    request: AdminProvisionRequest, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Create a new Admin account with an explicit user-creation quota (super admin only)."""
    if current_user.role != models.UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Super admin privileges required.")

    existing = db.query(models.User).filter(models.User.email == request.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="An account with this email already exists.")
        
    new_admin = models.User(
        email=request.email,
        role=models.UserRole.ADMIN,
        user_limit=request.user_limit,
        created_by_id=current_user.id
    )
    db.add(new_admin)
    db.flush() # Get ID for log
    
    log = models.AdministrativeLog(
        actor_id=current_user.id,
        target_id=new_admin.id,
        action="PROVISION",
        details=f"Provisioned Admin {new_admin.email} with limit {new_admin.user_limit}"
    )
    db.add(log)
    db.commit()
    
    # Send the onboarding email (best-effort).
    email_sent = False
    try:
        # One-time onboarding token (24h expiry).
        setup_token = create_access_token(
            data={"sub": new_admin.id, "purpose": "account_onboarding"},
            expires_delta=timedelta(hours=24)
        )
        setup_url = f"{FRONTEND_URL}/setup-password?token={setup_token}"

        creds = TokenService.get_google_credentials(db, current_user.id)
        if creds:
            email_service.send_provisioning_email(
                to_email=new_admin.email,
                role="Admin",
                setup_url=setup_url,
                creds=creds
            )
            email_sent = True
            logger.info(f"[PROVISION] Onboarding email sent to Admin {new_admin.email}")
    except Exception as e:
        logger.error(f"[PROVISION] Onboarding email failed for Admin {new_admin.email}: {e}")
        # Account creation takes priority; a failed email is non-fatal here.

    return {
        "message": "Admin account created.",
        "email_dispatched": email_sent
    }

@router.get("/sovereign/admins")
def list_admins(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """List all Admin accounts with their user counts and quotas (super admin only)."""
    if current_user.role != models.UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Super admin privileges required.")

    from sqlalchemy import func
    managed_user = aliased(models.User)
    # Fetch all admins and their managed-user counts in a single query.
    stats = db.query(
        models.User, 
        func.count(managed_user.id).label('user_count')
    ).outerjoin(
        managed_user, managed_user.created_by_id == models.User.id
    ).filter(
        models.User.role == models.UserRole.ADMIN
    ).group_by(models.User.id).all()

    result = []
    for adm, user_count in stats:
        result.append({
            "id": adm.id,
            "email": adm.email,
            "user_limit": adm.user_limit,
            "current_users": user_count,
            "is_over_quota": user_count > adm.user_limit,
            "is_activated": adm.hashed_password is not None,
            "created_at": adm.created_at
        })
    return result

class AdminQuotaUpdateRequest(BaseModel):
    user_limit: int

@router.patch("/sovereign/admins/{admin_id}/quota")
def update_admin_quota(
    admin_id: str,
    request: AdminQuotaUpdateRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Update an Admin's user-creation quota (super admin only)."""
    if current_user.role != models.UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Super admin privileges required.")
        
    admin = db.query(models.User).filter(
        models.User.id == admin_id,
        models.User.role == models.UserRole.ADMIN
    ).first()
    
    if not admin:
        raise HTTPException(status_code=404, detail="Admin sector not found.")
        
    admin.user_limit = request.user_limit
    
    log = models.AdministrativeLog(
        actor_id=current_user.id,
        target_id=admin.id,
        action="QUOTA_CHANGE",
        details=f"Modified Admin {admin.email} limit to {request.user_limit}"
    )
    db.add(log)
    db.commit()
    return {"message": "Admin quota successfully adjusted."}

@router.delete("/sovereign/admins/{admin_id}")
def decommission_admin(
    admin_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Delete an Admin account and revoke its sessions (super admin only)."""
    if current_user.role != models.UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Super admin privileges required.")

    admin = db.query(models.User).filter(
        models.User.id == admin_id,
        models.User.role == models.UserRole.ADMIN
    ).first()

    if not admin:
        raise HTTPException(status_code=404, detail="Admin not found.")

    # Revoke sessions for the admin AND every user they created, so no tokens
    # keep working after the rows are deleted.
    managed_users = db.query(models.User).filter(models.User.created_by_id == admin.id).all()
    for u in managed_users:
        revoke_sessions(u.id)
    revoke_sessions(admin.id)

    log = models.AdministrativeLog(
        actor_id=current_user.id,
        target_id=admin.id,
        action="DECOMMISSION",
        details=f"Permanently decommissioned Admin sector {admin.email} and recursively revoked {len(managed_users)} user sessions."
    )
    db.add(log)
    db.delete(admin)
    db.commit()
    return {"message": "Admin account deleted."}

# --- Admin user-management endpoints ---

class UserProvisionRequest(BaseModel):
    email: str

@router.post("/management/users")
@limiter.limit("5/minute")
def provision_user(
    request: Request,
    payload: UserProvisionRequest, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Create a new User account within the admin's quota."""
    if current_user.role not in [models.UserRole.ADMIN, models.UserRole.SUPER_ADMIN]:
        raise HTTPException(status_code=403, detail="Administrator privileges required.")

    email_sent = False
    with db.begin_nested():
        if current_user.role == models.UserRole.ADMIN:
            fresh_admin = _lock_query(
                db.query(models.User).filter(models.User.id == current_user.id)
            ).first()
            if not fresh_admin:
                raise HTTPException(status_code=401, detail="Administrator identity invalid.")

            user_count = db.query(models.User).filter(models.User.created_by_id == current_user.id).count()
            if user_count >= fresh_admin.user_limit:
                raise HTTPException(
                    status_code=400,
                    detail=f"User limit reached ({fresh_admin.user_limit}). Contact your super admin to raise it."
                )

        existing = db.query(models.User).filter(models.User.email == payload.email).first()
        if existing:
            raise HTTPException(status_code=400, detail="An account with this email already exists.")

        new_user = models.User(
            email=payload.email,
            role=models.UserRole.USER,
            created_by_id=current_user.id,
            signup_source="manual"
        )
        db.add(new_user)
        db.flush()

        log = models.AdministrativeLog(
            actor_id=current_user.id,
            target_id=new_user.id,
            action="PROVISION",
            details=f"Provisioned User {new_user.email} under Admin {current_user.email}"
        )
        db.add(log)

    db.commit()
    
    # Send the onboarding email (best-effort).
    try:
        # One-time onboarding token (24h expiry).
        setup_token = create_access_token(
            data={"sub": new_user.id, "purpose": "account_onboarding"},
            expires_delta=timedelta(hours=24)
        )
        setup_url = f"{FRONTEND_URL}/setup-password?token={setup_token}"

        creds = TokenService.get_google_credentials(db, current_user.id)
        if creds:
            email_service.send_provisioning_email(
                to_email=new_user.email,
                role="User",
                setup_url=setup_url,
                creds=creds
            )
            email_sent = True
            logger.info(f"[PROVISION] Onboarding email sent to User {new_user.email}")
    except Exception as e:
        logger.error(f"[PROVISION] Onboarding email failed for User {new_user.email}: {e}")

    return {
        "message": "User account created.",
        "email_dispatched": email_sent
    }

@router.post("/management/resend-activation/{user_id}")
@limiter.limit("5/minute")
def resend_activation(
    request: Request,
    user_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Re-send the account setup link to an account that hasn't been activated yet."""
    if current_user.role not in [models.UserRole.ADMIN, models.UserRole.SUPER_ADMIN]:
        raise HTTPException(status_code=403, detail="Administrator privileges required.")

    target_user = db.query(models.User).filter(models.User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="Account not found.")

    # Admins may only act on users they created.
    if current_user.role == models.UserRole.ADMIN and target_user.created_by_id != current_user.id:
        raise HTTPException(status_code=403, detail="You don't have access to this account.")

    # Only applicable to accounts that haven't completed setup.
    if target_user.hashed_password:
        raise HTTPException(status_code=400, detail="This account is already activated.")

    # Fresh one-time onboarding token (24h expiry).
    setup_token = create_access_token(
        data={"sub": target_user.id, "purpose": "account_onboarding"}, 
        expires_delta=timedelta(hours=24)
    )
    setup_url = f"{FRONTEND_URL}/setup-password?token={setup_token}"

    email_sent = False
    try:
        creds = TokenService.get_google_credentials(db, current_user.id)
        if creds:
            email_service.send_provisioning_email(
                to_email=target_user.email,
                role=target_user.role.value.replace('_', ' ').title(),
                setup_url=setup_url,
                creds=creds
            )
            email_sent = True
            logger.info(f"[RESEND] Activation link re-dispatched for {target_user.email}")
    except Exception as e:
        logger.error(f"[RESEND] Dispatch failure for {target_user.email}: {e}")
        raise HTTPException(status_code=500, detail="Email dispatch system failure.")

    return {
        "message": "Activation link re-sent.",
        "email_dispatched": email_sent
    }

@router.get("/management/users")
@limiter.limit("15/minute")
def list_managed_users(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Results per page")
):
    """Paginated list of users visible to the caller (admins see only their own)."""
    if current_user.role not in [models.UserRole.ADMIN, models.UserRole.SUPER_ADMIN]:
        raise HTTPException(status_code=403, detail="Administrator privileges required.")

    # Admins see only the users they created; super admin sees all.
    query = db.query(models.User).filter(models.User.role == models.UserRole.USER)
    if current_user.role == models.UserRole.ADMIN:
        query = query.filter(models.User.created_by_id == current_user.id)

    total = query.count()
    skip = (page - 1) * page_size
    from sqlalchemy import func
    # Fetch users and their campaign counts in a single query.
    stats = query.outerjoin(
        models.Campaign, models.User.id == models.Campaign.user_id
    ).with_entities(
        models.User, 
        func.count(models.Campaign.id).label('campaign_count')
    ).group_by(models.User.id).order_by(models.User.created_at.desc()).offset(skip).limit(page_size).all()
    
    result = []
    for u, campaign_count in stats:
        has_mailbox = db.query(models.OAuthAccount).filter(models.OAuthAccount.user_id == u.id).first() is not None
        result.append({
            "id": u.id,
            "email": u.email,
            "campaign_count": campaign_count,
            "is_demo": u.is_demo,
            "is_activated": u.hashed_password is not None,
            "has_mailbox": has_mailbox,
            "created_at": u.created_at
        })

    return {
        "users": result,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if total > 0 else 0
    }

@router.delete("/management/users/{user_id}")
@limiter.limit("5/minute")
def decommission_user(
    request: Request,
    user_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Delete a user and revoke their sessions (admins limited to their own users)."""
    if current_user.role not in [models.UserRole.ADMIN, models.UserRole.SUPER_ADMIN]:
        raise HTTPException(status_code=403, detail="Administrator privileges required.")

    # Admins can only delete users they created.
    query = db.query(models.User).filter(models.User.id == user_id, models.User.role == models.UserRole.USER)
    if current_user.role == models.UserRole.ADMIN:
        query = query.filter(models.User.created_by_id == current_user.id)

    target_user = query.first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found in your account.")

    # Revoke all of the user's active tokens immediately.
    revoke_sessions(target_user.id)

    log = models.AdministrativeLog(
        actor_id=current_user.id,
        target_id=target_user.id,
        action="DECOMMISSION",
        details=f"Permanently decommissioned identity {target_user.email}"
    )
    db.add(log)
    db.delete(target_user)
    db.commit()
    return {"message": "User account deleted."}

@router.get("/google/url")
def get_google_auth_url(current_user: models.User = Depends(get_current_user)):
    """Return the Google OAuth2 URL used to connect the user's mailbox."""
    from app.core.auth import GoogleAuthService
    return {"url": GoogleAuthService.get_authorization_url(user_id=current_user.id, email=current_user.email)}

capability_router = APIRouter(prefix="/connect", tags=["Capability & Mailbox"])

@capability_router.post("/google/mailbox")
def connect_google_mailbox(
    payload: Dict[str, Any] = Body(...), 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Exchange the OAuth code for a refresh token and store it encrypted (AES-256)."""
    code = payload.get("code")
    state = payload.get("state")
    redirect_uri = payload.get("redirect_uri")

    if not code:
        raise HTTPException(status_code=400, detail="Mailbox authorization code missing.")

    # Validate the OAuth state token to prevent CSRF.
    from app.core.auth import GoogleAuthService
    if not state or not GoogleAuthService.validate_state(current_user.id, state):
        logger.warning(f"[SECURITY] OAuth state mismatch for user_id={current_user.id}; possible CSRF.")
        raise HTTPException(
            status_code=403, 
            detail="Authorization session expired or integrity check failed. Please restart the connection flow."
        )

    # Exchange the auth code for a long-lived refresh token.
    from app.core.auth import GoogleAuthService
    logger.info(f"[AUTH] Exchanging OAuth code for user_id={current_user.id}")
    try:
        mailbox_data = GoogleAuthService.verify_auth_code_for_mailbox(code, redirect_uri=redirect_uri)
    except Exception as e:
        logger.error(f"[AUTH] OAuth code exchange failed for user_id={current_user.id}: {e}")
        raise

    refresh_token = mailbox_data.get("refresh_token")
    email = mailbox_data.get("email")
    logger.info(f"[AUTH] Token exchange complete. email={email}, has_refresh={bool(refresh_token)}")

    if not email or email.lower() != current_user.email.lower():
        logger.warning(f"[IDENTITY MISMATCH] User {current_user.email} tried to connect mailbox {email}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Account mismatch. Please connect the mailbox for your account: {current_user.email}. (Detected: {email})"
        )

    if not refresh_token:
        logger.error(f"[AUTH] Refresh token missing for user_id={current_user.id}")
        raise HTTPException(
            status_code=400, 
            detail="Failed to capture refresh token. Please ensure you've granted email permissions and try 'Consent' prompt."
        )

    # Store the mailbox credentials (encrypted).
    # Check if this user already has a Google account connected
    oauth_acc = db.query(models.OAuthAccount).filter(
        models.OAuthAccount.user_id == current_user.id,
        models.OAuthAccount.provider == "google"
    ).first()

    encrypted_token = encrypt_token(refresh_token)
    access_token_encrypted = encrypt_token(mailbox_data.get("access_token")) if mailbox_data.get("access_token") else None
    token_expiry_dt = datetime.datetime.now(UTC).replace(tzinfo=None) + datetime.timedelta(seconds=3600)

    if oauth_acc:
        logger.info(f"[AUTH] Updating stored mailbox credentials for user_id={current_user.id}")
        oauth_acc.email_address = email
        oauth_acc.encrypted_refresh_token = encrypted_token
        oauth_acc.access_token = access_token_encrypted
        oauth_acc.token_expiry = token_expiry_dt
        oauth_acc.mailbox_health_status = "HEALTHY"
        oauth_acc.mailbox_last_checked_at = datetime.datetime.now(UTC).replace(tzinfo=None)
        oauth_acc.mailbox_last_error = None
    else:
        logger.info(f"[AUTH] Storing new mailbox credentials for {email}")
        oauth_acc = models.OAuthAccount(
            user_id=current_user.id,
            provider="google",
            email_address=email,
            encrypted_refresh_token=encrypted_token,
            access_token=access_token_encrypted,
            token_expiry=token_expiry_dt,
            mailbox_health_status="HEALTHY",
            mailbox_last_checked_at=datetime.datetime.now(UTC).replace(tzinfo=None),
            mailbox_last_error=None,
        )
        db.add(oauth_acc)

    db.commit()
    logger.info(f"[AUTH] Mailbox connected and credentials stored for {email}")
    return {"message": "Mailbox connected successfully.", "email": email}


@capability_router.delete("/mailbox")
def disconnect_mailbox(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Disconnect the user's mailbox and revoke the refresh token at Google."""
    oauth_acc = db.query(models.OAuthAccount).filter(models.OAuthAccount.user_id == current_user.id).first()
    if oauth_acc:
        try:
            # Revoke the token at Google so it's invalidated at the source.
            refresh_token = decrypt_token(oauth_acc.encrypted_refresh_token)
            import requests
            revoke_resp = requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": refresh_token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10
            )
            if revoke_resp.status_code == 200:
                logger.info(f"[AUTH] Upstream revocation successful for {oauth_acc.email_address}")
            else:
                logger.warning(f"[AUTH] Upstream revocation returned non-200 status: {revoke_resp.status_code}")
        except Exception as e:
            logger.error(f"[AUTH] Failed to execute upstream revocation for {current_user.id}: {e}")

        db.delete(oauth_acc)
        db.commit()
    return {"message": "Mailbox disconnected and authorization revoked."}


class SaveCalSettingsRequest(BaseModel):
    event_type_id: int | None = None
    timezone: str | None = None


@capability_router.get("/cal/url")
def get_cal_auth_url(current_user: models.User = Depends(get_current_user)):
    """
    Returns the secure Cal.com authorization URL for redirecting the client.
    """
    from app.core.auth import CalAuthService
    return {"url": CalAuthService.get_authorization_url(user_id=current_user.id)}


@capability_router.post("/cal/callback")
def connect_cal_calendar(
    payload: Dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Exchanges authorization code for access and refresh tokens, encrypting them in the User table.
    """
    code = payload.get("code")
    state = payload.get("state")
    
    if not code:
        raise HTTPException(status_code=400, detail="Authorization code missing.")
        
    from app.core.auth import CalAuthService
    if not state or not CalAuthService.validate_state(current_user.id, state):
        logger.warning(f"[SECURITY] Cal OAuth State Mismatch for user_id={current_user.id}.")
        raise HTTPException(
            status_code=403, 
            detail="Authorization session expired or integrity check failed."
        )

    # Exchange code for tokens
    token_data = CalAuthService.exchange_code_for_tokens(code)
    
    # Verify the connected Cal.com account matches the signed-in user.
    cal_email = CalAuthService.get_user_email(token_data["access_token"])
    if not cal_email or cal_email.lower() != current_user.email.lower():
        logger.warning(f"[IDENTITY MISMATCH] User {current_user.email} tried to connect Cal.com account associated with {cal_email}")
        raise HTTPException(
            status_code=400,
            detail=f"Account mismatch. Please connect the Cal.com calendar for your account: {current_user.email}. (Detected: {cal_email or 'unknown'})"
        )
    
    # Store encrypted tokens in user record
    current_user.cal_access_token = encrypt_token(token_data["access_token"])
    current_user.cal_refresh_token = encrypt_token(token_data["refresh_token"])
    
    if token_data.get("expires_at"):
        try:
            import dateutil.parser
            current_user.cal_token_expires_at = dateutil.parser.parse(token_data["expires_at"]).replace(tzinfo=None)
        except Exception:
            current_user.cal_token_expires_at = datetime.datetime.now() + datetime.timedelta(seconds=1800)
    else:
        current_user.cal_token_expires_at = datetime.datetime.now() + datetime.timedelta(seconds=1800)
        
    db.commit()
    logger.info(f"[AUTH] Cal.com calendar successfully connected for {current_user.email}")
    return {"message": "Cal.com calendar successfully connected!"}


@capability_router.get("/cal/status")
def get_cal_status(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Returns the current Cal.com integration status.
    Includes reauth_required flag when tokens have been auto-cleared due to expiry.
    """
    from app.core.config import settings
    from app.integrations.cal import cal_provider

    # Resolve active access token to verify connection integrity.
    # If the refresh token is revoked/invalid, it will automatically clear and commit, returning None.
    token = cal_provider.get_valid_access_token(db, current_user)
    is_connected = token is not None

    return {
        "connected": is_connected,
        "cal_event_type_id": current_user.cal_event_type_id or settings.CAL_EVENT_TYPE_ID,
        "cal_timezone": current_user.cal_timezone or settings.CAL_TIMEZONE,
        "reauth_required": not is_connected and current_user.cal_event_type_id is not None,
    }


@capability_router.get("/cal/event-types")
def get_cal_event_types(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Fetches all event types from the user's connected Cal.com account.
    """
    from app.integrations.cal import cal_provider
    if not current_user.cal_refresh_token:
        return {"event_types": []}
    event_types = cal_provider.get_event_types(db, current_user)
    return {"event_types": event_types}


@capability_router.post("/cal/settings")
def save_cal_settings(
    payload: SaveCalSettingsRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Saves user-specific event type ID and timezone preferences.
    """
    if payload.event_type_id is not None:
        current_user.cal_event_type_id = payload.event_type_id
    if payload.timezone is not None:
        current_user.cal_timezone = payload.timezone
    db.commit()
    return {"message": "Cal.com settings saved successfully."}


@capability_router.delete("/cal")
def disconnect_cal_calendar(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Decomissions the Cal.com connection, revoking the tokens from the user record.
    """
    current_user.cal_access_token = None
    current_user.cal_refresh_token = None
    current_user.cal_token_expires_at = None
    current_user.cal_event_type_id = None
    current_user.cal_timezone = "UTC"
    db.commit()
    return {"message": "Cal.com calendar disconnected successfully."}


@capability_router.post("/refresh-tokens")
def trigger_token_refresh(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Manually trigger token refresh for the current user.
    Useful for testing or when user experiences auth failures.
    Returns: Status of Gmail and Calendar token refresh operations.
    """
    from app.core.token_service import TokenService
    
    results = {
        "google": {"status": "pending"},
        "calendar": {"status": "pending"},
        "timestamp": datetime.datetime.now(UTC).isoformat()
    }
    
    try:
        # Attempt to refresh Gmail token
        gmail_success = TokenService.refresh_and_update_access(
            db, current_user.id, provider="google"
        )
        results["google"]["status"] = "refreshed" if gmail_success else "failed"
        logger.info(f"[TOKEN-REFRESH] Manual Gmail refresh for user {current_user.id}: {results['google']['status']}")
    except Exception as e:
        results["google"]["status"] = "error"
        results["google"]["error"] = str(e)
        logger.error(f"[TOKEN-REFRESH] Error during Gmail refresh for user {current_user.id}: {e}")
    
    try:
        # Verify Cal.com token
        cal_success = TokenService.refresh_and_update_access(
            db, current_user.id, provider="cal.com"
        )
        results["calendar"]["status"] = "verified" if cal_success else "invalid"
        logger.info(f"[TOKEN-REFRESH] Manual Cal.com verification for user {current_user.id}: {results['calendar']['status']}")
    except Exception as e:
        results["calendar"]["status"] = "error"
        results["calendar"]["error"] = str(e)
        logger.error(f"[TOKEN-REFRESH] Error during Cal.com verification for user {current_user.id}: {e}")
    
    return {
        "message": "Token refresh completed",
        "results": results,
        "next_auto_refresh": (datetime.datetime.now(UTC) + timedelta(hours=6)).isoformat()
    }
