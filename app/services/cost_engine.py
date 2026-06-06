from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import (
    get_cost_default_per_second_rate,
    get_cost_default_rate_per_1k,
    get_cost_infra_rate_per_ms,
    get_cost_infra_rate_per_mb,
)
from app.models import ModelPricing, ToolRegistry
from app.schemas import CostSummary, TelemetryEventCreate
from app.services.ai_model_pricing import MODEL_PRICING


class CostEngine:
    def calculate(self, event_data: TelemetryEventCreate, db: Session) -> CostSummary:
        external_cost = Decimal("0")

        # Infra cost priority:
        # 1. Caller supplied a pre-computed value (e.g. actual Azure cost per request)
        # 2. Decorator supplied input/output data sizes → data-transfer cost + compute time cost
        # 3. Latency-only fallback using COST_INFRA_RATE_PER_MS env rate
        if event_data.infra_cost and event_data.infra_cost > 0:
            infra_cost = Decimal(str(event_data.infra_cost))
        else:
            latency_ms = Decimal(str(max(int(event_data.latency_ms or 0), 0)))
            input_mb = Decimal(str(event_data.input_data_size_mb or 0))
            output_mb = Decimal(str(event_data.output_data_size_mb or 0))
            rate_per_ms = get_cost_infra_rate_per_ms()
            # compute cost from actual latency (time the infra was busy)
            compute_cost = latency_ms * rate_per_ms
            # data transfer cost: $0.00001 per MB in/out (overridable via env)
            rate_per_mb = get_cost_infra_rate_per_mb()
            transfer_cost = (input_mb + output_mb) * rate_per_mb
            infra_cost = (compute_cost + transfer_cost).quantize(Decimal("0.000001"))

        input_tokens = event_data.prompt_tokens or 0
        output_tokens = event_data.completion_tokens or 0
        total_tokens = input_tokens + output_tokens

        input_token_cost = Decimal("0")
        output_token_cost = Decimal("0")

        # Unified traces supply pre-computed LLM cost — skip all price lookups
        if event_data.precomputed_llm_cost is not None:
            llm_cost = Decimal(str(event_data.precomputed_llm_cost))
        else:
            llm_cost = Decimal("0")

        if event_data.precomputed_llm_cost is None and total_tokens > 0:
            # 1. Primary: ai_model_pricing catalogue (always up-to-date)
            catalogue_pricing = MODEL_PRICING.get(event_data.model_name or "")
            if catalogue_pricing:
                input_token_cost = (
                    Decimal(str(input_tokens)) / Decimal("1000000")
                    * Decimal(str(catalogue_pricing.input_per_1m))
                ).quantize(Decimal("0.000001"))
                output_token_cost = (
                    Decimal(str(output_tokens)) / Decimal("1000000")
                    * Decimal(str(catalogue_pricing.output_per_1m))
                ).quantize(Decimal("0.000001"))
                llm_cost = input_token_cost + output_token_cost
            else:
                # 2. Fallback: DB model_pricing table (provider-specific overrides)
                db_pricing = None
                if event_data.provider and event_data.model_name:
                    db_pricing = (
                        db.query(ModelPricing)
                        .filter(
                            ModelPricing.provider == event_data.provider,
                            ModelPricing.model_name == event_data.model_name,
                        )
                        .first()
                    )
                if db_pricing:
                    input_token_cost = (
                        Decimal(str(input_tokens)) / Decimal("1000")
                        * Decimal(str(db_pricing.input_cost_per_1k))
                    ).quantize(Decimal("0.000001"))
                    output_token_cost = (
                        Decimal(str(output_tokens)) / Decimal("1000")
                        * Decimal(str(db_pricing.output_cost_per_1k))
                    ).quantize(Decimal("0.000001"))
                    llm_cost = input_token_cost + output_token_cost
                else:
                    # 3. Last resort: tool registry cost model (non-LLM tools)
                    tool = (
                        db.query(ToolRegistry)
                        .filter(
                            (ToolRegistry.tool_name == (event_data.model_name or event_data.tool_name))
                            | (ToolRegistry.tool_name == event_data.component_name)
                        )
                        .first()
                    )
                    cost_model = (tool.cost_model if tool and tool.cost_model else None) or "per_token"
                    base_cost = Decimal(str(tool.base_cost)) if tool and tool.base_cost is not None else Decimal("0")
                    latency_s = (Decimal(str(max(int(event_data.latency_ms or 0), 0))) / Decimal("1000")).quantize(Decimal("0.000001"))

                    if cost_model == "per_token":
                        rate_per_1k = base_cost if base_cost > 0 else get_cost_default_rate_per_1k()
                        llm_cost = (Decimal(str(total_tokens)) / Decimal("1000")) * rate_per_1k
                    elif cost_model == "per_request":
                        llm_cost = base_cost
                    elif cost_model == "per_second":
                        rate_per_s = base_cost if base_cost > 0 else get_cost_default_per_second_rate()
                        llm_cost = latency_s * rate_per_s
                    elif cost_model == "fixed":
                        llm_cost = base_cost
                    elif cost_model == "custom":
                        meta = event_data.metadata_json or {}
                        multiplier = Decimal(str(meta.get("custom_multiplier", 1) or 1))
                        per_mb_in = Decimal(str(meta.get("per_mb_in", 0) or 0))
                        per_mb_out = Decimal(str(meta.get("per_mb_out", 0) or 0))
                        mb_in = Decimal(str(event_data.input_data_size_mb or 0))
                        mb_out = Decimal(str(event_data.output_data_size_mb or 0))
                        llm_cost = (base_cost * multiplier) + (per_mb_in * mb_in) + (per_mb_out * mb_out)
                    else:
                        llm_cost = (Decimal(str(total_tokens)) / Decimal("1000")) * get_cost_default_rate_per_1k()

        for ext in event_data.external_tools:
            external_cost += Decimal(str(ext.cost))

        total_cost = llm_cost + infra_cost + external_cost
        return CostSummary(
            llm_cost=llm_cost.quantize(Decimal("0.000001")),
            infra_cost=infra_cost.quantize(Decimal("0.000001")),
            external_cost=external_cost.quantize(Decimal("0.000001")),
            total_cost=total_cost.quantize(Decimal("0.000001")),
            input_token_cost=input_token_cost.quantize(Decimal("0.000001")),
            output_token_cost=output_token_cost.quantize(Decimal("0.000001")),
        )
