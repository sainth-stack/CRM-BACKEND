import pandas as pd
import io
import re
from urllib.parse import urlparse
from sqlalchemy.orm import Session
import logging

logger = logging.getLogger("CSVTrimmingEngine")

def get_fuzzy_map(cols):
    """
    Advanced Fuzzy Logic for SSoT Header Identification.
    Maps raw CSV headers to the new 60-field protocol.
    """
    f_map = {}
    # Base patterns
    patterns = {
        'contact_full_name': r'contact\s*full\s*name',
        'title': r'^title$',
        'seniority': r'^seniority$',
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
        'contact_mobile_phone': r'contact\s*mobile\s*phone$',
        'company_phone_1': r'company\s*phone\s*1'
    }

    # Dynamic Clustered Patterns (Email 1-10, Mobile 1-10)
    for i in range(1, 11):
        patterns[f'email_{i}'] = fr'^email\s*{i}$'
        patterns[f'email_{i}_validation'] = fr'^email\s*{i}\s*validation$'
        
        # Special case: The first mobile phone often has no number '1' in the base header
        if i == 1:
            patterns[f'contact_mobile_phone_{i}'] = fr'contact\s*mobile\s*phone$'
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
        "Contact Full Name", "Title", "Seniority", "Company Name - Cleaned", "Website", 
        "Primary Email", "Contact LI Profile URL", 
        "Email 1", "Email 1 Validation", "Email 2", "Email 2 Validation", 
        "Email 3", "Email 3 Validation", "Email 4", "Email 4 Validation", 
        "Email 5", "Email 5 Validation", "Email 6", "Email 6 Validation", 
        "Email 7", "Email 7 Validation", "Email 8", "Email 8 Validation", 
        "Email 9", "Email 9 Validation", "Email 10", "Email 10 Validation", 
        "Contact Phone 1", "Company Phone 1", "Contact Location", "Company Location", 
        "Company Description", "Company Website Domain", "Company Industry", 
        "Company LI Profile Url", "Company LinkedIn ID", "Company Revenue Range", 
        "Company Staff Count Range", "Time in Role", "Time at Company"
    ]

    def process_csv_content(self, content: bytes, target_location: str, target_industry: str, target_size: str, campaign_id: str, db: Session):
        """
        V2 High-Fidelity CSV Ingestion Engine.
        Processes SSoT artifacts into normalized contact/company clusters for Stage 3 validation.
        """
        # 1. Detect Encoding and Load
        encodings = ['utf-8', 'latin-1', 'utf-8-sig', 'cp1252', 'iso-8859-1']
        df = None
        
        # Try all encodings
        for enc in encodings:
            try:
                # Use engine='python' for better error handling on weirdly formatted CSVs
                df = pd.read_csv(io.BytesIO(content), on_bad_lines='skip', low_memory=False, encoding=enc)
                logger.info(f"Successfully ingested CSV with {enc} encoding.")
                break
            except Exception as e:
                logger.debug(f"Failed CSV ingestion with {enc}: {e}")
                continue

        # Final Hail Mary: Read as utf-8 with errors='replace' via string buffer
        if df is None:
            try:
                decoded = content.decode('utf-8', errors='replace')
                df = pd.read_csv(io.StringIO(decoded), on_bad_lines='skip', low_memory=False)
                logger.info("Successfully ingested CSV using UTF-8 Replacement Fallback.")
            except Exception as e:
                logger.error(f"Critical CSV Parse Failure: {e}")
                return {}, {}

        # Handle Duplicate Columns (Pandas creates .1, .2 suffix)
        # We deduplicate by keeping the first occurrence
        cols = pd.Series(df.columns)
        for dupe in cols[cols.duplicated()].unique():
            cols[cols == dupe] = [dupe if i == 0 else f"{dupe}_dupe_{i}" for i in range(cols[cols == dupe].count())]
        df.columns = cols

        # 2. Fuzzy Identity Mapping
        f_map = get_fuzzy_map(df.columns)
        df = df.rename(columns=f_map)

        # 3. Protocol-Strict Trimming (60+ Fields)
        REQUIRED_FIELDS = list(set(f_map.values())) # Only keep fields we identified
        df = df[[c for c in df.columns if c in REQUIRED_FIELDS]]

        unique_cos = {}
        contacts_map = {}

        for _, row in df.iterrows():
            # Domain Identification (The Anchor)
            raw_domain = row.get('company_website_domain') or row.get('website')
            if not raw_domain or pd.isna(raw_domain): continue
            
            domain = str(raw_domain).lower().strip().replace('https://', '').replace('http://', '').replace('www.', '').rstrip('/')
            if not domain or domain == 'nan': continue
            
            # Company Normalization
            if domain not in unique_cos:
                unique_cos[domain] = {
                    "name": row.get('company_name_cleaned') or domain,
                    "domain": domain,
                    "location": row.get('company_location'),
                    "industry": row.get('company_industry'),
                    "size": row.get('company_staff_count_range'),
                    "description": row.get('company_description'),
                    "revenue": row.get('company_revenue_range'),
                    "linkedin": row.get('company_li_profile_url'),
                    "linkedin_id": row.get('company_linkedin_id'),
                    "website": raw_domain
                }
            
            # Contact Clustering
            if domain not in contacts_map:
                contacts_map[domain] = []
            
            # Extract prospect with full metadata cluster
            prospect = row.to_dict()
            # Clean up: remove NaNs and keep only relevant fields
            prospect = {k: v for k, v in prospect.items() if pd.notna(v)}
            
            # Ensure email exists for basic identification
            if not prospect.get('primary_email') and not any(prospect.get(f'email_{i}') for i in range(1, 11)):
                continue

            contacts_map[domain].append(prospect)

        logger.info(f"📊 SSoT Ingestion Complete: {len(unique_cos)} unique companies identified.")
        return contacts_map, unique_cos

    def trim_csv(self, file_path: str, max_rows: int = 100) -> str:
        """
        Surgical Protocol: Trims the mission artifact to the high-fidelity batch limit.
        """
        try:
            # Detect Encoding
            encodings = ['utf-8', 'latin-1', 'utf-8-sig', 'cp1252']
            df = None
            for enc in encodings:
                try:
                    df = pd.read_csv(file_path, on_bad_lines='skip', low_memory=False, encoding=enc)
                    break
                except: continue

            if df is None:
                raise ValueError("Could not read CSV for trimming.")

            if df is not None:
                # Filter Columns
                existing_cols = [c for c in self.SELECTED_COLUMNS if c in df.columns]
                df = df[existing_cols]

                if len(df) > max_rows:
                    logger.info(f"✂️ [TRIMMER] Mission artifact exceeds limit ({len(df)} rows). Trimming to {max_rows}.")
                    df = df.head(max_rows)
                
                # Save back to disk (Replaces raw file)
                df.to_csv(file_path, index=False)
            
            return file_path
        except Exception as e:
            logger.error(f"Trimming Failure: {e}")
            return file_path

    def trim_csv_from_bytes(self, content: bytes, max_rows: int = 100) -> str:
        """
        Surgical Protocol: Trims the mission artifact in-memory.
        Returns the trimmed content as a UTF-8 string.
        """
        try:
            # Detect Encoding
            encodings = ['utf-8', 'latin-1', 'utf-8-sig', 'cp1252']
            df = None
            for enc in encodings:
                try:
                    df = pd.read_csv(io.BytesIO(content), on_bad_lines='skip', low_memory=False, encoding=enc)
                    break
                except: continue

            if df is None:
                raise ValueError("Could not read CSV bytes for trimming.")

            # Filter Columns
            existing_cols = [c for c in self.SELECTED_COLUMNS if c in df.columns]
            df = df[existing_cols]

            if len(df) > max_rows:
                logger.info(f"✂️ [IN-MEMORY TRIMMER] Trimming to {max_rows} rows.")
                df = df.head(max_rows)
            
            return df.to_csv(index=False)
        except Exception as e:
            logger.error(f"In-Memory Trimming Failure: {e}")
            return content.decode('utf-8', errors='ignore')
