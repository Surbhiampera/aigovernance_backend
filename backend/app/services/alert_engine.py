"""AlertEngine — usage-based, zero-hardcode monitoring.

All thresholds come from DB tables:
  budgets.alert_threshold_percent  → % of budget to alert at
  budgets.limit_amount             → absolute cost cap
  rate_limits.max_tokens_per_day   → daily token quota
  governance_rules                 → custom metric thresholds

Fires alerts at configurable percentages (threshold%, 90%, 100%) and a
predictive alert when velocity * remaining_days >= limit.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Alert, Budget, DailyOrgSummary, GovernanceRule, RateLimit, UsageAnomaly
from app.schemas import CostSummary, TelemetryEventCreate

logger = logging.getLogger(__name__)


class AlertEngine:
    def evaluate(
        self,
        db: Session,
        event_data: TelemetryEventCreate,
        cost_summary: CostSummary,
        security_result: dict,
        anomaly_score: Decimal,
        abnormal_usage_spike: bool,
        telemetry_id: int | None = None,
    ) -> None:
        event_metrics = {
            "total_cost": Decimal(str(cost_summary.total_cost)),
            "risk_score": Decimal(str(security_result["risk_score"])),
            "latency_ms": Decimal(str(event_data.latency_ms)),
            "prompt_tokens": Decimal(str(event_data.prompt_tokens)),
            "completion_tokens": Decimal(str(event_data.completion_tokens)),
            "total_tokens": Decimal(str(event_data.prompt_tokens + event_data.completion_tokens)),
            "data_out_mb": Decimal(str(event_data.output_data_size_mb)),
            "anomaly_score": Decimal(str(anomaly_score)),
        }
        # Single source of truth for the tool/model name attached to every
        # alert so downstream views can trace the alert back to its exact
        # tool — same linkage strategy as the Super Admin Log module.
        tool_ref = event_data.model_name or event_data.tool_name

        if security_result["pii_detected"]:
            _model = tool_ref or "unknown model"
            _project = event_data.project_id or "unknown project"
            _pii_type = security_result.get("pii_type") or "sensitive data"
            self._create_alert(
                db=db,
                org_id=event_data.org_id,
                project_id=event_data.project_id,
                tool_name=tool_ref,
                telemetry_id=telemetry_id,
                alert_type="pii_detected",
                severity="critical",
                message=(
                    f"PII ({_pii_type}) detected in model '{_model}' "
                    f"[project: {_project}] — event {event_data.event_id}, "
                    f"risk score {security_result['risk_score']:.1f}."
                ),
                threshold=None,
                actual=Decimal(str(security_result["risk_score"])),
            )

        if security_result["misuse_pattern_detected"]:
            self._create_alert(
                db=db,
                org_id=event_data.org_id,
                project_id=event_data.project_id,
                tool_name=tool_ref,
                telemetry_id=telemetry_id,
                alert_type="misuse_pattern",
                severity="critical",
                message=f"Misuse pattern detected for event {event_data.event_id}.",
                threshold=None,
                actual=Decimal("1"),
            )

        if security_result["data_out_violation"]:
            self._create_alert(
                db=db,
                org_id=event_data.org_id,
                project_id=event_data.project_id,
                tool_name=tool_ref,
                telemetry_id=telemetry_id,
                alert_type="data_out_violation",
                severity="high",
                message=f"Data-out policy exceeded for event {event_data.event_id}.",
                threshold=None,
                actual=Decimal(str(event_data.output_data_size_mb)),
            )

        if abnormal_usage_spike:
            self._create_alert(
                db=db,
                org_id=event_data.org_id,
                project_id=event_data.project_id,
                tool_name=tool_ref,
                telemetry_id=telemetry_id,
                alert_type="usage_spike",
                severity="high",
                message=f"Abnormal usage spike for {tool_ref or 'unknown tool'}.",
                threshold=Decimal("1"),
                actual=Decimal(str(anomaly_score)),
            )

        self._evaluate_budgets(db, event_data.org_id, event_data.project_id, telemetry_id, tool_ref)
        self._evaluate_token_quotas(db, event_data, telemetry_id, tool_ref)
        self._evaluate_custom_rules(db, event_data, event_metrics, telemetry_id, tool_ref)
        db.flush()

    # ─────────────────── budget monitoring ───────────────────

    def _evaluate_budgets(
        self,
        db: Session,
        org_id: str,
        project_id: str | None,
        telemetry_id: int | None,
        tool_name: str | None = None,
    ) -> None:
        today = date.today()
        month_start = today.replace(day=1)

        budgets = db.query(Budget).filter(Budget.org_id == org_id).all()
        for budget in budgets:
            if not budget.limit_amount or budget.limit_amount <= 0:
                continue

            spent_q = db.query(func.coalesce(func.sum(DailyOrgSummary.total_cost), 0)).filter(
                DailyOrgSummary.org_id == org_id
            )
            if budget.project_id:
                spent_q = spent_q.filter(DailyOrgSummary.project_id == budget.project_id)

            if budget.budget_type == "daily":
                spent_q = spent_q.filter(DailyOrgSummary.date == today)
            else:
                spent_q = spent_q.filter(DailyOrgSummary.date >= month_start)

            spent = Decimal(str(spent_q.scalar() or 0))
            limit_amount = Decimal(str(budget.limit_amount))
            # Threshold from DB — never hardcoded
            threshold_pct = Decimal(str(budget.alert_threshold_percent or 80))
            usage_pct = (spent / limit_amount * 100).quantize(Decimal("0.1"))

            # Alert at configured threshold, 90%, and 100%
            for alert_pct in sorted({threshold_pct, Decimal("90"), Decimal("100")}):
                if usage_pct >= alert_pct:
                    sev = "critical" if alert_pct >= Decimal("100") else "high"
                    self._create_alert(
                        db=db,
                        org_id=org_id,
                        project_id=budget.project_id,
                        tool_name=tool_name,
                        telemetry_id=telemetry_id,
                        alert_type=f"budget_{alert_pct:.0f}pct",
                        severity=sev,
                        message=(
                            f"{(budget.budget_type or 'monthly').capitalize()} budget at "
                            f"{usage_pct}% (${spent:.2f} of ${limit_amount:.2f})."
                        ),
                        threshold=limit_amount,
                        actual=spent,
                    )

            # Predictive forecast for monthly budgets
            if budget.budget_type != "daily":
                days_elapsed = max((today - month_start).days + 1, 1)
                days_in_month = calendar.monthrange(today.year, today.month)[1]
                days_remaining = days_in_month - today.day
                velocity = spent / Decimal(str(days_elapsed))
                forecast = spent + velocity * Decimal(str(days_remaining))
                forecast_pct = (forecast / limit_amount * 100).quantize(Decimal("0.1"))
                if forecast_pct >= Decimal("100") and usage_pct < Decimal("100"):
                    self._create_alert(
                        db=db,
                        org_id=org_id,
                        project_id=budget.project_id,
                        tool_name=tool_name,
                        telemetry_id=telemetry_id,
                        alert_type="budget_forecast_overrun",
                        severity="high",
                        message=(
                            f"Forecast: monthly budget will be exceeded "
                            f"(projected ${forecast:.2f} vs limit ${limit_amount:.2f})."
                        ),
                        threshold=limit_amount,
                        actual=forecast,
                    )

    # ─────────────────── token quota monitoring ───────────────────

    def _evaluate_token_quotas(
        self,
        db: Session,
        event_data: TelemetryEventCreate,
        telemetry_id: int | None,
        tool_name: str | None = None,
    ) -> None:
        today = date.today()
        rate_limits = db.query(RateLimit).filter(RateLimit.org_id == event_data.org_id).all()
        for rl in rate_limits:
            if not rl.max_tokens_per_day:
                continue

            used_today = int(
                db.query(func.coalesce(func.sum(DailyOrgSummary.total_tokens), 0))
                .filter(DailyOrgSummary.org_id == event_data.org_id, DailyOrgSummary.date == today)
                .scalar()
                or 0
            )
            quota = Decimal(str(rl.max_tokens_per_day))
            used = Decimal(str(used_today))
            pct = (used / quota * 100).quantize(Decimal("0.1")) if quota > 0 else Decimal("0")

            if pct >= Decimal("80"):
                sev = "critical" if pct >= Decimal("100") else "high"
                self._create_alert(
                    db=db,
                    org_id=event_data.org_id,
                    project_id=event_data.project_id,
                    tool_name=tool_name or event_data.model_name or event_data.tool_name,
                    telemetry_id=telemetry_id,
                    alert_type="token_quota",
                    severity=sev,
                    message=(
                        f"Daily token quota at {pct}% "
                        f"({used:.0f} of {quota:.0f} tokens used)."
                    ),
                    threshold=quota,
                    actual=used,
                )

    # ─────────────────── custom governance rules ───────────────────

    def _evaluate_custom_rules(
        self,
        db: Session,
        event_data: TelemetryEventCreate,
        metrics: dict[str, Decimal],
        telemetry_id: int | None = None,
        tool_name: str | None = None,
    ) -> None:
        rules = db.query(GovernanceRule).filter(GovernanceRule.is_active.is_(True)).all()
        for rule in rules:
            metric_value = metrics.get(rule.metric_name)
            if metric_value is None:
                continue

            if rule.scope_level == "tool" and rule.scope_reference and rule.scope_reference != (event_data.model_name or event_data.tool_name):
                continue
            if rule.scope_level == "project" and rule.scope_reference and rule.scope_reference != event_data.project_id:
                continue
            if rule.scope_level == "organization" and rule.scope_reference and rule.scope_reference != event_data.org_id:
                continue

            if self._compare(metric_value, Decimal(str(rule.threshold_value)), rule.operator):
                self._create_alert(
                    db=db,
                    org_id=event_data.org_id,
                    project_id=event_data.project_id,
                    tool_name=tool_name or event_data.model_name or event_data.tool_name,
                    telemetry_id=telemetry_id,
                    alert_type=f"rule:{rule.metric_name}",
                    severity=rule.severity,
                    message=f"Rule '{rule.rule_name}' triggered: {rule.metric_name} {rule.operator} {rule.threshold_value}.",
                    threshold=Decimal(str(rule.threshold_value)),
                    actual=metric_value,
                    rule_id=rule.id,
                )

    # ─────────────────── anomaly-to-alert conversion ───────────────────

    def create_daily_anomaly_alerts(self, db: Session) -> int:
        recent = (
            db.query(UsageAnomaly)
            .filter(UsageAnomaly.status == "open")
            .order_by(UsageAnomaly.created_at.desc())
            .limit(20)
            .all()
        )
        created = 0
        for anomaly in recent:
            # Resolve telemetry_id from anomaly.event_id when available so the
            # generated alert remains linked to its source event (and through
            # it, to the org/project/tool/model).
            telemetry_id = None
            if anomaly.event_id:
                from app.models import TelemetryEvent
                evt = (
                    db.query(TelemetryEvent.id)
                    .filter(TelemetryEvent.event_id == anomaly.event_id)
                    .first()
                )
                if evt:
                    telemetry_id = evt[0]
            self._create_alert(
                db=db,
                org_id=anomaly.org_id,
                project_id=anomaly.project_id,
                tool_name=anomaly.tool_name,
                telemetry_id=telemetry_id,
                alert_type=anomaly.anomaly_type,
                severity=anomaly.severity,
                message=anomaly.message or "Anomaly detected during scheduled scan.",
                threshold=Decimal(str(anomaly.baseline_value)),
                actual=Decimal(str(anomaly.observed_value)),
            )
            created += 1
        db.flush()
        return created

    # ─────────────────── helpers ───────────────────

    def _compare(self, left: Decimal, right: Decimal, operator: str) -> bool:
        return {
            ">": left > right, ">=": left >= right,
            "<": left < right, "<=": left <= right,
            "=": left == right,
        }.get(operator, False)

    def _create_alert(
        self,
        db: Session,
        org_id: str | None,
        project_id: str | None,
        telemetry_id: int | None,
        alert_type: str,
        severity: str,
        message: str,
        threshold: Decimal | None,
        actual: Decimal | None,
        rule_id: int | None = None,
        tool_name: str | None = None,
    ) -> None:
        recent_cutoff = date.today() - timedelta(days=1)
        existing = (
            db.query(Alert)
            .filter(
                Alert.org_id == org_id,
                Alert.project_id == project_id,
                Alert.tool_name == tool_name,
                Alert.alert_type == alert_type,
                Alert.status == "active",
                func.date(Alert.created_at) >= recent_cutoff,
            )
            .first()
        )
        if existing:
            return

        db.add(
            Alert(
                org_id=org_id,
                project_id=project_id,
                tool_name=tool_name,
                rule_id=rule_id,
                alert_type=alert_type,
                severity=severity,
                message=message,
                threshold_value=threshold,
                actual_value=actual,
                status="active",
                telemetry_id=telemetry_id,
            )
        )

        # Dispatch external notifications (email + WhatsApp) for high/critical alerts
        try:
            from app.services.notification_service import notification_service
            notification_service.notify(
                alert_type=alert_type,
                severity=severity,
                message=message,
                org_id=org_id or "",
                project_id=project_id,
            )
        except Exception as exc:
            logger.warning("Notification dispatch failed: %s", exc)
