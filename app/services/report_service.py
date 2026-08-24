"""Per-project governance report — aggregates cost, budget, governance-rule,
alert and audit data into a single payload consumed by the report exporters
(app/services/report_exporters.py) and the JSON preview endpoint
(GET /reports/projects/{project_id}).
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models import (
    Alert,
    AiRequest,
    AuditLog,
    Budget,
    GovernanceRule,
    Organization,
    Project,
    RequestCost,
)


def _date_filter(query, model, *, start: Optional[date], end: Optional[date]):
    if start:
        query = query.filter(func.date(model.created_at) >= start)
    if end:
        query = query.filter(func.date(model.created_at) <= end)
    return query


def build_project_report(
    db: Session,
    *,
    project_id: str,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> Optional[dict]:
    """Assemble the full report payload for one project, or None if it doesn't exist."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return None
    org = db.query(Organization).filter(Organization.id == project.org_id).first()

    # ---- cost summary -----------------------------------------------------
    cost_q = db.query(
        func.count(
            func.distinct(func.coalesce(AiRequest.parent_request_id, AiRequest.request_id))
        ).label("total_requests"),
        func.sum(RequestCost.input_tokens).label("input_tokens"),
        func.sum(RequestCost.output_tokens).label("output_tokens"),
        func.sum(RequestCost.total_tokens).label("total_tokens"),
        func.sum(RequestCost.input_token_cost).label("input_cost"),
        func.sum(RequestCost.output_token_cost).label("output_cost"),
        func.sum(RequestCost.total_cost).label("total_cost"),
    ).join(AiRequest, AiRequest.request_id == RequestCost.request_id).filter(
        RequestCost.project_id == project_id
    )
    cost_q = _date_filter(cost_q, RequestCost, start=start, end=end)
    totals = cost_q.first()

    # ---- cost by model ------------------------------------------------------
    model_q = db.query(
        RequestCost.model_name,
        RequestCost.provider,
        func.count(
            func.distinct(func.coalesce(AiRequest.parent_request_id, AiRequest.request_id))
        ).label("total_requests"),
        func.sum(RequestCost.total_tokens).label("total_tokens"),
        func.sum(RequestCost.total_cost).label("total_cost"),
    ).join(AiRequest, AiRequest.request_id == RequestCost.request_id).filter(
        RequestCost.project_id == project_id
    )
    model_q = _date_filter(model_q, RequestCost, start=start, end=end)
    by_model_rows = (
        model_q.group_by(RequestCost.model_name, RequestCost.provider)
        .order_by(func.sum(RequestCost.total_cost).desc())
        .all()
    )

    # ---- budgets & utilization (spend measured over the report period, or
    # all time when no range is given) --------------------------------------
    budgets = db.query(Budget).filter(Budget.project_id == project_id).all()
    budget_rows = []
    for b in budgets:
        spend_q = db.query(
            func.coalesce(func.sum(RequestCost.total_cost), Decimal("0"))
        ).filter(RequestCost.project_id == project_id)
        spend_q = _date_filter(spend_q, RequestCost, start=start, end=end)
        spend = float(spend_q.scalar() or 0)

        limit = float(b.limit_amount) if b.limit_amount else None
        threshold = b.alert_threshold_percent or 80
        if limit and limit > 0:
            pct = round(spend / limit * 100, 1)
            status = "exceeded" if pct >= 100 else ("warning" if pct >= threshold else "ok")
        else:
            pct = 0.0
            status = "no_budget"
        budget_rows.append({
            "budget_type": b.budget_type,
            "limit_amount": limit,
            "current_spend": spend,
            "alert_threshold_percent": threshold,
            "utilization_percent": pct,
            "status": status,
        })

    # ---- governance rules in effect for this project -----------------------
    # Org-wide rules (project_id IS NULL) apply to every project; project-scoped
    # rules apply only to this one. Same scoping as the proxy enforcement path
    # (see app/services/governance_rule_service.py::_load_rules).
    rules = (
        db.query(GovernanceRule)
        .filter(
            GovernanceRule.is_active.is_(True),
            GovernanceRule.org_id == project.org_id,
            or_(
                GovernanceRule.project_id.is_(None),
                GovernanceRule.project_id == project_id,
            ),
        )
        .order_by(GovernanceRule.created_at.desc())
        .all()
    )

    # ---- alerts --------------------------------------------------------------
    alert_q = db.query(Alert).filter(Alert.project_id == project_id)
    alert_q = _date_filter(alert_q, Alert, start=start, end=end)
    alerts = alert_q.order_by(Alert.created_at.desc()).limit(50).all()

    # ---- audit trail (non-PII only — this report is not a compliance/security
    # artifact; see app/routers/audit_logs.py for the PII-gated equivalent) ----
    audit_q = db.query(AuditLog).filter(
        AuditLog.project_id == project_id,
        AuditLog.compliance_relevant.is_(False),
    )
    if start:
        audit_q = audit_q.filter(func.date(AuditLog.occurred_at) >= start)
    if end:
        audit_q = audit_q.filter(func.date(AuditLog.occurred_at) <= end)
    audit_entries = audit_q.order_by(AuditLog.occurred_at.desc()).limit(50).all()

    return {
        "generated_at": datetime.utcnow(),
        "period": {"start": start, "end": end},
        "project": {
            "id": project.id,
            "name": project.project_name or project.id,
            "environment": project.environment,
            "org_id": project.org_id,
            "org_name": org.org_name if org else project.org_id,
            "created_at": project.created_at,
        },
        "summary": {
            "total_requests": totals.total_requests or 0,
            "input_tokens": totals.input_tokens or 0,
            "output_tokens": totals.output_tokens or 0,
            "total_tokens": totals.total_tokens or 0,
            "input_cost": float(totals.input_cost or 0),
            "output_cost": float(totals.output_cost or 0),
            "total_cost": float(totals.total_cost or 0),
            "currency": "USD",
        },
        "cost_by_model": [
            {
                "model_name": r.model_name,
                "provider": r.provider or "",
                "total_requests": r.total_requests or 0,
                "total_tokens": r.total_tokens or 0,
                "total_cost": float(r.total_cost or 0),
            }
            for r in by_model_rows
        ],
        "budgets": budget_rows,
        "governance_rules": [
            {
                "rule_name": r.rule_name,
                "description": r.description,
                "metric_name": r.metric_name,
                "operator": r.operator,
                "threshold_value": float(r.threshold_value or 0),
                "severity": r.severity,
                "scope_level": r.scope_level,
            }
            for r in rules
        ],
        "alerts": [
            {
                "alert_type": a.alert_type,
                "severity": a.severity,
                "message": a.message,
                "status": a.status,
                "tool_name": a.tool_name,
                "created_at": a.created_at,
            }
            for a in alerts
        ],
        "audit_entries": [
            {
                "audit_action": e.audit_action,
                "audit_category": e.audit_category,
                "audit_status": e.audit_status,
                "actor_id": e.actor_id,
                "entity_type": e.entity_type,
                "change_summary": e.change_summary,
                "occurred_at": e.occurred_at,
            }
            for e in audit_entries
        ],
    }
