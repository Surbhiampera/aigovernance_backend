"""Dynamic lookup endpoints — every dropdown value is sourced from the
database (existing rows) or from injected configuration via env vars.

No hardcoded enums in source code.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.config import get_lookup_defaults
from app.core.deps import get_db
from app.models import (
    AiRequest,
    Alert,
    ApiKey,
    Budget,
    GovernanceRule,
    Organization,
    Project,
    RequestCost,
    User,
)

router = APIRouter(prefix="/lookups", tags=["lookups"])


def _merge(*sources: list[Optional[str]]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for source in sources:
        for value in source or []:
            if not value:
                continue
            key = str(value).strip()
            if not key or key.lower() in seen:
                continue
            seen.add(key.lower())
            out.append(key)
    return out


def _distinct(db: Session, column) -> list[Optional[str]]:
    return [row[0] for row in db.query(column).distinct().all()]


# ---------------------------------------------------------------------------
# Proxy / request lookups
# ---------------------------------------------------------------------------

@router.get("/providers")
def list_providers(*, db: Session = Depends(get_db)) -> list[str]:
    return _merge(
        get_lookup_defaults("PROVIDERS"),
        _distinct(db, AiRequest.model_name),
    )


@router.get("/request-types")
def list_request_types(*, db: Session = Depends(get_db)) -> list[str]:
    return _merge(
        get_lookup_defaults("REQUEST_TYPES"),
        _distinct(db, AiRequest.request_type),
    )


@router.get("/request-statuses")
def list_request_statuses(*, db: Session = Depends(get_db)) -> list[str]:
    return _merge(
        get_lookup_defaults("REQUEST_STATUSES"),
        _distinct(db, AiRequest.status),
    )


# ---------------------------------------------------------------------------
# Governance / rule lookups
# ---------------------------------------------------------------------------

@router.get("/rule-metrics")
def list_rule_metrics(*, db: Session = Depends(get_db)) -> list[str]:
    return _merge(get_lookup_defaults("RULE_METRICS"), _distinct(db, GovernanceRule.metric_name))


@router.get("/rule-scopes")
def list_rule_scopes(*, db: Session = Depends(get_db)) -> list[str]:
    return _merge(get_lookup_defaults("RULE_SCOPES"), _distinct(db, GovernanceRule.scope_level))


@router.get("/rule-operators")
def list_rule_operators(*, db: Session = Depends(get_db)) -> list[str]:
    return _merge(get_lookup_defaults("RULE_OPERATORS"), _distinct(db, GovernanceRule.operator))


@router.get("/severities")
def list_severities(*, db: Session = Depends(get_db)) -> list[str]:
    return _merge(
        get_lookup_defaults("SEVERITIES"),
        _distinct(db, GovernanceRule.severity),
        _distinct(db, Alert.severity),
    )


# ---------------------------------------------------------------------------
# Org / project / budget lookups
# ---------------------------------------------------------------------------

@router.get("/plan-types")
def list_plan_types(*, db: Session = Depends(get_db)) -> list[str]:
    return _merge(get_lookup_defaults("PLAN_TYPES"), _distinct(db, Organization.plan_type))


@router.get("/environments")
def list_environments(*, db: Session = Depends(get_db)) -> list[str]:
    return _merge(get_lookup_defaults("ENVIRONMENTS"), _distinct(db, Project.environment))


@router.get("/budget-periods")
def list_budget_periods(*, db: Session = Depends(get_db)) -> list[str]:
    return _merge(get_lookup_defaults("BUDGET_PERIODS"), _distinct(db, Budget.budget_type))


# ---------------------------------------------------------------------------
# Proxy-observed org / project IDs (for dashboard filters)
# ---------------------------------------------------------------------------

@router.get("/proxy-orgs")
def list_proxy_orgs(*, db: Session = Depends(get_db)) -> list[dict]:
    rows = (
        db.query(AiRequest.org_id)
        .distinct()
        .filter(AiRequest.org_id.isnot(None))
        .order_by(AiRequest.org_id)
        .all()
    )
    return [{"id": r[0], "label": r[0]} for r in rows if r[0]]


@router.get("/proxy-projects")
def list_proxy_projects(
    *,
    org_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> list[dict]:
    q = (
        db.query(AiRequest.project_id)
        .distinct()
        .filter(AiRequest.project_id.isnot(None))
        .order_by(AiRequest.project_id)
    )
    if org_id:
        q = q.filter(AiRequest.org_id == org_id)
    rows = q.all()
    return [{"id": r[0], "label": r[0]} for r in rows if r[0]]


# ---------------------------------------------------------------------------
# Scope references (for governance rule builder)
# ---------------------------------------------------------------------------

@router.get("/scope-references")
def list_scope_references(*, scope: str, db: Session = Depends(get_db)) -> list[dict]:
    scope = (scope or "").strip().lower()
    if scope == "organization":
        rows = db.query(Organization.id, Organization.org_name).all()
        return [{"id": r[0], "label": r[1] or r[0]} for r in rows]
    if scope == "project":
        rows = db.query(Project.id, Project.project_name).all()
        return [{"id": r[0], "label": r[1] or r[0]} for r in rows]
    if scope == "user":
        rows = db.query(User.id, User.email).all()
        return [{"id": r[0], "label": r[1] or r[0]} for r in rows]
    if scope == "api_key":
        rows = db.query(ApiKey.id, ApiKey.key_name).all()
        return [{"id": r[0], "label": r[1] or r[0]} for r in rows]
    return []
