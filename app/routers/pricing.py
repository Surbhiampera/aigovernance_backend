from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import ModelPricing
from app.schemas import ModelPricingCreate, ModelPricingResponse

router = APIRouter(prefix="/pricing", tags=["pricing"])


@router.get("/", response_model=list[ModelPricingResponse])
def list_pricing(db: Session = Depends(get_db)):
    return db.query(ModelPricing).order_by(ModelPricing.provider.asc(), ModelPricing.model_name.asc()).all()


@router.post("/", response_model=ModelPricingResponse)
def create_or_update_pricing(data: ModelPricingCreate, db: Session = Depends(get_db)):
    existing = (
        db.query(ModelPricing)
        .filter(ModelPricing.provider == data.provider, ModelPricing.model_name == data.model_name)
        .first()
    )
    if existing:
        existing.input_cost_per_1k = data.input_cost_per_1k
        existing.output_cost_per_1k = data.output_cost_per_1k
        existing.currency = data.currency
    else:
        existing = ModelPricing(**data.model_dump())
        db.add(existing)
    db.commit()
    db.refresh(existing)
    return existing


@router.delete("/{pricing_id}")
def delete_pricing(pricing_id: int, db: Session = Depends(get_db)):
    row = db.query(ModelPricing).filter(ModelPricing.id == pricing_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Pricing entry not found")
    db.delete(row)
    db.commit()
    return {"detail": "Pricing entry deleted"}
