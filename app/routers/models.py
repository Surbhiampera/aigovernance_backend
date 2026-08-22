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


@router.get("/catalog")
def list_model_catalog(db: Session = Depends(get_db)):
    """Every model the platform knows how to price/route, for populating a
    model-selection dropdown (Proxy Setup deployment form, Organization/Project
    creation). Sourced from the static pricing catalogue plus any custom rows
    added to the model_pricing table that aren't already in the static one.
    """
    from app.services.ai_model_pricing import MODEL_PRICING

    catalog = [
        {
            "model_name": name,
            "provider": p.provider,
            "category": p.category,
            "input_per_1m": p.input_per_1m,
            "output_per_1m": p.output_per_1m,
            "context_window": p.context_window,
            "max_output_tokens": p.max_output_tokens,
        }
        for name, p in MODEL_PRICING.items()
    ]

    static_names = set(MODEL_PRICING.keys())
    for r in db.query(ModelPricing).order_by(ModelPricing.model_name.asc()).all():
        if r.model_name in static_names:
            continue
        catalog.append({
            "model_name": r.model_name,
            "provider": r.provider,
            "category": None,
            "input_per_1m": float(r.input_cost_per_1k or 0) * 1000,
            "output_per_1m": float(r.output_cost_per_1k or 0) * 1000,
            "context_window": None,
            "max_output_tokens": None,
        })

    catalog.sort(key=lambda m: (m["provider"] or "", m["model_name"]))
    return catalog
