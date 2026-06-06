"""Audit log read endpoints with RBAC.

General audit logs require any valid API key.
PII-flagged logs (compliance_relevant=True / audit_category='security')
require role 'admin' or 'security_reviewer'.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import get_db, require_api_key, require_role
from app.models import AuditLog

router = APIRouter(prefix="/audit-logs", tags=["audit-logs"])

_PII_ROLES = ("admin", "security_reviewer")


def _effective_org(key, org_id_param: Optional[str]) -> Optional[str]:
    """Return the org_id to scope the query.

    Master key (dict with org_id=None) may optionally pass org_id as a query
    param to narrow results. Org-level keys always use their own org_id.
    """
    if isinstance(key, dict):
        return org_id_param or None
    return getattr(key, "org_id", None)


def _serialize(row: AuditLog) -> dict:
    return {
        "audit_id": row.audit_id,
        "org_id": row.org_id,
        "project_id": row.project_id,
        "actor_type": row.actor_type,
        "actor_id": row.actor_id,
        "actor_ip": row.actor_ip,
        "audit_category": row.audit_category,
        "audit_action": row.audit_action,
        "audit_status": row.audit_status,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "request_id": row.request_id,
        "trace_id": row.trace_id,
        "policy_triggered": row.policy_triggered,
        "compliance_relevant": row.compliance_relevant,
        "requires_review": row.requires_review,
        "change_summary": row.change_summary,
        "metadata": row.audit_metadata,
        "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
    }


@router.get("")
def list_audit_logs(
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    audit_category: Optional[str] = Query(None),
    audit_action: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _key=Depends(require_api_key),
):
    """Return non-sensitive audit log rows (compliance_relevant=False).

    Any authenticated API key may call this endpoint.
    """
    q = db.query(AuditLog).filter(AuditLog.compliance_relevant == False)  # noqa: E712
    scoped_org = _effective_org(_key, org_id)
    if scoped_org:
        q = q.filter(AuditLog.org_id == scoped_org)
    if project_id:
        q = q.filter(AuditLog.project_id == project_id)
    if audit_category:
        q = q.filter(AuditLog.audit_category == audit_category)
    if audit_action:
        q = q.filter(AuditLog.audit_action == audit_action)
    rows = q.order_by(AuditLog.occurred_at.desc()).offset(offset).limit(limit).all()
    return {"total": len(rows), "offset": offset, "items": [_serialize(r) for r in rows]}


@router.get("/pii")
def list_pii_audit_logs(
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    requires_review: Optional[bool] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _key=Depends(require_role(*_PII_ROLES)),
):
    """Return PII-flagged audit log rows (compliance_relevant=True).

    Requires role 'admin' or 'security_reviewer'.
    """
    q = db.query(AuditLog).filter(AuditLog.compliance_relevant == True)  # noqa: E712
    scoped_org = _effective_org(_key, org_id)
    if scoped_org:
        q = q.filter(AuditLog.org_id == scoped_org)
    if project_id:
        q = q.filter(AuditLog.project_id == project_id)
    if requires_review is not None:
        q = q.filter(AuditLog.requires_review == requires_review)
    rows = q.order_by(AuditLog.occurred_at.desc()).offset(offset).limit(limit).all()
    return {"total": len(rows), "offset": offset, "items": [_serialize(r) for r in rows]}


@router.get("/summary")
def audit_log_summary(
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _key=Depends(require_role(*_PII_ROLES)),
):
    """Return counts grouped by category/action for compliance dashboards.

    Requires role 'admin' or 'security_reviewer'.
    """
    from sqlalchemy import func

    scoped_org = _effective_org(_key, org_id)
    q = db.query(
        AuditLog.audit_category,
        AuditLog.audit_action,
        AuditLog.audit_status,
        func.count(AuditLog.id).label("count"),
    )
    if scoped_org:
        q = q.filter(AuditLog.org_id == scoped_org)
    if project_id:
        q = q.filter(AuditLog.project_id == project_id)
    rows = q.group_by(
        AuditLog.audit_category, AuditLog.audit_action, AuditLog.audit_status
    ).all()
    return [
        {"category": r[0], "action": r[1], "status": r[2], "count": r[3]}
        for r in rows
    ]
