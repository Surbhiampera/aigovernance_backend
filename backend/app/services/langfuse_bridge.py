"""Langfuse bridge — additive observability mirror.

The existing telemetry pipeline (DB, cost engine, alerts, security) remains the
system of record.  This module *also* mirrors each ingested event to a
Langfuse instance so teams get the Langfuse waterfall UI, prompt/response
inspector, score/eval features, and dataset workflows for free.

Behaviour:
- No-op when ``LANGFUSE_ENABLED`` is not ``true``.
- No-op when the ``langfuse`` package is not installed (silent import skip).
- Network/SDK errors are swallowed — mirroring NEVER blocks ingestion.

Environment::

    LANGFUSE_ENABLED=true
    LANGFUSE_PUBLIC_KEY=pk-lf-...
    LANGFUSE_SECRET_KEY=sk-lf-...
    LANGFUSE_HOST=https://cloud.langfuse.com  (or self-host URL)

The mirror runs once per ingest — see ``mirror_event`` below.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from app.schemas import TelemetryEventCreate

logger = logging.getLogger(__name__)

_client: Any = None
_initialised: bool = False
_disabled_reason: Optional[str] = None


def _enabled() -> bool:
    return os.getenv("LANGFUSE_ENABLED", "false").strip().lower() == "true"


def _get_client() -> Any:
    """Lazily build a Langfuse client.  Returns ``None`` when unavailable."""
    global _client, _initialised, _disabled_reason
    if _initialised:
        return _client
    _initialised = True

    if not _enabled():
        _disabled_reason = "LANGFUSE_ENABLED is not 'true'"
        return None

    try:
        from langfuse import Langfuse  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dep
        _disabled_reason = f"langfuse package not installed ({exc})"
        logger.info("Langfuse mirror disabled: %s", _disabled_reason)
        return None

    public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY")
    host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    if not public_key or not secret_key:
        _disabled_reason = "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set"
        logger.info("Langfuse mirror disabled: %s", _disabled_reason)
        return None

    try:
        _client = Langfuse(public_key=public_key, secret_key=secret_key, host=host)
        logger.info("Langfuse mirror initialised (host=%s)", host)
    except Exception as exc:  # pragma: no cover
        _disabled_reason = f"Langfuse client init failed: {exc}"
        logger.warning("Langfuse mirror disabled: %s", _disabled_reason)
        _client = None
    return _client


def mirror_event(
    event: TelemetryEventCreate,
    *,
    llm_cost: float,
    total_cost: float,
    risk_score: float,
    pii_detected: bool,
) -> None:
    """Best-effort mirror of one telemetry event into Langfuse.

    Creates a trace and a generation span carrying the same identifiers used
    by the local DB so cross-referencing is trivial.  Any exception is logged
    and swallowed — this MUST NOT break the ingestion path.
    """
    client = _get_client()
    if client is None:
        return

    try:
        parent_trace_id = (event.metadata_json or {}).get("parent_trace_id")
        session_id = (
            (event.metadata_json or {}).get("session_name")
            or event.trace_id
            or parent_trace_id
        )

        trace = client.trace(
            id=event.trace_id or event.event_id,
            name=event.tool_name or event.component_name or "ai-governance-event",
            user_id=event.user_id,
            session_id=session_id,
            tags=list(event.tags or []),
            metadata={
                "org_id": event.org_id,
                "project_id": event.project_id,
                "service_type": event.service_type,
                "execution_type": event.execution_type,
                "request_id": event.request_id,
                "parent_trace_id": parent_trace_id,
                "risk_score": risk_score,
                "pii_detected": pii_detected,
                "data_in_mb": float(event.input_data_size_mb or 0),
                "data_out_mb": float(event.output_data_size_mb or 0),
                **(event.metadata_json or {}),
            },
        )

        # Mirror the LLM call as a generation
        if event.model_name or event.prompt_tokens or event.completion_tokens:
            trace.generation(
                id=event.event_id,
                name=event.model_name or event.tool_name,
                model=event.model_name,
                start_time=event.started_at,
                end_time=event.completed_at,
                usage={
                    "input": event.prompt_tokens or 0,
                    "output": event.completion_tokens or 0,
                    "total": (event.prompt_tokens or 0) + (event.completion_tokens or 0),
                    "unit": "TOKENS",
                    "input_cost": llm_cost,  # already aggregated; close enough for UI
                    "output_cost": 0.0,
                    "total_cost": total_cost,
                },
                level="ERROR" if (event.status or "").lower() not in {"success", "completed"} else "DEFAULT",
                status_message=event.status,
                metadata={"provider": event.provider},
            )

        # Mirror execution-pipeline stages as nested spans for the waterfall view
        for stage in event.stages or []:
            trace.span(
                name=stage.stage_name,
                start_time=event.started_at,
                metadata={
                    "system_name": stage.system_name,
                    "stage_order": stage.stage_order,
                    "retry_count": stage.retry_count,
                    "stage_latency_ms": stage.stage_latency_ms,
                    "details": stage.details,
                },
                level="ERROR" if (stage.status or "").lower() not in {"success", "completed"} else "DEFAULT",
                status_message=stage.status,
            )

        # Surface governance-relevant signals as Langfuse scores
        try:
            trace.score(name="risk_score", value=float(risk_score))
            if pii_detected:
                trace.score(name="pii_detected", value=1.0)
        except Exception:  # scoring is purely cosmetic
            pass

    except Exception as exc:  # pragma: no cover
        logger.debug("Langfuse mirror skipped for event %s: %s", event.event_id, exc)


def status() -> dict:
    """Diagnostic helper exposed via /workers status if desired."""
    _get_client()
    return {
        "enabled": _enabled(),
        "client_ready": _client is not None,
        "host": os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com") if _enabled() else None,
        "disabled_reason": _disabled_reason,
    }
