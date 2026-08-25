"""Per-project governance report — aggregates cost, budget, governance-rule,
alert and audit data into a single payload consumed by the report exporters
(app/services/report_exporters.py) and the JSON preview endpoint
(GET /reports/projects/{project_id}).
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from app.models import (
    Alert,
    AiRequest,
    AuditLog,
    Budget,
    DataSecurityLog,
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

    # ---- audit trail summary (non-PII only — this report is not a
    # compliance/security artifact; see app/routers/audit_logs.py for the
    # PII-gated equivalent). A per-project audit trail can run into the
    # thousands of rows (e.g. one entry per proxied request), so the report
    # surfaces counts rather than a raw dump. ----
    audit_filters = [
        AuditLog.project_id == project_id,
        AuditLog.compliance_relevant.is_(False),
    ]
    if start:
        audit_filters.append(func.date(AuditLog.occurred_at) >= start)
    if end:
        audit_filters.append(func.date(AuditLog.occurred_at) <= end)

    total_audit_events = db.query(func.count(AuditLog.id)).filter(*audit_filters).scalar() or 0

    by_action_rows = (
        db.query(AuditLog.audit_action, func.count(AuditLog.id).label("cnt"))
        .filter(*audit_filters)
        .group_by(AuditLog.audit_action)
        .order_by(func.count(AuditLog.id).desc())
        .limit(8)
        .all()
    )
    by_status_rows = (
        db.query(AuditLog.audit_status, func.count(AuditLog.id).label("cnt"))
        .filter(*audit_filters)
        .group_by(AuditLog.audit_status)
        .all()
    )
    first_event_at, last_event_at = (
        db.query(func.min(AuditLog.occurred_at), func.max(AuditLog.occurred_at))
        .filter(*audit_filters)
        .first()
        or (None, None)
    )

    # ---- security summary (PII / misuse / data-out signals for the period) --
    security_filters = [DataSecurityLog.project_id == project_id]
    if start:
        security_filters.append(func.date(DataSecurityLog.created_at) >= start)
    if end:
        security_filters.append(func.date(DataSecurityLog.created_at) <= end)

    security_totals = db.query(
        func.count(DataSecurityLog.id).label("total_events"),
        func.sum(case((DataSecurityLog.pii_detected.is_(True), 1), else_=0)).label("pii_detections"),
        func.sum(case((DataSecurityLog.data_out_violation.is_(True), 1), else_=0)).label("data_out_violations"),
        func.sum(case((DataSecurityLog.misuse_pattern_detected.is_(True), 1), else_=0)).label("misuse_flags"),
        func.sum(case((DataSecurityLog.abnormal_usage_spike.is_(True), 1), else_=0)).label("usage_spikes"),
        func.avg(DataSecurityLog.risk_score).label("avg_risk_score"),
    ).filter(*security_filters).first()

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
        "audit_summary": {
            "total_events": total_audit_events,
            "by_action": [{"action": a or "unknown", "count": c} for a, c in by_action_rows],
            "by_status": [{"status": s or "unknown", "count": c} for s, c in by_status_rows],
            "first_event_at": first_event_at,
            "last_event_at": last_event_at,
        },
        "security_summary": {
            "total_events": security_totals.total_events or 0,
            "pii_detections": int(security_totals.pii_detections or 0),
            "data_out_violations": int(security_totals.data_out_violations or 0),
            "misuse_flags": int(security_totals.misuse_flags or 0),
            "usage_spikes": int(security_totals.usage_spikes or 0),
            "avg_risk_score": round(float(security_totals.avg_risk_score or 0), 1),
        },
    }
