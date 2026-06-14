import re
import unicodedata
from app.core.logging_config import logger

def sanitize_text(text: str, max_length: int = 500) -> str:
    """
    Standard Operational Text Normalization.
    Strips leading/trailing whitespace, removes non-printable characters, 
    and enforces maximum length constraints to prevent resource exhaustion.
    """
    if not text:
        return ""
    
    # 1. Normalize Unicode (NFKC) to handle various encoding variants
    text = unicodedata.normalize('NFKC', text)
    
    # 2. Remove control characters and non-printable sequences
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    
    # 3. Strip and Truncate
    text = text.strip()
    if len(text) > max_length:
        logger.debug(f"[SANITIZER] Truncating input from {len(text)} to {max_length} characters.")
        text = text[:max_length]
    
    return text

def strip_null_bytes(value):
    """Recursively remove NUL (0x00) bytes from strings (incl. inside lists/dicts).

    PostgreSQL TEXT/VARCHAR cannot store a NUL byte and rejects the ENTIRE insert
    with 'A string literal cannot contain NUL (0x00) characters', silently dropping
    an otherwise-valid row. Scraped website text and messy CSV cells occasionally
    contain stray NULs, so we scrub them at the DB-write boundary. NUL is the only
    byte Postgres text columns forbid; other control chars are left untouched."""
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, list):
        return [strip_null_bytes(v) for v in value]
    if isinstance(value, tuple):
        return tuple(strip_null_bytes(v) for v in value)
    if isinstance(value, dict):
        return {k: strip_null_bytes(v) for k, v in value.items()}
    return value


def sanitize_html(html_content: str) -> str:
    """Strip script/style tags and HTML markup to produce clean text for LLM input
    (also removes XSS artifacts)."""
    if not html_content:
        return ""
    
    # Remove Scripts and Styles entirely
    clean_text = re.sub(r'<(script|style|nav|footer|header)[^>]*?>.*?</\1>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    
    # Strip remaining HTML tags
    clean_text = re.sub(r'<[^>]*?>', ' ', clean_text)
    
    # Collapse multiple whitespaces/newlines into a single space
    clean_text = re.sub(r'\s+', ' ', clean_text)
    
    return clean_text.strip()

def sanitize_for_llm(text: str, context_limit: int = 15000) -> str:
    """
    Injection Shield & Context Optimizer.
    Protects the LLM system prompt from 'Ignore previous instructions' tokens, 
    nested triple-backticks, and system-level prompt hijacking patterns.
    """
    if not text:
        return ""
        
    # 1. Mitigate Prompt Injection Tokens
    dangerous_patterns = [
        r"(?i)ignore\s+previous\s+instructions",
        r"(?i)system:",
        r"(?i)user:",
        r"(?i)assistant:",
        r"\[INST\]",
        r"<<SYS>>",
        r"<s>",
        r"</s>"
    ]
    
    for pattern in dangerous_patterns:
        text = re.sub(pattern, "[FILTERED]", text)
        
    # 2. Neutralize triple backticks to prevent context breakout
    text = text.replace("```", "'''")
    
    # 3. Truncate for high-performance context window management
    if len(text) > context_limit:
        logger.warning(f"[SANITIZER] Context overflow intercepted. Truncating {len(text)} chars to {context_limit}.")
        text = text[:context_limit]
        
    return text
