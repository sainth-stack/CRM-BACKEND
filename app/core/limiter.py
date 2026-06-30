from slowapi import Limiter
from slowapi.util import get_remote_address
import os

# --- Centralized Limiter (IP-based, Cloud-backed for scalability) ---
# Single shared instance: every router must import `limiter` from here rather
# than constructing its own, since each Limiter() opens its own Redis
# connection and slowapi has no way to merge state across instances.
#
# swallow_errors=True: a transient Redis blip (e.g. Upstash dropping an idle
# TLS connection) must never 500 an unrelated request like /auth/refresh.
# If the storage call fails, slowapi treats the limit check as passed instead
# of raising - rate limiting degrades, auth does not.
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=os.getenv("REDIS_URL"),
    swallow_errors=True,
)
