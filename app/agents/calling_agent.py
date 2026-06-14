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

# Optional distributed dependencies — the agent degrades gracefully if absent.
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


def retry_with_backoff(max_retries: int = 3, backoff: int = 2):
    """Retry decorator with exponential backoff.

    Terminal Twilio 4xx errors (auth, bad number, billing) are never retried;
    any other failure backs off for ``backoff ** attempt`` seconds, up to
    ``max_retries`` attempts.
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            retries = 0
            while retries < max_retries:
                try:
                    return func(*args, **kwargs)
                except TwilioRestException as e:
                    # 4xx (auth, bad number, billing) are terminal — do not retry.
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


class DistributedCluster:
    """Redis-backed shared state for outbound calling.

    Provides cluster-wide rate limiting (calls-per-second), per-number 24h
    deduplication, atomic call claims, and call telemetry. When Redis is not
    configured it falls back to permissive no-ops (dev only — no distributed
    guarantees).
    """
    def __init__(self):
        self.url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.r = redis.from_url(self.url, decode_responses=True) if redis else None
        self._ttl = 86400  # 24 hours
        self._cps_limit = 2  # max 2 calls/sec, cluster-wide

    def rate_limit_check(self) -> bool:
        """Cluster-wide calls-per-second cap to avoid Twilio rate bans."""
        if not self.r: return True  # no Redis in dev: allow (no distributed limiting)
        key = f"sentinel:cps:{int(time.time())}"
        current_calls = self.r.incr(key)
        self.r.expire(key, 2)
        return current_calls <= self._cps_limit

    def phone_spam_guard(self, phone: str) -> bool:
        """Deduplication: allow at most one call per number per 24 hours."""
        if not self.r: return True
        key = f"sentinel:phone_lock:{phone}"
        return bool(self.r.set(key, "LOCKED", nx=True, ex=self._ttl))

    def claim_mission(self, mission_id: str, data: Dict[str, Any]) -> bool:
        """Atomically claim a call so only one worker dispatches it (SETNX)."""
        if not self.r: return True
        return bool(self.r.set(f"mission:{mission_id}", json.dumps(data), nx=True, ex=self._ttl))

    def update_telemetry(self, mission_id: str, status: str, response_time: float):
        """Record per-call status and latency for observability."""
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
    """Fault-tolerant outbound calling agent (Twilio + OpenAI).

    Responsibilities:
      1. Cluster-wide rate limiting (CPS) and per-number deduplication.
      2. Strict E.164 phone validation (phonenumbers.is_valid_number).
      3. A correlation id (corid) threaded through logs for traceability.
      4. Hard-timed Twilio dispatch with bounded retries.
    """

    def __init__(self):
        # Credentials and config from the environment.
        self.account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        self.auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        self.from_number = os.getenv("TWILIO_FROM_NUMBER")
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.allowed_webhook_domains = ["ai-priori.com", "localhost"]  # strict domain whitelist

        if not all([self.account_sid, self.auth_token, self.from_number]):
            raise ValueError("Missing required Twilio credentials (account SID / auth token / from number).")

        if not (phonenumbers and pytz):
            raise RuntimeError("Required libraries 'phonenumbers' and 'pytz' are not installed.")

        from twilio.http.http_client import TwilioHttpClient
        http_client = TwilioHttpClient(timeout=10)
        self.client = Client(self.account_sid, self.auth_token, http_client=http_client)
        self.cluster = DistributedCluster()
        self.validator = RequestValidator(self.auth_token)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=50)  # pool for hard-timed calls
        self.llm = ChatOpenAI(model="gpt-4o-mini", temperature=0, top_p=1, seed=42)

    def _validate_coordinate(self, phone: str) -> Optional[str]:
        """Validate and normalize a phone number to E.164, or None if invalid."""
        try:
            parsed = phonenumbers.parse(phone, None)
            if not phonenumbers.is_valid_number(parsed):
                logger.error(f"[CALLING-AGENT] Rejected {phone}: not a valid E.164 number.")
                return None
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        except Exception:
            return None

    def _is_safe_to_call(self, phone_obj: Any, consent: bool) -> bool:
        """True only with consent AND within the recipient's local business hours."""
        if not consent: return False
        try:
            tz_list = phonenumber_tz.time_zones_for_number(phone_obj)
            target_tz = pytz.timezone(tz_list[0])
            local_now = datetime.datetime.now(pytz.utc).astimezone(target_tz)
            if local_now.weekday() >= 5: return False  # block weekends
            return 9 <= local_now.hour <= 18  # 9am-6pm local
        except Exception: return False

    @retry_with_backoff(max_retries=3)
    def mobilize_mission(self, to_phone: str, message: str, webhook_url: str, consent: bool, corid: str = None):
        """Place an outbound call after validation, compliance, rate-limit and dedup checks."""
        trace_id = corid or str(uuid.uuid4())
        start_ts = time.time()

        # 1. Validate the phone number and the webhook domain.
        target_phone = self._validate_coordinate(to_phone)
        if not target_phone or not any(domain in webhook_url for domain in self.allowed_webhook_domains):
            return {"status": "BLOCKED", "reason": "SECURITY_SANITY_FAILURE", "corid": trace_id}

        # 2. Consent / business-hours and cluster rate-limit gate.
        parsed_phone = phonenumbers.parse(target_phone)
        if not self._is_safe_to_call(parsed_phone, consent) or not self.cluster.rate_limit_check():
            return {"status": "BLOCKED", "reason": "COMPLIANCE_OR_CPS_CAP", "corid": trace_id}

        # 3. Per-number 24h deduplication.
        if not self.cluster.phone_spam_guard(target_phone):
            logger.warning(f"[CALLING-AGENT] Skipped {target_phone}: already called within 24h.")
            return {"status": "SKIPPED", "reason": "SPAM_SHIELD_ACTIVE", "corid": trace_id}

        try:
            # 4. Generate the call script (7s cap; fall back to the raw message).
            try:
                prompt = ChatPromptTemplate.from_messages([
                    ("system", "Natural B2B voice script. End with meeting email promise. ESCAPE all XML characters."),
                    ("human", "Payload: {text}")
                ])
                script = self.executor.submit((prompt | self.llm).invoke, {"text": message}).result(timeout=7).content
                script = saxutils.escape(script[:1500])
            except Exception: script = saxutils.escape(message[:1500])

            # 5. Atomically claim this call (prevents duplicate dispatch across workers).
            if not self.cluster.claim_mission(trace_id, {"script": script, "to": target_phone}):
                return {"status": "SKIPPED", "reason": "ATOMIC_COLLISION", "corid": trace_id}

            # 6. Dispatch via Twilio (10s cap).
            try:
                call = self.executor.submit(
                    self.client.calls.create,
                    to=target_phone,
                    from_=self.from_number,
                    url=f"{webhook_url}?corid={trace_id}",
                    status_callback=f"{webhook_url}/status?corid={trace_id}",
                    machine_detection='Enable',  # answering-machine detection
                    record=True,
                    time_limit=300
                ).result(timeout=10)

                self.cluster.update_telemetry(trace_id, "SUCCESS", time.time() - start_ts)
                return {"status": "SUCCESS", "call_sid": call.sid, "corid": trace_id}
            except concurrent.futures.TimeoutError:
                return {"status": "FAILED", "reason": "TWILIO_HANDSHAKE_TIMEOUT", "corid": trace_id}

        except Exception as e:
            logger.error(f"[CALLING-AGENT] Call {trace_id} failed: {e}")
            return {"status": "FAILED", "reason": str(e), "corid": trace_id}

    def audit_mission(self, call_sid: str, corid: str) -> Optional[MissionMetric]:
        """Fetch the call outcome and transcribe its recording (polls for the recording)."""
        try:
            call = self.client.calls(call_sid).fetch()
            start_audit = time.time()

            # Poll for the recording with backoff (total ~45s window).
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
                tmp = f"call_recording_{uuid.uuid4()}.mp3"
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
            logger.error(f"[CALLING-AGENT] Call audit failed: {e}")
            return None
