"""Budget enforcement — called by the proxy before forwarding to Azure.

Checks org-level and project-level budgets against current-month spend
from request_cost.total_cost.  Raises HTTPException(429) when a hard limit
is exceeded; writes an audit row and an Alert for both warning and hard-limit
events without raising.

Unknown-priced requests (cost_model_type='unknown') contribute $0 to
tracked spend.  The governance rule service blocks unpriced models
upstream, so budget accuracy is maintained by exclusion.
"""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)


def check_budget(
    *,
    db: Session,
    org_id: str,
    project_id: Optional[str],
    request_id: str,
    source_ip: Optional[str],
    key_id: str,
) -> None:
    """Raise HTTP 429 if any applicable budget is exceeded.

    Checks project budget first (most specific), then org budget.
    Writes audit + alert for warning threshold crossings without blocking.
    No-ops when no Budget row exists for a scope.
    """
    from app.models import Alert, Budget, RequestCost
    from app.services.audit_service import log_event

    today = datetime.utcnow()
    month_start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Current-month spend for the org (used by both scope checks)
    org_spend: Decimal = db.query(
        func.coalesce(func.sum(RequestCost.total_cost), Decimal("0"))
    ).filter(
        RequestCost.org_id == org_id,
        RequestCost.created_at >= month_start,
    ).scalar() or Decimal("0")

    # Scope 1: project budget
    if project_id:
        proj_budget = (
            db.query(Budget)
            .filter(Budget.org_id == org_id, Budget.project_id == project_id)
            .first()
        )
        if proj_budget and proj_budget.limit_amount and proj_budget.limit_amount > 0:
            proj_spend: Decimal = db.query(
                func.coalesce(func.sum(RequestCost.total_cost), Decimal("0"))
            ).filter(
                RequestCost.org_id == org_id,
                RequestCost.project_id == project_id,
                RequestCost.created_at >= month_start,
            ).scalar() or Decimal("0")

            _evaluate_budget(
                db=db,
                budget=proj_budget,
                spend=proj_spend,
                scope="project",
                org_id=org_id,
                project_id=project_id,
                request_id=request_id,
                source_ip=source_ip,
            )

    # Scope 2: org budget (project_id IS NULL on the budget row)
    org_budget = (
        db.query(Budget)
        .filter(Budget.org_id == org_id, Budget.project_id.is_(None))
        .first()
    )
    if org_budget and org_budget.limit_amount and org_budget.limit_amount > 0:
        _evaluate_budget(
            db=db,
            budget=org_budget,
            spend=org_spend,
            scope="org",
            org_id=org_id,
            project_id=project_id,
            request_id=request_id,
            source_ip=source_ip,
        )


def _evaluate_budget(
    *,
    db: Session,
    budget,
    spend: Decimal,
    scope: str,
    org_id: str,
    project_id: Optional[str],
    request_id: str,
    source_ip: Optional[str],
) -> None:
    from app.models import Alert
    from app.services.audit_service import log_event

    limit = Decimal(str(budget.limit_amount))
    threshold_pct = int(budget.alert_threshold_percent or 80)
    ratio = float(spend / limit) if limit > 0 else 0.0
    threshold_ratio = threshold_pct / 100.0

    if ratio >= 1.0:
        # Hard block — budget fully exhausted
        _write_budget_audit(
            db=db,
            action="budget_exceeded",
            status="failure",
            requires_review=True,
            budget=budget,
            spend=spend,
            scope=scope,
            org_id=org_id,
            project_id=project_id,
            request_id=request_id,
            source_ip=source_ip,
        )
        _upsert_budget_alert(
            db=db,
            budget=budget,
            spend=spend,
            scope=scope,
            org_id=org_id,
            project_id=project_id,
            alert_type="budget_exceeded",
            severity="critical",
        )
        db.commit()
        raise HTTPException(
            status_code=429,
            detail={
                "error": "budget_exceeded",
                "scope": scope,
                "budget_limit": float(limit),
                "current_spend": float(spend),
                "currency": "USD",
                "request_id": request_id,
                "detail": (
                    f"{scope.capitalize()} budget of ${limit:.2f} USD exceeded. "
                    f"Current spend: ${spend:.2f}."
                ),
            },
        )

    if ratio >= threshold_ratio:
        # Warning — threshold crossed but not exhausted; request proceeds
        _log.warning(
            "Budget warning [%s %s]: %.1f%% of $%.2f limit used ($%.2f spent).",
            scope, org_id, ratio * 100, float(limit), float(spend),
        )
        _write_budget_audit(
            db=db,
            action="budget_warning",
            status="warning",
            requires_review=False,
            budget=budget,
            spend=spend,
            scope=scope,
            org_id=org_id,
            project_id=project_id,
            request_id=request_id,
            source_ip=source_ip,
        )
        _upsert_budget_alert(
            db=db,
            budget=budget,
            spend=spend,
            scope=scope,
            org_id=org_id,
            project_id=project_id,
            alert_type="budget_warning",
            severity="high",
        )
        # Commit now rather than leaving this on the request's still-open
        # transaction — see verify_governance_key() for why: this Alert
        # row's lock would otherwise be held through the outbound LLM call,
        # serializing concurrent requests for the same org/project.
        db.commit()


def _write_budget_audit(
    *,
    db: Session,
    action: str,
    status: str,
    requires_review: bool,
    budget,
    spend: Decimal,
    scope: str,
    org_id: str,
    project_id: Optional[str],
    request_id: str,
    source_ip: Optional[str],
) -> None:
    from app.services.audit_service import log_event

    limit = float(budget.limit_amount)
    log_event(
        db,
        org_id=org_id,
        project_id=project_id,
        audit_category="governance",
        audit_action=action,
        audit_status=status,
        actor_type="governance_engine",
        actor_id=None,
        actor_ip=source_ip,
        entity_type="budget",
        entity_id=str(budget.id),
        request_id=request_id,
        policy_triggered=True,
        compliance_relevant=True,
        requires_review=requires_review,
        change_summary=(
            f"Budget {action.replace('_', ' ')}: "
            f"${float(spend):.2f} of ${limit:.2f} limit (scope: {scope})"
        ),
        metadata={
            "scope": scope,
            "budget_id": budget.id,
            "limit": limit,
            "spent": float(spend),
            "threshold_pct": int(budget.alert_threshold_percent or 80),
        },
        flush=False,
    )


def _upsert_budget_alert(
    *,
    db: Session,
    budget,
    spend: Decimal,
    scope: str,
    org_id: str,
    project_id: Optional[str],
    alert_type: str,
    severity: str,
) -> None:
    from app.models import Alert

    existing = (
        db.query(Alert)
        .filter(
            Alert.org_id == org_id,
            Alert.alert_type == alert_type,
            Alert.status == "active",
        )
        .first()
    )
    if existing:
        # Update actual_value so the dashboard shows current spend
        existing.actual_value = float(spend)
        return

    alert = Alert(
        org_id=org_id,
        project_id=project_id,
        alert_type=alert_type,
        severity=severity,
        message=(
            f"{scope.capitalize()} budget {alert_type.replace('_', ' ')}: "
            f"${float(spend):.2f} of ${float(budget.limit_amount):.2f} used."
        ),
        threshold_value=float(budget.limit_amount),
        actual_value=float(spend),
        status="active",
    )
    db.add(alert)
