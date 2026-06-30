import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    # 1. CORE INTELLIGENCE & DATABASE LAYER
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    NEON_DB_URL = os.getenv("NEON_DB_URL")
    
    # 2. AI COST GOVERNANCE & REQUEST CACHING
    LLM_DAILY_BUDGET_USD = float(os.getenv("LLM_DAILY_BUDGET_USD", "50.00"))
    LLM_CAMPAIGN_BUDGET_USD = float(os.getenv("LLM_CAMPAIGN_BUDGET_USD", "10.00"))
    LLM_CACHE_ENABLED = os.getenv("LLM_CACHE_ENABLED", "True").lower() == "true"
    LLM_CACHE_TTL_SECONDS = int(os.getenv("LLM_CACHE_TTL_SECONDS", "86400"))

    # CSV ingestion cap. Replaces the old hard 100-row limit. Set high enough to
    # process realistic uploads while protecting the t2.medium box from OOM.
    MAX_CSV_ROWS = int(os.getenv("MAX_CSV_ROWS", "2000"))

    # Pipeline in-stage concurrency (latency vs memory tradeoff). Raise these on a
    # larger instance to cut wall-clock time for big campaigns.
    #
    # Stage 3 = merged ICP-qualify + research. Per company it runs the v2 multi-layer
    # enrichment (website + RSS + Google News + DDGS + hiring/ATS + Wikipedia) and one
    # enrichment LLM call, then the MEDDPICC validation LLM call. It is I/O-bound, not
    # CPU-bound.
    #
    # MEMORY: the whole Phase-2 batch shares ONE curl_cffi AsyncSession (created in
    # campaign_service.stage_3_icp_filtering), and trafilatura/lxml parsing is globally
    # bounded to 6 concurrent parses (enrichment_v2.TRAFILATURA_CONCURRENCY). The heavy
    # worker's peak RSS stays well under its 380 MB ceiling at this concurrency.
    # Do NOT switch back to a per-company session — that multiplies pool memory by the
    # concurrency factor and breaks the envelope.
    #
    # THROUGHPUT GOVERNOR is OpenAI TPM, not RAM/CPU.
    # Actual token budget per company (measured):
    #   Enrichment input (6 sources, char budgets)  ≈  8,000-9,500 tokens
    #   ICP/MEDDPICC call (prompt + structured out) ≈  3,000-3,500 tokens
    #   Per-company total                           ≈ 12,000-13,000 tokens
    #
    # Safe concurrency formula:  floor(TPM_limit / avg_tokens × safety_margin)
    #   Tier 1 (200K TPM): floor(200K / 12.5K × 0.90) = 14   ← DEFAULT
    #   Tier 2 (2M TPM):   floor(2M   / 12.5K × 0.90) = 144  (cap at 40 for RAM)
    #   Tier 3+:           raise further as needed
    # Running at 20 on Tier-1 generates ~250K TPM → trips circuit breaker repeatedly.
    ICP_CONCURRENCY = int(os.getenv("ICP_CONCURRENCY", "13"))        # screener + enrich + ICP — Tier-1 safe ceiling (200K TPM)
    STAGE5_CONCURRENCY = int(os.getenv("STAGE5_CONCURRENCY", "25"))   # stakeholder ranking (~2K tokens/call — TPM-safe)
    STAGE6_CONCURRENCY = int(os.getenv("STAGE6_CONCURRENCY", "20"))   # email drafting — reduced from 25; retry storms at 25 can hit ~250K TPM

    # ICP acceptance threshold (0-100). A company is ACCEPTED when its inferred
    # operator_fit score (+ optional need/precondition bonus) >= this value AND it
    # does not hard-fail an explicit firmographic requirement. Score-driven (no
    # pass/fail/unknown cliff). Lower to accept more, raise to be stricter.
    ICP_ACCEPT_THRESHOLD = int(os.getenv("ICP_ACCEPT_THRESHOLD", "55"))

    # Cross-campaign reuse of a domain's structured research profile (skips crawl +
    # enrichment). A stored profile is reused only if it was refreshed within this
    # many days; older than this, the domain is re-crawled + re-enriched so the
    # research never goes permanently stale.
    ICP_PROFILE_FRESHNESS_DAYS = int(os.getenv("ICP_PROFILE_FRESHNESS_DAYS", "30"))

    # 3. PRODUCTION OBSERVABILITY, METRICS & ALERTS
    SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
    STORAGE_MODE = os.getenv("STORAGE_MODE", "local")

    # 4. IDENTITY, SECURITY & SESSION MANAGEMENT
    ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "paste_secure_aes_256_base64_encoded_token_here")
    JWT_SECRET = os.getenv("JWT_SECRET", "AI_PRIORI_ENTERPRISE_IDENT_SESSION_KEY_PRODUCTION")
    ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,https://app.ai-priori.com")
    API_BASE_URL = os.getenv("API_BASE_URL", os.getenv("BACKEND_URL", "http://localhost:8000")).rstrip("/")
    
    # Resolve dynamic FRONTEND_URL fallback for config properties
    _frontend_url = os.getenv("FRONTEND_URL")
    if not _frontend_url:
        _env_origins = os.getenv("ALLOWED_ORIGINS")
        if _env_origins:
            _first_origin = _env_origins.split(",")[0].strip().strip('"').strip("'")
            if _first_origin:
                _frontend_url = _first_origin
    if not _frontend_url:
        _frontend_url = "http://localhost:5173"

    # 5. GOOGLE IDENTITY BRIDGE & STATELESS GMAIL CREDENTIALS
    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
    VITE_GOOGLE_CLIENT_ID = os.getenv("VITE_GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
    GOOGLE_REDIRECT_URL = os.getenv("GOOGLE_REDIRECT_URL", f"{_frontend_url}/connect-mailbox")
    
    GMAIL_CREDENTIALS_JSON = os.getenv("GMAIL_CREDENTIALS_JSON")
    GMAIL_TOKEN_JSON = os.getenv("GMAIL_TOKEN_JSON")
    
    # 6. CAL.COM GATEWAY & SCHEDULING INTEGRATIONS
    CAL_CLIENT_ID = os.getenv("CAL_CLIENT_ID")
    CAL_CLIENT_SECRET = os.getenv("CAL_CLIENT_SECRET")
    CAL_REDIRECT_URI = os.getenv("CAL_REDIRECT_URI", f"{_frontend_url}/connect-calendar")
    
    CAL_API_KEY = os.getenv("CAL_API_KEY")
    CAL_USERNAME = os.getenv("CAL_USERNAME")
    CAL_EVENT_LINK = os.getenv("CAL_EVENT_LINK")
    CAL_TIMEZONE = os.getenv("CAL_TIMEZONE", "UTC")
    
    # Safe environment parser to prevent ValueError crashes on blank or non-numeric inputs
    _cal_event_env = os.getenv("CAL_EVENT_TYPE_ID")
    if _cal_event_env and _cal_event_env.strip().isdigit():
        CAL_EVENT_TYPE_ID = int(_cal_event_env.strip())
    else:
        CAL_EVENT_TYPE_ID = 5137238  # Stable Default Fallback
        
    # 7. OUTBOUND SMTP & MAIL BOX HOSTS
    EMAIL_HOST = os.getenv("EMAIL_HOST", "smtp.gmail.com")
    EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
    EMAIL_USER = os.getenv("EMAIL_USER")
    EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
    EMAIL_FROM = os.getenv("EMAIL_FROM")
    
    
    # 9. DISTRIBUTED EXECUTION LAYER (REDIS BROKER)
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    
    # Dispatch poller — the durable reconciliation loop that actually sends
    # scheduled emails (DB-driven, survives server restarts / lost Celery tasks).
    # 120s default: halves idle Redis command churn vs 60s; max send delay ~2 min.
    DISPATCH_POLL_SECONDS = int(os.getenv("DISPATCH_POLL_SECONDS", "60"))

    # 10. STATE-MACHINE TIMERS & NUDGE TUNING COORDINATES
    REPLY_FALLBACK_WINDOW_DAYS = int(os.getenv("REPLY_FALLBACK_WINDOW_DAYS", "90"))
    NUDGE_DISPATCH_STALE_MINUTES = int(os.getenv("NUDGE_DISPATCH_STALE_MINUTES", "15"))

    # === [TEST-TUNABLE] REMINDER WAIT TIME =================================
    # How long to wait after sending an email before the NEXT follow-up/reminder
    # to the same prospect becomes due. Lower this to test the reminder sequence
    # faster (e.g. "2" = next reminder due in 2 minutes instead of 2 days).
    NUDGE_FOLLOWUP_DELAY_MINUTES = int(os.getenv("NUDGE_FOLLOWUP_DELAY_MINUTES", "2880"))
    # =========================================================================
    SWEEP_STUCK_MINUTES = int(os.getenv("SWEEP_STUCK_MINUTES", "10"))

    # How long to hold sibling prospects after a discovery email is sent to one DM
    # at the same company, to avoid hammering the same company from multiple angles.
    DISCOVERY_HOLD_WINDOW_HOURS = int(os.getenv("DISCOVERY_HOLD_WINDOW_HOURS", "96"))

    # Limits
    MAX_NEUTRAL_FOLLOWUPS = int(os.getenv("MAX_NEUTRAL_FOLLOWUPS", "2"))

    # Reminders
    REMINDER_24H_MIN_HOURS = int(os.getenv("REMINDER_24H_MIN_HOURS", "22"))
    REMINDER_24H_MAX_HOURS = int(os.getenv("REMINDER_24H_MAX_HOURS", "25"))
    REMINDER_1H_MIN_MINUTES = int(os.getenv("REMINDER_1H_MIN_MINUTES", "45"))
    REMINDER_1H_MAX_MINUTES = int(os.getenv("REMINDER_1H_MAX_MINUTES", "75"))

    # 11. CELERY BEAT SCHEDULES (seconds between each periodic task run)

    # === [TEST-TUNABLE] INBOX POLLING =======================================
    # How often (seconds) the inbox_worker checks the mailbox for new prospect
    # replies. Lower this to see incoming replies picked up faster during testing.
    INBOX_POLL_SECONDS            = int(os.getenv("INBOX_POLL_SECONDS",            "300"))    # 5 min
    # =========================================================================

    MEETING_CHECK_SECONDS         = int(os.getenv("MEETING_CHECK_SECONDS",         "3600"))   # 1 hr

    # === [TEST-TUNABLE] REMINDER SCHEDULING TRACKER =========================
    # How often (seconds) the orchestrator's check_all_inactivity_task runs —
    # this is the loop that notices a prospect's reminder is due (per
    # NUDGE_FOLLOWUP_DELAY_MINUTES above) and schedules/drafts the next one.
    # Lower this so a shortened reminder wait time above actually gets acted on
    # promptly instead of waiting up to 30 minutes for the next tracker pass.
    INACTIVITY_CHECK_SECONDS      = int(os.getenv("INACTIVITY_CHECK_SECONDS",      "1800"))   # 30 min
    # =========================================================================

    SWEEP_STUCK_CAMPAIGNS_SECONDS = int(os.getenv("SWEEP_STUCK_CAMPAIGNS_SECONDS", "1800"))   # 30 min
    SWEEP_STRANDED_DISPATCHES_SECONDS = int(os.getenv("SWEEP_STRANDED_DISPATCHES_SECONDS", "600"))  # 10 min
    REACTIVATION_CHECK_SECONDS    = int(os.getenv("REACTIVATION_CHECK_SECONDS",    "86400"))  # 24 hr

    # 12. EMAIL DELIVERY WINDOWS (prospect local time, 24-h "HH:MM" format)
    # Two windows per day; emails outside these windows are held until the next slot.
    SEND_WINDOW_MORNING_START   = os.getenv("SEND_WINDOW_MORNING_START",   "09:30")
    SEND_WINDOW_MORNING_END     = os.getenv("SEND_WINDOW_MORNING_END",     "11:59")
    SEND_WINDOW_AFTERNOON_START = os.getenv("SEND_WINDOW_AFTERNOON_START", "13:30")
    SEND_WINDOW_AFTERNOON_END   = os.getenv("SEND_WINDOW_AFTERNOON_END",   "16:00")
    # Minimum gap (minutes) between consecutive sends queued for the same user.
    SEND_STAGGER_MINUTES = int(os.getenv("SEND_STAGGER_MINUTES", "3"))

    # 13. OUTBOUND DISPATCH WORKER LIMITS
    # Max drafts dispatched per single poll cycle (caps burst on a large backlog).
    MAX_DISPATCH_PER_RUN = int(os.getenv("MAX_DISPATCH_PER_RUN", "300"))
    # Draft age (days) after which a QUEUED draft is auto-failed as stale.
    MAX_STALE_DRAFT_DAYS = int(os.getenv("MAX_STALE_DRAFT_DAYS", "3"))
    # Grace window (minutes) before the sweeper flags a dispatch as stranded.
    SWEEPER_STALE_GRACE_MINUTES = int(os.getenv("SWEEPER_STALE_GRACE_MINUTES", "30"))

    # 14. GMAIL INBOX SCAN SETTINGS
    # Rolling lookback window used in the Gmail search query (e.g. "newer_than:30d").
    GMAIL_SCAN_LOOKBACK_DAYS = int(os.getenv("GMAIL_SCAN_LOOKBACK_DAYS", "30"))
    # Max pages (× GMAIL_PAGE_SIZE messages each) fetched per inbox poll.
    GMAIL_MAX_PAGES  = int(os.getenv("GMAIL_MAX_PAGES",  "5"))
    GMAIL_PAGE_SIZE  = int(os.getenv("GMAIL_PAGE_SIZE",  "15"))

    # 15. CAL.COM SLOT SETTINGS
    # Days ahead to look for the first available slot when auto-booking.
    CAL_SLOTS_LOOKAHEAD_DAYS = int(os.getenv("CAL_SLOTS_LOOKAHEAD_DAYS", "7"))
    # Number of alternative slots offered to the prospect on booking failure.
    CAL_SLOTS_LIMIT = int(os.getenv("CAL_SLOTS_LIMIT", "5"))

    # 16. CONTENT / INTELLIGENCE SETTINGS
    # Maximum age of a news item to be used as a "recent_news" hook in initial emails.
    NEWS_CUTOFF_MONTHS = int(os.getenv("NEWS_CUTOFF_MONTHS", "12"))

    # AWS Storage Credentials & Region
    AWS_STORAGE_BUCKET_NAME= os.getenv("AWS_STORAGE_BUCKET_NAME", "focalreach")
    AWS_REGION = os.getenv("AWS_REGION", "us-west-1")
    AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
    # Key prefix ("folder") that every S3 object is stored under within the bucket.
    AWS_S3_PREFIX = os.getenv("AWS_S3_PREFIX", "focalreach").strip("/")

    def s3_key(self, key: str) -> str:
        """Prefixes an S3 object key with AWS_S3_PREFIX, if set."""
        return f"{self.AWS_S3_PREFIX}/{key}" if self.AWS_S3_PREFIX else key

    # LLM Model & Circuit Breaker Settings
    INPUT_VALIDATION_MODEL = os.getenv("INPUT_VALIDATION_MODEL", "gpt-4o-mini")
    OPENAI_CIRCUIT_FAILURE_THRESHOLD = int(os.getenv("OPENAI_CIRCUIT_FAILURE_THRESHOLD", "3"))
    OPENAI_CIRCUIT_RECOVERY_TIMEOUT = int(os.getenv("OPENAI_CIRCUIT_RECOVERY_TIMEOUT", "180"))

    # Logging Configuration
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

settings = Settings()
