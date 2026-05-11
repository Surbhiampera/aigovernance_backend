from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import ModelPricing, ToolRegistry
from app.schemas import CostSummary, TelemetryEventCreate


class CostEngine:
    def calculate(self, event_data: TelemetryEventCreate, db: Session) -> CostSummary:
        external_cost = Decimal("0")
        infra_cost = Decimal(str(event_data.infra_cost or 0))

        total_tokens = event_data.prompt_tokens + event_data.completion_tokens

        # Unified traces supply pre-computed LLM cost — skip DB price lookup
        if event_data.precomputed_llm_cost is not None:
            llm_cost = Decimal(str(event_data.precomputed_llm_cost))
        elif total_tokens > 0:
            llm_cost = Decimal("0")
        else:
            llm_cost = Decimal("0")

        # Try model-specific pricing first (only when cost not pre-computed)
        if event_data.precomputed_llm_cost is None and total_tokens > 0:
            pricing = None
            if event_data.provider and event_data.model_name:
                pricing = (
                    db.query(ModelPricing)
                    .filter(
                        ModelPricing.provider == event_data.provider,
                        ModelPricing.model_name == event_data.model_name,
                    )
                    .first()
                )
            if pricing:
                llm_cost = (
                    Decimal(str(event_data.prompt_tokens)) / Decimal("1000") * Decimal(str(pricing.input_cost_per_1k))
                    + Decimal(str(event_data.completion_tokens)) / Decimal("1000") * Decimal(str(pricing.output_cost_per_1k))
                )
            else:
                # Fall back to tool registry cost model
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
                    rate_per_1k = base_cost if base_cost > 0 else Decimal("0.0025")
                    if total_tokens > 0:
                        llm_cost = (Decimal(str(total_tokens)) / Decimal("1000")) * rate_per_1k
                elif cost_model == "per_request":
                    llm_cost = base_cost
                elif cost_model == "per_second":
                    rate_per_s = base_cost if base_cost > 0 else Decimal("0.0001")
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
                    if total_tokens > 0:
                        llm_cost = (Decimal(str(total_tokens)) / Decimal("1000")) * Decimal("0.0025")

        if infra_cost == 0 and event_data.latency_ms > 0:
            infra_cost = Decimal(str(event_data.latency_ms)) * Decimal("0.00008")

        for ext in event_data.external_tools:
            external_cost += Decimal(str(ext.cost))

        total_cost = llm_cost + infra_cost + external_cost
        return CostSummary(
            llm_cost=llm_cost.quantize(Decimal("0.000001")),
            infra_cost=infra_cost.quantize(Decimal("0.000001")),
            external_cost=external_cost.quantize(Decimal("0.000001")),
            total_cost=total_cost.quantize(Decimal("0.000001")),
        )
