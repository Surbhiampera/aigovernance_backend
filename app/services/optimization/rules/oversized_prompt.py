"""Rule 3 — oversized_prompt: flags a project whose p95 prompt size dwarfs its
median, then attributes the cause using system_tokens / tool_definition_tokens
(populated by proxy.py's Step 1 enrichment) so the tip coaches the right fix.
No re-tokenization — the counts are already stored in TokenUsage.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_tip_min_requests, get_tip_prompt_outlier_ratio
from app.models import AiRequest, TokenUsage
from app.services.optimization.registry import TipRule, tip_registry
from app.services.optimization.utils import before_window_end, project_eq


@tip_registry.register
class OversizedPromptRule(TipRule):
    tip_type = "oversized_prompt"

    def evaluate(
        self, *, db: Session, org_id: str, project_id: Optional[str],
        window_start: date, window_end: date,
    ) -> list[dict]:
        min_requests = get_tip_min_requests()
        outlier_ratio = get_tip_prompt_outlier_ratio()
        window_days = max((window_end - window_start).days, 1)

        base_filters = [
            AiRequest.org_id == org_id,
            project_eq(AiRequest.project_id, project_id),
            TokenUsage.created_at >= window_start,
            before_window_end(TokenUsage.created_at, window_end),
            TokenUsage.input_tokens > 0,
        ]

        count = (
            db.query(func.count(TokenUsage.id))
            .join(AiRequest, AiRequest.request_id == TokenUsage.request_id)
            .filter(*base_filters)
            .scalar()
        ) or 0
        if count < min_requests:
            return []

        p95, median = (
            db.query(
                func.percentile_cont(0.95).within_group(TokenUsage.input_tokens),
                func.percentile_cont(0.5).within_group(TokenUsage.input_tokens),
            )
            .join(AiRequest, AiRequest.request_id == TokenUsage.request_id)
            .filter(*base_filters)
            .one()
        )
        p95 = float(p95 or 0)
        median = float(median or 0)
        if median <= 0 or p95 <= float(outlier_ratio) * median:
            return []

        outlier_threshold = p95 * 0.9
        avg_system, avg_tool, avg_input = (
            db.query(
                func.avg(TokenUsage.system_tokens),
                func.avg(TokenUsage.tool_definition_tokens),
                func.avg(TokenUsage.input_tokens),
            )
            .join(AiRequest, AiRequest.request_id == TokenUsage.request_id)
            .filter(*base_filters, TokenUsage.input_tokens >= outlier_threshold)
            .one()
        )
        avg_system = float(avg_system or 0)
        avg_tool = float(avg_tool or 0)
        avg_input = float(avg_input or p95) or p95

        if avg_input > 0 and avg_tool / avg_input >= 0.3:
            cause = "tool_definitions"
            message = (
                f"The largest prompts in this project are dominated by tool definitions "
                f"(~{avg_tool:.0f} of {avg_input:.0f} tokens). Trim unused tool definitions "
                f"from the request to shrink prompt size."
            )
        elif avg_input > 0 and avg_system / avg_input >= 0.3:
            cause = "system_prompt"
            message = (
                f"The largest prompts in this project are dominated by the system prompt "
                f"(~{avg_system:.0f} of {avg_input:.0f} tokens), resent every turn. Shorten "
                f"or cache the system prompt."
            )
        else:
            cause = "conversation_history"
            message = (
                f"The p95 prompt size ({p95:.0f} tokens) is {p95 / median:.1f}x the project "
                f"median ({median:.0f} tokens) with no single dominant cause — conversation "
                f"history is likely growing unbounded. Truncate or summarize older turns."
            )

        sample_ids = [
            r.request_id for r in (
                db.query(AiRequest.request_id)
                .join(TokenUsage, TokenUsage.request_id == AiRequest.request_id)
                .filter(*base_filters)
                .order_by(TokenUsage.input_tokens.desc())
                .limit(5)
                .all()
            )
        ]

        return [{
            "org_id": org_id,
            "project_id": project_id,
            "model_name": None,
            "tip_type": self.tip_type,
            "severity": "medium",
            "title": "Prompt sizes are heavily skewed by outliers",
            "message": message,
            "estimated_monthly_savings": Decimal("0"),
            "confidence": "medium",
            "evidence_json": {
                "rule": self.tip_type,
                "observed": {"metric": "p95_input_tokens", "value": p95, "unit": "tokens"},
                "baseline": {"metric": "median_input_tokens", "value": median, "unit": "tokens"},
                "sample_request_ids": sample_ids,
                "sample_size": count,
                "window_days": window_days,
                "params": {
                    "cause": cause,
                    "avg_system_tokens": avg_system,
                    "avg_tool_definition_tokens": avg_tool,
                },
            },
            "period_start": window_start,
            "period_end": window_end,
        }]
