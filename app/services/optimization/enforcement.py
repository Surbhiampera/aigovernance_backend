"""Live proxy enforcement for applied optimization tips.

Distinct from governance_rule_service.py: that module only *blocks* (403)
requests that violate a threshold. This module *rewrites* the outgoing
request — silently redirecting a model or clamping/raising max_tokens —
because that's what "Apply" on an optimization tip actually promises.
Reuses the governance_rules table (see app/routers/optimization_tips.py for
the metric_name values this writes: model_redirect,
optimization_max_tokens_cap, optimization_max_tokens_floor).

Both entry points are called from the proxy's pre-flight path and must
never raise — a bug here should degrade to "no override applied", not break
a live request. Callers wrap these in try/except regardless, but the
queries below are read-only and defensive on their own.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)


def _active_rule(*, db: Session, org_id: str, project_id: Optional[str], metric_name: str, scope_reference: str):
    """Most specific active rule for this org/project/model: a project-scoped
    row wins over an org-wide one (project_id IS NULL), matching the
    precedence already used by governance_rule_service._enforce_allow_list."""
    from app.models import GovernanceRule

    rows = (
        db.query(GovernanceRule)
        .filter(
            GovernanceRule.is_active.is_(True),
            GovernanceRule.org_id == org_id,
            GovernanceRule.metric_name == metric_name,
            GovernanceRule.scope_reference == scope_reference,
        )
        .all()
    )
    if not rows:
        return None
    if project_id:
        project_row = next((r for r in rows if r.project_id == project_id), None)
        if project_row:
            return project_row
    return next((r for r in rows if r.project_id is None), None)


def resolve_model_redirect(*, db: Session, org_id: str, project_id: Optional[str], model: str) -> str:
    """Return the redirect target for `model` if an active model_redirect
    rule exists for this org/project, else `model` unchanged."""
    try:
        rule = _active_rule(
            db=db, org_id=org_id, project_id=project_id,
            metric_name="model_redirect", scope_reference=model,
        )
        if rule and rule.redirect_target_model:
            return rule.redirect_target_model
    except Exception:
        _log.warning("Model redirect lookup failed for model=%s org=%s", model, org_id, exc_info=True)
    return model


def apply_token_overrides(
    *, db: Session, org_id: str, project_id: Optional[str], model: str, forward_body: dict,
) -> None:
    """Mutate forward_body["max_tokens"] per active cap/floor rules for this
    org/project/model. Cap: lowers an over-limit value, or injects the cap if
    the client omitted max_tokens. Floor: raises only if the client's value
    is below it — never injects one the client didn't ask for."""
    try:
        current = forward_body.get("max_tokens")

        cap_rule = _active_rule(
            db=db, org_id=org_id, project_id=project_id,
            metric_name="optimization_max_tokens_cap", scope_reference=model,
        )
        if cap_rule is not None:
            cap = int(cap_rule.threshold_value)
            if current is None or current > cap:
                forward_body["max_tokens"] = cap
                current = cap

        floor_rule = _active_rule(
            db=db, org_id=org_id, project_id=project_id,
            metric_name="optimization_max_tokens_floor", scope_reference=model,
        )
        if floor_rule is not None and current is not None:
            floor = int(floor_rule.threshold_value)
            if current < floor:
                forward_body["max_tokens"] = floor
    except Exception:
        _log.warning("Token override lookup failed for model=%s org=%s", model, org_id, exc_info=True)
