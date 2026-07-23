"""Rule 5 — cache_opportunity: flags repeated (identical, sanitized) prompts
sent to the same model — a prompt/response caching opportunity.

Hashes AiRequest.sanitized_prompt_text (the masked field — never prompt_text)
in SQL and groups by the hash, so raw prompt bodies never leave the database
and never enter evidence_json — only a hash prefix does. Drill-down goes
through sample_request_ids and the existing request-detail endpoint.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_tip_duplicate_min_hits, get_tip_min_monthly_savings
from app.services.optimization.registry import TipRule, tip_registry

# window_end_exclusive is window_end + 1 day: ar.created_at is a timestamp,
# window_end is a bare inclusive date, and "<= window_end" would cast to
# midnight and silently drop every row from window_end's own day.
_QUERY = text(
    """
    SELECT
        encode(sha256(convert_to(ar.sanitized_prompt_text, 'UTF8')), 'hex') AS prompt_hash,
        ar.model_name AS model_name,
        count(*) AS hit_count,
        array_agg(ar.request_id ORDER BY ar.created_at DESC) AS request_ids,
        avg(rc.input_token_cost) AS avg_input_cost
    FROM ai_requests ar
    JOIN request_cost rc ON rc.request_id = ar.request_id
    WHERE ar.org_id = :org_id
      AND ar.project_id IS NOT DISTINCT FROM :project_id
      AND ar.created_at >= :window_start
      AND ar.created_at < :window_end_exclusive
      AND ar.sanitized_prompt_text IS NOT NULL
      AND ar.sanitized_prompt_text <> ''
    GROUP BY 1, 2
    HAVING count(*) >= :min_hits
    """
)


@tip_registry.register
class CacheOpportunityRule(TipRule):
    tip_type = "cache_opportunity"

    def evaluate(
        self, *, db: Session, org_id: str, project_id: Optional[str],
        window_start: date, window_end: date,
    ) -> list[dict]:
        min_hits = get_tip_duplicate_min_hits()
        min_savings = get_tip_min_monthly_savings()
        window_days = max((window_end - window_start).days, 1)

        rows = db.execute(
            _QUERY,
            {
                "org_id": org_id,
                "project_id": project_id,
                "window_start": window_start,
                "window_end_exclusive": window_end + timedelta(days=1),
                "min_hits": min_hits,
            },
        ).mappings().all()

        tips = []
        for row in rows:
            hit_count = int(row["hit_count"])
            avg_cost = Decimal(str(row["avg_input_cost"] or 0))
            repeats_saved = hit_count - 1
            total_savings = Decimal(repeats_saved) * avg_cost
            monthly_savings = (total_savings / Decimal(window_days)) * Decimal(30)
            if monthly_savings < min_savings:
                continue

            model_name = row["model_name"]
            sample_ids = list(row["request_ids"])[:5]
            hash_prefix = row["prompt_hash"][:16]

            tips.append({
                "org_id": org_id,
                "project_id": project_id,
                "model_name": model_name,
                "tip_type": self.tip_type,
                "severity": "medium",
                "title": f"A prompt repeated {hit_count} times against {model_name}",
                "message": (
                    f"The same sanitized prompt was sent to {model_name} {hit_count} times "
                    f"over the last {window_days} days. Prompt or response caching could save "
                    f"an estimated ${monthly_savings:.2f}/month."
                ),
                "estimated_monthly_savings": monthly_savings.quantize(Decimal("0.000001")),
                "confidence": "high",
                "evidence_json": {
                    "rule": self.tip_type,
                    "observed": {"metric": "duplicate_hits", "value": hit_count, "unit": "count"},
                    "baseline": {"metric": "duplicate_hits", "value": min_hits, "unit": "count"},
                    "sample_request_ids": sample_ids,
                    "sample_size": hit_count,
                    "window_days": window_days,
                    "params": {"model": model_name, "prompt_hash_prefix": hash_prefix},
                },
                "period_start": window_start,
                "period_end": window_end,
            })
        return tips
