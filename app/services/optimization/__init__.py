from app.services.optimization.registry import tip_registry
import app.services.optimization.rules as _rules  # noqa: F401 — triggers @tip_registry.register on all 5 rules

__all__ = ["tip_registry"]
