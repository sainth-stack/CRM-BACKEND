import logging
import sys
import os
import contextvars

# Global Contextual Trace Identifier: Thread/Async-safe campaign tracking
campaign_id_var = contextvars.ContextVar("campaign_id", default=None)

class CampaignFilter(logging.Filter):
    """
    Observability Filter: Injects the active Campaign ID from contextvars into log records.
    Ensures that distributed task logs can be correlated across asynchronous boundaries.
    """
    def filter(self, record):
        cid = campaign_id_var.get()
        record.campaign_id = f"[Campaign: {cid}] " if cid else ""
        return True

def setup_logging():
    """
    Enterprise Structured Logging Configuration.
    Configures the root logger for production-ready output to stdout/stderr with trace correlation.
    """
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()

    # Make the log stream UTF-8-safe so emoji/non-ASCII in log messages never raise
    # a UnicodeEncodeError on a non-UTF-8 console (e.g. Windows cp1252). No-op where
    # stdout is already UTF-8 (Linux/containers). backslashreplace guarantees output.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass

    # timestamp | level | [Campaign: ID] [module:line] | message
    log_format = "%(asctime)s | %(levelname)-8s | %(campaign_id)s[%(name)s:%(lineno)d] | %(message)s"
    
    # Root configuration
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level, logging.INFO))
    
    # Clear existing handlers
    if root_logger.hasHandlers():
        root_logger.handlers.clear()
        
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(log_format))
    handler.addFilter(CampaignFilter())
    root_logger.addHandler(handler)

    # Enable route visibility for operational observability
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    logger = logging.getLogger("ai_priori")
    logger.info(f"Distributed logging initialized at {log_level} level with Campaign Correlation Filter.")
    return logger

logger = logging.getLogger("ai_priori")
