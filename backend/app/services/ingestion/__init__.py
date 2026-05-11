from app.services.ingestion.registry import adapter_registry
from app.services.ingestion.normalizer import IngestionNormalizer
import app.services.ingestion.adapters as _adapters  # noqa: F401 — triggers @adapter_registry.register on all 4 adapters

__all__ = ["adapter_registry", "IngestionNormalizer"]
