"""Abstract base for all vendor adapters.

Implement this interface to add a new AI vendor without touching any core code.
Register the subclass with @adapter_registry.register and it is automatically
discovered by the ingestion pipeline.
"""
import uuid
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from app.models import ToolConnector
    from app.schemas import TelemetryEventCreate


class VendorAdapter(ABC):
    """Transform vendor-specific log payloads into the platform's standard schema."""

    provider_name: ClassVar[str] = ""

    @abstractmethod
    def normalize(
        self,
        raw: dict[str, Any],
        connector: "ToolConnector",
    ) -> list["TelemetryEventCreate"]:
        """Convert one raw vendor payload into one or more TelemetryEventCreate instances."""
        ...

    def pull(self, connector: "ToolConnector") -> list[dict[str, Any]]:
        """Pull recent logs from the vendor API (API-pull ingestion mode only).

        Default returns [] — override in adapters that support server-side retrieval.
        """
        return []

    @staticmethod
    def _safe_org_id(connector: "ToolConnector") -> str:
        """Return org_id with fallback to 'default', normalising empty strings."""
        return (connector.org_id or "").strip() or "default"

    def _safe_event_id(self, raw: dict[str, Any]) -> str:
        """Return event_id from raw payload or generate a vendor-prefixed UUID."""
        return raw.get("id") or f"{self.provider_name}-{uuid.uuid4().hex}"
