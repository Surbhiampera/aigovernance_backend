from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import ModelPricing

router = APIRouter(prefix="/models", tags=["models"])


@router.get("/")
def list_models(db: Session = Depends(get_db)):
    rows = db.query(ModelPricing).order_by(ModelPricing.model_name.asc()).all()
    return [
        {
            "model_name": r.model_name,
            "provider": r.provider,
            "input_cost_per_1k": float(r.input_cost_per_1k or 0),
            "output_cost_per_1k": float(r.output_cost_per_1k or 0),
            "currency": r.currency,
        }
        for r in rows
    ]
