from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
import datetime

from app.db.database import get_db
from app.db import models
from app.core.security import get_current_user, get_visibility_filter

router = APIRouter()

@router.get("/{dm_id}")
def get_prospect_details(
    dm_id: str, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Stakeholder Dossier Audit.
    Retrieves detailed communication history and metadata for a specific prospect.
    """
    # Sector Query Boundary: Guarantee hierarchical relationship ownership
    dm = db.query(models.DecisionMaker).join(models.Campaign).filter(
        models.DecisionMaker.id == dm_id,
        get_visibility_filter(db, current_user)
    ).first()
    if not dm:
        raise HTTPException(status_code=404, detail="Stakeholder not found")
    
    logs = []
    for log in dm.logs:
        log_dict = log.__dict__.copy()
        log_dict.pop("_sa_instance_state", None)
        logs.append(log_dict)
        
    result = {
        "id": dm.id,
        "name": dm.name,
        "position": dm.position,
        "email": dm.email,
        "linkedin": dm.linkedin,
        "status": dm.status,
        "company_name": dm.target_company.name if dm.target_company else "N/A",
        "logs": sorted(logs, key=lambda x: x.get('received_at') or datetime.datetime.min, reverse=True)
    }
    
    return result
