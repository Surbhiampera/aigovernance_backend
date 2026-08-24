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
    """Only the models we actually have a deployment for, for populating a
    model-selection dropdown (Proxy Setup deployment form, Organization/Project
    creation). Sourced from STANDARD_MODEL_DEPLOYMENTS (what every new org gets
    auto-provisioned with) plus any model already registered as a
    ModelDeployment row — deliberately excludes every other model in
    MODEL_PRICING that we have no deployment for. Pricing/context metadata is
    filled in from MODEL_PRICING where the name matches.
    """
    from app.config import get_standard_model_deployments
    from app.models import ModelDeployment
    from app.services.ai_model_pricing import MODEL_PRICING

    deployed = {}
    for tpl in get_standard_model_deployments():
        model_name = tpl.get("model_name")
        if model_name:
            deployed[model_name] = tpl.get("provider")

    for row in (
        db.query(ModelDeployment.model_name, ModelDeployment.provider)
        .filter(ModelDeployment.is_active.is_(True))
        .distinct()
    ):
        deployed.setdefault(row.model_name, row.provider)

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
