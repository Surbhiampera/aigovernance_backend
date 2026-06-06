"""Immutable audit log writer for the proxy layer.

All proxy request lifecycle events (received, pii_scan, forwarded,
response_received, blocked, completed) are written here.  Rows are
never updated — only inserted.
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session


def _new_audit_id() -> str:
    return f"aud-{uuid.uuid4().hex[:20]}"


def log_event(
    db: Session,
    *,
    org_id: str,
    project_id: Optional[str] = None,
    audit_category: str,
    audit_action: str,
    audit_status: str = "success",
    actor_type: str = "system",
    actor_id: Optional[str] = None,
    actor_ip: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    request_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    policy_triggered: bool = False,
    compliance_relevant: bool = False,
    requires_review: bool = False,
    change_summary: Optional[str] = None,
    metadata: Optional[dict] = None,
    flush: bool = True,
) -> None:
    """Append one row to audit_logs. Never raises — errors are swallowed so
    audit failures never crash the proxy pipeline."""
    try:
        from app.models import AuditLog

        row = AuditLog(
            audit_id=_new_audit_id(),
            org_id=org_id,
            project_id=project_id,
            actor_type=actor_type,
            actor_id=actor_id,
            actor_ip=actor_ip,
            audit_category=audit_category,
            audit_action=audit_action,
            audit_status=audit_status,
            entity_type=entity_type,
            entity_id=entity_id,
            request_id=request_id,
            trace_id=trace_id,
            policy_triggered=policy_triggered,
            compliance_relevant=compliance_relevant,
            requires_review=requires_review,
            change_summary=change_summary,
            audit_metadata=metadata or {},
            occurred_at=datetime.utcnow(),
        )
        db.add(row)
        if flush:
            db.flush()
    except Exception:
        pass


def log_pii_detection(
    db: Session,
    *,
    org_id: str,
    project_id: Optional[str],
    request_id: str,
    pii_types: list[str],
    action_taken: str,
    actor_ip: Optional[str] = None,
) -> None:
    log_event(
        db,
        org_id=org_id,
        project_id=project_id,
        audit_category="security",
        audit_action="pii_detected",
        audit_status="warning" if action_taken == "alert" else ("failure" if action_taken == "block" else "success"),
        actor_type="governance_engine",
        actor_ip=actor_ip,
        entity_type="ai_request",
        entity_id=request_id,
        request_id=request_id,
        policy_triggered=True,
        compliance_relevant=True,
        requires_review=(action_taken == "block"),
        change_summary=f"PII detected: {', '.join(pii_types)} — action: {action_taken}",
        metadata={"pii_types": pii_types, "action": action_taken},
    )


def log_request_blocked(
    db: Session,
    *,
    org_id: str,
    project_id: Optional[str],
    request_id: str,
    reason: str,
    actor_ip: Optional[str] = None,
) -> None:
    log_event(
        db,
        org_id=org_id,
        project_id=project_id,
        audit_category="security",
        audit_action="blocked",
        audit_status="failure",
        actor_type="governance_engine",
        actor_ip=actor_ip,
        entity_type="ai_request",
        entity_id=request_id,
        request_id=request_id,
        policy_triggered=True,
        compliance_relevant=True,
        requires_review=True,
        change_summary=f"Request blocked: {reason}",
        metadata={"block_reason": reason},
    )
