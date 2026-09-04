"""Rule 1 — response_length: flags models whose completions run far longer
than their prompts. Aggregates DailyOrgSummary (tool_name holds the model
name — see app/workers/tasks.py::_rebuild_daily_summary).
"""
from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import (
    get_tip_min_monthly_savings,
    get_tip_min_requests,
    get_tip_output_input_ratio,
)
from app.models import AiRequest, DailyOrgSummary, TokenUsage
from app.services.ai_model_pricing import get_model_pricing
from app.services.optimization.registry import TipRule, tip_registry
from app.services.optimization.utils import before_window_end, project_eq


@tip_registry.register
class ResponseLengthRule(TipRule):
    tip_type = "response_length"

    def evaluate(
        self, *, db: Session, org_id: str, project_id: Optional[str],
        window_start: date, window_end: date,
    ) -> list[dict]:
        ratio_threshold = get_tip_output_input_ratio()
        min_requests = get_tip_min_requests()
        min_savings = get_tip_min_monthly_savings()
        window_days = max((window_end - window_start).days, 1)

        rows = (
            db.query(
                DailyOrgSummary.tool_name,
                func.sum(DailyOrgSummary.total_prompt_tokens).label("prompt_tokens"),
                func.sum(DailyOrgSummary.total_completion_tokens).label("completion_tokens"),
                func.sum(DailyOrgSummary.total_events).label("total_events"),
            )
            .filter(
                DailyOrgSummary.org_id == org_id,
                project_eq(DailyOrgSummary.project_id, project_id),
                DailyOrgSummary.date >= window_start,
                DailyOrgSummary.date <= window_end,
            )
            .group_by(DailyOrgSummary.tool_name)
            .all()
        )

        tips = []
        for row in rows:
            model_name = row.tool_name
            prompt_tokens = int(row.prompt_tokens or 0)
            completion_tokens = int(row.completion_tokens or 0)
            total_events = int(row.total_events or 0)
            if total_events < min_requests or prompt_tokens <= 0 or not model_name or model_name == "unknown":
                continue

            ratio = Decimal(completion_tokens) / Decimal(prompt_tokens)
            if ratio <= ratio_threshold:
                continue

            excess_tokens = completion_tokens - int(ratio_threshold * prompt_tokens)
            if excess_tokens <= 0:
                continue

            avg_prompt_tokens = prompt_tokens / total_events
            suggested_max_tokens = max(256, math.ceil(float(ratio_threshold) * avg_prompt_tokens))

            pricing = get_model_pricing(model_name)
            if not pricing:
                continue
            output_rate = Decimal(str(pricing.output_per_1m))
            excess_cost = (Decimal(excess_tokens) / Decimal(1_000_000)) * output_rate
            monthly_savings = (excess_cost / Decimal(window_days)) * Decimal(30)
            if monthly_savings < min_savings:
                continue

            sample_ids = [
                r.request_id for r in (
                    db.query(AiRequest.request_id)
                    .join(TokenUsage, TokenUsage.request_id == AiRequest.request_id)
                    .filter(
                        AiRequest.org_id == org_id,
                        project_eq(AiRequest.project_id, project_id),
                        AiRequest.model_name == model_name,
                        TokenUsage.created_at >= window_start,
                        before_window_end(TokenUsage.created_at, window_end),
                    )
                    .order_by(TokenUsage.output_tokens.desc())
                    .limit(5)
                    .all()
                )
            ]

            tips.append({
                "org_id": org_id,
                "project_id": project_id,
                "model_name": model_name,
                "tip_type": self.tip_type,
                "severity": "high" if ratio >= ratio_threshold * 2 else "medium",
                "title": f"Responses from {model_name} run {float(ratio):.1f}x longer than input",
                "message": (
                    f"Over the last {window_days} days, {model_name} produced "
                    f"{completion_tokens:,} completion tokens against {prompt_tokens:,} prompt "
                    f"tokens ({float(ratio):.1f}x, vs a {float(ratio_threshold):.1f}x baseline). "
                    f"Setting a max_tokens cap could save an estimated "
                    f"${monthly_savings:.2f}/month."
                ),
                "estimated_monthly_savings": monthly_savings.quantize(Decimal("0.000001")),
                "confidence": "medium",
                "evidence_json": {
                    "rule": self.tip_type,
                    "observed": {"metric": "output_input_ratio", "value": float(ratio), "unit": "ratio"},
                    "baseline": {"metric": "output_input_ratio", "value": float(ratio_threshold), "unit": "ratio"},
                    "sample_request_ids": sample_ids,
                    "sample_size": total_events,
                    "window_days": window_days,
                    "params": {
                        "model": model_name,
                        "excess_completion_tokens": excess_tokens,
                        "suggested_max_tokens": suggested_max_tokens,
                    },
                },
                "period_start": window_start,
                "period_end": window_end,
            })
        return tips
