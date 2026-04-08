import logging
import sys
import os

def setup_logging():
    """
    Enterprise Structured Logging Configuration.
    Configures the root logger for production-ready output to stdout/stderr.
    """
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    
    # Define a clean, machine-parsable format (or at least consistent)
    # timestamp | level | [module:line] | message
    log_format = "%(asctime)s | %(levelname)-8s | [%(name)s:%(lineno)d] | %(message)s"
    
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format=log_format,
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )

    # Suppress noise from verbose libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    logger = logging.getLogger("ai_priori")
    logger.info(f"Logging initialized at {log_level} level")
    return logger

logger = logging.getLogger("ai_priori")
