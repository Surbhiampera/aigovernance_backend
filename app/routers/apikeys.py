from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.core.deps import get_db
from app.models import ApiKey
from app.schemas import ApiKeyCreate, ApiKeyResponse

router = APIRouter(prefix="/api-keys", tags=["api-keys"])

@router.get("/", response_model=list[ApiKeyResponse])
def list_api_keys(org_id: Optional[str] = Query(None), project_id: Optional[str] = Query(None), db: Session = Depends(get_db)):
    query = db.query(ApiKey)
    if org_id:
        query = query.filter(ApiKey.org_id == org_id)
    if project_id:
        query = query.filter(ApiKey.project_id == project_id)
    return query.all()

@router.post("/", response_model=ApiKeyResponse)
def create_api_key(data: ApiKeyCreate, db: Session = Depends(get_db)):
    key = ApiKey(id=data.id, org_id=data.org_id, project_id=data.project_id, key_name=data.key_name, provider=data.provider)
    db.add(key)
    db.commit()
    db.refresh(key)
    return key

@router.delete("/{key_id}")
def delete_api_key(key_id: str, db: Session = Depends(get_db)):
    key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    db.delete(key)
    db.commit()
    return {"detail": "API key deleted"}
