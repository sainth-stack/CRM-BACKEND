import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    # 1. CORE INTELLIGENCE & DATABASE LAYER
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    NEON_DB_URL = os.getenv("NEON_DB_URL")
    
    # 2. AI COST GOVERNANCE & REQUEST CACHING
    LLM_DAILY_BUDGET_USD = float(os.getenv("LLM_DAILY_BUDGET_USD", "50.00"))
    LLM_CACHE_ENABLED = os.getenv("LLM_CACHE_ENABLED", "True").lower() == "true"
    LLM_CACHE_TTL_SECONDS = int(os.getenv("LLM_CACHE_TTL_SECONDS", "86400"))
    
    # 3. PRODUCTION OBSERVABILITY, METRICS & ALERTS
    SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
    STORAGE_MODE = os.getenv("STORAGE_MODE", "local")
    AWS_STORAGE_BUCKET_NAME = os.getenv("AWS_STORAGE_BUCKET_NAME", "company-outreach-storage-bucket")
    
    # 4. IDENTITY, SECURITY & SESSION MANAGEMENT
    ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "paste_secure_aes_256_base64_encoded_token_here")
    JWT_SECRET = os.getenv("JWT_SECRET", "AI_PRIORI_ENTERPRISE_IDENT_SESSION_KEY_PRODUCTION")
    ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,https://app.ai-priori.com")
    
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
    
    # 8. DISCOVERY SWARM & AI RESEARCH ENGINE
    TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
    
    # 9. DISTRIBUTED EXECUTION LAYER (REDIS BROKER)
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    
    # 10. STATE-MACHINE TIMERS & NUDGE TUNING COORDINATES
    REPLY_FALLBACK_WINDOW_DAYS = int(os.getenv("REPLY_FALLBACK_WINDOW_DAYS", "90"))
    NUDGE_DISPATCH_STALE_MINUTES = int(os.getenv("NUDGE_DISPATCH_STALE_MINUTES", "15"))
    NUDGE_FOLLOWUP_DELAY_DAYS = int(os.getenv("NUDGE_FOLLOWUP_DELAY_DAYS", "2"))
    SWEEP_STUCK_MINUTES = int(os.getenv("SWEEP_STUCK_MINUTES", "10"))
    
    # Limits
    MAX_NEUTRAL_FOLLOWUPS = int(os.getenv("MAX_NEUTRAL_FOLLOWUPS", "11"))
    
    # Reminders
    REMINDER_24H_MIN_HOURS = int(os.getenv("REMINDER_24H_MIN_HOURS", "22"))
    REMINDER_24H_MAX_HOURS = int(os.getenv("REMINDER_24H_MAX_HOURS", "25"))
    REMINDER_1H_MIN_MINUTES = int(os.getenv("REMINDER_1H_MIN_MINUTES", "45"))
    REMINDER_1H_MAX_MINUTES = int(os.getenv("REMINDER_1H_MAX_MINUTES", "75"))

    # AWS Storage Credentials & Region
    AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
    AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")

    # LLM Model & Circuit Breaker Settings
    INPUT_VALIDATION_MODEL = os.getenv("INPUT_VALIDATION_MODEL", "gpt-4o-mini")
    OPENAI_CIRCUIT_FAILURE_THRESHOLD = int(os.getenv("OPENAI_CIRCUIT_FAILURE_THRESHOLD", "3"))
    OPENAI_CIRCUIT_RECOVERY_TIMEOUT = int(os.getenv("OPENAI_CIRCUIT_RECOVERY_TIMEOUT", "180"))

    # Logging Configuration
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

settings = Settings()
