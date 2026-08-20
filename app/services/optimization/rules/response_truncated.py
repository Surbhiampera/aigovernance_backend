"""Rule 4 — response_truncated: flags a model whose responses are frequently
cut off (finish_reason == "length"). Cheapest rule — the column is already
populated by every provider adapter; no new data needed.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.config import get_tip_min_requests, get_tip_truncation_rate
from app.models import AiResponse
from app.services.optimization.registry import TipRule, tip_registry
from app.services.optimization.utils import before_window_end, project_eq


@tip_registry.register
class ResponseTruncatedRule(TipRule):
    tip_type = "response_truncated"

    def evaluate(
        self, *, db: Session, org_id: str, project_id: Optional[str],
        window_start: date, window_end: date,
    ) -> list[dict]:
        min_requests = get_tip_min_requests()
        rate_threshold = get_tip_truncation_rate()
        window_days = max((window_end - window_start).days, 1)

        rows = (
            db.query(
                AiResponse.model_name,
                func.count(AiResponse.id).label("total"),
                func.sum(case((AiResponse.finish_reason == "length", 1), else_=0)).label("truncated"),
            )
            .filter(
                AiResponse.org_id == org_id,
                project_eq(AiResponse.project_id, project_id),
                AiResponse.created_at >= window_start,
                before_window_end(AiResponse.created_at, window_end),
            )
            .group_by(AiResponse.model_name)
            .all()
        )

        tips = []
        for row in rows:
            model_name = row.model_name
            total = int(row.total or 0)
            truncated = int(row.truncated or 0)
            if total < min_requests or truncated == 0 or not model_name:
                continue
            rate = Decimal(truncated) / Decimal(total)
            if rate < rate_threshold:
                continue

            sample_ids = [
                r.request_id for r in (
                    db.query(AiResponse.request_id)
                    .filter(
                        AiResponse.org_id == org_id,
                        project_eq(AiResponse.project_id, project_id),
                        AiResponse.model_name == model_name,
                        AiResponse.finish_reason == "length",
                        AiResponse.created_at >= window_start,
                        before_window_end(AiResponse.created_at, window_end),
                    )
                    .order_by(AiResponse.created_at.desc())
                    .limit(5)
                    .all()
                )
            ]

            tips.append({
                "org_id": org_id,
                "project_id": project_id,
                "model_name": model_name,
                "tip_type": self.tip_type,
                "severity": "high" if rate >= rate_threshold * 2 else "medium",
                "title": f"{model_name} responses are truncated {float(rate) * 100:.0f}% of the time",
                "message": (
                    f"Over the last {window_days} days, {truncated} of {total} "
                    f"({float(rate) * 100:.0f}%) responses from {model_name} hit "
                    f"finish_reason='length'. Truncated responses are paid for and discarded — "
                    f"raise max_tokens or shorten the ask."
                ),
                "estimated_monthly_savings": Decimal("0"),
                "confidence": "high",
                "evidence_json": {
                    "rule": self.tip_type,
                    "observed": {"metric": "truncation_rate", "value": float(rate), "unit": "ratio"},
                    "baseline": {"metric": "truncation_rate", "value": float(rate_threshold), "unit": "ratio"},
                    "sample_request_ids": sample_ids,
                    "sample_size": total,
                    "window_days": window_days,
                    "params": {"model": model_name, "truncated_count": truncated},
                },
                "period_start": window_start,
                "period_end": window_end,
            })
        return tips
