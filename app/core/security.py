import hashlib
import jwt
import socket
import ipaddress
import urllib.parse
from datetime import datetime, timedelta, UTC
from typing import Optional
# Force a global 15-second socket timeout so a slow external API call can't
# hang worker threads indefinitely.
socket.setdefaulttimeout(15)
from cryptography.fernet import Fernet
from fastapi import HTTPException, status, Depends
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from sqlalchemy.orm import Session
from app.db import models
from app.db.database import get_db
from app.core.logging_config import logger
from app.core.config import settings

def validate_url_for_ssrf(url: str) -> tuple[str, str]:
    """SSRF guard (TOCTOU-resistant).

    Rejects private/loopback/cloud-metadata addresses, resolves DNS up front, and
    returns the validated IP so a later lookup can't be rebound to an internal host.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ["http", "https"]:
            raise HTTPException(status_code=400, detail="Only HTTP/HTTPS URLs are permitted.")
        
        hostname = parsed.hostname
        if not hostname:
            raise HTTPException(status_code=400, detail="Invalid coordinate: missing hostname.")
        
        # Phase 1: Direct IP Check & DNS Resolution Audit (Dual-Stack)
        try:
            # Check if hostname is already a valid IP
            ipaddress.ip_address(hostname)
            resolved_ips = [hostname]
        except ValueError:
            # Resolve hostname to all available IPs (IPv4 & IPv6)
            try:
                addr_info = socket.getaddrinfo(hostname, None)
                resolved_ips = list(set(info[4][0] for info in addr_info))
            except socket.gaierror:
                raise HTTPException(status_code=400, detail="Coordinate resolution failed. Domain unreachable.")

        # Reject the URL if ANY resolved IP is private/restricted.
        validated_ip = None
        for ip in resolved_ips:
            # Normalize IPv6 link-local/scope identifiers if present
            clean_ip = ip.split('%')[0] if '%' in ip else ip
            curr_ip_obj = ipaddress.ip_address(clean_ip)
            
            if curr_ip_obj.is_private or curr_ip_obj.is_loopback or curr_ip_obj.is_link_local or curr_ip_obj.is_reserved:
                 logger.warning(f"[SECURITY] SSRF ATTEMPT BLOCKED: {hostname} resolved to internal/restricted IP {ip}")
                 raise HTTPException(status_code=400, detail="Access to internal IP addresses is prohibited.")
            
            # Prefer IPv4 for outbound compatibility if available
            if not validated_ip or (curr_ip_obj.version == 4 and ipaddress.ip_address(validated_ip).version == 6):
                validated_ip = ip

        return url, validated_ip
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"SSRF Audit Failure: {e}")
        raise HTTPException(status_code=400, detail="Invalid URL format or security protocol failure.")

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

def verify_password(plain_password, hashed_password):
    """Verifies a plain-text password against a hashed persistent credential."""
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    """Generates a secure PBKDF2 hash for a user-provided password."""
    return pwd_context.hash(password)

# Configuration from centralized settings
SECRET_KEY = settings.JWT_SECRET
if not SECRET_KEY:
    logger.error("Security Infrastructure Failure: 'JWT_SECRET' not identified in settings.")
    raise RuntimeError("Security Infrastructure Failure: 'JWT_SECRET' configuration is required.")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60
REFRESH_TOKEN_EXPIRE_DAYS = 7

# Encryption setup for persistent OAuth refresh tokens
ENCRYPTION_KEY = settings.ENCRYPTION_KEY
if not ENCRYPTION_KEY:
    logger.error("Security Infrastructure Failure: 'ENCRYPTION_KEY' not identified in settings.")
    raise RuntimeError("Security Infrastructure Failure: 'ENCRYPTION_KEY' configuration is required for persistent token security.")

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

def hash_token(token: str) -> str:
    """Generates a non-reversible SHA-256 fingerprint for a session token."""
    return hashlib.sha256(token.encode()).hexdigest()

# Distributed Session Revocation Cache (Redis-backed)
# Stores revocation timestamps to invalidate JWTs globally across the cluster.
logger.debug("[SECURITY] Session revocation infrastructure active.")

def _get_redis():
    """Retrieves an active Redis client connection for distributed operations."""
    try:
        import redis
        r = redis.from_url(settings.REDIS_URL, socket_connect_timeout=5)
        return r
    except Exception:
        return None

def revoke_sessions(user_id: str):
    """Revoke all of a user's existing JWTs across the cluster (Redis-backed)."""
    r = _get_redis()
    if r:
        revocation_time = datetime.now(UTC).timestamp()
        r.setex(f"revoked:{user_id}", 60 * 60 * 24 * 8, str(revocation_time))
        logger.info(f"[SECURITY] Distributed session revocation active for user {user_id}")
    else:
        logger.critical(f"[SECURITY] MISSION FAILURE: Redis unreachable. Partial revocation for {user_id} - Security integrity compromised.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Security infrastructure temporarily unavailable. Please try again shortly."
        )

def acquire_lock(lock_key: str, ttl: int = 300) -> bool:
    """
    Distributed Task Locking.
    Ensures that a specific operational boundary (e.g., polling an inbox or sending a nudge)
    is owned by exactly one worker instance at any given time.
    """
    r = _get_redis()
    if not r:
        logger.error(f"[LOCK] Redis unavailable. Refusing to acquire lock for {lock_key}.")
        return False
    
    # NX=True ensures the key is only set if it does not already exist
    return bool(r.set(f"lock:{lock_key}", "locked", ex=ttl, nx=True))

def release_lock(lock_key: str):
    """
    Lock Decommissioning.
    Explicitly releases a distributed lock to allow subsequent task execution.
    """
    r = _get_redis()
    if r:
        r.delete(f"lock:{lock_key}")

def lock_exists(lock_key: str) -> bool:
    """Non-destructive liveness probe: is this distributed lock currently held?

    Used by the stuck-campaign sweeper to avoid resurrecting a campaign whose
    stage worker is still alive (holding the lock). Fail-safe: on any Redis error
    we return False (treat as not-held) so recovery is never blocked by a probe.
    """
    r = _get_redis()
    if not r:
        return False
    try:
        return bool(r.exists(f"lock:{lock_key}"))
    except Exception:
        return False

def _get_revocation_time(user_id: str) -> float:
    """Return the last recorded revocation timestamp for a user (0.0 if none)."""
    r = _get_redis()
    if r:
        try:
            val = r.get(f"revoked:{user_id}")
            return float(val) if val else 0.0
        except Exception as e:
            logger.warning(f"[SECURITY] Redis query failed during identity verification: {e}. Falling back gracefully.")
            return 0.0
    
    logger.warning("[SECURITY] Redis unreachable during identity verification. Falling back gracefully to zero revocation timestamp.")
    return 0.0

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Generate a signed JWT access token."""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(UTC) + expires_delta
    else:
        expire = datetime.now(UTC) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({
        "exp": expire,
        "iat": datetime.now(UTC).timestamp() # Use float iat for high-precision revocation match
    })
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def create_refresh_token(data: dict) -> str:
    """Generate a long-lived JWT refresh token."""
    to_encode = data.copy()
    expire = datetime.now(UTC) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({
        "exp": expire, 
        "type": "refresh",
        "iat": datetime.now(UTC).timestamp()
    })
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> models.User:
    """Resolve the current user from the JWT: validate the token, honor distributed
    revocation, and load the user from the database."""
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
            
        # Distributed revocation check: reject tokens issued before a global logout.
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
            
        # Always load the user from the DB for fresh state (role, quota, demo expiry).
        user = db.query(models.User).filter(models.User.id == user_id).first()
        if user is None:
            raise credentials_exception
            
    except jwt.PyJWTError:
        raise credentials_exception

    # Demo accounts: reject once the trial window has expired.
    if user.is_demo and user.demo_expires_at:
        if user.demo_expires_at.replace(tzinfo=UTC) < datetime.now(UTC):
            logger.info(f"[SECURITY] Trial identity expired for {user.email}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your demo period of 5 days has been completed, please take subscription to continue with our services please contact to our sales team - sales@ai-priori.com"
            )

    return user

def get_visibility_filter(db: Session, current_user: models.User):
    """Return a query filter limiting a user to the campaign data they may see.

    Every role can now own and operate on its OWN campaigns (admins and super admins
    can start/view/delete campaigns under their own account). Scope by role:
      * super_admin -> ONLY their own campaigns (no access to other users'/admins' data).
      * admin       -> campaigns of the users they created PLUS their own.
      * user        -> their own campaigns.
    """
    from sqlalchemy import or_
    role = str(current_user.role).lower().split('.')[-1]
    if role == "super_admin":
        return models.Campaign.user_id == current_user.id
    if role == "admin":
        target_user_ids = db.query(models.User.id).filter(models.User.created_by_id == current_user.id)
        return or_(
            models.Campaign.user_id.in_(target_user_ids),
            models.Campaign.user_id == current_user.id,
        )
    return models.Campaign.user_id == current_user.id

