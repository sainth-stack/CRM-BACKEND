import os
from fastapi import APIRouter, Depends, HTTPException, status, Body, Request, Query
from sqlalchemy.orm import Session
from typing import Dict, Any
from pydantic import BaseModel, EmailStr
import datetime
from datetime import UTC, timedelta
import random
from app.db import models
from app.db.database import get_db
from app.core.security import (
    create_access_token, 
    create_refresh_token, 
    get_current_user, 
    encrypt_token, 
    verify_password, 
    get_password_hash,
    revoke_sessions
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

class LoginRequest(BaseModel):
    email: str
    password: str

class VerifyOTPRequest(BaseModel):
    email: str
    otp: str

@router.post("/login")
@limiter.limit("10/minute")  # Brute-force shield: 10 attempts/minute per IP
async def login(request: Request, credentials: LoginRequest = Body(...), db: Session = Depends(get_db)):
    """
    Standard Email/Password Sign-In.
    Verifies credentials and issues a JWT session.
    """
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

    # Issue Internal Stateless Session (Fat JWT Payload)
    access_token = create_access_token(data={
        "sub": user.id, 
        "email": user.email,
        "role": user.role.value,
        "created_by_id": user.created_by_id,
        "user_limit": user.user_limit,
        "is_demo": user.is_demo,
        "demo_expires_at": user.demo_expires_at.isoformat() if user.demo_expires_at else None,
        "has_used_trial_quota": user.has_used_trial_quota,
        "provider": user.provider
    })
    refresh_token = create_refresh_token(data={"sub": user.id})

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "email": user.email
        }
    }

@router.post("/manual-add-user")
@limiter.limit("5/minute")
async def manual_add_user(request: Request, payload: LoginRequest, db: Session = Depends(get_db)):
    """
    Direct Identity Provisioning.
    Allows administrative persistence of new user profiles without public sign-up flows.
    """
    existing_user = db.query(models.User).filter(models.User.email == payload.email).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="User already exists")
    
    user = models.User(
        email=payload.email,
        hashed_password=get_password_hash(payload.password)
    )
    db.add(user)
    db.commit()
    return {"message": "User added successfully"}

@router.post("/demo/signup")
@limiter.limit("5/minute")   # Bot-flood shield: 5 demo signups/minute per IP
async def demo_signup(request: Request, credentials: LoginRequest = Body(...), db: Session = Depends(get_db)):
    """
    Trial Identity Mobilization.
    Initializes a temporary 5-day assessment account and dispatches a verification coordinate (OTP).
    """
    existing_user = db.query(models.User).filter(models.User.email == credentials.email).first()
    if existing_user:
        # Boundary Enforcement: Prevent Trial Renewal Loops
        if existing_user.is_demo:
            raise HTTPException(
                status_code=400, 
                detail="Identity already registered for a professional assessment. Please sign in to continue or contact sales@ai-priori.com for extension."
            )
        raise HTTPException(status_code=400, detail="Identitiy already exists. Please sign in or use a different email.")
    
    # Generate 6-digit OTP for initial verification
    otp = str(random.randint(100000, 999999))

    user = models.User(
        email=credentials.email,
        hashed_password=get_password_hash(credentials.password),
        is_demo=True,
        signup_source="demo",
        otp_code=otp,
        otp_expiry=datetime.datetime.now(UTC) + timedelta(minutes=15)
    )
    db.add(user)
    db.commit()
    
    try:
        email_service.send_verification_email(user.email, otp)
    except Exception as e:
        logger.error(f"[DEMO] Identity verification dispatch failure: {e}")
        # In production, we might want to log this but continue
        
    return {"message": "Identity verification mobilized. Please check your email for the code."}

@router.post("/demo/verify")
async def verify_demo_otp(request: VerifyOTPRequest, db: Session = Depends(get_db)):
    """
    Trial Boundary Activation.
    Validates the provided OTP and activates the 5-day temporal boundary for the trial identity.
    """
    user = db.query(models.User).filter(models.User.email == request.email).first()
    if not user or not user.otp_code or user.otp_code != request.otp:
        raise HTTPException(status_code=400, detail="Invalid verification code.")
    
    if not user.otp_expiry or user.otp_expiry.replace(tzinfo=UTC) < datetime.datetime.now(UTC):
        raise HTTPException(status_code=400, detail="Verification code has expired.")
    
    # Activate Demo Boundary
    user.demo_expires_at = datetime.datetime.now(UTC) + timedelta(days=5)
    user.otp_code = None
    user.otp_expiry = None
    db.commit()
    
    # Issue initial session (Stateless Structure)
    access_token = create_access_token(data={
        "sub": user.id, 
        "email": user.email,
        "role": user.role.value,
        "created_by_id": user.created_by_id,
        "user_limit": user.user_limit,
        "is_demo": user.is_demo,
        "demo_expires_at": user.demo_expires_at.isoformat() if user.demo_expires_at else None,
        "has_used_trial_quota": user.has_used_trial_quota,
        "provider": user.provider
    })
    refresh_token = create_refresh_token(data={"sub": user.id})
    
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": {
            "id": user.id,
            "email": user.email,
            "demo_expires_at": user.demo_expires_at
        }
    }



class ForgotPasswordRequest(BaseModel):
    email: str

@router.post("/forgot-password")
@limiter.limit("5/minute")   # Enumeration shield: 5 reset requests/minute per IP
async def forgot_password(request: Request, payload: ForgotPasswordRequest = Body(...), db: Session = Depends(get_db)):
    """
    Identity Recovery Initialization.
    Dispatches a secure 6-digit verification code to the registered email to authorize password restoration.
    """
    user = db.query(models.User).filter(models.User.email == payload.email).first()
    if not user:
        # Explicitly checking for user as requested for clearer UX feedback
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="Identity verification failed: User account does not exist."
        )

    # Generate 6-digit high-variance OTP
    otp = str(random.randint(100000, 999999))
    user.otp_code = otp
    user.otp_expiry = datetime.datetime.now(UTC) + timedelta(minutes=10)
    db.commit()

    try:
        email_service.send_verification_email(user.email, otp)
    except Exception as e:
        logger.error(f"[AUTH] Identity verification dispatch failure: {e}")
        raise HTTPException(status_code=500, detail="Identity portal communication failed.")

    return {"message": "Identity verification code mobilized to your vaulted email."}

class VerifyOTPRequest(BaseModel):
    email: str
    otp: str

@router.post("/verify-otp")
@limiter.limit("10/minute")
async def verify_otp(request: Request, payload: VerifyOTPRequest, db: Session = Depends(get_db)):
    """
    Verification Coordinate Validation.
    Confirms the legitimacy of the verification code and issues a temporary reset authorization session.
    """
    user = db.query(models.User).filter(models.User.email == payload.email).first()
    if not user or not user.otp_code or user.otp_code != payload.otp:
        raise HTTPException(status_code=400, detail="Invalid verification code.")

    # Time-based validation
    if not user.otp_expiry or user.otp_expiry.replace(tzinfo=UTC) < datetime.datetime.now(UTC):
        raise HTTPException(status_code=400, detail="Verification code has expired.")

    # Issue temporary Reset Authorization Token
    # Purpose: Prevent unauthorized direct access to reset-password endpoint
    reset_token = create_access_token(data={"sub": user.id, "purpose": "password_reset"}, expires_delta=timedelta(minutes=15))
    
    return {"reset_token": reset_token}

class ResetPasswordRequest(BaseModel):
    new_password: str
    confirm_password: str
    reset_token: str

@router.post("/reset-password")
@limiter.limit("10/minute")
async def reset_password(request: Request, payload: ResetPasswordRequest, db: Session = Depends(get_db)):
    """
    Cryptographic Credential Restoration.
    Updates the user's hashed password and terminates all pre-existing sessions to ensure identity integrity.
    """
    if payload.new_password != payload.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match.")

    try:
        from app.core.security import SECRET_KEY, ALGORITHM
        import jwt
        decoded_payload = jwt.decode(payload.reset_token, SECRET_KEY, algorithms=[ALGORITHM])
        if decoded_payload.get("purpose") != "password_reset":
            raise HTTPException(status_code=401, detail="Unauthorized reset session.")
        user_id = decoded_payload.get("sub")
    except Exception:
        raise HTTPException(status_code=401, detail="Reset session expired or invalid.")

    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Identity profile not found.")

    # Commit secure hashed credentials
    user.hashed_password = get_password_hash(payload.new_password)
    user.otp_code = None # Invalidate OTP after success
    user.otp_expiry = None
    db.commit()
    
    # Dispatch Kill-Switch: Terminate all pre-existing stateless JWTs globally
    revoke_sessions(user.id)

    return {"message": "Identity credentials updated. You may now initialize a secure session."}

@router.get("/me")
async def get_me(current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    Identity Profile Audit.
    Retrieves the authenticated actor's profile, including role-based capabilities and trial boundary status.
    """
    has_mailbox = db.query(models.OAuthAccount).filter(models.OAuthAccount.user_id == current_user.id).first() is not None
    is_expired = False
    if current_user.is_demo and current_user.demo_expires_at:
        is_expired = current_user.demo_expires_at.replace(tzinfo=UTC) < datetime.datetime.now(UTC)
        
    return {
        "id": current_user.id,
        "email": current_user.email,
        "role": current_user.role.value,
        "user_limit": current_user.user_limit,
        "provider": current_user.provider,
        "has_mailbox": has_mailbox,
        "is_demo": current_user.is_demo,
        "demo_expires_at": current_user.demo_expires_at,
        "is_expired": is_expired,
        "has_used_trial_quota": current_user.has_used_trial_quota,
        "created_at": current_user.created_at
    }

# --- Super Admin Sovereign Management Hub ---

class AdminProvisionRequest(BaseModel):
    email: str
    password: str
    user_limit: int = 5

@router.post("/sovereign/admins")
async def provision_admin(
    request: AdminProvisionRequest, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Sovereign Asset Provisioning.
    Authorizes a Super Admin to establish new Administrative sectors with explicit user creation quotas.
    """
    if current_user.role != models.UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Sovereign authority required for this operation.")
        
    existing = db.query(models.User).filter(models.User.email == request.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Identity portal already contains this email.")
        
    new_admin = models.User(
        email=request.email,
        hashed_password=get_password_hash(request.password),
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
    
    # Autonomous Provisioning Dispatch
    email_sent = False
    try:
        creds = TokenService.get_google_credentials(db, current_user.id)
        if creds:
            email_service.send_provisioning_email(
                to_email=new_admin.email,
                role="Admin",
                password=request.password,
                creds=creds
            )
            email_sent = True
            logger.info(f"[PROVISION] Autonomous dispatch successful for Admin {new_admin.email}")
    except Exception as e:
        logger.error(f"[PROVISION] Autonomous dispatch failure for Admin {new_admin.email}: {e}")
        # Note: We prioritize identity creation; dispatch failure is non-terminal for the process
        
    return {
        "message": "Admin sector provisioned successfully.",
        "email_dispatched": email_sent
    }

@router.get("/sovereign/admins")
async def list_admins(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Administrative Hierarchy Audit.
    Provides the Super Admin with comprehensive visibility into all managed sectors and their respective quotas.
    """
    if current_user.role != models.UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Sovereign authority required.")
        
    admins = db.query(models.User).filter(models.User.role == models.UserRole.ADMIN).all()
    result = []
    for adm in admins:
        # Calculate current user consumption for this admin
        user_count = db.query(models.User).filter(models.User.created_by_id == adm.id).count()
        result.append({
            "id": adm.id,
            "email": adm.email,
            "user_limit": adm.user_limit,
            "current_users": user_count,
            "is_over_quota": user_count > adm.user_limit,
            "created_at": adm.created_at
        })
    return result

class AdminQuotaUpdateRequest(BaseModel):
    user_limit: int

@router.patch("/sovereign/admins/{admin_id}/quota")
async def update_admin_quota(
    admin_id: str,
    request: AdminQuotaUpdateRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Sovereign Quota Management.
    Permits the adjustment of Administrative recruitment limits to scale operations within a specific sector.
    """
    if current_user.role != models.UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Sovereign authority required.")
        
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
async def decommission_admin(
    admin_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Sovereign Decommission Protocol.
    Permanently revokes Administrative authority and terminates the associated sector.
    """
    if current_user.role != models.UserRole.SUPER_ADMIN:
        raise HTTPException(status_code=403, detail="Sovereign authority required.")
        
    admin = db.query(models.User).filter(
        models.User.id == admin_id,
        models.User.role == models.UserRole.ADMIN
    ).first()
    
    if not admin:
        raise HTTPException(status_code=404, detail="Admin sector not found.")
        
    log = models.AdministrativeLog(
        actor_id=current_user.id,
        target_id=admin.id,
        action="DECOMMISSION",
        details=f"Permanently decommissioned Admin sector {admin.email}"
    )
    db.add(log)
    db.delete(admin)
    db.commit()
    return {"message": "Admin sector decommissioned successfully."}

# --- Admin Operational Management Hub ---

class UserProvisionRequest(BaseModel):
    email: str
    password: str

@router.post("/management/users")
@limiter.limit("5/minute")
async def provision_user(
    request: Request,
    payload: UserProvisionRequest, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Operational Identity Provisioning.
    Allows Administrators to establish new User identities within their provisioned sector quota.
    """
    if current_user.role not in [models.UserRole.ADMIN, models.UserRole.SUPER_ADMIN]:
        raise HTTPException(status_code=403, detail="Administrative authority required.")
        
    # Boundary Enforcement for Admins
    if current_user.role == models.UserRole.ADMIN:
        # Dynamic Query: Fetch explicit fresh state to prevent stale JWT claim bypass
        fresh_admin = db.query(models.User).filter(models.User.id == current_user.id).first()
        if not fresh_admin:
            raise HTTPException(status_code=401, detail="Administrator identity invalid.")
            
        user_count = db.query(models.User).filter(models.User.created_by_id == current_user.id).count()
        if user_count >= fresh_admin.user_limit:
            raise HTTPException(
                status_code=400, 
                detail=f"User provision limit reached ({fresh_admin.user_limit}). Contact Super Admin for sector expansion."
            )
            
    existing = db.query(models.User).filter(models.User.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Identity portal already contains this email.")
        
    new_user = models.User(
        email=payload.email,
        hashed_password=get_password_hash(payload.password),
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
    
    # Autonomous Provisioning Dispatch
    email_sent = False
    try:
        creds = TokenService.get_google_credentials(db, current_user.id)
        if creds:
            email_service.send_provisioning_email(
                to_email=new_user.email,
                role="User",
                password=payload.password,
                creds=creds
            )
            email_sent = True
            logger.info(f"[PROVISION] Autonomous dispatch successful for User {new_user.email}")
    except Exception as e:
        logger.error(f"[PROVISION] Autonomous dispatch failure for User {new_user.email}: {e}")
        
    return {
        "message": "User identity provisioned successfully.",
        "email_dispatched": email_sent
    }

@router.get("/management/users")
@limiter.limit("15/minute")
async def list_managed_users(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Results per page")
):
    """
    Operational Sector Audit.
    Retrieves a paginated listing of all User identities active within the Administrator's jurisdiction.
    Enforces strict hierarchical boundaries and memory-safe pagination.
    """
    if current_user.role not in [models.UserRole.ADMIN, models.UserRole.SUPER_ADMIN]:
        raise HTTPException(status_code=403, detail="Administrative authority required.")
        
    # Boundary Enforcement: Filter by creator if not Super Admin
    query = db.query(models.User).filter(models.User.role == models.UserRole.USER)
    if current_user.role == models.UserRole.ADMIN:
        query = query.filter(models.User.created_by_id == current_user.id)
    
    # Mathematical Offset Calculation
    total = query.count()
    skip = (page - 1) * page_size
    users = query.order_by(models.User.created_at.desc()).offset(skip).limit(page_size).all()
    
    result = []
    for u in users:
        # Metrics: Campaign Activity
        campaign_count = db.query(models.Campaign).filter(models.Campaign.user_id == u.id).count()
        result.append({
            "id": u.id,
            "email": u.email,
            "campaign_count": campaign_count,
            "is_demo": u.is_demo,
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
async def decommission_user(
    request: Request,
    user_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Operational Decommission Protocol.
    Permanently revokes User authority and terminates the associated identity sector.
    """
    if current_user.role not in [models.UserRole.ADMIN, models.UserRole.SUPER_ADMIN]:
        raise HTTPException(status_code=403, detail="Administrative authority required.")
        
    # Admins can only delete their own provisioned users
    query = db.query(models.User).filter(models.User.id == user_id, models.User.role == models.UserRole.USER)
    if current_user.role == models.UserRole.ADMIN:
        query = query.filter(models.User.created_by_id == current_user.id)
        
    target_user = query.first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User sector not found in your jurisdiction.")
        
    log = models.AdministrativeLog(
        actor_id=current_user.id,
        target_id=target_user.id,
        action="DECOMMISSION",
        details=f"Decommissioned User identity {target_user.email}"
    )
    db.add(log)
    db.delete(target_user)
    db.commit()
    return {"message": "User sector decommissioned successfully."}

@router.get("/google/url")
async def get_google_auth_url(current_user: models.User = Depends(get_current_user)):
    """
    Authorization Gateway Initialization.
    Generates the high-fidelity Google OAuth2 URL required to grant the system mailbox capabilities.
    """
    from app.core.auth import GoogleAuthService
    return {"url": GoogleAuthService.get_authorization_url(email=current_user.email)}

capability_router = APIRouter(prefix="/connect", tags=["Capability & Mailbox"])

@capability_router.post("/google/mailbox")
async def connect_google_mailbox(
    payload: Dict[str, Any] = Body(...), 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Step 2: Capability Vaulting.
    Exchanges an OAuth code for a refresh token and vaults it using AES-256.
    """
    code = payload.get("code")
    redirect_uri = payload.get("redirect_uri")
    if not code:
        raise HTTPException(status_code=400, detail="Mailbox authorization code missing.")

    # 1. Exchange Code for Persistent Refresh Token
    from app.core.auth import GoogleAuthService
    logger.info(f"[AUTH] MOBILIZING CODE EXCHANGE. User Identity: {current_user.email}, Redirect: {redirect_uri}")
    try:
        mailbox_data = await GoogleAuthService.verify_auth_code_for_mailbox(code, redirect_uri=redirect_uri)
    except Exception as e:
        logger.error(f"[REJECTED] Handshake Collision for {current_user.email}: {e}")
        raise
        
    refresh_token = mailbox_data.get("refresh_token")
    email = mailbox_data.get("email")
    logger.info(f"[AUTH] CAPTURED HANDSHAKE. Authorized Identity: {email}")

    if not email or email.lower() != current_user.email.lower():
        logger.warning(f"[IDENTITY MISMATCH] Sector User: {current_user.email}, Authorized Identity: {email}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Identity mismatch. Professional authorization failed. You must connect the mailbox associated with your registered identity: {current_user.email}."
        )

    if not refresh_token:
        logger.error(f"[VAULT FAILURE] Refresh token missing for authorized identity {email}")
        raise HTTPException(
            status_code=400, 
            detail="Failed to capture refresh token. Please ensure you've granted email permissions and try 'Consent' prompt."
        )

    # 2. Vault Capability
    # Check if this user already has a Google account connected
    oauth_acc = db.query(models.OAuthAccount).filter(
        models.OAuthAccount.user_id == current_user.id,
        models.OAuthAccount.provider == "google"
    ).first()

    encrypted_token = encrypt_token(refresh_token)

    if oauth_acc:
        logger.info(f"[VAULT UPDATE] Updating established capability for {email}")
        oauth_acc.email_address = email
        oauth_acc.encrypted_refresh_token = encrypted_token
    else:
        logger.info(f"[VAULT NEW] Synchronizing new mailbox capability for {email}")
        oauth_acc = models.OAuthAccount(
            user_id=current_user.id,
            provider="google",
            email_address=email,
            encrypted_refresh_token=encrypted_token
        )
        db.add(oauth_acc)

    db.commit()
    logger.info(f"[SUCCESS] CAPABILITY VAULTED AND SYNCHRONIZED FOR {email}")
    return {"message": "Mailbox synchronization active. Capability vaulted successfully.", "email": email}

@capability_router.delete("/mailbox")
async def disconnect_mailbox(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Capability Decommissioning.
    Permanently revokes the system's authorization to access the user's mailbox.
    """
    oauth_acc = db.query(models.OAuthAccount).filter(models.OAuthAccount.user_id == current_user.id).first()
    if oauth_acc:
        db.delete(oauth_acc)
        db.commit()
    return {"message": "Mailbox decommissioned successfully."}
