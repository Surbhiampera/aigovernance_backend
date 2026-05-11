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
        return [
            TelemetryEventCreate(
                event_id=self._safe_event_id(raw),
                org_id=self._safe_org_id(connector),
                project_id=connector.project_id,
                provider=connector.provider or "unknown",
                tool_name=connector.tool_name,
                model_name=_pick(raw, _MODEL_KEYS) or connector.tool_name,
                service_type=raw.get("object") or raw.get("type") or "api_call",
                status=raw.get("status") or "success",
                prompt_tokens=int(_pick(usage, _PROMPT_KEYS) or 0),
                completion_tokens=int(_pick(usage, _COMPLETION_KEYS) or 0),
                started_at=datetime.now(timezone.utc),
                raw_usage_json=usage if isinstance(usage, dict) else {},
                metadata_json={
                    "source": "generic_adapter",
                    "connector": connector.connector_name,
                },
            )
        ]
