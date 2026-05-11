"""OpenAI vendor adapter.

Normalises OpenAI Chat Completions / Completions API responses into
TelemetryEventCreate instances.

Expected raw payload (Chat Completions response):
{
  "id": "chatcmpl-abc",
  "object": "chat.completion",
  "created": 1700000000,
  "model": "gpt-4o",
  "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
  "system_fingerprint": "fp_xxx"
}
"""
import logging
from datetime import date, datetime, timezone
from typing import Any, TYPE_CHECKING

import httpx

from app.schemas import TelemetryEventCreate
from app.services.ingestion.base import VendorAdapter
from app.services.ingestion.registry import adapter_registry

if TYPE_CHECKING:
    from app.models import ToolConnector

logger = logging.getLogger(__name__)


@adapter_registry.register
class OpenAIAdapter(VendorAdapter):
    provider_name = "openai"

    def normalize(
        self,
        raw: dict[str, Any],
        connector: "ToolConnector",
    ) -> list[TelemetryEventCreate]:
        usage = raw.get("usage") or {}
        created_ts = raw.get("created")
        started_at = (
            datetime.fromtimestamp(created_ts, tz=timezone.utc)
            if created_ts
            else datetime.now(timezone.utc)
        )
        return [
            TelemetryEventCreate(
                event_id=self._safe_event_id(raw),
                org_id=self._safe_org_id(connector),
                project_id=connector.project_id,
                provider="openai",
                tool_name=connector.tool_name,
                model_name=raw.get("model") or connector.tool_name,
                service_type=raw.get("object") or "chat.completion",
                status="success",
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                started_at=started_at,
                raw_usage_json=usage,
                metadata_json={
                    "source": "openai_api",
                    "connector": connector.connector_name,
                    "system_fingerprint": raw.get("system_fingerprint"),
                },
            )
        ]

    def pull(self, connector: "ToolConnector") -> list[dict[str, Any]]:
        """Pull today's usage records from OpenAI's usage API endpoint."""
        if not connector.api_key:
            logger.warning("OpenAI connector '%s' has no api_key — skipping pull.", connector.connector_name)
            return []

        endpoint = (connector.endpoint_url or "https://api.openai.com/v1").rstrip("/")
        headers = {"Authorization": f"Bearer {connector.api_key}"}
        try:
            resp = httpx.get(
                f"{endpoint}/usage?date={date.today().isoformat()}",
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                logger.warning("OpenAI usage endpoint returned unexpected type %s for '%s'.", type(data), connector.connector_name)
                return []
            return data.get("data") or []
        except httpx.HTTPStatusError as exc:
            logger.warning("OpenAI pull HTTP error for '%s': %s", connector.connector_name, exc)
            return []
        except Exception as exc:
            logger.warning("OpenAI pull failed for '%s': %s", connector.connector_name, exc)
            return []
