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
from datetime import date, datetime, timezone
from typing import Any, TYPE_CHECKING

import httpx

from app.schemas import TelemetryEventCreate
from app.services.ingestion.base import VendorAdapter
from app.services.ingestion.registry import adapter_registry

if TYPE_CHECKING:
    from app.models import ToolConnector

@adapter_registry.register
class OpenAIAdapter(VendorAdapter):
    provider_name = "openai"

    def normalize(
        self,
        raw: dict[str, Any],
        connector: "ToolConnector",
    ) -> list[TelemetryEventCreate]:
        # Support both nested {"usage": {...}} and flat root-level token fields
        usage = raw.get("usage") or {}
        prompt_tokens = int(
            usage.get("prompt_tokens") or raw.get("prompt_tokens") or 0
        )
        completion_tokens = int(
            usage.get("completion_tokens") or raw.get("completion_tokens") or 0
        )

        # latency_seconds (flat payloads) or latency_ms (standard)
        latency_ms = int(
            raw.get("latency_ms")
            or int((raw.get("latency_seconds") or 0) * 1000)
        )

        # estimated_cost_usd → used as precomputed LLM cost so CostEngine skips recalc
        estimated_cost = raw.get("estimated_cost_usd")
        from decimal import Decimal
        precomputed_llm_cost = Decimal(str(estimated_cost)) if estimated_cost is not None else None

        # timestamp / created → started_at
        created_ts = raw.get("created")
        timestamp_str = raw.get("timestamp")
        if created_ts:
            started_at = datetime.fromtimestamp(created_ts, tz=timezone.utc)
        elif timestamp_str:
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
                provider="openai",
                tool_name=connector.tool_name,
                model_name=raw.get("model") or raw.get("model_name") or connector.tool_name,
                service_type=raw.get("object") or raw.get("route") or raw.get("service_type") or "chat.completion",
                status=raw.get("status") or "success",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
                precomputed_llm_cost=precomputed_llm_cost,
                started_at=started_at,
                raw_usage_json=usage if usage else {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                },
                metadata_json={
                    "source": "openai_api",
                    "connector": connector.connector_name,
                    "route": raw.get("route"),
                    "system_fingerprint": raw.get("system_fingerprint"),
                },
            )
        ]

    def pull(self, connector: "ToolConnector") -> list[dict[str, Any]]:
        """Pull today's usage records from OpenAI's usage API endpoint."""
        if not connector.api_key:
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
                return []
            return data.get("data") or []
        except (httpx.HTTPStatusError, Exception):
            return []
