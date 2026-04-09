import os
import jwt
from datetime import datetime, timedelta, UTC
from typing import Optional, Dict, Any
from cryptography.fernet import Fernet
from fastapi import HTTPException, status, Depends
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from sqlalchemy.orm import Session
from app.db import models
from app.db.database import get_db
from app.core.logging_config import logger

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

def verify_password(plain_password, hashed_password):
    """Verifies a plain-text password against a hashed persistent credential."""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    """Generates a secure PBKDF2 hash for a user-provided password."""
    return pwd_context.hash(password)

# Configuration from environment variables
SECRET_KEY = os.getenv("JWT_SECRET")
if not SECRET_KEY:
    logger.error("Security Infrastructure Failure: 'JWT_SECRET' not identified in environment.")
    raise RuntimeError("Security Infrastructure Failure: 'JWT_SECRET' environment variable is required.")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 # 24 hours
REFRESH_TOKEN_EXPIRE_DAYS = 7

# Encryption setup for persistent OAuth refresh tokens
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
if not ENCRYPTION_KEY:
    logger.error("Security Infrastructure Failure: 'ENCRYPTION_KEY' not identified in environment.")
    raise RuntimeError("Security Infrastructure Failure: 'ENCRYPTION_KEY' environment variable is required for persistent token security.")

cipher_suite = Fernet(ENCRYPTION_KEY.encode())

def encrypt_token(token: str) -> str:
    """Encrypts a string (e.g., OAuth refresh token) using AES-256 (Fernet) for secure storage."""
    return cipher_suite.encrypt(token.encode()).decode()

def decrypt_token(encrypted_token: str) -> str:
    """Decrypts an AES-256 encrypted string to its original plain-text representation."""
    try:
        return cipher_suite.decrypt(encrypted_token.encode()).decode()
    except Exception:
        logger.error("[SECURITY] Cryptographic decryption failure.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to decrypt secure token"
        )

# Distributed Session Revocation Cache (Redis-backed, in-memory fallback)
# Stores revocation timestamps keyed by user_id to invalidate JWTs issued before the last reset.
_REVOCATION_FALLBACK: dict = {}  # Used only if Redis is unreachable
logger.debug("[SECURITY] Session revocation cache initialized.")

def _get_redis():
    """Retrieves an active Redis client connection for distributed operations."""
    try:
        import redis
        r = redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), socket_connect_timeout=1)
        r.ping()
        return r
    except Exception:
        return None

def revoke_sessions(user_id: str):
    """
    Distributed Session Termination.
    Instantly terminates all pre-existing JWTs for a user across the entire server cluster.
    """
    revocation_time = int(datetime.now(UTC).timestamp())
    r = _get_redis()
    if r:
        # TTL: 8 days (exceeds max potential JWT lifetime)
        r.setex(f"revoked:{user_id}", 60 * 60 * 24 * 8, revocation_time)
        logger.info(f"[SECURITY] Distributed session revocation active for user {user_id}")
    else:
        _REVOCATION_FALLBACK[user_id] = revocation_time
        logger.warning(f"[SECURITY] Redis unavailable — revocation stored in-memory for user {user_id} (single-pod scale only).")

def _get_revocation_time(user_id: str) -> int:
    """Retrieves the last recorded revocation timestamp for a user identity."""
    r = _get_redis()
    if r:
        val = r.get(f"revoked:{user_id}")
        return int(val) if val else 0
    return _REVOCATION_FALLBACK.get(user_id, 0)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Generates a high-fidelity JWT access token for stateful session management."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(UTC) + expires_delta
    else:
        expire = datetime.now(UTC) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({
        "exp": expire,
        "iat": int(datetime.now(UTC).timestamp())
    })
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def create_refresh_token(data: dict) -> str:
    """Generates a long-lived JWT refresh token designed for persistent identity anchoring."""
    to_encode = data.copy()
    expire = datetime.now(UTC) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({
        "exp": expire, 
        "type": "refresh",
        "iat": int(datetime.now(UTC).timestamp())
    })
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)

async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> models.User:
    """
    Zero-Trust Identity Resolution.
    Validates JWT integrity, enforces distributed revocation policies, and assembles the localized user identity.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    if not token:
        raise credentials_exception

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
            
        # Enterprise Edge Defense: Distributed Session Revocation Check
        iat = payload.get("iat")
        if iat is not None:
            revocation_time = _get_revocation_time(user_id)
            if revocation_time and iat < revocation_time:
                logger.warning(f"[SECURITY] Intercepted revoked JWT for user {user_id}")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Session terminated. Security credentials have been reset.",
                    headers={"WWW-Authenticate": "Bearer"}
                )
            
        role_claim = payload.get("role")
        if role_claim is None:
            # Persistent Identity Resolution
            user = db.query(models.User).filter(models.User.id == user_id).first()
            if user is None:
                raise credentials_exception
        else:
            # Stateless Synthetic Identity Resolution
            user = models.User(
                id=user_id,
                email=payload.get("email"),
                role=models.UserRole(role_claim),
                created_by_id=payload.get("created_by_id"),
                user_limit=payload.get("user_limit", 0),
                is_demo=payload.get("is_demo", False),
                has_used_trial_quota=payload.get("has_used_trial_quota", False),
                provider=payload.get("provider")
            )
            demo_expires_at_str = payload.get("demo_expires_at")
            if demo_expires_at_str:
                user.demo_expires_at = datetime.fromisoformat(demo_expires_at_str)
                
    except jwt.PyJWTError:
        raise credentials_exception

    # Tactical Boundary Enforcement: Demo Identity Expiry Gate
    if user.is_demo and user.demo_expires_at:
        if user.demo_expires_at.replace(tzinfo=UTC) < datetime.now(UTC):
            logger.info(f"[SECURITY] Trial identity expired for {user.email}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your demo period of 5 days has been completed, please take subscription to continue with our services please contact to our sales team - sales@ai-priori.com"
            )

    return user
