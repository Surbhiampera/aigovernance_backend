"""Scheduled task functions called by APScheduler.

All DB sessions are managed by the caller (scheduler.py).
Three jobs:
  _rebuild_daily_summary   — aggregates AiRequest + RequestCost → DailyOrgSummary
  _detect_daily_anomalies  — compares today vs N-day baseline → UsageAnomaly rows
  _rebuild_monthly_summary — rolls up DailyOrgSummary → MonthlyOrgSummary
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Integer, cast, func
from sqlalchemy.orm import Session

from app.models import (
    AiRequest,
    DailyOrgSummary,
    MonthlyOrgSummary,
    Organization,
    RequestCost,
    UsageAnomaly,
)

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Daily aggregation — AiRequest + RequestCost → DailyOrgSummary
# ---------------------------------------------------------------------------

def _rebuild_daily_summary(*, db: Session, summary_date: date) -> int:
    rows = (
        db.query(
            RequestCost.org_id,
            RequestCost.project_id,
            RequestCost.model_name,
            func.count(RequestCost.request_id).label("total_requests"),
            func.sum(RequestCost.input_tokens).label("input_tokens"),
            func.sum(RequestCost.output_tokens).label("output_tokens"),
            func.sum(RequestCost.total_tokens).label("total_tokens"),
            func.sum(RequestCost.total_cost).label("total_cost"),
            func.sum(RequestCost.input_token_cost).label("input_cost"),
            func.sum(RequestCost.output_token_cost).label("output_cost"),
        )
        .filter(func.date(RequestCost.created_at) == summary_date)
        .filter(RequestCost.org_id.isnot(None))
        .group_by(RequestCost.org_id, RequestCost.project_id, RequestCost.model_name)
        .all()
    )

    # Latency and status counts come from AiRequest
    req_rows = (
        db.query(
            AiRequest.org_id,
            AiRequest.project_id,
            AiRequest.model_name,
            func.count(AiRequest.request_id).label("total"),
            func.avg(
                func.extract("epoch", AiRequest.completed_at - AiRequest.created_at) * 1000
            ).label("avg_latency_ms"),
            func.sum(
                cast(AiRequest.request_status == "success", Integer)
            ).label("success_count"),
            func.sum(
                cast(AiRequest.request_status != "success", Integer)
            ).label("failure_count"),
        )
        .filter(func.date(AiRequest.created_at) == summary_date)
        .filter(AiRequest.org_id.isnot(None))
        .group_by(AiRequest.org_id, AiRequest.project_id, AiRequest.model_name)
        .all()
    )

    req_index: dict[tuple, Any] = {
        (r.org_id, r.project_id, r.model_name): r for r in req_rows
    }

    db.query(DailyOrgSummary).filter(DailyOrgSummary.date == summary_date).delete(
        synchronize_session=False
    )

    inserted = 0
    for row in rows:
        org_id = (row.org_id or "").strip()
        if not org_id:
            continue

        req = req_index.get((row.org_id, row.project_id, row.model_name))
        success_count = int(req.success_count or 0) if req else 0
        failure_count = int(req.failure_count or 0) if req else 0
        avg_latency_ms = int(req.avg_latency_ms or 0) if req else 0

        db.add(DailyOrgSummary(
            org_id=org_id,
            project_id=row.project_id,
            tool_name=(row.model_name or "").strip() or "unknown",
            date=summary_date,
            total_events=row.total_requests or 0,
            total_cost=row.total_cost or Decimal("0"),
            llm_cost=row.total_cost or Decimal("0"),
            infra_cost=Decimal("0"),
            external_cost=Decimal("0"),
            total_prompt_tokens=row.input_tokens or 0,
            total_completion_tokens=row.output_tokens or 0,
            total_tokens=row.total_tokens or 0,
            avg_latency_ms=avg_latency_ms,
            success_count=success_count,
            failure_count=failure_count,
            anomaly_count=0,
            misuse_count=0,
            total_input_mb=Decimal("0"),
            total_output_mb=Decimal("0"),
            avg_risk_score=Decimal("0"),
        ))
        inserted += 1

    db.flush()
    return inserted


# ---------------------------------------------------------------------------
# Anomaly detection — compare today's DailyOrgSummary against N-day baseline
# ---------------------------------------------------------------------------

# (anomaly_type, attribute on DailyOrgSummary row for today's value)
_SPIKE_METRICS: list[tuple[str, str]] = [
    ("cost_spike",    "total_cost"),
    ("token_spike",   "total_tokens"),
    ("request_spike", "total_events"),
    ("latency_spike", "avg_latency_ms"),
]


def _detect_daily_anomalies(*, db: Session, summary_date: date) -> int:
    """Insert UsageAnomaly rows for metrics that spike above baseline.

    Compares today's DailyOrgSummary values against the rolling average of the
    prior ANOMALY_BASELINE_DAYS days.  Requires at least 3 baseline days before
    flagging to avoid false positives on new orgs/projects.

    Also handles error_rate_spike (failure_count / total_requests ratio).
    Updates DailyOrgSummary.anomaly_count for each affected row.
    """
    from app.config import (
        get_anomaly_baseline_days,
        get_anomaly_high_severity_ratio,
        get_anomaly_spike_ratio,
    )

    baseline_days = get_anomaly_baseline_days()
    spike_ratio = get_anomaly_spike_ratio()
    high_ratio = get_anomaly_high_severity_ratio()

    baseline_start = summary_date - timedelta(days=baseline_days)
    baseline_end = summary_date - timedelta(days=1)

    # ── Today's rows ────────────────────────────────────────────────────────
    today_rows = (
        db.query(DailyOrgSummary)
        .filter(DailyOrgSummary.date == summary_date)
        .all()
    )
    if not today_rows:
        return 0

    # ── Baseline averages per (org, project, tool) ──────────────────────────
    baseline_rows = (
        db.query(
            DailyOrgSummary.org_id,
            DailyOrgSummary.project_id,
            DailyOrgSummary.tool_name,
            func.count().label("day_count"),
            func.avg(DailyOrgSummary.total_cost).label("avg_cost"),
            func.avg(DailyOrgSummary.total_tokens).label("avg_tokens"),
            func.avg(DailyOrgSummary.total_events).label("avg_events"),
            func.avg(DailyOrgSummary.avg_latency_ms).label("avg_latency"),
            func.avg(DailyOrgSummary.success_count).label("avg_success"),
            func.avg(DailyOrgSummary.failure_count).label("avg_failure"),
        )
        .filter(
            DailyOrgSummary.date >= baseline_start,
            DailyOrgSummary.date <= baseline_end,
        )
        .group_by(
            DailyOrgSummary.org_id,
            DailyOrgSummary.project_id,
            DailyOrgSummary.tool_name,
        )
        .all()
    )
    baseline_index = {
        (r.org_id, r.project_id, r.tool_name): r for r in baseline_rows
    }

    # ── Deduplication: anomalies already recorded for today ─────────────────
    existing_keys: set[tuple] = set(
        db.query(
            UsageAnomaly.org_id,
            UsageAnomaly.project_id,
            UsageAnomaly.tool_name,
            UsageAnomaly.anomaly_type,
        )
        .filter(func.date(UsageAnomaly.created_at) == summary_date)
        .all()
    )

    inserted = 0
    anomaly_counts: dict[tuple, int] = {}

    for row in today_rows:
        key = (row.org_id, row.project_id, row.tool_name)
        baseline = baseline_index.get(key)

        if not baseline or int(baseline.day_count or 0) < 3:
            continue

        # ── Standard scalar spike metrics ────────────────────────────────
        metric_map = {
            "cost_spike":    (Decimal(str(row.total_cost    or 0)), Decimal(str(baseline.avg_cost    or 0))),
            "token_spike":   (Decimal(str(row.total_tokens  or 0)), Decimal(str(baseline.avg_tokens  or 0))),
            "request_spike": (Decimal(str(row.total_events  or 0)), Decimal(str(baseline.avg_events  or 0))),
            "latency_spike": (Decimal(str(row.avg_latency_ms or 0)), Decimal(str(baseline.avg_latency or 0))),
        }

        # ── Error rate spike (computed from counts) ───────────────────────
        today_total = (row.success_count or 0) + (row.failure_count or 0)
        baseline_total = float(baseline.avg_success or 0) + float(baseline.avg_failure or 0)
        if today_total >= 10 and baseline_total > 0:
            today_err_rate = Decimal(str(row.failure_count or 0)) / Decimal(str(today_total))
            baseline_err_rate = Decimal(str(float(baseline.avg_failure or 0))) / Decimal(str(baseline_total))
            # Only flag if baseline error rate is meaningful (≥ 1%)
            if baseline_err_rate >= Decimal("0.01"):
                metric_map["error_rate_spike"] = (today_err_rate, baseline_err_rate)

        for anomaly_type, (today_val, baseline_val) in metric_map.items():
            dedup_key = (row.org_id, row.project_id, row.tool_name, anomaly_type)
            if dedup_key in existing_keys:
                continue
            if baseline_val <= 0:
                continue

            ratio = today_val / baseline_val
            if ratio < spike_ratio:
                continue

            severity = "high" if ratio >= high_ratio else "medium"
            db.add(UsageAnomaly(
                org_id=row.org_id,
                project_id=row.project_id,
                tool_name=row.tool_name,
                anomaly_type=anomaly_type,
                severity=severity,
                anomaly_score=round(float(ratio), 4),
                baseline_value=float(baseline_val),
                observed_value=float(today_val),
                message=(
                    f"{anomaly_type.replace('_', ' ').title()}: "
                    f"observed {float(today_val):.4g} vs baseline {float(baseline_val):.4g} "
                    f"({float(ratio):.2f}x)"
                ),
                status="open",
            ))
            existing_keys.add(dedup_key)
            inserted += 1
            anomaly_counts[key] = anomaly_counts.get(key, 0) + 1

    # ── Back-fill anomaly_count on today's DailyOrgSummary rows ─────────────
    for row in today_rows:
        key = (row.org_id, row.project_id, row.tool_name)
        count = anomaly_counts.get(key, 0)
        if count > 0:
            row.anomaly_count = count

    if inserted:
        _log.info("anomaly_detection: %d anomaly rows inserted for %s", inserted, summary_date)

    db.flush()
    return inserted


# ---------------------------------------------------------------------------
# Monthly aggregation — DailyOrgSummary → MonthlyOrgSummary
# ---------------------------------------------------------------------------

def _rebuild_monthly_summary(*, db: Session) -> int:
    today = date.today()
    month_start = today.replace(day=1)
    valid_org_ids = db.query(Organization.id).subquery()

    rows = (
        db.query(
            DailyOrgSummary.org_id,
            DailyOrgSummary.project_id,
            DailyOrgSummary.tool_name,
            func.sum(DailyOrgSummary.total_events).label("total_events"),
            func.sum(DailyOrgSummary.total_cost).label("total_cost"),
            func.sum(DailyOrgSummary.total_tokens).label("total_tokens"),
            func.sum(DailyOrgSummary.total_prompt_tokens).label("total_prompt_tokens"),
            func.sum(DailyOrgSummary.total_completion_tokens).label("total_completion_tokens"),
            func.avg(DailyOrgSummary.avg_latency_ms).label("avg_latency_ms"),
            func.sum(DailyOrgSummary.success_count).label("success_count"),
            func.sum(DailyOrgSummary.failure_count).label("failure_count"),
        )
        .filter(
            DailyOrgSummary.date >= month_start,
            DailyOrgSummary.date <= today,
            DailyOrgSummary.org_id.isnot(None),
            DailyOrgSummary.org_id.in_(valid_org_ids),
        )
        .group_by(
            DailyOrgSummary.org_id,
            DailyOrgSummary.project_id,
            DailyOrgSummary.tool_name,
        )
        .all()
    )

    db.query(MonthlyOrgSummary).filter(
        MonthlyOrgSummary.month == month_start
    ).delete(synchronize_session=False)

    inserted = 0
    for row in rows:
        org_id = (row.org_id or "").strip()
        if not org_id:
            continue
        db.add(MonthlyOrgSummary(
            org_id=org_id,
            project_id=row.project_id,
            tool_name=(row.tool_name or "").strip() or "unknown",
            month=month_start,
            total_events=row.total_events or 0,
            total_cost=row.total_cost or Decimal("0"),
            llm_cost=row.total_cost or Decimal("0"),
            infra_cost=Decimal("0"),
            external_cost=Decimal("0"),
            total_tokens=row.total_tokens or 0,
            total_prompt_tokens=row.total_prompt_tokens or 0,
            total_completion_tokens=row.total_completion_tokens or 0,
            avg_latency_ms=int(row.avg_latency_ms or 0),
            success_count=row.success_count or 0,
            failure_count=row.failure_count or 0,
            anomaly_count=0,
            misuse_count=0,
        ))
        inserted += 1

    db.flush()
    return inserted
