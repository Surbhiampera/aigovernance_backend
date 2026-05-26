"""Plug-and-play adapter registry.

New vendors self-register by decorating their VendorAdapter subclass with
@adapter_registry.register.  No changes to core code are needed.

Example
-------
from app.services.ingestion.registry import adapter_registry
from app.services.ingestion.base import VendorAdapter

@adapter_registry.register
class MyVendorAdapter(VendorAdapter):
    provider_name = "myvendor"
    ...
"""
import logging
from typing import Optional, Type

from app.services.ingestion.base import VendorAdapter

logger = logging.getLogger(__name__)


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, Type[VendorAdapter]] = {}
        self._instances: dict[str, VendorAdapter] = {}

    def register(self, cls: Type[VendorAdapter]) -> Type[VendorAdapter]:
        """Class decorator — registers an adapter by its provider_name class variable."""
        key = cls.provider_name.lower().strip()
        self._adapters[key] = cls
        self._instances[key] = cls()
        logger.debug("Ingestion adapter registered: %s → %s", key, cls.__name__)
        return cls

    def resolve(self, provider: str) -> Optional[VendorAdapter]:
        """Return the cached adapter instance for the given provider string.

        Falls back to the 'generic' adapter when no exact match is found.
        """
        key = (provider or "").strip().lower()
        instance = self._instances.get(key) or self._instances.get("generic")
        if instance is None:
            logger.warning("No adapter found for provider '%s' and no generic fallback.", key)
        return instance

    def registered_providers(self) -> list[str]:
        return sorted(self._instances.keys())


adapter_registry = AdapterRegistry()
