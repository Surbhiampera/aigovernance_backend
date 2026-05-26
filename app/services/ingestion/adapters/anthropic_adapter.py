"""Anthropic vendor adapter.

Normalises Anthropic Messages API responses into TelemetryEventCreate.

Expected raw payload:
{
  "id": "msg_abc",
  "type": "message",
  "model": "claude-3-5-sonnet-20241022",
  "usage": {"input_tokens": 100, "output_tokens": 50},
  "stop_reason": "end_turn",
  "created_at": "2024-01-01T00:00:00Z"   (optional)
}

Anthropic does not expose a server-side log retrieval API; ingest via webhook.
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


def _parse_status(stop_reason: str | None) -> str:
    if stop_reason == "end_turn":
        return "success"
    return stop_reason or "success"


def _parse_timestamp(raw: dict[str, Any]) -> datetime:
    created_ts = raw.get("created_at")
    if created_ts:
        try:
            return datetime.fromisoformat(created_ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            pass
    return datetime.now(timezone.utc)


@adapter_registry.register
class AnthropicAdapter(VendorAdapter):
    provider_name = "anthropic"

    def normalize(
        self,
        raw: dict[str, Any],
        connector: "ToolConnector",
    ) -> list[TelemetryEventCreate]:
        usage = raw.get("usage") or {}
        return [
            TelemetryEventCreate(
                event_id=self._safe_event_id(raw),
                org_id=self._safe_org_id(connector),
                project_id=connector.project_id,
                provider="anthropic",
                tool_name=connector.tool_name,
                model_name=raw.get("model") or connector.tool_name,
                service_type=raw.get("type") or "message",
                status=_parse_status(raw.get("stop_reason")),
                prompt_tokens=int(usage.get("input_tokens") or 0),
                completion_tokens=int(usage.get("output_tokens") or 0),
                started_at=_parse_timestamp(raw),
                raw_usage_json=usage,
                metadata_json={
                    "source": "anthropic_api",
                    "connector": connector.connector_name,
                    "stop_reason": raw.get("stop_reason"),
                },
            )
        ]
