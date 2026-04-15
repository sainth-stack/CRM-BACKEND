import os
import json
import requests
from typing import List, Dict, Optional
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from app.core.logging_config import logger

load_dotenv()

# --- CONFIGURATION ---
APOLLO_API_KEY = os.getenv("APOLLO_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

class ContactInfo(BaseModel):
    name: str
    title: str
    email: Optional[str]
    email_status: str
    phone: Optional[str]
    linkedin: Optional[str]
    seniority: str
    relevance_score: int
    relevance_justification: str

class PersonaMap(BaseModel):
    target_titles: List[str] = Field(description="5 high-probability decision maker titles for this specific industry.")

class ApolloDMAgent:
    """
    Sovereign Identity Discovery Agent (Apollo v4).
    Dynamically maps industries to personas and extracts verified PII (Email/Phone).
    """
    def __init__(self):
        if not APOLLO_API_KEY:
            logger.error("[APOLLO AGENT] API Key missing. Critical failure.")
        
        self.llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
        self.api_url = "https://api.apollo.io/v1/people/search"

    def _map_sector_to_titles(self, industry: str) -> List[str]:
        """
        Phase 1: Strategic Persona Mapping.
        Uses LLM to identify the 5 most likely buyers/decision makers for a given sector.
        """
        logger.info(f"[APOLLO AGENT] Mapping dynamic personas for sector: {industry}")
        
        structured_llm = self.llm.with_structured_output(PersonaMap)
        sys_prompt = "You are a Chief Revenue Officer. Given an industryvertical, list the 5 most common high-level job titles that represent strategic decision makers (buyers) in that specific industry."
        
        try:
            mapping = structured_llm.invoke([
                SystemMessage(content=sys_prompt),
                HumanMessage(content=f"Industry: {industry}")
            ])
            return mapping.target_titles
        except Exception as e:
            logger.error(f"[APOLLO AGENT] Persona mapping failed: {e}")
            return ["CEO", "Founder", "VP of Operations", "Director", "Owner"]

    def find_decision_makers(self, domain: str, industry: str) -> List[ContactInfo]:
        """
        Phase 2: High-Fidelity API Discovery.
        Executes a targeted search for verified executives with direct dials.
        """
        if not APOLLO_API_KEY:
            return []

        # 1. Generate sector-specific titles
        target_titles = self._map_sector_to_titles(industry)
        
        # 2. Prepare Apollo Payload
        # We specifically request direct phones and verified emails
        payload = {
            "api_key": APOLLO_API_KEY,
            "q_organization_domains": domain,
            "person_titles": target_titles,
            "prospected_by_current_team": [False], # Avoid duplicates within account
        }

        headers = {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache"
        }

        try:
            logger.info(f"[APOLLO AGENT] Executing lookup for {domain} with titles: {target_titles}")
            response = requests.post(self.api_url, json=payload, headers=headers, timeout=15)
            
            if response.status_code == 429:
                logger.warning("[APOLLO AGENT] Rate limit hit. Protocol: Throttled.")
                return []
            
            response.raise_for_status()
            data = response.json()
            
            raw_people = data.get("people", [])
            final_contacts = []

            for p in raw_people:
                # EDGE CASE: Strict verification audit
                # We only want 'verified' emails or high-fidelity data.
                email = p.get("email")
                email_status = p.get("email_status", "unknown")
                
                # Extract Phones (Logic: Prefer work_phone, fallback to mobile)
                phone = p.get("work_phone") or p.get("mobile_phone") or p.get("phone_numbers", [{}])[0].get("sanitized_number")

                if email_status == "verified" or email:
                    # Map Apollo seniority schema to our executive matrix
                    seniority = p.get("seniority", "Unknown")
                    
                    final_contacts.append(ContactInfo(
                        name=f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
                        title=p.get("title", "Unknown"),
                        email=email,
                        email_status=email_status,
                        phone=phone,
                        linkedin=p.get("linkedin_url"),
                        seniority=seniority,
                        relevance_score=90 if any(t.lower() in p.get("title", "").lower() for t in target_titles) else 70,
                        relevance_justification=f"Directly matched {industry} persona: {p.get('title')}"
                    ))

            # Return Top 3 unique high-level contacts
            return sorted(final_contacts, key=lambda x: x.relevance_score, reverse=True)[:3]

        except Exception as e:
            logger.error(f"[APOLLO AGENT] Handsake failed for {domain}: {e}")
            return []

if __name__ == "__main__":
    # --- ISOLATION TEST SUITE ---
    print("\n=== APOLLO DM AGENT ISOLATION TEST ===")
    agent = ApolloDMAgent()
    
    test_domain = "apple.com" 
    test_industry = "Consumer Electronics & Software"
    
    print(f"[*] Dispatching research for: {test_domain} ({test_industry})")
    contacts = agent.find_decision_makers(test_domain, test_industry)
    
    if not contacts:
        print("[!] Result: Zero verified identities discovered.")
    else:
        for i, c in enumerate(contacts):
            print(f"\n[DM #{i+1}] {c.name}")
            print(f"  > Title:  {c.title}")
            print(f"  > Email:  {c.email} ({c.email_status})")
            print(f"  > Phone:  {c.phone or 'N/A'}")
            print(f"  > Link:   {c.linkedin}")
