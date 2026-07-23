"""Rule 2 — model_substitution: flags a heavily-used model that has a cheaper,
already-deployed, same-category substitute with enough context window.

Candidate set = get_deployments_for_org() (only models the org can actually
reach), minus anything block-listed via GovernanceRule. Recosting uses
app.services.cost_lookup.calculate_cost — the same lookup order proxy.py
bills with — so this tip's numbers never disagree with the invoice.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_tip_min_monthly_savings
from app.models import AiRequest, RequestCost, TokenUsage
from app.services.ai_model_pricing import get_model_pricing
from app.services.cost_lookup import calculate_cost
from app.services.deployment_service import get_deployments_for_org
from app.services.governance_rule_service import _load_rules
from app.services.optimization.registry import TipRule, tip_registry
from app.services.optimization.utils import before_window_end, project_eq


@tip_registry.register
class ModelSubstitutionRule(TipRule):
    tip_type = "model_substitution"

    def evaluate(
        self, *, db: Session, org_id: str, project_id: Optional[str],
        window_start: date, window_end: date,
    ) -> list[dict]:
        min_savings = get_tip_min_monthly_savings()
        window_days = max((window_end - window_start).days, 1)

        rows = (
            db.query(
                RequestCost.model_name,
                func.sum(RequestCost.total_cost).label("total_cost"),
                func.sum(RequestCost.input_tokens).label("input_tokens"),
                func.sum(RequestCost.output_tokens).label("output_tokens"),
            )
            .filter(
                RequestCost.org_id == org_id,
                project_eq(RequestCost.project_id, project_id),
                RequestCost.created_at >= window_start,
                before_window_end(RequestCost.created_at, window_end),
            )
            .group_by(RequestCost.model_name)
            .all()
        )
        if not rows:
            return []

        blocked_models = {
            r.scope_reference for r in _load_rules(db=db, org_id=org_id, project_id=project_id)
            if r.metric_name == "blocked_model"
        }

        deployments = get_deployments_for_org(db, org_id=org_id, project_id=project_id, requested_model=None)
        candidate_models = {
            (d.model_name, d.provider) for d in deployments
            if d.model_name and d.model_name not in blocked_models
        }

        tips = []
        for row in rows:
            model_name = row.model_name
            if not model_name or model_name in blocked_models:
                continue
            total_cost = Decimal(str(row.total_cost or 0))
            input_tokens = int(row.input_tokens or 0)
            output_tokens = int(row.output_tokens or 0)
            if total_cost <= 0 or (input_tokens + output_tokens) <= 0:
                continue

            current_pricing = get_model_pricing(model_name)
            if not current_pricing:
                continue

            peak_tokens = (
                db.query(func.max(TokenUsage.total_tokens))
                .join(AiRequest, AiRequest.request_id == TokenUsage.request_id)
                .filter(
                    AiRequest.org_id == org_id,
                    project_eq(AiRequest.project_id, project_id),
                    AiRequest.model_name == model_name,
                    TokenUsage.created_at >= window_start,
                    before_window_end(TokenUsage.created_at, window_end),
                )
                .scalar()
            ) or 0

            best_candidate: Optional[tuple[str, str]] = None
            best_cost: Optional[Decimal] = None
            for cand_model, cand_provider in candidate_models:
                if cand_model == model_name:
                    continue
                cand_pricing = get_model_pricing(cand_model)
                if not cand_pricing or cand_pricing.category != current_pricing.category:
                    continue
                if cand_pricing.context_window < peak_tokens:
                    continue

                _, _, cand_total_cost, *_ = calculate_cost(
                    db=db, model=cand_model, provider=cand_provider or "",
                    input_tokens=input_tokens, output_tokens=output_tokens,
                )
                if best_cost is None or cand_total_cost < best_cost:
                    best_cost = cand_total_cost
                    best_candidate = (cand_model, cand_provider or "")

            if not best_candidate or best_cost is None:
                continue

            savings = total_cost - best_cost
            if savings <= 0:
                continue
            monthly_savings = (savings / Decimal(window_days)) * Decimal(30)
            if monthly_savings < min_savings:
                continue

            sample_ids = [
                r.request_id for r in (
                    db.query(AiRequest.request_id)
                    .filter(
                        AiRequest.org_id == org_id,
                        project_eq(AiRequest.project_id, project_id),
                        AiRequest.model_name == model_name,
                        AiRequest.created_at >= window_start,
                        before_window_end(AiRequest.created_at, window_end),
                    )
                    .order_by(AiRequest.created_at.desc())
                    .limit(5)
                    .all()
                )
            ]

            tips.append({
                "org_id": org_id,
                "project_id": project_id,
                "model_name": model_name,
                "tip_type": self.tip_type,
                "severity": "medium",
                "title": f"Switch from {model_name} to {best_candidate[0]} for similar quality at lower cost",
                "message": (
                    f"Over the last {window_days} days, {model_name} cost ${total_cost:.2f} for "
                    f"{input_tokens + output_tokens:,} tokens. {best_candidate[0]} is already "
                    f"deployed for this org, is in the same pricing category, and would have cost "
                    f"${best_cost:.2f} for the same volume — an estimated "
                    f"${monthly_savings:.2f}/month saving."
                ),
                "estimated_monthly_savings": monthly_savings.quantize(Decimal("0.000001")),
                "confidence": "medium",
                "evidence_json": {
                    "rule": self.tip_type,
                    "observed": {"metric": "blended_cost", "value": float(total_cost), "unit": "USD"},
                    "baseline": {"metric": "blended_cost", "value": float(best_cost), "unit": "USD"},
                    "sample_request_ids": sample_ids,
                    "sample_size": input_tokens + output_tokens,
                    "window_days": window_days,
                    "params": {"from_model": model_name, "to_model": best_candidate[0]},
                },
                "period_start": window_start,
                "period_end": window_end,
            })
        return tips
