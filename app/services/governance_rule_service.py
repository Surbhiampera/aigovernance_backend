"""Governance rule enforcement — called by the proxy before forwarding to Azure.

Enforces four rule types sourced from the governance_rules table:

  allowed_model      scope_reference = model name; if any active rules exist
                     for the org/project, the requested model MUST be in the set.
  blocked_model      scope_reference = model name; the requested model MUST NOT
                     match any active rule.
  max_input_tokens   threshold_value = token ceiling for tiktoken-estimated input.
  max_output_tokens  threshold_value = ceiling for body["max_tokens"] parameter.

Precedence (block list always wins):
  1. blocked_model  → 403 immediately; allow list not consulted.
  2. allowed_model  → if any rules exist and model is absent → 403.
  3. max_output_tokens → checked against body["max_tokens"].
  (max_input_tokens is checked separately after token counting.)

Additionally enforces: if a model has no known pricing entry in either the
static catalogue or the model_pricing DB table, the request is blocked (403).
This prevents untracked cost accumulation that would silently bypass budgets.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def check_governance_rules(
    *,
    db: Session,
    org_id: str,
    project_id: Optional[str],
    model: str,
    max_output_tokens_requested: Optional[int],
    request_id: str,
    source_ip: Optional[str],
) -> None:
    """Enforce model allow/block and output-token rules.

    Called after body parse, before PII scan and token counting.
    Raises HTTP 403 on any violation.
    """
    rules = _load_rules(db=db, org_id=org_id, project_id=project_id)

    _enforce_pricing_known(
        db=db, model=model, org_id=org_id, project_id=project_id,
        request_id=request_id, source_ip=source_ip,
    )

    _enforce_block_list(
        rules=rules, model=model, org_id=org_id, project_id=project_id,
        request_id=request_id, source_ip=source_ip, db=db,
    )

    _enforce_allow_list(
        rules=rules, model=model, org_id=org_id, project_id=project_id,
        request_id=request_id, source_ip=source_ip, db=db,
    )

    if max_output_tokens_requested is not None:
        _enforce_max_output_tokens(
            rules=rules,
            requested=max_output_tokens_requested,
            org_id=org_id, project_id=project_id,
            request_id=request_id, source_ip=source_ip, db=db,
        )


def check_max_input_tokens(
    *,
    db: Session,
    org_id: str,
    project_id: Optional[str],
    model: str,
    input_tokens: int,
    request_id: str,
    source_ip: Optional[str],
) -> None:
    """Enforce max_input_tokens rules after tiktoken counting.

    Called after input token count is known, before _store_request.
    Raises HTTP 403 on violation.
    """
    rules = _load_rules(db=db, org_id=org_id, project_id=project_id)
    _enforce_max_input_tokens(
        rules=rules,
        input_tokens=input_tokens,
        org_id=org_id, project_id=project_id,
        request_id=request_id, source_ip=source_ip, db=db,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_rules(*, db: Session, org_id: str, project_id: Optional[str]) -> list:
    from app.models import GovernanceRule

    return (
        db.query(GovernanceRule)
        .filter(
            GovernanceRule.is_active.is_(True),
            GovernanceRule.org_id == org_id,
        )
        .all()
    )


def _model_has_pricing(db: Session, model: str) -> bool:
    """Return True if the model appears in the static catalogue or model_pricing table."""
    from app.services.ai_model_pricing import MODEL_PRICING
    from app.models import ModelPricing

    if model in MODEL_PRICING:
        return True
    return db.query(ModelPricing).filter(ModelPricing.model_name == model).first() is not None


def _enforce_pricing_known(
    *, db: Session, model: str, org_id: str, project_id: Optional[str],
    request_id: str, source_ip: Optional[str],
) -> None:
    if _model_has_pricing(db, model):
        return

    _log.warning(
        "Blocking request: model %r has no pricing entry (org=%s). "
        "Add alias in ai_model_pricing.py or a row to model_pricing table.",
        model, org_id,
    )
    _write_violation_audit(
        db=db,
        action="unknown_model_pricing",
        org_id=org_id, project_id=project_id,
        request_id=request_id, source_ip=source_ip,
        rule_id=None,
        summary=f"Model '{model}' has no pricing entry — request blocked.",
        metadata={"model": model},
    )
    db.flush()
    raise HTTPException(
        status_code=403,
        detail={
            "error": "governance_violation",
            "rule": "unknown_model_pricing",
            "model": model,
            "request_id": request_id,
            "detail": (
                f"Model '{model}' has no pricing entry. "
                "Add it to ai_model_pricing.py or the model_pricing table before use."
            ),
        },
    )


def _enforce_block_list(
    *, rules: list, model: str, org_id: str, project_id: Optional[str],
    request_id: str, source_ip: Optional[str], db: Session,
) -> None:
    for rule in rules:
        if rule.metric_name == "blocked_model" and rule.scope_reference == model:
            _write_violation_audit(
                db=db,
                action="model_blocked",
                org_id=org_id, project_id=project_id,
                request_id=request_id, source_ip=source_ip,
                rule_id=rule.id,
                summary=f"Governance rule '{rule.rule_name}' blocked model '{model}'.",
                metadata={"rule_id": rule.id, "rule_name": rule.rule_name, "model": model},
            )
            _create_governance_alert(db=db, rule=rule, org_id=org_id, project_id=project_id)
            db.flush()
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "governance_violation",
                    "rule": "model_blocked",
                    "model": model,
                    "rule_id": rule.id,
                    "request_id": request_id,
                    "detail": f"Model '{model}' is explicitly blocked by governance rule '{rule.rule_name}'.",
                },
            )


def _enforce_allow_list(
    *, rules: list, model: str, org_id: str, project_id: Optional[str],
    request_id: str, source_ip: Optional[str], db: Session,
) -> None:
    allow_rules = [r for r in rules if r.metric_name == "allowed_model"]
    if not allow_rules:
        return  # no allow-list configured — all models permitted

    allowed = {r.scope_reference for r in allow_rules}
    if model in allowed:
        return

    _write_violation_audit(
        db=db,
        action="model_not_allowed",
        org_id=org_id, project_id=project_id,
        request_id=request_id, source_ip=source_ip,
        rule_id=None,
        summary=f"Model '{model}' not in allowed model list for org '{org_id}'.",
        metadata={"model": model, "allowed_models": sorted(allowed)},
    )
    db.flush()
    raise HTTPException(
        status_code=403,
        detail={
            "error": "governance_violation",
            "rule": "model_not_allowed",
            "model": model,
            "allowed_models": sorted(allowed),
            "request_id": request_id,
            "detail": (
                f"Model '{model}' is not in the allowed model list for this organisation."
            ),
        },
    )


def _enforce_max_output_tokens(
    *, rules: list, requested: int, org_id: str, project_id: Optional[str],
    request_id: str, source_ip: Optional[str], db: Session,
) -> None:
    for rule in rules:
        if rule.metric_name != "max_output_tokens":
            continue
        limit = int(rule.threshold_value)
        if requested > limit:
            _write_violation_audit(
                db=db,
                action="max_output_tokens_exceeded",
                org_id=org_id, project_id=project_id,
                request_id=request_id, source_ip=source_ip,
                rule_id=rule.id,
                summary=(
                    f"Requested max_tokens ({requested}) exceeds governance "
                    f"limit ({limit}) set by rule '{rule.rule_name}'."
                ),
                metadata={
                    "rule_id": rule.id, "rule_name": rule.rule_name,
                    "limit": limit, "actual": requested,
                },
            )
            _create_governance_alert(db=db, rule=rule, org_id=org_id, project_id=project_id)
            db.flush()
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "governance_violation",
                    "rule": "max_output_tokens_exceeded",
                    "rule_id": rule.id,
                    "limit": limit,
                    "requested": requested,
                    "request_id": request_id,
                    "detail": (
                        f"Requested max_tokens ({requested}) exceeds governance "
                        f"limit of {limit} tokens."
                    ),
                },
            )


def _enforce_max_input_tokens(
    *, rules: list, input_tokens: int, org_id: str, project_id: Optional[str],
    request_id: str, source_ip: Optional[str], db: Session,
) -> None:
    for rule in rules:
        if rule.metric_name != "max_input_tokens":
            continue
        limit = int(rule.threshold_value)
        if input_tokens > limit:
            _write_violation_audit(
                db=db,
                action="max_input_tokens_exceeded",
                org_id=org_id, project_id=project_id,
                request_id=request_id, source_ip=source_ip,
                rule_id=rule.id,
                summary=(
                    f"Input token count ({input_tokens}) exceeds governance "
                    f"limit ({limit}) set by rule '{rule.rule_name}'."
                ),
                metadata={
                    "rule_id": rule.id, "rule_name": rule.rule_name,
                    "limit": limit, "actual": input_tokens,
                },
            )
            _create_governance_alert(db=db, rule=rule, org_id=org_id, project_id=project_id)
            db.flush()
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "governance_violation",
                    "rule": "max_input_tokens_exceeded",
                    "rule_id": rule.id,
                    "limit": limit,
                    "actual": input_tokens,
                    "request_id": request_id,
                    "detail": (
                        f"Input token count ({input_tokens}) exceeds governance "
                        f"limit of {limit} tokens."
                    ),
                },
            )


def _write_violation_audit(
    *, db: Session, action: str, org_id: str, project_id: Optional[str],
    request_id: str, source_ip: Optional[str], rule_id: Optional[int],
    summary: str, metadata: dict,
) -> None:
    from app.services.audit_service import log_event

    log_event(
        db,
        org_id=org_id,
        project_id=project_id,
        audit_category="governance",
        audit_action=action,
        audit_status="failure",
        actor_type="governance_engine",
        actor_ip=source_ip,
        entity_type="governance_rule",
        entity_id=str(rule_id) if rule_id else None,
        request_id=request_id,
        policy_triggered=True,
        compliance_relevant=True,
        requires_review=True,
        change_summary=summary,
        metadata=metadata,
        flush=False,
    )


def _create_governance_alert(*, db: Session, rule, org_id: str, project_id: Optional[str]) -> None:
    from app.models import Alert

    existing = (
        db.query(Alert)
        .filter(
            Alert.org_id == org_id,
            Alert.rule_id == rule.id,
            Alert.alert_type == "governance_violation",
            Alert.status == "active",
        )
        .first()
    )
    if existing:
        return

    alert = Alert(
        org_id=org_id,
        project_id=project_id,
        rule_id=rule.id,
        alert_type="governance_violation",
        severity=rule.severity,
        message=f"Governance rule '{rule.rule_name}' triggered: {rule.metric_name}.",
        threshold_value=float(rule.threshold_value),
        status="active",
        tool_name=rule.scope_reference,
    )
    db.add(alert)
