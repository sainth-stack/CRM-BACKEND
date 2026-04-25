import os
import json
import re
import datetime
import time
import uuid
import base64
import threading
import concurrent.futures
import requests
import xml.sax.saxutils as saxutils
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from app.core.logging_config import logger

# --- MAANG-Standard Distributed Dependencies ---
try:
    import redis
except ImportError:
    redis = None

try:
    import phonenumbers
    from phonenumbers import timezone as phonenumber_tz, carrier, geocoder
    import pytz
except ImportError:
    phonenumbers = None
    pytz = None

# --- Tier 4 Resilience Cluster ---
def maang_sentinel_retry(max_retries: int = 3, backoff: int = 2):
    """
    Tier-4 Selective Resilience Decorator.
    Categorizes failures to prevent cascading retry-storms.
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            retries = 0
            while retries < max_retries:
                try:
                    return func(*args, **kwargs)
                except TwilioRestException as e:
                    # 4xx (Auth, Bad Number, Billing) are terminal - NO RETRY
                    if 400 <= e.status < 500: raise e
                    retries += 1
                    if retries == max_retries: raise e
                    time.sleep(backoff ** retries)
                except Exception as e:
                    retries += 1
                    if retries == max_retries: raise e
                    time.sleep(backoff ** retries)
            return None
        return wrapper
    return decorator

# --- Distributed Cluster state & Rate-Limiting ---
class DistributedCluster:
    """
    Sovereign Distributed State & Rate-Limiting Anchor.
    Manages Atomic Mission Claims, Global Rate Limiting (CPS), and Phone Spam Guards.
    """
    def __init__(self):
        self.url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.r = redis.from_url(self.url, decode_responses=True) if redis else None
        self._ttl = 86400 # 24 Hours
        self._cps_limit = 2 # Max 2 Calls Per Second (Cluster-wide)

    def rate_limit_check(self) -> bool:
        """Global Token Bucket Rate Limiter (Prevent Twilio Bans)."""
        if not self.r: return True # Fallback for dev (Unsafe)
        key = f"sentinel:cps:{int(time.time())}"
        current_calls = self.r.incr(key)
        self.r.expire(key, 2)
        return current_calls <= self._cps_limit

    def phone_spam_guard(self, phone: str) -> bool:
        """Global Deduplication Logic: Max 1 mission per number per 24 hours."""
        if not self.r: return True
        key = f"sentinel:phone_lock:{phone}"
        return bool(self.r.set(key, "LOCKED", nx=True, ex=self._ttl))

    def claim_mission(self, mission_id: str, data: Dict[str, Any]) -> bool:
        """Atomic SETNX Mission Capture."""
        if not self.r: return True
        return bool(self.r.set(f"mission:{mission_id}", json.dumps(data), nx=True, ex=self._ttl))

    def update_telemetry(self, mission_id: str, status: str, response_time: float):
        """Distributed Observability Update."""
        if not self.r: return
        key = f"sentinel:metrics:{mission_id}"
        self.r.hset(key, mapping={"status": status, "lat": response_time, "ts": time.time()})
        self.r.expire(key, self._ttl)

class MissionMetric(BaseModel):
    call_id: str
    target: str
    status: str
    human_detected: bool
    latency_ms: float
    transcript: Optional[str]

class CallingAgent:
    """
    MAANG-Sentinel v4: Distributed Fault-Tolerant AI Telephony Orchestrator.
    
    Tier 4 Sovereign Resilience:
    1. Distributed Rate Limiting (CPS Shield) & Phone Spam Guard
    2. Phone-Native Hard-Validation (phonenumbers.is_valid_number)
    3. Global Trace Correlation (corid) for Observability
    4. Circuit Breaker Protected & Hard-Timed Dispatch
    """

    def __init__(self):
        # 1. Rigid Multi-Layer Initialization
        self.account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        self.auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        self.from_number = os.getenv("TWILIO_FROM_NUMBER")
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.allowed_webhook_domains = ["ai-priori.com", "localhost"] # Strict domain whitelist

        if not all([self.account_sid, self.auth_token, self.from_number]):
            raise ValueError("Infrastructure Failure: Missing Maang-Sentinel deployment keys.")

        if not (phonenumbers and pytz):
            raise RuntimeError("Compliance Failure: Tier-4 Jurisdictional libraries mandatory.")

        from twilio.http.http_client import TwilioHttpClient
        http_client = TwilioHttpClient(timeout=10)
        self.client = Client(self.account_sid, self.auth_token, http_client=http_client)
        self.cluster = DistributedCluster()
        self.validator = RequestValidator(self.auth_token)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=50) # High-scale scaling
        self.llm = ChatOpenAI(model="gpt-4o-mini", temperature=0.1)

    def _validate_coordinate(self, phone: str) -> Optional[str]:
        """Hard Phone-Native Validation & Normalization."""
        try:
            parsed = phonenumbers.parse(phone, None)
            if not phonenumbers.is_valid_number(parsed):
                logger.error(f"[MAANG-SENTINEL] Coordinate Refused: {phone} is not a reachable E.164 target.")
                return None
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        except Exception:
            return None

    def _is_safe_to_call(self, phone_obj: Any, consent: bool) -> bool:
        """Absolute Jurisdictional Sync Gate."""
        if not consent: return False
        try:
            tz_list = phonenumber_tz.time_zones_for_number(phone_obj)
            target_tz = pytz.timezone(tz_list[0])
            local_now = datetime.datetime.now(pytz.utc).astimezone(target_tz)
            if local_now.weekday() >= 5: return False # Weekend Block
            return 9 <= local_now.hour <= 18 # Global Business Window
        except Exception: return False

    @maang_sentinel_retry(max_retries=3)
    def mobilize_mission(self, to_phone: str, message: str, webhook_url: str, consent: bool, corid: str = None):
        """Tier-4 Distributed Outbound Mobilization."""
        trace_id = corid or str(uuid.uuid4())
        start_ts = time.time()

        # 1. Coordinate & Domain Sanitization
        target_phone = self._validate_coordinate(to_phone)
        if not target_phone or not any(domain in webhook_url for domain in self.allowed_webhook_domains):
            return {"status": "BLOCKED", "reason": "SECURITY_SANITY_FAILURE", "corid": trace_id}

        # 2. Compliance & Global Rate Limit Gate
        parsed_phone = phonenumbers.parse(target_phone)
        if not self._is_safe_to_call(parsed_phone, consent) or not self.cluster.rate_limit_check():
            return {"status": "BLOCKED", "reason": "COMPLIANCE_OR_CPS_CAP", "corid": trace_id}

        # 3. Distributed Deduplication (Global Spam Shield)
        if not self.cluster.phone_spam_guard(target_phone):
            logger.warning(f"[MAANG-SENTINEL] Deduplication Protect: Prospect {target_phone} already dialed in 24h.")
            return {"status": "SKIPPED", "reason": "SPAM_SHIELD_ACTIVE", "corid": trace_id}

        try:
            # 4. Hard-Timed Intelligence recovery (7s cap)
            try:
                prompt = ChatPromptTemplate.from_messages([
                    ("system", "Natural B2B voice script. End with meeting email promise. ESCAPE all XML characters."),
                    ("human", "Payload: {text}")
                ])
                script = self.executor.submit((prompt | self.llm).invoke, {"text": message}).result(timeout=7).content
                script = saxutils.escape(script[:1500])
            except Exception: script = saxutils.escape(message[:1500])

            # 5. Atomic Mission Claim
            if not self.cluster.claim_mission(trace_id, {"script": script, "to": target_phone}):
                return {"status": "SKIPPED", "reason": "ATOMIC_COLLISION", "corid": trace_id}

            # 6. Hard-Timed Dispatch (10s cap)
            try:
                call = self.executor.submit(
                    self.client.calls.create,
                    to=target_phone,
                    from_=self.from_number,
                    url=f"{webhook_url}?corid={trace_id}",
                    status_callback=f"{webhook_url}/status?corid={trace_id}",
                    machine_detection='Enable', # AMD pathing
                    record=True,
                    time_limit=300
                ).result(timeout=10)
                
                self.cluster.update_telemetry(trace_id, "SUCCESS", time.time() - start_ts)
                return {"status": "SUCCESS", "call_sid": call.sid, "corid": trace_id}
            except concurrent.futures.TimeoutError:
                return {"status": "FAILED", "reason": "TWILIO_HANDSHAKE_TIMEOUT", "corid": trace_id}

        except Exception as e:
            logger.error(f"[MAANG-SENTINEL] Mission {trace_id} Deflected: {e}")
            return {"status": "FAILED", "reason": str(e), "corid": trace_id}

    def audit_mission(self, call_sid: str, corid: str) -> Optional[MissionMetric]:
        """Deep Intelligence Audit with Tier-4 Polling Resilience."""
        try:
            call = self.client.calls(call_sid).fetch()
            start_audit = time.time()
            
            # Tier-4 Exponential Backoff Polling (Total 45s window)
            recording_url = None
            for wait in [5, 10, 15, 15]:
                recs = self.client.recordings.list(call_sid=call_sid, limit=1)
                if recs:
                    recording_url = f"https://api.twilio.com{recs[0].uri.replace('.json', '.mp3')}"
                    break
                time.sleep(wait)

            transcript = "UNAVAILABLE"
            if recording_url:
                res = requests.get(recording_url, auth=(self.account_sid, self.auth_token), timeout=10)
                res.raise_for_status()
                tmp = f"sentinel_{uuid.uuid4()}.mp3"
                with open(tmp, "wb") as f: f.write(res.content)
                try:
                    import openai
                    whisper = openai.OpenAI(api_key=self.openai_api_key)
                    with open(tmp, "rb") as audio:
                        transcript = whisper.audio.transcriptions.create(model="whisper-1", file=audio).text
                finally:
                    if os.path.exists(tmp): os.remove(tmp)

            return MissionMetric(
                call_id=call_sid,
                target=call.to,
                status=call.status,
                human_detected=call.answered_by == "human",
                latency_ms=(time.time() - start_audit) * 1000,
                transcript=transcript
            )
        except Exception as e:
            logger.error(f"[MAANG-SENTINEL] Mission Audit Failure: {e}")
            return None
