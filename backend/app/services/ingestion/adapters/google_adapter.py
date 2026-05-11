"""Google / Vertex AI vendor adapter.

Normalises Vertex AI / Gemini API response payloads.

Expected raw payload (Gemini generateContent response):
{
  "usageMetadata": {
    "promptTokenCount": 100,
    "candidatesTokenCount": 50,
    "totalTokenCount": 150
  },
  "modelVersion": "gemini-1.5-pro-001"
}
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


@adapter_registry.register
class GoogleAdapter(VendorAdapter):
    provider_name = "google"

    def normalize(
        self,
        raw: dict[str, Any],
        connector: "ToolConnector",
    ) -> list[TelemetryEventCreate]:
        meta = raw.get("usageMetadata") or {}
        return [
            TelemetryEventCreate(
                event_id=self._safe_event_id(raw),
                org_id=self._safe_org_id(connector),
                project_id=connector.project_id,
                provider="google",
                tool_name=connector.tool_name,
                model_name=raw.get("modelVersion") or raw.get("model") or connector.tool_name,
                service_type="generateContent",
                status="success",
                prompt_tokens=int(meta.get("promptTokenCount") or 0),
                completion_tokens=int(meta.get("candidatesTokenCount") or 0),
                started_at=datetime.now(timezone.utc),
                raw_usage_json=meta,
                metadata_json={
                    "source": "google_api",
                    "connector": connector.connector_name,
                },
            )
        ]
