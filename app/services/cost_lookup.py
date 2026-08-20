"""Shared cost calculation — the single source of truth for pricing lookups.

Extracted from app/routers/proxy.py so the live proxy path and the
optimization-tips engine (app/services/optimization/rules/model_substitution.py)
never disagree about what a given token volume would cost. cost_engine.py
(used only by the ingestion pipeline) is a separate, older implementation that
has already drifted from this one — do not add a third copy.
"""
from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)


def calculate_cost(
    *,
    db: Session,
    model: str,
    provider: str = "",
    input_tokens: int,
    output_tokens: int,
) -> tuple[Decimal, Decimal, Decimal, str, dict, str]:
    """Return (input_cost, output_cost, total_cost, pricing_source, pricing_snapshot, pricing_version).

    Lookup order:
      1. DB model_pricing filtered by provider+model (admin overrides, most recent wins)
      2. DB model_pricing filtered by model only (provider-agnostic DB entry)
      3. PROVIDER_PRICING catalogue (provider-specific static rates)
      4. MODEL_PRICING catalogue (generic static rates)
      5. Default rate from config
    """
    from app.models import ModelPricing as ModelPricingRow
    from app.services.ai_model_pricing import (
        get_model_pricing_for_provider,
        normalize_model_name,
        normalize_provider,
    )

    canonical = normalize_model_name(model)
    prov = normalize_provider(provider)

    # 1. DB lookup: provider+model specific (highest priority — admin can override any rate)
    db_price = None
    if prov:
        db_price = (
            db.query(ModelPricingRow)
            .filter(
                ModelPricingRow.model_name == canonical,
                ModelPricingRow.provider == prov,
                ModelPricingRow.effective_from <= datetime.utcnow(),
            )
            .order_by(ModelPricingRow.effective_from.desc())
            .first()
        )
    # 2. DB lookup: model only (provider-agnostic fallback)
    if not db_price:
        db_price = (
            db.query(ModelPricingRow)
            .filter(
                ModelPricingRow.model_name == canonical,
                ModelPricingRow.effective_from <= datetime.utcnow(),
            )
            .order_by(ModelPricingRow.effective_from.desc())
            .first()
        )

    if db_price:
        in_rate = Decimal(str(db_price.input_cost_per_1k))
        out_rate = Decimal(str(db_price.output_cost_per_1k))
        input_cost = (Decimal(str(input_tokens)) / Decimal("1000") * in_rate).quantize(Decimal("0.000001"))
        output_cost = (Decimal(str(output_tokens)) / Decimal("1000") * out_rate).quantize(Decimal("0.000001"))
        eff_date = db_price.effective_from.date().isoformat() if db_price.effective_from else "unknown"
        pricing_source = "database"
        pricing_snapshot = {
            "input_cost_per_1k": float(in_rate),
            "output_cost_per_1k": float(out_rate),
            "provider": db_price.provider or prov,
            "currency": db_price.currency or "USD",
            "effective_from": eff_date,
            "source": "database",
        }
        pricing_version = f"{canonical}@{eff_date}"
    else:
        # 3+4. Provider-specific catalogue, then generic catalogue
        catalogue_entry = get_model_pricing_for_provider(canonical, prov)
        if catalogue_entry:
            input_cost = Decimal(str(round((input_tokens / 1_000_000) * catalogue_entry.input_per_1m, 6)))
            output_cost = Decimal(str(round((output_tokens / 1_000_000) * catalogue_entry.output_per_1m, 6)))
            pricing_source = "catalogue"
            pricing_snapshot = {
                "input_cost_per_1k": catalogue_entry.input_per_1m / 1000,
                "output_cost_per_1k": catalogue_entry.output_per_1m / 1000,
                "provider": catalogue_entry.provider,
                "currency": "USD",
                "effective_from": None,
                "source": "catalogue",
            }
            pricing_version = f"catalogue:{catalogue_entry.provider}"
        else:
            input_cost = Decimal("0")
            output_cost = Decimal("0")
            pricing_source = "unknown"
            pricing_snapshot = {}
            pricing_version = "catalogue"
            _log.warning(
                "No pricing entry for model %r — cost recorded as $0 "
                "(add alias in ai_model_pricing.py or a row to model_pricing table)",
                model,
            )

    total_cost = (input_cost + output_cost).quantize(Decimal("0.000001"))
    return input_cost, output_cost, total_cost, pricing_source, pricing_snapshot, pricing_version
