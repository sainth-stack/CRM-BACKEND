import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    # Timeouts & Windows
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

settings = Settings()
