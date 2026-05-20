"""Generic / fallback vendor adapter.

Handles any vendor not covered by a specific adapter by applying best-effort
field mapping across common naming conventions.

Recognised token field names:
  prompt_tokens, input_tokens, promptTokenCount  → prompt_tokens
  completion_tokens, output_tokens, candidatesTokenCount → completion_tokens

Recognised model field names: model, model_name, modelVersion
Recognised id field names:    id, request_id, completion_id
"""
import logging
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from app.schemas import TelemetryEventCreate
from app.services.ingestion.base import VendorAdapter
from app.services.ingestion.registry import adapter_registry

if TYPE_CHECKING:
    from app.models import ToolConnector

logger = logging.getLogger(__name__)

_PROMPT_KEYS = ("prompt_tokens", "input_tokens", "promptTokenCount")
_COMPLETION_KEYS = ("completion_tokens", "output_tokens", "candidatesTokenCount")
_MODEL_KEYS = ("model", "model_name", "modelVersion")
_ID_KEYS = ("id", "request_id", "completion_id")


def _pick(d: dict, keys: tuple) -> Any:
    for k in keys:
        if k in d:
            return d[k]
    return None


@adapter_registry.register
class GenericAdapter(VendorAdapter):
    provider_name = "generic"

    def normalize(
        self,
        raw: dict[str, Any],
        connector: "ToolConnector",
    ) -> list[TelemetryEventCreate]:
        usage = raw.get("usage") or raw.get("usageMetadata") or raw
        raw_id = _pick(raw, _ID_KEYS)
        if raw_id:
            raw["id"] = str(raw_id)
        estimated_cost = raw.get("estimated_cost_usd")
        from decimal import Decimal
        precomputed_llm_cost = Decimal(str(estimated_cost)) if estimated_cost is not None else None

        latency_ms = int(
            raw.get("latency_ms")
            or int((raw.get("latency_seconds") or 0) * 1000)
        )

        timestamp_str = raw.get("timestamp")
        if timestamp_str:
            try:
                started_at = datetime.fromisoformat(str(timestamp_str))
            except ValueError:
                started_at = datetime.now(timezone.utc)
        else:
            started_at = datetime.now(timezone.utc)

        return [
            TelemetryEventCreate(
                event_id=self._safe_event_id(raw),
                org_id=self._safe_org_id(connector),
                project_id=connector.project_id,
                provider=connector.provider or "unknown",
                tool_name=connector.tool_name,
                model_name=_pick(raw, _MODEL_KEYS) or connector.tool_name,
                service_type=raw.get("object") or raw.get("route") or raw.get("type") or "api_call",
                status=raw.get("status") or "success",
                prompt_tokens=int(_pick(usage, _PROMPT_KEYS) or 0),
                completion_tokens=int(_pick(usage, _COMPLETION_KEYS) or 0),
                latency_ms=latency_ms,
                precomputed_llm_cost=precomputed_llm_cost,
                started_at=started_at,
                raw_usage_json=usage if isinstance(usage, dict) else {},
                metadata_json={
                    "source": "generic_adapter",
                    "connector": connector.connector_name,
                    "route": raw.get("route"),
                },
            )
        ]
