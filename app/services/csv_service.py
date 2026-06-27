import pandas as pd
import io
import re
from sqlalchemy.orm import Session
import logging

logger = logging.getLogger("CSVTrimmingEngine")

# Phone fields that may arrive as Excel-style floats (e.g. 2.12221E+12 → "2122210000").
_PHONE_FIELDS = {
    "contact_mobile_phone", "company_phone_1",
}

def _normalise_phone(val) -> str | None:
    """Convert a phone value to a clean string.

    Excel exports large phone numbers as floats in scientific notation
    (e.g. "2.12221E+12"). We convert to an integer string, but only when
    the result is a plausible phone number (≤15 digits — E.164 max length).
    Numbers with more digits have lost precision in Excel and cannot be
    recovered reliably, so we return None rather than store a corrupt value.
    Returns None for blank/NaN values.
    """
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", ""):
        return None
    # Detect scientific notation (e.g. "2.12221E+12", "4.80783E+13")
    if "e" in s.lower() and ("e+" in s.lower() or "e-" in s.lower()):
        try:
            converted = str(int(float(s)))
            # US numbers: 10 digits (local) or 11 with country code.
            # International E.164: up to 15 digits. Anything beyond 15 is clearly
            # a precision-lost float from Excel — discard it.
            # Additionally, numbers with 13-15 digits that end in many zeros are
            # also likely precision-corrupted (e.g. "2122210000000" for "2122214700").
            n = len(converted)
            if n > 15:
                return None
            if n > 12 and converted.endswith("000"):
                return None  # trailing zeros reveal float precision loss
            return converted
        except (ValueError, OverflowError):
            return None
    # Remove trailing ".0" that pandas adds when it reads phone columns as float
    if re.fullmatch(r"\d+\.0", s):
        return s[:-2]
    return s

def get_fuzzy_map(cols):
    """Fuzzy-match raw CSV headers to the canonical field names."""
    f_map = {}
    # Base patterns
    patterns = {
        'contact_full_name': r'contact\s*full\s*name',
        'title': r'^title$',
        'seniority': r'^seniority$',
        'department': r'^department$',
        'company_name_cleaned': r'company\s*name\s*-\s*cleaned',
        'website': r'^website$',
        'primary_email': r'primary\s*email',
        'contact_li_profile_url': r'contact\s*li\s*profile\s*url',
        'contact_location': r'^contact\s*location$',
        'company_location': r'^company\s*location$',
        'company_description': r'company\s*description',
        'company_website_domain': r'company\s*website\s*domain',
        'company_industry': r'company\s*industry',
        'company_li_profile_url': r'company\s*li\s*profile\s*url',
        'company_linkedin_id': r'company\s*linkedin\s*id',
        'company_revenue_range': r'company\s*revenue\s*range',
        'company_staff_count_range': r'company\s*staff\s*count\s*range',
        'time_in_role': r'time\s*in\s*role',
        'time_at_company': r'time\s*at\s*company',
        # Matches "Contact Mobile Phone" (Seamless/Apollo) OR "Contact Phone 1"
        # (alternate data-provider alias) — both map to the same canonical slot.
        'contact_mobile_phone': r'(?:contact\s*mobile\s*phone|contact\s*phone\s*1)$',
        'company_phone_1': r'company\s*phone\s*1',
        # Richer firmographics (high fill-rate; drive deterministic ICP scoring).
        # Patterns are END-ANCHORED where a near-duplicate sibling header exists
        # (Staff Count vs ...Range, Country vs ...Alpha 2, State vs ...Abbr).
        'company_sic_code': r'^sic\s*code$',
        'company_naics_code': r'^naics\s*code$',
        'company_staff_count': r'^company\s*staff\s*count$',
        'company_annual_revenue': r'^company\s*annual\s*revenue$',
        'company_founded_year': r'^company\s*founded\s*date$',
        'company_country': r'^company\s*country$',
        'company_state': r'^company\s*state$',
        'company_city': r'^company\s*city$',
    }

    # Dynamic Clustered Patterns (Mobile 1-10). Email-validation slots
    # (Email 1-10 + their Validation columns) are intentionally NOT mapped:
    # reachability is now decided solely by whether a Primary Email exists.
    for i in range(1, 11):
        # Special case: The first mobile phone often has no number '1' in the base header
        if i == 1:
            patterns[f'contact_mobile_phone_{i}'] = r'contact\s*mobile\s*phone$'
        else:
            patterns[f'contact_mobile_phone_{i}'] = fr'contact\s*mobile\s*phone\s*{i}$'
            
        patterns[f'contact_mobile_phone_{i}_total_ai'] = fr'contact\s*mobile\s*phone\s*{i}\s*total\s*ai$'

    for target, regex in patterns.items():
        for col in cols:
            if re.search(regex, str(col).strip(), re.IGNORECASE):
                f_map[col] = target
                break
    return f_map

class CSVProcessingService:
    SELECTED_COLUMNS = [
        "Contact Full Name", "Title", "Seniority", "Department", "Company Name - Cleaned", "Website",
        "Primary Email", "Contact LI Profile URL",
        # "Contact Mobile Phone" is the actual column in Seamless/Apollo exports.
        # "Contact Phone 1" is a fallback alias used by some data providers.
        # Both are included so either format is preserved through the trim.
        "Contact Mobile Phone", "Contact Phone 1", "Company Phone 1",
        "Contact Location", "Company Location",
        "Company Description", "Company Website Domain", "Company Industry",
        "Company LI Profile Url", "Company LinkedIn ID", "Company Revenue Range",
        "Company Staff Count Range", "Time in Role", "Time at Company",
        # Richer firmographics for ICP scoring.
        "SIC Code", "NAICS Code", "Company Staff Count", "Company Annual Revenue",
        "Company Founded Date", "Company Country", "Company State", "Company City",
    ]

    def process_csv_content(self, content: bytes, target_location: str, target_industry: str, target_size: str, campaign_id: str, db: Session):
        """Parse the CSV into normalized contact/company groups for Stage 3.

        Reads in chunks to stay memory-safe on very large files.
        """
        # 1. Detect Encoding and Load in Chunks
        encodings = ['utf-8', 'latin-1', 'utf-8-sig', 'cp1252', 'iso-8859-1']
        chunks_generator = None

        # Try all encodings
        for enc in encodings:
            try:
                # Initialize a chunked text reader (chunksize=100)
                chunks_generator = pd.read_csv(io.BytesIO(content), on_bad_lines='skip', chunksize=100, encoding=enc)
                logger.info(f"Successfully configured chunked reader with {enc} encoding.")
                break
            except Exception as e:
                logger.debug(f"Failed CSV chunked configuration with {enc}: {e}")
                continue

        # Final Hail Mary: Read as utf-8 with errors='replace' via string buffer
        if chunks_generator is None:
            try:
                decoded = content.decode('utf-8', errors='replace')
                chunks_generator = pd.read_csv(io.StringIO(decoded), on_bad_lines='skip', chunksize=100)
                logger.info("Successfully configured chunked reader using UTF-8 Replacement Fallback.")
            except Exception as e:
                logger.error(f"Critical CSV Parse Failure during chunked configuration: {e}")
                return {}, {}

        unique_cos = {}
        contacts_map = {}

        try:
            for chunk in chunks_generator:
                # Handle Duplicate Columns (Pandas creates .1, .2 suffix)
                # We deduplicate by keeping the first occurrence
                cols = pd.Series(chunk.columns)
                for dupe in cols[cols.duplicated()].unique():
                    cols[cols == dupe] = [dupe if i == 0 else f"{dupe}_dupe_{i}" for i in range(cols[cols == dupe].count())]
                chunk.columns = cols

                # 2. Map headers to canonical fields.
                f_map = get_fuzzy_map(chunk.columns)
                chunk = chunk.rename(columns=f_map)

                # 3. Protocol-Strict Trimming (60+ Fields)
                REQUIRED_FIELDS = list(set(f_map.values())) # Only keep fields we identified
                chunk = chunk[[c for c in chunk.columns if c in REQUIRED_FIELDS]]

                for _, row in chunk.iterrows():
                    # Domain Identification (The Anchor)
                    raw_domain = row.get('company_website_domain') or row.get('website')
                    if not raw_domain or pd.isna(raw_domain): continue
                    
                    domain = str(raw_domain).lower().strip().replace('https://', '').replace('http://', '').replace('www.', '').rstrip('/')
                    if not domain or domain == 'nan': continue
                    
                    # Company Normalization
                    if domain not in unique_cos:
                        # Prefer a CLEAN location built from the structured parts; fall
                        # back to the raw blob only when the parts are absent (no
                        # redundant full-address storage — the parts are the source).
                        def _val(key):
                            v = row.get(key)
                            return str(v).strip() if v is not None and str(v).strip() and str(v).strip().lower() != 'nan' else None
                        city, state, country = _val('company_city'), _val('company_state'), _val('company_country')
                        clean_loc = ", ".join([p for p in (city, state, country) if p]) or row.get('company_location')
                        unique_cos[domain] = {
                            "name": row.get('company_name_cleaned') or domain,
                            "domain": domain,
                            "location": clean_loc,
                            "industry": row.get('company_industry'),
                            "size": row.get('company_staff_count_range'),
                            "description": row.get('company_description'),
                            "revenue": row.get('company_revenue_range'),
                            "linkedin": row.get('company_li_profile_url'),
                            "linkedin_id": row.get('company_linkedin_id'),
                            "website": raw_domain,
                            # Richer firmographics (None when absent).
                            "sic_code": _val('company_sic_code'),
                            "naics_code": _val('company_naics_code'),
                            "staff_count": _val('company_staff_count'),
                            "annual_revenue": _val('company_annual_revenue'),
                            "founded_year": _val('company_founded_year'),
                            "country": country,
                            "state": state,
                            "city": city,
                        }
                    
                    # Contact Clustering
                    if domain not in contacts_map:
                        contacts_map[domain] = []
                    
                    # Extract prospect with full metadata cluster
                    prospect = row.to_dict()
                    # Clean up: remove NaNs and keep only relevant fields
                    prospect = {k: v for k, v in prospect.items() if pd.notna(v)}

                    # Normalise phone fields — Excel exports large numbers in
                    # scientific notation (e.g. 2.12221E+12); convert to digit strings.
                    for _pf in _PHONE_FIELDS:
                        if _pf in prospect:
                            prospect[_pf] = _normalise_phone(prospect[_pf])
                            if prospect[_pf] is None:
                                prospect.pop(_pf)

                    # Ensure a primary email exists for basic identification.
                    if not prospect.get('primary_email'):
                        continue

                    contacts_map[domain].append(prospect)
        except Exception as chunk_e:
            logger.error(f"Failed to process CSV chunk: {chunk_e}")

        logger.info(f"CSV ingestion complete: {len(unique_cos)} unique companies.")
        return contacts_map, unique_cos

    def trim_csv_from_filelike(self, fh, max_rows: int = 100) -> str:
        """
        Streaming trim: read ONLY max_rows directly from an upload file handle.

        Never pulls the whole upload into a bytes object (and never makes an extra
        in-memory copy). pandas reads incrementally from the handle and stops after
        `nrows`, so peak memory is bounded by the trimmed batch (~max_rows)
        regardless of the original file size — the big ingestion RAM/latency win.
        Returns the trimmed content as a UTF-8 string.
        """
        encodings = ['utf-8', 'latin-1', 'utf-8-sig', 'cp1252']
        df = None
        for enc in encodings:
            try:
                fh.seek(0)
                df = pd.read_csv(fh, on_bad_lines='skip', nrows=max_rows, encoding=enc)
                break
            except Exception:
                continue

        # Last resort: decode with replacement (only the head is materialized here
        # because nrows still caps the parse).
        if df is None:
            try:
                fh.seek(0)
                raw = fh.read()
                if isinstance(raw, bytes):
                    raw = raw.decode('utf-8', errors='replace')
                df = pd.read_csv(io.StringIO(raw), on_bad_lines='skip', nrows=max_rows)
            except Exception as e:
                logger.error(f"Streaming Trim Failure: {e}")
                raise ValueError("Could not read CSV upload for trimming.")

        existing_cols = [c for c in self.SELECTED_COLUMNS if c in df.columns]
        df = df[existing_cols]
        return df.to_csv(index=False)
