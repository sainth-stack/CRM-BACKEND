"""
search_hints.py — Derive targeted enrichment search hints from user_intel + campaign prompt.

Completely sender-driven: no hardcoded domain vocabulary. The pain-side of the
sender's capability_to_pain_map is the only reference point, supplemented by
key phrases extracted from the campaign prompt via regex (no LLM call needed).

Output (search_hints) is passed to enrich_company() as an optional list so the
enrichment layer can run a targeted pain-signal search (Layer 6) using THIS
sender's actual pain vocabulary instead of any fixed industry terms.
"""
from __future__ import annotations

import re

# Words too generic to be useful search modifiers.
_STOP = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "shall", "can", "need",
    "such", "that", "this", "these", "those", "their", "they", "them",
    "we", "our", "you", "your", "it", "its", "any", "all", "both", "each",
    "more", "most", "other", "into", "through", "during", "before", "after",
    "focus", "identify", "look", "prioritize", "favor", "organizations",
    "companies", "seeking", "evaluating", "experiencing", "struggling",
    "enable", "improve", "reduce", "increase", "help", "ensure", "provide",
    "including", "solution", "solutions", "approach", "initiative", "program",
    "across", "between", "without", "within", "toward", "against", "about",
}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _extract_pain_phrases(capmap: list) -> list[str]:
    """Pull the pain side of each capability→pain entry."""
    out = []
    for item in capmap or []:
        pain = ""
        if isinstance(item, dict):
            pain = item.get("pain") or ""
        elif isinstance(item, str):
            pain = item
        pain = _clean(pain)
        if pain and len(pain) >= 6:
            out.append(pain[:100])
    return out


def _extract_prompt_phrases(prompt: str, max_phrases: int = 6) -> list[str]:
    """Extract 2-4 word noun phrases from the campaign prompt via regex.

    Splits on clause boundaries (comma, semicolon, newline), takes 2-4 word
    chunks that contain at least two non-stop-word tokens. No LLM needed.
    Works for any campaign domain because it reads the user's actual words.
    """
    if not prompt:
        return []

    clauses = re.split(r"[,;\n]", prompt)
    candidates: list[tuple[int, str]] = []  # (specificity_score, phrase)

    for clause in clauses:
        clause = clause.strip()
        # Find sequences of 2-4 consecutive meaningful words.
        words = re.findall(r"\b[a-zA-Z][a-zA-Z/-]{1,}\b", clause)
        words_lower = [w.lower() for w in words]

        for start in range(len(words)):
            for length in range(2, 5):  # 2, 3, 4 words
                end = start + length
                if end > len(words):
                    break
                chunk = words[start:end]
                chunk_lower = words_lower[start:end]
                non_stop = [w for w in chunk_lower if w not in _STOP]
                if len(non_stop) < 2:
                    continue
                phrase = " ".join(chunk).lower()
                if len(phrase) < 8:
                    continue
                candidates.append((len(non_stop), phrase))

    # Deduplicate, prefer longer/more-specific phrases, avoid substrings of longer picks.
    candidates.sort(key=lambda x: (-x[0], -len(x[1])))
    result: list[str] = []
    for _, phrase in candidates:
        if any(phrase in kept or kept in phrase for kept in result):
            continue
        result.append(phrase)
        if len(result) >= max_phrases:
            break

    return result


def derive_search_hints(
    user_intel: dict,
    campaign_prompt: str = "",
    max_total: int = 8,
) -> list[str]:
    """Return a deduped list of search hint phrases for targeted enrichment.

    Sources (in priority order):
      1. Pain-side phrases from user_intel.capability_to_pain_map
         (what THIS sender solves → what to look for in prospects)
      2. Key noun phrases extracted from the campaign prompt
         (what the user explicitly flagged as important for this run)

    No hardcoded domain vocabulary. Works for any sender × campaign combination.
    The returned list is passed to enrich_company(search_hints=...) so the
    enrichment layer 6 can run a targeted pain-signal DDGS search.
    """
    hints: list[str] = []

    # 1. Sender's pain map (highest specificity — directly from user_intel)
    capmap = user_intel.get("capability_to_pain_map") or []
    hints.extend(_extract_pain_phrases(capmap))

    # 2. Campaign prompt phrases (user's own words — fills gaps when capmap is thin)
    hints.extend(_extract_prompt_phrases(campaign_prompt, max_phrases=max_total))

    # Deduplicate while preserving order, cap length.
    seen: set[str] = set()
    result: list[str] = []
    for h in hints:
        h_norm = h.lower().strip()
        if h_norm and h_norm not in seen:
            # Skip a hint that is a substring of one already kept (less specific).
            if any(h_norm in kept for kept in seen):
                continue
            seen.add(h_norm)
            result.append(h)
        if len(result) >= max_total:
            break

    return result
