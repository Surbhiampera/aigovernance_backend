"""Dynamic lookup endpoints — every dropdown value is sourced from the
database (existing rows) or from injected configuration via env vars.

No hardcoded enums in source code. Operators (``>``, ``<`` …) are the only
literals here because they are language tokens, not business data.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.config import get_lookup_defaults
from app.core.deps import get_db
from app.models import (
    Alert,
    ApiKey,
    Budget,
    GovernanceRule,
    Organization,
    Project,
    TelemetryEvent,
    ToolConnector,
    ToolRegistry,
    User,
)

router = APIRouter(prefix="/lookups", tags=["lookups"])


def _merge(*sources: list[Optional[str]]) -> list[str]:
    """Merge any number of value lists, dedupe (case-insensitive) and preserve order."""
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


@router.get("/auth-types")
def list_auth_types(db: Session = Depends(get_db)) -> list[str]:
    """Connector auth types — sourced from DB + LOOKUP_AUTH_TYPES env var."""
    return _merge(get_lookup_defaults("AUTH_TYPES"), _distinct(db, ToolConnector.auth_type))


@router.get("/ingestion-modes")
def list_ingestion_modes(db: Session = Depends(get_db)) -> list[str]:
    return _merge(
        get_lookup_defaults("INGESTION_MODES"),
        _distinct(db, ToolConnector.ingestion_mode),
    )


@router.get("/connector-statuses")
def list_connector_statuses(db: Session = Depends(get_db)) -> list[str]:
    return _merge(
        get_lookup_defaults("CONNECTOR_STATUSES"),
        _distinct(db, ToolConnector.status),
    )


@router.get("/tool-types")
def list_tool_types(db: Session = Depends(get_db)) -> list[str]:
    return _merge(get_lookup_defaults("TOOL_TYPES"), _distinct(db, ToolRegistry.tool_type))


@router.get("/providers")
def list_providers(db: Session = Depends(get_db)) -> list[str]:
    return _merge(
        get_lookup_defaults("PROVIDERS"),
        _distinct(db, ToolRegistry.vendor),
        _distinct(db, ToolConnector.provider),
        _distinct(db, TelemetryEvent.provider),
    )


@router.get("/rule-metrics")
def list_rule_metrics(db: Session = Depends(get_db)) -> list[str]:
    """Rule-engine metric options — config-injected + values used by existing rules."""
    return _merge(get_lookup_defaults("RULE_METRICS"), _distinct(db, GovernanceRule.metric_name))


@router.get("/rule-scopes")
def list_rule_scopes(db: Session = Depends(get_db)) -> list[str]:
    return _merge(get_lookup_defaults("RULE_SCOPES"), _distinct(db, GovernanceRule.scope_level))


@router.get("/rule-operators")
def list_rule_operators(db: Session = Depends(get_db)) -> list[str]:
    return _merge(get_lookup_defaults("RULE_OPERATORS"), _distinct(db, GovernanceRule.operator))


@router.get("/severities")
def list_severities(db: Session = Depends(get_db)) -> list[str]:
    return _merge(
        get_lookup_defaults("SEVERITIES"),
        _distinct(db, GovernanceRule.severity),
        _distinct(db, Alert.severity),
    )


@router.get("/event-statuses")
def list_event_statuses(db: Session = Depends(get_db)) -> list[str]:
    """Telemetry-event statuses — observed in DB + injected defaults."""
    return _merge(
        get_lookup_defaults("EVENT_STATUSES"),
        _distinct(db, TelemetryEvent.status),
    )


@router.get("/plan-types")
def list_plan_types(db: Session = Depends(get_db)) -> list[str]:
    return _merge(get_lookup_defaults("PLAN_TYPES"), _distinct(db, Organization.plan_type))


@router.get("/environments")
def list_environments(db: Session = Depends(get_db)) -> list[str]:
    return _merge(get_lookup_defaults("ENVIRONMENTS"), _distinct(db, Project.environment))


@router.get("/budget-periods")
def list_budget_periods(db: Session = Depends(get_db)) -> list[str]:
    return _merge(get_lookup_defaults("BUDGET_PERIODS"), _distinct(db, Budget.budget_type))


@router.get("/tracing-orgs")
def list_tracing_orgs(db: Session = Depends(get_db)) -> list[dict]:
    """Distinct org_ids observed in telemetry — Tracing Module as single source of truth."""
    rows = (
        db.query(TelemetryEvent.org_id)
        .distinct()
        .filter(TelemetryEvent.org_id.isnot(None))
        .order_by(TelemetryEvent.org_id)
        .all()
    )
    return [{"id": r[0], "label": r[0]} for r in rows if r[0]]


@router.get("/tracing-projects")
def list_tracing_projects(
    org_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Distinct project_ids observed in telemetry, optionally scoped to an org."""
    query = (
        db.query(TelemetryEvent.project_id)
        .distinct()
        .filter(TelemetryEvent.project_id.isnot(None))
        .order_by(TelemetryEvent.project_id)
    )
    if org_id:
        query = query.filter(TelemetryEvent.org_id == org_id)
    rows = query.all()
    return [{"id": r[0], "label": r[0]} for r in rows if r[0]]


@router.get("/scope-references")
def list_scope_references(scope: str, db: Session = Depends(get_db)) -> list[dict]:
    """Return reference IDs for a chosen scope so admins can target rules precisely.

    The scope name itself is data (not a hardcoded enum) — it must be one of
    the values returned by ``/lookups/rule-scopes``.
    """
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
    if scope == "tool":
        rows = db.query(ToolRegistry.tool_name).all()
        return [{"id": r[0], "label": r[0]} for r in rows]
    return []
