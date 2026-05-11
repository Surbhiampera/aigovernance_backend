"""Pure task functions — no Celery, no decorators.

Called by app.scheduler (APScheduler) and by the /workers endpoints for
on-demand manual triggers.  All DB sessions are managed by the caller.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.models import (
    ConnectorSyncLog,
    DailyOrgSummary,
    MonthlyOrgSummary,
    Organization,
    TelemetryEvent,
    ToolConnector,
    UsageAnomaly,
)

logger = logging.getLogger(__name__)


# ─────────────────────── daily aggregation ───────────────────────

def _rebuild_daily_summary(db: Session, summary_date: date) -> int:
    rows = (
        db.query(
            TelemetryEvent.org_id,
            TelemetryEvent.project_id,
            TelemetryEvent.model_name,
            func.count(TelemetryEvent.id).label("total_events"),
            func.sum(TelemetryEvent.total_cost).label("total_cost"),
            func.sum(TelemetryEvent.llm_cost).label("llm_cost"),
            func.sum(TelemetryEvent.infra_cost).label("infra_cost"),
            func.sum(TelemetryEvent.external_cost).label("external_cost"),
            func.sum(TelemetryEvent.prompt_tokens).label("total_prompt_tokens"),
            func.sum(TelemetryEvent.completion_tokens).label("total_completion_tokens"),
            func.sum(TelemetryEvent.total_tokens).label("total_tokens"),
            func.avg(TelemetryEvent.latency_ms).label("avg_latency_ms"),
            func.sum(case((TelemetryEvent.status.in_(["success", "completed"]), 1), else_=0)).label("success_count"),
            func.sum(case((TelemetryEvent.status.in_(["success", "completed"]), 0), else_=1)).label("failure_count"),
            func.sum(case((TelemetryEvent.abnormal_usage_spike.is_(True), 1), else_=0)).label("anomaly_count"),
            func.sum(case((TelemetryEvent.misuse_detected.is_(True), 1), else_=0)).label("misuse_count"),
            func.sum(TelemetryEvent.input_data_size_mb).label("total_input_mb"),
            func.sum(TelemetryEvent.output_data_size_mb).label("total_output_mb"),
            func.avg(TelemetryEvent.risk_score).label("avg_risk_score"),
        )
        .filter(
            func.date(TelemetryEvent.created_at) == summary_date,
            TelemetryEvent.org_id.isnot(None),
            func.trim(TelemetryEvent.org_id) != "",
        )
        .group_by(TelemetryEvent.org_id, TelemetryEvent.project_id, TelemetryEvent.model_name)
        .all()
    )

    db.query(DailyOrgSummary).filter(DailyOrgSummary.date == summary_date).delete(synchronize_session=False)

    inserted = 0
    for row in rows:
        org_id = (row.org_id or "").strip()
        if not org_id:
            continue
        db.add(
            DailyOrgSummary(
                org_id=org_id,
                project_id=row.project_id,
                tool_name=(row.model_name or "").strip() or "unknown",
                date=summary_date,
                total_events=row.total_events or 0,
                total_cost=row.total_cost or Decimal("0"),
                llm_cost=row.llm_cost or Decimal("0"),
                infra_cost=row.infra_cost or Decimal("0"),
                external_cost=row.external_cost or Decimal("0"),
                total_prompt_tokens=row.total_prompt_tokens or 0,
                total_completion_tokens=row.total_completion_tokens or 0,
                total_tokens=row.total_tokens or 0,
                avg_latency_ms=int(row.avg_latency_ms or 0),
                success_count=row.success_count or 0,
                failure_count=row.failure_count or 0,
                anomaly_count=row.anomaly_count or 0,
                misuse_count=row.misuse_count or 0,
                total_input_mb=row.total_input_mb or Decimal("0"),
                total_output_mb=row.total_output_mb or Decimal("0"),
                avg_risk_score=Decimal(str(row.avg_risk_score or 0)).quantize(Decimal("0.01")),
            )
        )
        inserted += 1
    db.flush()
    return inserted


# ─────────────────────── monthly aggregation ───────────────────────

def _rebuild_monthly_summary(db: Session) -> int:
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
            func.sum(DailyOrgSummary.llm_cost).label("llm_cost"),
            func.sum(DailyOrgSummary.infra_cost).label("infra_cost"),
            func.sum(DailyOrgSummary.external_cost).label("external_cost"),
            func.sum(DailyOrgSummary.total_tokens).label("total_tokens"),
            func.sum(DailyOrgSummary.total_prompt_tokens).label("total_prompt_tokens"),
            func.sum(DailyOrgSummary.total_completion_tokens).label("total_completion_tokens"),
            func.avg(DailyOrgSummary.avg_latency_ms).label("avg_latency_ms"),
            func.sum(DailyOrgSummary.success_count).label("success_count"),
            func.sum(DailyOrgSummary.failure_count).label("failure_count"),
            func.sum(DailyOrgSummary.anomaly_count).label("anomaly_count"),
            func.sum(DailyOrgSummary.misuse_count).label("misuse_count"),
        )
        .filter(
            DailyOrgSummary.date >= month_start,
            DailyOrgSummary.date <= today,
            DailyOrgSummary.org_id.isnot(None),
            func.trim(DailyOrgSummary.org_id) != "",
            DailyOrgSummary.org_id.in_(valid_org_ids),
        )
        .group_by(DailyOrgSummary.org_id, DailyOrgSummary.project_id, DailyOrgSummary.tool_name)
        .all()
    )

    db.query(MonthlyOrgSummary).filter(MonthlyOrgSummary.month == month_start).delete(synchronize_session=False)

    inserted = 0
    for row in rows:
        org_id = (row.org_id or "").strip()
        if not org_id:
            continue
        db.add(
            MonthlyOrgSummary(
                org_id=org_id,
                project_id=row.project_id,
                tool_name=(row.tool_name or "").strip() or "unknown",
                month=month_start,
                total_events=row.total_events or 0,
                total_cost=row.total_cost or Decimal("0"),
                llm_cost=row.llm_cost or Decimal("0"),
                infra_cost=row.infra_cost or Decimal("0"),
                external_cost=row.external_cost or Decimal("0"),
                total_tokens=row.total_tokens or 0,
                total_prompt_tokens=row.total_prompt_tokens or 0,
                total_completion_tokens=row.total_completion_tokens or 0,
                avg_latency_ms=int(row.avg_latency_ms or 0),
                success_count=row.success_count or 0,
                failure_count=row.failure_count or 0,
                anomaly_count=row.anomaly_count or 0,
                misuse_count=row.misuse_count or 0,
            )
        )
        inserted += 1
    db.flush()
    return inserted


# ─────────────────────── anomaly detection ───────────────────────

def _detect_anomalies(db: Session) -> int:
    today = date.today()
    tool_rows = (
        db.query(
            DailyOrgSummary.org_id,
            DailyOrgSummary.project_id,
            DailyOrgSummary.tool_name,
            func.sum(DailyOrgSummary.total_events).label("events_today"),
            func.sum(DailyOrgSummary.total_cost).label("cost_today"),
            func.avg(DailyOrgSummary.avg_latency_ms).label("latency_today"),
        )
        .filter(DailyOrgSummary.date == today)
        .group_by(DailyOrgSummary.org_id, DailyOrgSummary.project_id, DailyOrgSummary.tool_name)
        .all()
    )

    created = 0
    for row in tool_rows:
        baseline = (
            db.query(
                func.avg(DailyOrgSummary.total_events).label("avg_events"),
                func.avg(DailyOrgSummary.total_cost).label("avg_cost"),
                func.avg(DailyOrgSummary.avg_latency_ms).label("avg_latency"),
            )
            .filter(
                DailyOrgSummary.org_id == row.org_id,
                DailyOrgSummary.tool_name == row.tool_name,
                DailyOrgSummary.date >= today - timedelta(days=7),
                DailyOrgSummary.date < today,
            )
            .first()
        )
        if not baseline or not baseline.avg_events:
            continue

        checks = [
            ("usage_spike", Decimal(str(baseline.avg_events or 0)), Decimal(str(row.events_today or 0))),
            ("cost_spike", Decimal(str(baseline.avg_cost or 0)), Decimal(str(row.cost_today or 0))),
            ("latency_spike", Decimal(str(baseline.avg_latency or 0)), Decimal(str(row.latency_today or 0))),
        ]
        for anomaly_type, base_val, observed in checks:
            if base_val <= 0:
                continue
            score = observed / base_val
            if score >= Decimal("1.8"):
                db.add(
                    UsageAnomaly(
                        org_id=row.org_id,
                        project_id=row.project_id,
                        tool_name=row.tool_name,
                        anomaly_type=anomaly_type,
                        severity="high" if score >= Decimal("2.5") else "medium",
                        anomaly_score=score.quantize(Decimal("0.01")),
                        baseline_value=base_val.quantize(Decimal("0.01")),
                        observed_value=observed.quantize(Decimal("0.01")),
                        message=f"{anomaly_type.replace('_', ' ')} for {row.tool_name}: {observed:.2f} vs baseline {base_val:.2f}.",
                    )
                )
                created += 1

    db.flush()
    return created


# ─────────────────────── connector pull ───────────────────────

def _pull_connector(connector: ToolConnector) -> tuple[list[dict], str | None]:
    """HTTP pull for a single connector. Returns (events_list, error_or_None)."""
    import httpx

    if not connector.endpoint_url:
        return [], "no endpoint_url configured"

    headers: dict[str, str] = {"Accept": "application/json"}
    auth_type = (connector.auth_type or "").lower()
    if connector.api_key:
        if auth_type == "x-api-key":
            headers["X-API-Key"] = connector.api_key
        else:
            headers["Authorization"] = f"Bearer {connector.api_key}"

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(connector.endpoint_url, headers=headers)
        resp.raise_for_status()
        body = resp.json()
        if isinstance(body, list):
            return body, None
        if isinstance(body, dict):
            for key in ("events", "data", "items", "results", "logs"):
                if isinstance(body.get(key), list):
                    return body[key], None
        return [], None
    except Exception as exc:
        return [], str(exc)


def _run_connector_poll(db: Session) -> dict:
    """Poll all active pull-mode connectors and ingest any returned events."""
    from app.models import TelemetryEvent as TE
    from app.services.cost_engine import CostEngine
    from app.services.security_engine import SecurityEngine
    from app.services.alert_engine import AlertEngine

    connectors = (
        db.query(ToolConnector)
        .filter(
            ToolConnector.status == "active",
            ToolConnector.sync_enabled.is_(True),
            ToolConnector.ingestion_mode == "api",
        )
        .all()
    )

    total_pulled = 0
    total_errors = 0

    for connector in connectors:
        t0 = time.time()
        raw_events, error = _pull_connector(connector)
        duration_ms = int((time.time() - t0) * 1000)

        events_ingested = 0
        if raw_events:
            for raw in raw_events:
                try:
                    event_id = raw.get("event_id") or str(uuid.uuid4())
                    existing = db.query(TE).filter(TE.event_id == event_id).first()
                    if existing:
                        continue

                    row = TE(
                        event_id=event_id,
                        org_id=connector.org_id or raw.get("org_id", "default"),
                        project_id=connector.project_id or raw.get("project_id"),
                        tool_name=raw.get("tool_name", connector.tool_name),
                        provider=raw.get("provider", connector.provider),
                        model_name=raw.get("model_name"),
                        status=raw.get("status", "success"),
                        prompt_tokens=int(raw.get("prompt_tokens", 0)),
                        completion_tokens=int(raw.get("completion_tokens", 0)),
                        total_tokens=int(raw.get("total_tokens", 0)),
                        latency_ms=int(raw.get("latency_ms", 0)),
                        input_data_size_mb=Decimal(str(raw.get("input_data_size_mb", 0))),
                        output_data_size_mb=Decimal(str(raw.get("output_data_size_mb", 0))),
                        metadata_json=raw.get("metadata_json", {}),
                    )
                    db.add(row)
                    db.flush()

                    CostEngine().calculate(db, row)
                    SecurityEngine().analyze(db, row, raw.get("contains_pii", False), raw.get("pii_type"))
                    AlertEngine().evaluate(db, row)
                    events_ingested += 1
                except Exception as exc:
                    logger.warning("Connector %s: failed to ingest event: %s", connector.connector_name, exc)

        sync_status = "error" if error and not events_ingested else ("no_data" if not events_ingested else "success")
        db.add(ConnectorSyncLog(
            connector_id=connector.id,
            connector_name=connector.connector_name,
            sync_status=sync_status,
            events_pulled=events_ingested,
            error_message=error,
            duration_ms=duration_ms,
        ))

        connector.last_ingested_at = datetime.utcnow()
        connector.last_sync_status = sync_status
        connector.last_sync_error = error
        connector.total_events_pulled = (connector.total_events_pulled or 0) + events_ingested

        total_pulled += events_ingested
        if error:
            total_errors += 1
        logger.info("Connector %s: %d events, status=%s", connector.connector_name, events_ingested, sync_status)

    db.flush()
    return {"connectors_polled": len(connectors), "events_pulled": total_pulled, "errors": total_errors}
