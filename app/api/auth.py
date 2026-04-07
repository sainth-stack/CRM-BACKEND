from fastapi import APIRouter, Depends, HTTPException, status, Body
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
    get_password_hash
)
from app.core.token_service import TokenService
from app.core.email_service import email_service

router = APIRouter(prefix="/auth", tags=["Authentication"])

class LoginRequest(BaseModel):
    email: str
    password: str

class VerifyOTPRequest(BaseModel):
    email: str
    otp: str

@router.post("/login")
async def login(request: LoginRequest, db: Session = Depends(get_db)):
    """
    Standard Email/Password Sign-In.
    Verifies credentials and issues a JWT session.
    """
    user = db.query(models.User).filter(models.User.email == request.email).first()
    if not user or not user.hashed_password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    if not verify_password(request.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Issue internal JWT session
    access_token = create_access_token(data={"sub": user.id, "email": user.email})
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
async def manual_add_user(request: LoginRequest, db: Session = Depends(get_db)):
    """Secret endpoint for admin to add users since public sign-up is disabled."""
    existing_user = db.query(models.User).filter(models.User.email == request.email).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="User already exists")
    
    user = models.User(
        email=request.email,
        hashed_password=get_password_hash(request.password)
    )
    db.add(user)
    db.commit()
    return {"message": "User added successfully"}

@router.post("/demo/signup")
async def demo_signup(request: LoginRequest, db: Session = Depends(get_db)):
    """Public onboarding for 5-day trial identities."""
    existing_user = db.query(models.User).filter(models.User.email == request.email).first()
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
    
    # Create temporary demo user
    # Note: demo_expires_at is set ONLY after OTP verification
    user = models.User(
        email=request.email,
        hashed_password=get_password_hash(request.password),
        is_demo=True,
        signup_source="demo",
        otp_code=otp,
        otp_expiry=datetime.datetime.now(UTC) + timedelta(minutes=15)
    )
    db.add(user)
    db.commit()
    
    try:
        from app.core.email_service import email_service
        email_service.send_otp_email(user.email, otp)
    except Exception as e:
        print(f"[DEMO] OTP Dispatch Error: {e}")
        # In production, we might want to log this but continue
        
    return {"message": "Identity verification mobilized. Please check your email for the code."}

@router.post("/demo/verify")
async def verify_demo_otp(request: VerifyOTPRequest, db: Session = Depends(get_db)):
    """Finalizes demo identity creation and activates the 5-day boundary."""
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
    
    # Issue initial session
    access_token = create_access_token(data={"sub": user.id, "email": user.email})
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

import random
import datetime
from datetime import UTC, timedelta
from app.core.email_service import email_service

class ForgotPasswordRequest(BaseModel):
    email: str

@router.post("/forgot-password")
async def forgot_password(request: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """Generates an OTP and dispatches it via GMail for identity verification."""
    user = db.query(models.User).filter(models.User.email == request.email).first()
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
        email_service.send_otp_email(user.email, otp)
    except Exception as e:
        print(f"[AUTH] OTP Dispatch Error: {e}")
        raise HTTPException(status_code=500, detail="Identity portal communication failed.")

    return {"message": "Identity verification code mobilized to your vaulted email."}

class VerifyOTPRequest(BaseModel):
    email: str
    otp: str

@router.post("/verify-otp")
async def verify_otp(request: VerifyOTPRequest, db: Session = Depends(get_db)):
    """Validates the 6-digit code and authorizes a temporary reset session."""
    user = db.query(models.User).filter(models.User.email == request.email).first()
    if not user or not user.otp_code or user.otp_code != request.otp:
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
async def reset_password(request: ResetPasswordRequest, db: Session = Depends(get_db)):
    """Executes the cryptographic update to user credentials."""
    if request.new_password != request.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match.")

    try:
        from app.core.security import SECRET_KEY, ALGORITHM
        import jwt
        payload = jwt.decode(request.reset_token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("purpose") != "password_reset":
            raise HTTPException(status_code=401, detail="Unauthorized reset session.")
        user_id = payload.get("sub")
    except Exception:
        raise HTTPException(status_code=401, detail="Reset session expired or invalid.")

    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Identity profile not found.")

    # Commit secure hashed credentials
    user.hashed_password = get_password_hash(request.new_password)
    user.otp_code = None # Invalidate OTP after success
    user.otp_expiry = None
    db.commit()

    return {"message": "Identity credentials updated. You may now initialize a secure session."}

@router.get("/me")
async def get_me(current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Protected route to return the current user's profile with capability status."""
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
    """Sovereign Provisioning: Super Admin only can create Admins."""
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
            print(f"[PROVISION] Dispatch mission successful for {new_admin.email}")
    except Exception as e:
        print(f"[PROVISION] Autonomous dispatch failure: {e}")
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
    """Sovereign Visibility: Super Admin can see all Admins and their quotas."""
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
    """Sovereign Control: Super Admin can modify an Admin's user creation limit."""
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
    """Sovereign Decommission: Super Admin can permanently remove an Admin."""
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
async def provision_user(
    request: UserProvisionRequest, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Administrative Provisioning: Admins can create Users up to their provisioned limit."""
    if current_user.role not in [models.UserRole.ADMIN, models.UserRole.SUPER_ADMIN]:
        raise HTTPException(status_code=403, detail="Administrative authority required.")
        
    # Boundary Enforcement for Admins
    if current_user.role == models.UserRole.ADMIN:
        user_count = db.query(models.User).filter(models.User.created_by_id == current_user.id).count()
        if user_count >= current_user.user_limit:
            raise HTTPException(
                status_code=400, 
                detail=f"User provision limit reached ({current_user.user_limit}). Contact Super Admin for sector expansion."
            )
            
    existing = db.query(models.User).filter(models.User.email == request.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Identity portal already contains this email.")
        
    new_user = models.User(
        email=request.email,
        hashed_password=get_password_hash(request.password),
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
                password=request.password,
                creds=creds
            )
            email_sent = True
            print(f"[PROVISION] Dispatch mission successful for {new_user.email}")
    except Exception as e:
        print(f"[PROVISION] Autonomous dispatch failure: {e}")
        
    return {
        "message": "User identity provisioned successfully.",
        "email_dispatched": email_sent
    }

@router.get("/management/users")
async def list_managed_users(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Sector Visibility: Admins can see all users they've provisioned."""
    if current_user.role not in [models.UserRole.ADMIN, models.UserRole.SUPER_ADMIN]:
        raise HTTPException(status_code=403, detail="Administrative authority required.")
        
    # Filter by creator if not Super Admin
    query = db.query(models.User).filter(models.User.role == models.UserRole.USER)
    if current_user.role == models.UserRole.ADMIN:
        query = query.filter(models.User.created_by_id == current_user.id)
        
    users = query.all()
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
    return result

@router.delete("/management/users/{user_id}")
async def decommission_user(
    user_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Sector Decommission: Admins can remove users they provisioned."""
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
    """Generates the secure Google OAuth2 portal URL for mailbox authorization."""
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
    print(f"[AUTH] MOBILIZING CODE EXCHANGE. User Identity: {current_user.email}, Redirect: {redirect_uri}")
    try:
        mailbox_data = await GoogleAuthService.verify_auth_code_for_mailbox(code, redirect_uri=redirect_uri)
    except Exception as e:
        print(f"[REJECTED] Handshake Collision: {e}")
        raise
        
    refresh_token = mailbox_data.get("refresh_token")
    email = mailbox_data.get("email")
    print(f"[AUTH] CAPTURED HANDSHAKE. Authorized Identity: {email}")

    if not email or email.lower() != current_user.email.lower():
        print(f"[IDENTITY MISMATCH] User: {current_user.email}, Authorized: {email}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Identity mismatch. Professional authorization failed. You must connect the mailbox associated with your registered identity: {current_user.email}."
        )

    if not refresh_token:
        print(f"[VAULT FAILURE] Refresh token missing for {email}")
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
        print(f"[VAULT UPDATE] Updating capability for {email}")
        oauth_acc.email_address = email
        oauth_acc.encrypted_refresh_token = encrypted_token
    else:
        print(f"[VAULT NEW] Synchronizing new capability for {email}")
        oauth_acc = models.OAuthAccount(
            user_id=current_user.id,
            provider="google",
            email_address=email,
            encrypted_refresh_token=encrypted_token
        )
        db.add(oauth_acc)

    db.commit()
    print(f"[SUCCESS] CAPABILITY VAULTED FOR {email}")
    return {"message": "Mailbox synchronization active. Capability vaulted successfully.", "email": email}

@capability_router.delete("/mailbox")
async def disconnect_mailbox(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Decommission mailbox capability for the current user."""
    oauth_acc = db.query(models.OAuthAccount).filter(models.OAuthAccount.user_id == current_user.id).first()
    if oauth_acc:
        db.delete(oauth_acc)
        db.commit()
    return {"message": "Mailbox decommissioned successfully."}
