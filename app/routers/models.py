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
    """Only the models we actually have an admin-configured API key for, for
    populating a model-selection dropdown (Proxy Setup deployment form,
    Organization/Project creation). Sourced from STANDARD_MODEL_DEPLOYMENTS
    entries with an api_key set plus any ModelDeployment row with an api_key
    set — deliberately excludes every other model in MODEL_PRICING (and any
    deployment row with no credentials) since nothing could actually serve
    those requests. Pricing/context metadata is filled in from MODEL_PRICING
    where the name matches.
    """
    from app.services.ai_model_pricing import MODEL_PRICING
    from app.services.deployment_service import get_credentialed_model_names

    deployed = get_credentialed_model_names(db)

    catalog = []
    for name, fallback_provider in deployed.items():
        pricing = MODEL_PRICING.get(name)
        catalog.append({
            "model_name": name,
            "provider": (pricing.provider if pricing else fallback_provider) or "Other",
            "category": pricing.category if pricing else None,
            "input_per_1m": pricing.input_per_1m if pricing else None,
            "output_per_1m": pricing.output_per_1m if pricing else None,
            "context_window": pricing.context_window if pricing else None,
            "max_output_tokens": pricing.max_output_tokens if pricing else None,
        })

    catalog.sort(key=lambda m: (m["provider"] or "", m["model_name"]))
    return catalog
