"""Control Ingest Service — vendor-agnostic, schema-driven cost + usage ingestion.

Accepts structured input from any AI vendor, resolves pricing entirely from the
DB (model_pricing / tool_registry), normalises across providers, and forwards
the result through the existing _ingest_event pipeline.

Zero hardcoded pricing values. All rates come from DB tables.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models import ModelPricing, RateLimit, ToolRegistry, TraceModelUsage, TraceToolUsage
from app.schemas import (
    ExternalToolCost,
    ModelUsageItem,
    PipelineStageCreate,
    TelemetryEventCreate,
    ToolUsageItemEnhanced,
)


# ---------------------------------------------------------------------------
# Public input schema (not a Pydantic model — validated at the router layer)
# ---------------------------------------------------------------------------

class SDKEvent:
    """Structured, vendor-agnostic event payload accepted by the control plane."""

    def __init__(
        self,
        *,
        org_id: str,
        project_id: str | None = None,
        user_id: str | None = None,
        provider: str,
        model_name: str | None = None,
        tool_name: str | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_per_call: Decimal | float | None = None,
        input_cost_per_1k: Decimal | float | None = None,
        output_cost_per_1k: Decimal | float | None = None,
        tool_usages: list[dict[str, Any]] | None = None,
        latency_ms: int = 0,
        status: str = "success",
        trace_id: str | None = None,
        service_type: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        stages: list[dict[str, Any]] | None = None,
        contains_pii: bool = False,
        pii_type: str | None = None,
        data_out_violation: bool = False,
        input_data_size_mb: float = 0.0,
        output_data_size_mb: float = 0.0,
        input_preview: str | None = None,
        output_preview: str | None = None,
        event_id: str | None = None,
    ) -> None:
        self.event_id = event_id or str(uuid.uuid4())
        self.org_id = org_id
        self.project_id = project_id
        self.user_id = user_id
        self.provider = provider
        self.model_name = model_name
        self.tool_name = tool_name or model_name or provider
        self.input_tokens = int(input_tokens or 0)
        self.output_tokens = int(output_tokens or 0)
        self.cost_per_call = Decimal(str(cost_per_call)) if cost_per_call is not None else None
        self.input_cost_per_1k = Decimal(str(input_cost_per_1k)) if input_cost_per_1k is not None else None
        self.output_cost_per_1k = Decimal(str(output_cost_per_1k)) if output_cost_per_1k is not None else None
        self.tool_usages = tool_usages or []
        self.latency_ms = int(latency_ms or 0)
        self.status = status
        self.trace_id = trace_id
        self.service_type = service_type
        self.tags = tags or []
        self.metadata = metadata or {}
        self.stages = stages or []
        self.contains_pii = contains_pii
        self.pii_type = pii_type
        self.data_out_violation = data_out_violation
        self.input_data_size_mb = float(input_data_size_mb or 0)
        self.output_data_size_mb = float(output_data_size_mb or 0)
        self.input_preview = input_preview
        self.output_preview = output_preview


class SDKIngestService:
    """Resolves DB pricing, normalises the event, and returns a TelemetryEventCreate."""

    # ------------------------------------------------------------------ public

    def to_telemetry(self, sdk_event: SDKEvent, db: Session) -> TelemetryEventCreate:
        pricing = self._resolve_pricing(sdk_event, db)
        external_tools = self._build_external_tools(sdk_event, db)
        stages = self._build_stages(sdk_event)

        return TelemetryEventCreate(
            event_id=sdk_event.event_id,
            trace_id=sdk_event.trace_id or sdk_event.event_id,
            org_id=sdk_event.org_id,
            project_id=sdk_event.project_id,
            user_id=sdk_event.user_id,
            provider=sdk_event.provider,
            model_name=sdk_event.model_name,
            tool_name=sdk_event.tool_name,
            service_type=sdk_event.service_type,
            prompt_tokens=sdk_event.input_tokens,
            completion_tokens=sdk_event.output_tokens,
            latency_ms=sdk_event.latency_ms,
            status=sdk_event.status,
            tags=sdk_event.tags,
            metadata_json=sdk_event.metadata,
            external_tools=external_tools,
            stages=stages,
            contains_pii=sdk_event.contains_pii,
            pii_type=sdk_event.pii_type,
            data_out_violation=sdk_event.data_out_violation,
            input_data_size_mb=Decimal(str(sdk_event.input_data_size_mb)),
            output_data_size_mb=Decimal(str(sdk_event.output_data_size_mb)),
            input_preview=sdk_event.input_preview,
            output_preview=sdk_event.output_preview,
            infra_cost=Decimal("0"),
            raw_usage_json={
                "input_tokens": sdk_event.input_tokens,
                "output_tokens": sdk_event.output_tokens,
                "total_tokens": sdk_event.input_tokens + sdk_event.output_tokens,
                "provider": sdk_event.provider,
                "model_name": sdk_event.model_name,
                "latency_ms": sdk_event.latency_ms,
                "pricing_source": pricing["source"],
            },
        )

    # ----------------------------------------------------------------- private

    def _resolve_pricing(self, sdk_event: SDKEvent, db: Session) -> dict:
        """Determine where pricing comes from. Never falls back to a hardcoded rate."""
        total_tokens = sdk_event.input_tokens + sdk_event.output_tokens

        if sdk_event.cost_per_call is not None:
            return {"source": "caller_per_call", "cost": sdk_event.cost_per_call}

        if sdk_event.input_cost_per_1k is not None or sdk_event.output_cost_per_1k is not None:
            in_rate = sdk_event.input_cost_per_1k or Decimal("0")
            out_rate = sdk_event.output_cost_per_1k or Decimal("0")
            cost = (
                Decimal(str(sdk_event.input_tokens)) / 1000 * in_rate
                + Decimal(str(sdk_event.output_tokens)) / 1000 * out_rate
            )
            return {"source": "caller_per_token", "cost": cost}

        if sdk_event.model_name and sdk_event.provider:
            row = (
                db.query(ModelPricing)
                .filter(
                    ModelPricing.provider == sdk_event.provider,
                    ModelPricing.model_name == sdk_event.model_name,
                )
                .first()
            )
            if row and total_tokens > 0:
                cost = (
                    Decimal(str(sdk_event.input_tokens)) / 1000 * Decimal(str(row.input_cost_per_1k or 0))
                    + Decimal(str(sdk_event.output_tokens)) / 1000 * Decimal(str(row.output_cost_per_1k or 0))
                )
                return {"source": "db_model_pricing", "cost": cost}

        tool_key = sdk_event.model_name or sdk_event.tool_name
        if tool_key:
            tool = db.query(ToolRegistry).filter(ToolRegistry.tool_name == tool_key).first()
            if tool and tool.base_cost and Decimal(str(tool.base_cost)) > 0:
                base = Decimal(str(tool.base_cost))
                cost_model = tool.cost_model or "per_token"
                if cost_model == "per_token" and total_tokens > 0:
                    cost = (Decimal(str(total_tokens)) / 1000) * base
                elif cost_model == "per_request":
                    cost = base
                else:
                    cost = (Decimal(str(total_tokens)) / 1000) * base if total_tokens > 0 else Decimal("0")
                return {"source": "db_tool_registry", "cost": cost}

        return {"source": "none", "cost": Decimal("0")}

    def _build_external_tools(self, sdk_event: SDKEvent, db: Session) -> list[ExternalToolCost]:
        results: list[ExternalToolCost] = []
        for tu in sdk_event.tool_usages:
            name = tu.get("name") or tu.get("tool_name", "unknown")
            cost_val = tu.get("cost")
            if cost_val is None:
                tool = db.query(ToolRegistry).filter(ToolRegistry.tool_name == name).first()
                cost_val = float(tool.base_cost or 0) if tool else 0.0
            results.append(ExternalToolCost(name=name, cost=Decimal(str(cost_val or 0))))
        return results

    def _build_stages(self, sdk_event: SDKEvent) -> list[PipelineStageCreate]:
        stages = []
        for i, s in enumerate(sdk_event.stages):
            stages.append(
                PipelineStageCreate(
                    stage_order=s.get("stage_order", i),
                    stage_name=s.get("stage_name", f"stage_{i}"),
                    system_name=s.get("system_name"),
                    status=s.get("status", "success"),
                    stage_latency_ms=int(s.get("stage_latency_ms", 0)),
                    retry_count=int(s.get("retry_count", 0)),
                    details=s.get("details", {}),
                )
            )
        return stages


# Module-level singleton
sdk_ingest_service = SDKIngestService()


# ─────────────────────────────────────────────────────────────────────────────
# Unified Trace Processor — multi-model / multi-tool per event
# ─────────────────────────────────────────────────────────────────────────────

class UnifiedTraceResult:
    """Container returned by UnifiedTraceProcessor.process()."""

    def __init__(
        self,
        telemetry_create: TelemetryEventCreate,
        model_records: list,
        tool_records: list,
    ) -> None:
        self.telemetry_create = telemetry_create
        self.model_records = model_records
        self.tool_records = tool_records


class UnifiedTraceProcessor:
    """
    Processes a unified trace payload containing multiple models and tools.

    Resolves per-model pricing from DB (model_pricing → tool_registry fallback),
    aggregates totals, and emits a single TelemetryEventCreate ready for the
    standard _ingest_event pipeline, plus ORM records for trace_model_usage /
    trace_tool_usage child tables.
    """

    def process(self, payload: "UnifiedTraceRequest", db: Session) -> UnifiedTraceResult:  # noqa: F821
        event_id = payload.event_id or str(uuid.uuid4())
        trace_id = payload.trace_id or event_id

        total_input_tokens = 0
        total_output_tokens = 0
        total_llm_cost = Decimal("0")
        max_latency_ms = 0
        model_records: list[TraceModelUsage] = []

        for m in payload.models:
            in_tok = int(m.input_tokens or 0)
            out_tok = int(m.output_tokens or 0)
            model_cost = self._resolve_model_cost(m, db)
            total_input_tokens += in_tok
            total_output_tokens += out_tok
            total_llm_cost += model_cost
            max_latency_ms = max(max_latency_ms, int(m.latency_ms or 0))

            model_records.append(
                TraceModelUsage(
                    event_id=event_id,
                    trace_id=trace_id,
                    org_id=payload.org_id,
                    project_id=payload.project_id,
                    model_name=m.model_name,
                    provider=m.provider,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    total_tokens=in_tok + out_tok,
                    llm_cost=model_cost,
                    latency_ms=int(m.latency_ms or 0),
                )
            )

        total_tool_cost = Decimal("0")
        external_tools: list[ExternalToolCost] = []
        tool_records: list[TraceToolUsage] = []

        for t in payload.tools:
            tool_cost = self._resolve_tool_cost(t, db)
            total_tool_cost += tool_cost
            tool_records.append(
                TraceToolUsage(
                    event_id=event_id,
                    trace_id=trace_id,
                    org_id=payload.org_id,
                    project_id=payload.project_id,
                    tool_name=t.tool_name,
                    tool_type=t.tool_type,
                    invocation_count=int(t.invocation_count or 1),
                    execution_time_ms=int(t.execution_time_ms or 0),
                    cost=tool_cost,
                )
            )
            if tool_cost > 0:
                external_tools.append(ExternalToolCost(name=t.tool_name, cost=tool_cost))

        primary_model = payload.models[0].model_name if payload.models else "multi-model"
        primary_provider = payload.models[0].provider if payload.models else None

        telemetry_create = TelemetryEventCreate(
            event_id=event_id,
            trace_id=trace_id,
            org_id=payload.org_id,
            project_id=payload.project_id,
            user_id=payload.user_id,
            provider=primary_provider,
            model_name=primary_model,
            tool_name=payload.workflow_name or primary_model or "unified-trace",
            service_type="unified-trace",
            prompt_tokens=total_input_tokens,
            completion_tokens=total_output_tokens,
            latency_ms=max_latency_ms,
            status=payload.status,
            tags=list(payload.tags),
            metadata_json={
                **payload.metadata,
                "is_unified_trace": True,
                "workflow_name": payload.workflow_name,
                "model_count": len(payload.models),
                "tool_count": len(payload.tools),
            },
            external_tools=external_tools,
            contains_pii=payload.contains_pii,
            pii_type=payload.pii_type,
            data_out_violation=payload.data_out_violation,
            input_data_size_mb=Decimal(str(payload.input_data_size_mb)),
            output_data_size_mb=Decimal(str(payload.output_data_size_mb)),
            infra_cost=Decimal("0"),
            precomputed_llm_cost=total_llm_cost,
            raw_usage_json={
                "is_unified_trace": True,
                "total_input_tokens": total_input_tokens,
                "total_output_tokens": total_output_tokens,
                "total_tokens": total_input_tokens + total_output_tokens,
                "total_llm_cost": float(total_llm_cost),
                "total_tool_cost": float(total_tool_cost),
                "model_count": len(payload.models),
                "tool_count": len(payload.tools),
                "models": [
                    {
                        "model_name": m.model_name,
                        "provider": m.provider,
                        "input_tokens": m.input_tokens,
                        "output_tokens": m.output_tokens,
                        "latency_ms": m.latency_ms,
                    }
                    for m in payload.models
                ],
                "tools": [
                    {
                        "tool_name": t.tool_name,
                        "tool_type": t.tool_type,
                        "invocation_count": t.invocation_count,
                        "execution_time_ms": t.execution_time_ms,
                    }
                    for t in payload.tools
                ],
            },
        )

        return UnifiedTraceResult(telemetry_create, model_records, tool_records)

    # ----------------------------------------------------------------- private

    def _resolve_model_cost(self, m: ModelUsageItem, db: Session) -> Decimal:
        if m.cost is not None:
            return Decimal(str(m.cost))

        in_tok = int(m.input_tokens or 0)
        out_tok = int(m.output_tokens or 0)

        if m.input_cost_per_1k is not None or m.output_cost_per_1k is not None:
            in_rate = Decimal(str(m.input_cost_per_1k or 0))
            out_rate = Decimal(str(m.output_cost_per_1k or 0))
            return (
                Decimal(str(in_tok)) / 1000 * in_rate
                + Decimal(str(out_tok)) / 1000 * out_rate
            )

        if m.model_name and m.provider:
            row = (
                db.query(ModelPricing)
                .filter(
                    ModelPricing.provider == m.provider,
                    ModelPricing.model_name == m.model_name,
                )
                .first()
            )
            if row and (in_tok + out_tok) > 0:
                return (
                    Decimal(str(in_tok)) / 1000 * Decimal(str(row.input_cost_per_1k or 0))
                    + Decimal(str(out_tok)) / 1000 * Decimal(str(row.output_cost_per_1k or 0))
                )

        return Decimal("0")

    def _resolve_tool_cost(self, t: ToolUsageItemEnhanced, db: Session) -> Decimal:
        if t.cost is not None:
            return Decimal(str(t.cost))

        tool = db.query(ToolRegistry).filter(ToolRegistry.tool_name == t.tool_name).first()
        if tool and tool.base_cost and Decimal(str(tool.base_cost)) > 0:
            base = Decimal(str(tool.base_cost))
            cost_model = tool.cost_model or "per_request"
            if cost_model == "per_request":
                return base * int(t.invocation_count or 1)
            elif cost_model == "per_second":
                return base * Decimal(str((t.execution_time_ms or 0) / 1000))
            else:
                return base

        return Decimal("0")


# Module-level singleton
unified_trace_processor = UnifiedTraceProcessor()


# Forward reference used in type hints above
class UnifiedTraceRequest:
    """Minimal interface expected by UnifiedTraceProcessor (defined in control.py)."""
    org_id: str
    project_id: "str | None"
    user_id: "str | None"
    trace_id: "str | None"
    workflow_name: "str | None"
    status: str
    models: "list[ModelUsageItem]"
    tools: "list[ToolUsageItemEnhanced]"
    tags: "list[str]"
    metadata: dict
    contains_pii: bool
    pii_type: "str | None"
    data_out_violation: bool
    input_data_size_mb: float
    output_data_size_mb: float
    event_id: "str | None"
