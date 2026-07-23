from app.services.optimization.rules import (  # noqa: F401 — each import triggers @tip_registry.register
    cache_opportunity,
    model_substitution,
    oversized_prompt,
    response_length,
    response_truncated,
)
