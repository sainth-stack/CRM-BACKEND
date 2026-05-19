from slowapi import Limiter
from slowapi.util import get_remote_address
import os

# --- Centralized Limiter (IP-based, Cloud-backed for scalability) ---
limiter = Limiter(key_func=get_remote_address, storage_uri=os.getenv("REDIS_URL"))
