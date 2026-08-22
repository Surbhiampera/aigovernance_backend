"""Per-org / per-project model selection — which models an org or project is
allowed to use, and which one is used when a proxy request doesn't specify one.

Reuses the existing governance_rules table (no schema changes):
  metric_name="allowed_model"  scope_reference=<model>   — one row per allowed model
  metric_name="default_model"  scope_reference=<model>   — at most one row per scope

scope_level is "organization" (project_id NULL) or "project" (project_id set).
Enforcement of allowed/blocked models at request time lives in
governance_rule_service.py; this module only manages the rows and answers
"what's currently selected" / "what's the default" for a given scope.
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session


def _model_catalog_names(db: Session) -> set[str]:
    from app.services.ai_model_pricing import MODEL_PRICING
    from app.models import ModelPricing

    names = set(MODEL_PRICING.keys())
    names.update(r.model_name for r in db.query(ModelPricing.model_name).all())
    return names


def validate_models(db: Session, model_names: list[str]) -> None:
    """Raise 400 if any model name isn't in the known catalogue."""
    catalog = _model_catalog_names(db)
    unknown = [m for m in model_names if m not in catalog]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model(s): {', '.join(unknown)}. Check GET /models/catalog for valid names.",
        )


def set_allowed_models(
    db: Session,
    *,
    org_id: str,
    project_id: Optional[str],
    allowed_models: Optional[list[str]],
    default_model: Optional[str],
) -> None:
    """Replace the allowed_model / default_model governance rules for this scope.

    scope is "organization" when project_id is None, else "project". Pass
    allowed_models=None to leave the allow-list untouched (e.g. only setting
    a default). Pass an empty list to clear it entirely.
    """
    from app.models import GovernanceRule

    if allowed_models is None and default_model is None:
        return

    all_selected = list(dict.fromkeys((allowed_models or []) + ([default_model] if default_model else [])))
    if all_selected:
        validate_models(db, all_selected)
    if default_model and allowed_models and default_model not in allowed_models:
        raise HTTPException(
            status_code=400,
            detail=f"default_model '{default_model}' must be included in allowed_models.",
        )

    scope_level = "organization" if project_id is None else "project"

    if allowed_models is not None:
        db.query(GovernanceRule).filter(
            GovernanceRule.org_id == org_id,
            GovernanceRule.project_id == project_id,
            GovernanceRule.metric_name == "allowed_model",
        ).delete(synchronize_session=False)
        for model in allowed_models:
            db.add(GovernanceRule(
                rule_name=f"allowed_model:{scope_level}:{org_id}:{project_id or '-'}:{model}:{uuid.uuid4().hex[:8]}",
                metric_name="allowed_model",
                operator="=",
                threshold_value=0,
                severity="medium",
                scope_level=scope_level,
                scope_reference=model,
                is_active=True,
                org_id=org_id,
                project_id=project_id,
            ))

    if default_model is not None:
        db.query(GovernanceRule).filter(
            GovernanceRule.org_id == org_id,
            GovernanceRule.project_id == project_id,
            GovernanceRule.metric_name == "default_model",
        ).delete(synchronize_session=False)
        db.add(GovernanceRule(
            rule_name=f"default_model:{scope_level}:{org_id}:{project_id or '-'}:{uuid.uuid4().hex[:8]}",
            metric_name="default_model",
            operator="=",
            threshold_value=0,
            severity="low",
            scope_level=scope_level,
            scope_reference=default_model,
            is_active=True,
            org_id=org_id,
            project_id=project_id,
        ))


def get_model_selection(db: Session, *, org_id: str, project_id: Optional[str]) -> dict:
    """Return {"allowed_models": [...], "default_model": ...} for exactly this
    scope (does not fall back to the org-wide scope when called with a
    project_id — callers needing the effective default should use
    get_default_model)."""
    from app.models import GovernanceRule

    rows = (
        db.query(GovernanceRule)
        .filter(
            GovernanceRule.org_id == org_id,
            GovernanceRule.project_id == project_id,
            GovernanceRule.metric_name.in_(["allowed_model", "default_model"]),
            GovernanceRule.is_active.is_(True),
        )
        .all()
    )
    allowed = sorted(r.scope_reference for r in rows if r.metric_name == "allowed_model")
    default = next((r.scope_reference for r in rows if r.metric_name == "default_model"), None)
    return {"allowed_models": allowed, "default_model": default}


def get_default_model(db: Session, *, org_id: str, project_id: Optional[str]) -> Optional[str]:
    """Project-level default_model if set, else org-wide default_model, else None."""
    from app.models import GovernanceRule

    q = db.query(GovernanceRule).filter(
        GovernanceRule.org_id == org_id,
        GovernanceRule.metric_name == "default_model",
        GovernanceRule.is_active.is_(True),
    )
    if project_id:
        project_rule = q.filter(GovernanceRule.project_id == project_id).first()
        if project_rule:
            return project_rule.scope_reference
    org_rule = q.filter(GovernanceRule.project_id.is_(None)).first()
    return org_rule.scope_reference if org_rule else None
