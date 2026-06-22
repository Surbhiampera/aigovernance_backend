"""Governance Proxy Server — the single entry point for all AI requests.

Client apps configure only:
    GOVERNANCE_KEY=gov-xxxx
    BASE_URL=https://governance.company.com

Endpoints:
    POST /proxy          — non-streaming chat completion
    POST /proxy/stream   — streaming chat completion (SSE)

Model is specified by the client via ?model=<name> query param or "model" in the
request body. The proxy resolves the correct Azure deployment from the DB and
forwards the request. Clients never see provider credentials.

Request lifecycle:
  1  Authenticate X-Governance-Key → resolve org_id + project_id
  2  Rate limit check (pre-body, cheapest gate)
  3  Parse body, resolve model → look up Azure deployment
  4  Budget + governance rules enforcement
  5  PII scan — mask or block based on pii_policies
  6  Count input tokens, store AiRequest row
  7  Forward sanitised request to Azure OpenAI (admin credentials only)
  8  Capture output tokens and latency
  9  Calculate cost (input + output)
 10  Store AiResponse, TokenUsage, RequestCost, AuditLog — return to caller
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, AsyncIterator, Optional

_log = logging.getLogger(__name__)

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import Integer, cast, func
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import AiRequest, AiResponse, RequestCost, RouteExecution, TokenUsage
from app.services.audit_service import log_event
from app.services.budget_service import check_budget
from app.services.deployment_service import build_provider_request, get_deployment_for_org
from app.services.governance_key_service import verify_governance_key

from app.services.pii_engine import scan_and_mask
from app.services.rate_limit_service import check_rate_limit
from app.services.token_counter import count_tokens

router = APIRouter(prefix="/proxy", tags=["proxy"])


# ---------------------------------------------------------------------------
# ID generators
# ---------------------------------------------------------------------------

def _new_request_id() -> str:
    return f"req-{uuid.uuid4().hex[:20]}"


def _new_response_id() -> str:
    return f"resp-{uuid.uuid4().hex[:20]}"


def _new_execution_id() -> str:
    return f"exec-{uuid.uuid4().hex[:20]}"


def _estimate_input_tokens(messages: list[dict], model: str) -> int:
    """Tiktoken fallback — only called when Azure does not return prompt_tokens."""
    tokens = 2
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            tokens += 4 + count_tokens(text=content, model_name=model)
        else:
            tokens += 4
    return tokens


def _store_route_execution(
    *,
    db: Session,
    ctx: dict,
    upstream_ms: int,
    total_ms: int,
    status: str,
) -> None:
    routing_ms = ctx.get("routing_ms", 0)
    governance_ms = ctx.get("governance_ms", 0)
    proxy_ms = max(total_ms - routing_ms - governance_ms - upstream_ms, 0)
    db.add(RouteExecution(
        execution_id=_new_execution_id(),
        request_id=ctx["request_id"],
        org_id=ctx["org_id"],
        project_id=ctx.get("project_id"),
        execution_status=status,
        selected_model=ctx["model"],
        selected_provider=ctx.get("provider", ""),
        upstream_url=ctx.get("url", ""),
        total_latency_ms=total_ms,
        routing_latency_ms=routing_ms,
        proxy_latency_ms=proxy_ms,
        upstream_latency_ms=upstream_ms,
        governance_check_ms=governance_ms,
        started_at=datetime.utcfromtimestamp(ctx["t_request_start"]),
        completed_at=datetime.utcnow(),
    ))


# ---------------------------------------------------------------------------
# Step 1: Authenticate governance key
# ---------------------------------------------------------------------------

def _authenticate(
    *,
    db: Session,
    raw_key: str,
) -> dict:
    identity = verify_governance_key(db=db, raw_key=raw_key)
    if not identity:
        raise HTTPException(status_code=401, detail="Invalid or expired governance key.")
    return identity


# ---------------------------------------------------------------------------
# Step 4: PII scan
# ---------------------------------------------------------------------------

def _scan_messages(
    *,
    messages: list[dict],
    org_id: str,
    project_id: Optional[str],
    db: Session,
):
    """Return (sanitised_messages, combined_pii_result)."""
    from app.services.pii_engine import PiiScanResult, compute_pii_severity
    sanitised: list[dict] = []
    combined = PiiScanResult(sanitized_text="")
    for idx, msg in enumerate(messages):
        content = msg.get("content", "")
        if not isinstance(content, str):
            sanitised.append(msg)
            continue
        result = scan_and_mask(
            text=content,
            org_id=org_id,
            project_id=project_id,
            db=db,
        )
        if result.pii_detected:
            combined.pii_detected = True
            combined.pii_types = list(set(combined.pii_types + result.pii_types))
        if result.pii_masked:
            combined.pii_masked = True
        if result.should_block:
            combined.action_taken = "block"
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "pii_block",
                    "message": "Request blocked: sensitive PII detected.",
                    "pii_types": result.pii_types,
                },
            )
        if result.action_taken == "mask":
            combined.action_taken = "mask"
        for entity in result.entity_details:
            combined.entity_details.append({
                **entity,
                "message_index": idx,
                "role": msg.get("role", "user"),
            })
        combined.entities_detected += result.entities_detected
        combined.entities_masked += result.entities_masked
        sanitised.append({**msg, "content": result.sanitized_text})
    combined.severity = compute_pii_severity(combined.pii_types, combined.entities_detected)
    return sanitised, combined


# ---------------------------------------------------------------------------
# Step 5: Store AiRequest
# ---------------------------------------------------------------------------

def _store_request(
    *,
    db: Session,
    request_id: str,
    org_id: str,
    project_id: Optional[str],
    key_id: str,
    request_type: str,
    model: str,
    deployment: str,
    payload: dict,
    input_tokens: int,
    source_ip: Optional[str],
    user_agent: Optional[str],
    pii_detected: bool = False,
    pii_types: list = None,
    pii_masked: bool = False,
    pii_action_taken: Optional[str] = None,
    pii_severity: str = "",
    pii_entities_detected: int = 0,
    pii_entities_masked: int = 0,
    pii_detail: Optional[list] = None,
    entry_point: Optional[str] = None,
) -> AiRequest:
    row = AiRequest(
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        governance_key_id=key_id,
        request_type=request_type,
        model_name=model,
        deployment_name=deployment,
        request_payload=payload,
        input_token_estimate=input_tokens,
        source_ip=source_ip,
        user_agent=user_agent,
        request_status="pending",
        pii_detected=pii_detected,
        pii_types=pii_types or [],
        pii_masked=pii_masked,
        pii_action_taken=pii_action_taken,
        pii_severity=pii_severity or None,
        pii_entities_detected=pii_entities_detected,
        pii_entities_masked=pii_entities_masked,
        pii_detail=pii_detail,
        entry_point=entry_point,
        created_at=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    return row


# URL and headers are now built per-provider by deployment_service.build_provider_request.


# ---------------------------------------------------------------------------
# Steps 9-10: Cost calculation, store response, store audit
# ---------------------------------------------------------------------------

def _calculate_cost(
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


def _store_response_and_cost(
    *,
    db: Session,
    request_id: str,
    org_id: str,
    project_id: Optional[str],
    model: str,
    deployment: str,
    provider: str = "",
    response_payload: dict,
    input_tokens: int,
    output_tokens: int,
    finish_reason: Optional[str],
    latency_ms: int,
    status: str,
    input_token_source: str,
    output_token_source: str,
) -> None:
    input_cost, output_cost, total_cost, pricing_source, pricing_snapshot, pricing_version = _calculate_cost(
        db=db,
        model=model,
        provider=provider,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    db.add(AiResponse(
        response_id=_new_response_id(),
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        model_name=model,
        response_payload=response_payload,
        finish_reason=finish_reason,
        latency_ms=latency_ms,
        response_status=status,
        created_at=datetime.utcnow(),
    ))

    db.add(TokenUsage(
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        model_name=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        input_token_source=input_token_source,
        output_token_source=output_token_source,
        is_estimated=(input_token_source != "azure" or output_token_source != "azure"),
        created_at=datetime.utcnow(),
    ))

    db.add(RequestCost(
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        model_name=model,
        provider=provider or None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        input_token_cost=input_cost,
        output_token_cost=output_cost,
        llm_cost=input_cost + output_cost,
        total_cost=total_cost,
        currency="USD",
        cost_model_type=pricing_source,
        pricing_snapshot=pricing_snapshot,
        pricing_version=pricing_version,
        created_at=datetime.utcnow(),
    ))

    req_row = db.query(AiRequest).filter(AiRequest.request_id == request_id).first()
    if req_row:
        req_row.request_status = status
        req_row.completed_at = datetime.utcnow()
        req_row.input_token_estimate = input_tokens

    db.flush()


def _store_audit(
    *,
    db: Session,
    request_id: str,
    org_id: str,
    project_id: Optional[str],
    key_id: str,
    action: str,
    status: str,
    source_ip: Optional[str],
    user_agent: Optional[str],
    detail: Optional[str] = None,
) -> None:
    log_event(
        db=db,
        org_id=org_id,
        project_id=project_id,
        actor_type="governance_key",
        actor_id=key_id,
        actor_ip=source_ip,
        audit_category="proxy",
        audit_action=action,
        audit_status=status,
        entity_type="ai_request",
        entity_id=request_id,
        request_id=request_id,
        change_summary=detail,
        compliance_relevant=False,
    )


def _log_blocked_request(
    *,
    db: Session,
    request_id: str,
    org_id: str,
    project_id: Optional[str],
    key_id: str,
    source_ip: Optional[str],
    user_agent: Optional[str],
    failure_code: str,
    failure_reason: str,
    request_payload: Optional[dict] = None,
) -> None:
    """Create the AiRequest row for requests blocked before _store_request ran.

    Rate limit, budget, malformed-body, and PII-block rejections all raise
    HTTPException in _run_pre_flight before the normal AiRequest row is
    created, so without this they'd be invisible in ai_requests (only
    surfacing, if at all, in audit_logs).
    """
    db.add(AiRequest(
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        governance_key_id=key_id,
        request_type="chat_completion",
        request_payload=request_payload,
        source_ip=source_ip,
        user_agent=user_agent,
        request_status="blocked",
        failure_code=failure_code,
        failure_reason=failure_reason,
        completed_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    ))
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise


def _mark_request_failed(
    *,
    db: Session,
    request_id: str,
    org_id: str,
    project_id: Optional[str],
    key_id: str,
    source_ip: Optional[str],
    user_agent: Optional[str],
    detail: str,
    req_status: str = "failed",
    failure_code: str = "error",
) -> None:
    """Mark ai_requests.status and write a failure audit row.

    Use this only when NO tokens were consumed (e.g. 429 rate limit, auth
    failure, PII block). Does not create request_costs or token_usage.
    For errors where Azure consumed tokens, use _mark_request_failed_with_cost().
    """
    req_row = db.query(AiRequest).filter(AiRequest.request_id == request_id).first()
    if req_row:
        req_row.request_status = req_status
        req_row.failure_code = failure_code
        req_row.failure_reason = detail
        req_row.completed_at = datetime.utcnow()

    _store_audit(
        db=db,
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        key_id=key_id,
        action="proxy_request_failed",
        status="error",
        source_ip=source_ip,
        user_agent=user_agent,
        detail=detail,
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise


def _azure_usage_from_error(exc: httpx.HTTPStatusError) -> tuple[int, int]:
    """Read prompt_tokens / completion_tokens from an Azure error response body.

    Azure includes a usage field in some error responses (content filter, context
    length exceeded, etc.). Returns (input_tokens, output_tokens).
    Falls back to (0, 0) if the body is absent, unparseable, or has no usage key.
    Callers should use our tiktoken estimate when both values are 0.
    """
    try:
        body = exc.response.json()
        usage = body.get("usage") or {}
        return int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
    except Exception:
        return 0, 0


def _mark_request_failed_with_cost(
    *,
    db: Session,
    request_id: str,
    org_id: str,
    project_id: Optional[str],
    key_id: str,
    model: str,
    deployment: str,
    provider: str = "",
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    source_ip: Optional[str],
    user_agent: Optional[str],
    detail: str,
    req_status: str = "failed",
    failure_code: str = "upstream_error",
    input_token_source: str = "tiktoken_estimate",
    output_token_source: str = "tiktoken_estimate",
) -> None:
    """Mark failure AND record consumed tokens + cost.

    Use when Azure received the request and consumed tokens before failing:
    content filter (400), timeout, partial stream, mid-stream error.
    Records token_usage and request_cost so the spend is auditable even
    though no usable response was returned to the caller.
    """
    req_row = db.query(AiRequest).filter(AiRequest.request_id == request_id).first()
    if req_row:
        req_row.request_status = req_status
        req_row.failure_code = failure_code
        req_row.failure_reason = detail
        req_row.completed_at = datetime.utcnow()

    input_cost, output_cost, total_cost, pricing_source, pricing_snapshot, pricing_version = _calculate_cost(
        db=db,
        model=model,
        provider=provider,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    db.add(TokenUsage(
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        model_name=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        input_token_source=input_token_source,
        output_token_source=output_token_source,
        is_estimated=(input_token_source != "azure" or output_token_source != "azure"),
        created_at=datetime.utcnow(),
    ))

    db.add(RequestCost(
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        model_name=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        input_token_cost=input_cost,
        output_token_cost=output_cost,
        llm_cost=input_cost + output_cost,
        total_cost=total_cost,
        currency="USD",
        cost_model_type=pricing_source,
        pricing_snapshot=pricing_snapshot,
        pricing_version=pricing_version,
        created_at=datetime.utcnow(),
    ))

    _store_audit(
        db=db,
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        key_id=key_id,
        action="proxy_request_failed",
        status="error",
        source_ip=source_ip,
        user_agent=user_agent,
        detail=detail,
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise


# ---------------------------------------------------------------------------
# Streaming path
# ---------------------------------------------------------------------------

def _is_valid_json(s: str) -> bool:
    try:
        json.loads(s)
        return True
    except Exception:
        return False


async def _stream_azure(
    *,
    url: str,
    headers: dict,
    body: dict,
    request_id: str,
    org_id: str,
    project_id: Optional[str],
    key_id: str,
    model: str,
    deployment: str,
    provider: str = "",
    input_tokens: int,
    messages: list[dict],
    source_ip: Optional[str],
    user_agent: Optional[str],
    db: Session,
    t_start: float,
    ctx: Optional[dict] = None,
) -> AsyncIterator[bytes]:
    chunks_raw: list[str] = []
    finish_reason: Optional[str] = None

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=5.0)
        ) as client:
            async with client.stream("POST", url=url, headers=headers, json=body) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line == "data: [DONE]":
                        yield b"data: [DONE]\n\n"
                        break
                    if line.startswith("data: "):
                        json_str = line[6:]
                        chunks_raw.append(json_str)
                        try:
                            chunk = json.loads(json_str)
                            choice = (chunk.get("choices") or [{}])[0]
                            finish_reason = finish_reason or choice.get("finish_reason")
                        except Exception:
                            pass
                        yield (line + "\n\n").encode()
    except httpx.HTTPStatusError as exc:
        yield f"data: {json.dumps({'error': str(exc)})}\n\n".encode()
        partial_output = "".join(
            json.loads(c).get("choices", [{}])[0].get("delta", {}).get("content") or ""
            for c in chunks_raw if _is_valid_json(c)
        )
        partial_output_tokens = count_tokens(text=partial_output, model_name=model) if partial_output else 0
        status_code = exc.response.status_code
        if status_code == 429 and partial_output_tokens == 0:
            _mark_request_failed(
                db=db, request_id=request_id, org_id=org_id, project_id=project_id,
                key_id=key_id, source_ip=source_ip, user_agent=user_agent,
                detail=f"Azure stream 429 rate limit: {exc}",
                failure_code="upstream_rate_limited",
            )
            if ctx:
                _store_route_execution(db=db, ctx=ctx, upstream_ms=int((time.time() - t_start) * 1000), total_ms=int((time.time() - ctx["t_request_start"]) * 1000), status="failed")
        else:
            # Use Azure's error body usage if available (e.g. content filter mid-stream),
            # otherwise fall back to tiktoken on the original messages.
            azure_input, _ = _azure_usage_from_error(exc)
            if azure_input > 0:
                billed_input = azure_input
                in_src = "azure"
            else:
                billed_input = _estimate_input_tokens(messages, model)
                in_src = "tiktoken_estimate"
            _err_upstream_ms = int((time.time() - t_start) * 1000)
            _mark_request_failed_with_cost(
                db=db, request_id=request_id, org_id=org_id, project_id=project_id,
                key_id=key_id, model=model, deployment=deployment,
                input_tokens=billed_input, output_tokens=partial_output_tokens,
                latency_ms=_err_upstream_ms,
                source_ip=source_ip, user_agent=user_agent,
                detail=f"Azure stream error {status_code} after {len(chunks_raw)} chunks: {exc}",
                failure_code=f"upstream_error_{status_code}",
                input_token_source=in_src,
                output_token_source="tiktoken_estimate",
            )
            if ctx:
                _store_route_execution(db=db, ctx=ctx, upstream_ms=_err_upstream_ms, total_ms=int((time.time() - ctx["t_request_start"]) * 1000), status="failed")
        db.commit()
        return
    except httpx.RequestError as exc:
        yield f"data: {json.dumps({'error': f'Azure unreachable: {exc}'})}\n\n".encode()
        _err_upstream_ms = int((time.time() - t_start) * 1000)
        _fallback_tokens = _estimate_input_tokens(messages, model)
        _mark_request_failed_with_cost(
            db=db, request_id=request_id, org_id=org_id, project_id=project_id,
            key_id=key_id, model=model, deployment=deployment,
            input_tokens=_fallback_tokens, output_tokens=0,
            latency_ms=_err_upstream_ms,
            source_ip=source_ip, user_agent=user_agent,
            detail=f"Azure stream unreachable (timeout/connection): {exc}",
            failure_code="upstream_unreachable",
            input_token_source="tiktoken_estimate",
            output_token_source="tiktoken_estimate",
        )
        if ctx:
            _store_route_execution(db=db, ctx=ctx, upstream_ms=_err_upstream_ms, total_ms=int((time.time() - ctx["t_request_start"]) * 1000), status="failed")
        db.commit()
        return

    latency_ms = int((time.time() - t_start) * 1000)

    combined = "".join(
        json.loads(c).get("choices", [{}])[0].get("delta", {}).get("content") or ""
        for c in chunks_raw
        if _is_valid_json(c)
    )
    output_tokens = count_tokens(text=combined, model_name=model) if combined else 0

    # Partial stream: connection dropped without receiving [DONE].
    # Azure consumed tokens for whatever was generated — record cost with
    # status="partial" so dashboards can distinguish from clean success.
    if finish_reason is None and chunks_raw:
        _partial_input = _estimate_input_tokens(messages, model)
        _mark_request_failed_with_cost(
            db=db, request_id=request_id, org_id=org_id, project_id=project_id,
            key_id=key_id, model=model, deployment=deployment,
            input_tokens=_partial_input, output_tokens=output_tokens,
            latency_ms=latency_ms,
            source_ip=source_ip, user_agent=user_agent,
            detail=f"Stream ended without [DONE] after {len(chunks_raw)} chunks",
            req_status="partial",
            failure_code="stream_incomplete",
            input_token_source="tiktoken_estimate",
            output_token_source="tiktoken_estimate",
        )
        if ctx:
            _store_route_execution(db=db, ctx=ctx, upstream_ms=latency_ms, total_ms=int((time.time() - ctx["t_request_start"]) * 1000), status="partial")
        db.commit()
        return

    _stream_input_tokens = _estimate_input_tokens(messages, model)
    _store_response_and_cost(
        db=db,
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        model=model,
        deployment=deployment,
        provider=provider,
        response_payload={"streamed": True, "chunks": len(chunks_raw)},
        input_tokens=_stream_input_tokens,
        output_tokens=output_tokens,
        finish_reason=finish_reason,
        latency_ms=latency_ms,
        status="success",
        input_token_source="tiktoken_estimate",
        output_token_source="tiktoken_estimate",
    )
    _store_audit(
        db=db,
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        key_id=key_id,
        action="proxy_stream_complete",
        status="success",
        source_ip=source_ip,
        user_agent=user_agent,
    )
    if ctx:
        _store_route_execution(db=db, ctx=ctx, upstream_ms=latency_ms, total_ms=int((time.time() - ctx["t_request_start"]) * 1000), status="success")
    db.commit()


# ---------------------------------------------------------------------------
# Shared enforcement pipeline (steps 1-6)
# ---------------------------------------------------------------------------

async def _run_pre_flight(
    *,
    request: Request,
    x_governance_key: str,
    model_override: Optional[str],
    db: Session,
) -> dict:
    """Auth, rate-limit, model resolution, budget/governance checks, PII scan, token count.

    Returns a context dict consumed by both proxy endpoints.
    Raises HTTPException on any enforcement failure.
    """
    t_request_start = time.time()

    identity = _authenticate(db=db, raw_key=x_governance_key)
    org_id: str = identity["org_id"]
    project_id: Optional[str] = identity["project_id"]
    key_id: str = identity["key_id"]

    source_ip: Optional[str] = request.client.host if request.client else None
    user_agent: Optional[str] = request.headers.get("user-agent")
    request_id = _new_request_id()

    try:
        check_rate_limit(
            db=db, org_id=org_id, project_id=project_id, key_id=key_id,
            model="", request_id=request_id, source_ip=source_ip,
        )
    except HTTPException as exc:
        _log_blocked_request(
            db=db, request_id=request_id, org_id=org_id, project_id=project_id,
            key_id=key_id, source_ip=source_ip, user_agent=user_agent,
            failure_code="rate_limited", failure_reason=str(exc.detail),
        )
        raise
    except Exception as _e:
        _log.warning("Rate limit check skipped: %s", _e)

    try:
        body: dict = await request.json()
    except Exception:
        _log_blocked_request(
            db=db, request_id=request_id, org_id=org_id, project_id=project_id,
            key_id=key_id, source_ip=source_ip, user_agent=user_agent,
            failure_code="invalid_request", failure_reason="Invalid JSON body.",
        )
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    model_name = model_override or body.get("model")
    if not model_name:
        _reason = "Model must be specified via ?model= query param or 'model' in the request body."
        _log_blocked_request(
            db=db, request_id=request_id, org_id=org_id, project_id=project_id,
            key_id=key_id, source_ip=source_ip, user_agent=user_agent,
            failure_code="invalid_request", failure_reason=_reason, request_payload=body,
        )
        raise HTTPException(status_code=400, detail=_reason)

    depl = get_deployment_for_org(db, org_id=org_id, project_id=project_id, requested_model=model_name)
    if not depl:
        _reason = f"Model '{model_name}' is not configured or not authorized for this project."
        _log_blocked_request(
            db=db, request_id=request_id, org_id=org_id, project_id=project_id,
            key_id=key_id, source_ip=source_ip, user_agent=user_agent,
            failure_code="model_not_authorized", failure_reason=_reason, request_payload=body,
        )
        raise HTTPException(status_code=404, detail=_reason)

    model: str = depl.model_name
    deployment: str = depl.deployment_name or depl.model_name
    t_routing_done = time.time()

    try:
        check_budget(
            db=db, org_id=org_id, project_id=project_id,
            request_id=request_id, source_ip=source_ip, key_id=key_id,
        )
    except HTTPException as exc:
        _log_blocked_request(
            db=db, request_id=request_id, org_id=org_id, project_id=project_id,
            key_id=key_id, source_ip=source_ip, user_agent=user_agent,
            failure_code="budget_exceeded", failure_reason=str(exc.detail), request_payload=body,
        )
        raise
    except Exception as _e:
        _log.warning("Budget check skipped: %s", _e)

    messages: list[dict] = body.get("messages", [])
    pii_detected = False
    pii_types: list = []
    pii_masked = False
    pii_action_taken: Optional[str] = None
    pii_severity: str = ""
    pii_entities_detected: int = 0
    pii_entities_masked: int = 0
    pii_detail: Optional[list] = None
    try:
        clean_messages, pii_result = _scan_messages(
            messages=messages, org_id=org_id, project_id=project_id, db=db,
        )
        pii_detected = pii_result.pii_detected
        pii_types = pii_result.pii_types
        pii_masked = pii_result.pii_masked
        pii_action_taken = pii_result.action_taken if pii_result.pii_detected else None
        pii_severity = pii_result.severity
        pii_entities_detected = pii_result.entities_detected
        pii_entities_masked = pii_result.entities_masked
        pii_detail = pii_result.entity_details if pii_result.entity_details else None
    except HTTPException as exc:
        _log_blocked_request(
            db=db, request_id=request_id, org_id=org_id, project_id=project_id,
            key_id=key_id, source_ip=source_ip, user_agent=user_agent,
            failure_code="pii_block",
            failure_reason=exc.detail.get("message") if isinstance(exc.detail, dict) else str(exc.detail),
            request_payload=body,
        )
        raise
    except Exception as _e:
        _log.warning("PII scan skipped: %s", _e)
        clean_messages = messages

    forward_body = {k: v for k, v in body.items() if k != "stream"}
    forward_body["messages"] = clean_messages
    forward_body["model"] = model

    input_tokens = 0  # real counts come from Azure usage after response

    t_governance_done = time.time()
    entry_point = request.url.path

    _store_request(
        db=db,
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        key_id=key_id,
        request_type="chat_completion",
        model=model,
        deployment=deployment,
        payload=forward_body,
        input_tokens=input_tokens,
        source_ip=source_ip,
        pii_detected=pii_detected,
        pii_types=pii_types,
        pii_masked=pii_masked,
        pii_action_taken=pii_action_taken,
        pii_severity=pii_severity,
        pii_entities_detected=pii_entities_detected,
        pii_entities_masked=pii_entities_masked,
        pii_detail=pii_detail,
        user_agent=user_agent,
        entry_point=entry_point,
    )
    db.commit()

    url, azure_hdrs = build_provider_request(depl)

    return dict(
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        key_id=key_id,
        model=model,
        deployment=deployment,
        provider=getattr(depl, "provider", None) or "",
        forward_body=forward_body,
        input_tokens=input_tokens,
        source_ip=source_ip,
        user_agent=user_agent,
        url=url,
        azure_hdrs=azure_hdrs,
        entry_point=entry_point,
        t_request_start=t_request_start,
        routing_ms=int((t_routing_done - t_request_start) * 1000),
        governance_ms=int((t_governance_done - t_routing_done) * 1000),
    )


# ---------------------------------------------------------------------------
# POST /proxy — non-streaming
# ---------------------------------------------------------------------------

@router.post("")
async def proxy_chat(
    request: Request,
    model: Optional[str] = Query(None, description="AI model name (overrides body 'model' field)"),
    x_governance_key: str = Header(..., alias="X-Governance-Key"),
    db: Session = Depends(get_db),
) -> Any:
    ctx = await _run_pre_flight(
        request=request, x_governance_key=x_governance_key, model_override=model, db=db,
    )
    t_start = time.time()

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=5.0)
        ) as client:
            azure_resp = await client.post(
                url=ctx["url"], headers=ctx["azure_hdrs"], json=ctx["forward_body"],
            )
            azure_resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if status_code == 429:
            _mark_request_failed(
                db=db, request_id=ctx["request_id"], org_id=ctx["org_id"],
                project_id=ctx["project_id"], key_id=ctx["key_id"],
                source_ip=ctx["source_ip"], user_agent=ctx["user_agent"],
                detail=f"Azure 429 rate limit: {exc}",
                failure_code="upstream_rate_limited",
            )
        else:
            azure_input, azure_output = _azure_usage_from_error(exc)
            if azure_input > 0:
                billed_input = azure_input
                in_src = "azure"
            else:
                billed_input = _estimate_input_tokens(ctx["forward_body"].get("messages", []), ctx["model"])
                in_src = "tiktoken_estimate"
            out_src = "azure" if azure_output > 0 else "tiktoken_estimate"
            _mark_request_failed_with_cost(
                db=db, request_id=ctx["request_id"], org_id=ctx["org_id"],
                project_id=ctx["project_id"], key_id=ctx["key_id"],
                model=ctx["model"], deployment=ctx["deployment"],
                input_tokens=billed_input, output_tokens=azure_output,
                latency_ms=int((time.time() - t_start) * 1000),
                source_ip=ctx["source_ip"], user_agent=ctx["user_agent"],
                detail=f"Azure error {status_code}: {exc}",
                failure_code=f"upstream_error_{status_code}",
                input_token_source=in_src, output_token_source=out_src,
            )
        try:
            azure_detail = exc.response.json()
        except Exception:
            azure_detail = exc.response.text or str(exc)
        _err_upstream_ms = int((time.time() - t_start) * 1000)
        _store_route_execution(db=db, ctx=ctx, upstream_ms=_err_upstream_ms, total_ms=int((time.time() - ctx["t_request_start"]) * 1000), status="failed")
        db.commit()
        raise HTTPException(status_code=status_code, detail=azure_detail)
    except httpx.RequestError as exc:
        _fallback_tokens = _estimate_input_tokens(ctx["forward_body"].get("messages", []), ctx["model"])
        _mark_request_failed_with_cost(
            db=db, request_id=ctx["request_id"], org_id=ctx["org_id"],
            project_id=ctx["project_id"], key_id=ctx["key_id"],
            model=ctx["model"], deployment=ctx["deployment"],
            input_tokens=_fallback_tokens, output_tokens=0,
            latency_ms=int((time.time() - t_start) * 1000),
            source_ip=ctx["source_ip"], user_agent=ctx["user_agent"],
            detail=f"Azure unreachable: {exc}",
            failure_code="upstream_unreachable",
            input_token_source="tiktoken_estimate", output_token_source="tiktoken_estimate",
        )
        _err_upstream_ms = int((time.time() - t_start) * 1000)
        _store_route_execution(db=db, ctx=ctx, upstream_ms=_err_upstream_ms, total_ms=int((time.time() - ctx["t_request_start"]) * 1000), status="failed")
        db.commit()
        raise HTTPException(status_code=502, detail=f"Azure OpenAI unreachable: {exc}")

    latency_ms = int((time.time() - t_start) * 1000)
    response_data: dict = azure_resp.json()

    usage = response_data.get("usage", {})
    azure_prompt = int(usage.get("prompt_tokens", 0))
    azure_completion = int(usage.get("completion_tokens", 0))

    if azure_prompt > 0:
        input_tokens = azure_prompt
        in_src = "azure"
    else:
        input_tokens = _estimate_input_tokens(ctx["forward_body"].get("messages", []), ctx["model"])
        in_src = "tiktoken_estimate"

    if azure_completion > 0:
        output_tokens = azure_completion
        out_src = "azure"
    else:
        output_text = "".join(
            (c.get("message", {}).get("content") or "")
            for c in response_data.get("choices", [])
        )
        output_tokens = count_tokens(text=output_text, model_name=ctx["model"])
        out_src = "tiktoken_estimate"

    finish_reason: Optional[str] = None
    choices = response_data.get("choices", [])
    if choices:
        finish_reason = choices[0].get("finish_reason")

    _store_response_and_cost(
        db=db,
        request_id=ctx["request_id"],
        org_id=ctx["org_id"],
        project_id=ctx["project_id"],
        model=ctx["model"],
        deployment=ctx["deployment"],
        provider=ctx.get("provider", ""),
        response_payload=response_data,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        finish_reason=finish_reason,
        latency_ms=latency_ms,
        status="success",
        input_token_source=in_src,
        output_token_source=out_src,
    )
    _store_audit(
        db=db,
        request_id=ctx["request_id"],
        org_id=ctx["org_id"],
        project_id=ctx["project_id"],
        key_id=ctx["key_id"],
        action="proxy_request_complete",
        status="success",
        source_ip=ctx["source_ip"],
        user_agent=ctx["user_agent"],
    )
    total_ms = int((time.time() - ctx["t_request_start"]) * 1000)
    _store_route_execution(db=db, ctx=ctx, upstream_ms=latency_ms, total_ms=total_ms, status="success")
    db.commit()

    return JSONResponse(
        content=response_data,
        headers={"X-Request-Id": ctx["request_id"]},
    )


# ---------------------------------------------------------------------------
# OpenAI-compatible aliases — /proxy/chat/completions and /proxy/v1/chat/completions
# The OpenAI SDK appends /chat/completions to whatever base_url is set.
# These routes let callers set base_url="https://host/proxy" and use the SDK
# without any patching.
# ---------------------------------------------------------------------------

@router.post("/chat/completions")
@router.post("/v1/chat/completions")
async def proxy_chat_openai_compat(
    request: Request,
    model: Optional[str] = Query(None, description="AI model name (overrides body 'model' field)"),
    x_governance_key: str = Header(..., alias="X-Governance-Key"),
    db: Session = Depends(get_db),
) -> Any:
    return await proxy_chat(request=request, model=model, x_governance_key=x_governance_key, db=db)


# ---------------------------------------------------------------------------
# POST /proxy/stream — SSE streaming
# ---------------------------------------------------------------------------

@router.post("/stream")
async def proxy_chat_stream(
    request: Request,
    model: Optional[str] = Query(None, description="AI model name (overrides body 'model' field)"),
    x_governance_key: str = Header(..., alias="X-Governance-Key"),
    db: Session = Depends(get_db),
) -> Any:
    ctx = await _run_pre_flight(
        request=request, x_governance_key=x_governance_key, model_override=model, db=db,
    )
    t_start = time.time()
    return StreamingResponse(
        _stream_azure(
            url=ctx["url"],
            headers=ctx["azure_hdrs"],
            body={**ctx["forward_body"], "stream": True},
            request_id=ctx["request_id"],
            org_id=ctx["org_id"],
            project_id=ctx["project_id"],
            key_id=ctx["key_id"],
            model=ctx["model"],
            deployment=ctx["deployment"],
            provider=ctx.get("provider", ""),
            input_tokens=ctx["input_tokens"],
            messages=ctx["forward_body"].get("messages", []),
            source_ip=ctx["source_ip"],
            user_agent=ctx["user_agent"],
            db=db,
            t_start=t_start,
            ctx=ctx,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Request-Id": ctx["request_id"]},
    )


# ---------------------------------------------------------------------------
# Stats breakdown by project + model
# ---------------------------------------------------------------------------

@router.get("/stats/by-project-model")
def stats_by_project_model(
    *,
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    start: Optional[date] = Query(None),
    end: Optional[date] = Query(None),
    db: Session = Depends(get_db),
) -> list[dict]:
    q = db.query(
        RequestCost.project_id,
        RequestCost.model_name,
        func.count(RequestCost.request_id).label("total_requests"),
        func.sum(RequestCost.input_tokens).label("input_tokens"),
        func.sum(RequestCost.output_tokens).label("output_tokens"),
        func.sum(RequestCost.total_tokens).label("total_tokens"),
        func.sum(RequestCost.input_token_cost).label("input_cost"),
        func.sum(RequestCost.output_token_cost).label("output_cost"),
        func.sum(RequestCost.llm_cost).label("llm_cost"),
        func.sum(RequestCost.total_cost).label("total_cost"),
    )
    if org_id:
        q = q.filter(RequestCost.org_id == org_id)
    if project_id:
        q = q.filter(RequestCost.project_id == project_id)
    if start:
        q = q.filter(func.date(RequestCost.created_at) >= start)
    if end:
        q = q.filter(func.date(RequestCost.created_at) <= end)

    rows = (
        q.group_by(RequestCost.project_id, RequestCost.model_name)
        .order_by(func.sum(RequestCost.total_cost).desc())
        .all()
    )

    return [
        {
            "project_id": r.project_id,
            "model_name": r.model_name,
            "total_requests": r.total_requests or 0,
            "input_tokens": r.input_tokens or 0,
            "output_tokens": r.output_tokens or 0,
            "total_tokens": r.total_tokens or 0,
            "input_cost": float(r.input_cost or 0),
            "output_cost": float(r.output_cost or 0),
            "llm_cost": float(r.llm_cost or 0),
            "total_cost": float(r.total_cost or 0),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Proxy stats — overview, trends, PII summary
# ---------------------------------------------------------------------------

@router.get("/stats/overview")
def proxy_stats_overview(
    *,
    org_id: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
) -> dict:
    cutoff = datetime.utcnow().date() - timedelta(days=days - 1)

    cost_q = db.query(
        func.count(RequestCost.request_id).label("total_requests"),
        func.sum(RequestCost.input_tokens).label("prompt_tokens"),
        func.sum(RequestCost.output_tokens).label("completion_tokens"),
        func.sum(RequestCost.total_tokens).label("total_tokens"),
        func.sum(RequestCost.llm_cost).label("llm_cost"),
        func.sum(RequestCost.total_cost).label("total_cost"),
    ).filter(func.date(RequestCost.created_at) >= cutoff)
    if org_id:
        cost_q = cost_q.filter(RequestCost.org_id == org_id)
    cost = cost_q.first()

    req_q = db.query(
        func.count(AiRequest.id).label("total"),
        func.sum(
            cast(AiRequest.request_status == "success", Integer)
        ).label("completed"),
    ).filter(func.date(AiRequest.created_at) >= cutoff)
    if org_id:
        req_q = req_q.filter(AiRequest.org_id == org_id)
    req_stats = req_q.first()
    total_reqs = int(req_stats.total or 0)
    completed = int(req_stats.completed or 0)

    latency_q = db.query(func.avg(AiResponse.latency_ms)).filter(
        func.date(AiResponse.created_at) >= cutoff,
    )
    if org_id:
        latency_q = latency_q.filter(AiResponse.org_id == org_id)
    avg_latency = latency_q.scalar() or 0

    pii_q = db.query(func.count(AiRequest.id)).filter(
        AiRequest.pii_detected == True,
        func.date(AiRequest.created_at) >= cutoff,
    )
    if org_id:
        pii_q = pii_q.filter(AiRequest.org_id == org_id)
    pii_detections = pii_q.scalar() or 0

    success_rate = round((completed / total_reqs * 100), 2) if total_reqs > 0 else 0

    return {
        "total_requests": total_reqs,
        "completed": completed,
        "success_rate": success_rate,
        "prompt_tokens": cost.prompt_tokens or 0,
        "completion_tokens": cost.completion_tokens or 0,
        "total_tokens": cost.total_tokens or 0,
        "llm_cost": float(cost.llm_cost or 0),
        "total_cost": float(cost.total_cost or 0),
        "avg_latency_ms": round(float(avg_latency), 2),
        "pii_detections": pii_detections,
    }


@router.get("/stats/trends")
def proxy_stats_trends(
    *,
    org_id: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
) -> list[dict]:
    cutoff = datetime.utcnow().date() - timedelta(days=days - 1)

    q = db.query(
        func.date(RequestCost.created_at).label("date"),
        func.count(RequestCost.request_id).label("total_requests"),
        func.sum(RequestCost.total_tokens).label("total_tokens"),
        func.sum(RequestCost.llm_cost).label("llm_cost"),
        func.sum(RequestCost.total_cost).label("total_cost"),
    ).filter(func.date(RequestCost.created_at) >= cutoff)
    if org_id:
        q = q.filter(RequestCost.org_id == org_id)

    rows = (
        q.group_by(func.date(RequestCost.created_at))
        .order_by(func.date(RequestCost.created_at).asc())
        .all()
    )

    return [
        {
            "date": str(r.date),
            "total_requests": r.total_requests or 0,
            "total_tokens": r.total_tokens or 0,
            "llm_cost": float(r.llm_cost or 0),
            "total_cost": float(r.total_cost or 0),
        }
        for r in rows
    ]


@router.get("/stats/pii")
def proxy_stats_pii(
    *,
    org_id: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
) -> dict:
    from sqlalchemy import text as _text

    cutoff = (datetime.utcnow().date() - timedelta(days=days - 1)).isoformat()

    org_filter = "AND org_id = :org_id" if org_id else ""

    type_rows = db.execute(_text(f"""
        SELECT unnested_type AS pii_type, COUNT(*) AS count
        FROM ai_requests,
             jsonb_array_elements_text(
                 CASE
                     WHEN jsonb_typeof(pii_types::jsonb) = 'array' THEN pii_types::jsonb
                     ELSE '[]'::jsonb
                 END
             ) AS unnested_type
        WHERE pii_detected = TRUE
          AND DATE(created_at) >= :cutoff
          {org_filter}
        GROUP BY unnested_type
        ORDER BY count DESC
    """), {"cutoff": cutoff, "org_id": org_id} if org_id else {"cutoff": cutoff}).fetchall()

    action_rows = db.execute(_text(f"""
        SELECT pii_action_taken AS action, COUNT(*) AS count
        FROM ai_requests
        WHERE pii_detected = TRUE
          AND pii_action_taken IS NOT NULL
          AND DATE(created_at) >= :cutoff
          {org_filter}
        GROUP BY pii_action_taken
        ORDER BY count DESC
    """), {"cutoff": cutoff, "org_id": org_id} if org_id else {"cutoff": cutoff}).fetchall()

    total_pii = sum(r.count for r in action_rows)

    return {
        "total_pii_requests": total_pii,
        "pii_type_breakdown": [{"pii_type": r.pii_type, "count": r.count} for r in type_rows],
        "action_breakdown": [{"action": r.action, "count": r.count} for r in action_rows],
    }


# ---------------------------------------------------------------------------
# Request search — dashboard traceability
# ---------------------------------------------------------------------------

@router.get("/v1/requests")
def list_proxy_requests(
    *,
    request_id: Optional[str] = None,
    org_id: Optional[str] = None,
    project_id: Optional[str] = None,
    request_type: Optional[str] = None,
    status: Optional[str] = None,
    pii_only: Optional[bool] = None,
    pii_severity: Optional[str] = None,
    pii_action_taken: Optional[str] = None,
    failure_only: Optional[bool] = None,
    failure_code: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> dict:
    # Raw SQL avoids ORM column-mapping failures when proxy-layer columns
    # (governance_key_id, source_ip, deployment_name, completed_at) have not
    # yet been applied to the deployed DB via _SAFE_ALTERS.
    from sqlalchemy import text as _text

    filters: list[str] = []
    count_filters: list[str] = []
    params: dict = {}

    if request_id:
        filters.append("r.request_id = :request_id")
        count_filters.append("request_id = :request_id")
        params["request_id"] = request_id
    if org_id:
        filters.append("r.org_id = :org_id")
        count_filters.append("org_id = :org_id")
        params["org_id"] = org_id
    if project_id:
        filters.append("r.project_id = :project_id")
        count_filters.append("project_id = :project_id")
        params["project_id"] = project_id
    if request_type:
        filters.append("r.request_type = :request_type")
        count_filters.append("request_type = :request_type")
        params["request_type"] = request_type
    if status:
        filters.append("r.request_status = :status")
        count_filters.append("request_status = :status")
        params["status"] = status
    if pii_only:
        filters.append("r.pii_detected = TRUE")
        count_filters.append("pii_detected = TRUE")
    if pii_severity:
        filters.append("r.pii_severity = :pii_severity")
        count_filters.append("pii_severity = :pii_severity")
        params["pii_severity"] = pii_severity
    if pii_action_taken:
        filters.append("r.pii_action_taken = :pii_action_taken")
        count_filters.append("pii_action_taken = :pii_action_taken")
        params["pii_action_taken"] = pii_action_taken
    if failure_only:
        filters.append("r.failure_code IS NOT NULL")
        count_filters.append("failure_code IS NOT NULL")
    if failure_code:
        filters.append("r.failure_code = :failure_code")
        count_filters.append("failure_code = :failure_code")
        params["failure_code"] = failure_code

    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    count_where = ("WHERE " + " AND ".join(count_filters)) if count_filters else ""

    try:
        total = (
            db.execute(_text(f"SELECT COUNT(*) FROM ai_requests {count_where}"), params).scalar() or 0
        )
        params["limit"] = limit
        params["offset"] = offset
        rows = db.execute(
            _text(
                f"SELECT r.request_id, r.org_id, r.project_id, r.request_type, r.model_name,"
                f" r.input_token_estimate, r.request_status, r.created_at,"
                f" r.pii_detected, r.pii_types, r.pii_action_taken,"
                f" r.client_ip, r.source_ip, r.received_at, r.provider, r.source_system,"
                f" tu.total_tokens, rc.total_cost,"
                f" COALESCE(tu.input_tokens, r.input_token_estimate) AS prompt_tokens,"
                f" tu.output_tokens AS completion_tokens,"
                f" rc.llm_cost,"
                f" rc.input_token_cost, rc.output_token_cost,"
                f" r.entry_point,"
                f" r.pii_severity, r.pii_entities_detected, r.pii_entities_masked,"
                f" r.failure_code, r.failure_reason, r.completed_at, r.request_payload"
                f" FROM ai_requests r"
                f" LEFT JOIN token_usage tu ON tu.request_id = r.request_id"
                f" LEFT JOIN request_cost rc ON rc.request_id = r.request_id"
                f" {where}"
                f" ORDER BY r.created_at DESC LIMIT :limit OFFSET :offset"
            ),
            params,
        ).fetchall()
    except Exception as exc:
        _log.warning("list_proxy_requests query failed: %s", exc)
        return {"total": 0, "offset": offset, "items": []}

    return {
        "total": total,
        "offset": offset,
        "items": [
            {
                "request_id":       r[0],
                "org_id":           r[1],
                "project_id":       r[2],
                "request_type":     r[3],
                "model_name":       r[4],
                "deployment_name":  None,
                "input_tokens":     r[5],
                "request_status":   r[6],
                "status":           r[6],  # backward-compat alias
                "created_at":       r[7].isoformat() if r[7] else None,
                "completed_at":     r[29].isoformat() if r[29] else None,
                "pii_detected":     r[8],
                "pii_types":        r[9] or [],
                "pii_action_taken": r[10],
                "client_ip":        r[11] or r[12],  # client_ip fallback to source_ip
                "received_at":      r[13].isoformat() if r[13] else (r[7].isoformat() if r[7] else None),
                "provider":         r[14],
                "source_system":    r[15],
                "total_tokens":     r[16],
                "total_cost":       float(r[17]) if r[17] is not None else None,
                "prompt_tokens":    r[18],
                "completion_tokens": r[19],
                "llm_cost":         float(r[20]) if r[20] is not None else None,
                "input_cost":       float(r[21]) if r[21] is not None else None,
                "output_cost":      float(r[22]) if r[22] is not None else None,
                "entry_point":           r[23],
                "pii_severity":          r[24],
                "pii_entities_detected": r[25] or 0,
                "pii_entities_masked":   r[26] or 0,
                "failure_code":          r[27],
                "failure_reason":        r[28],
                "request_payload":       r[30],
            }
            for r in rows
        ],
    }


@router.get("/v1/requests/{request_id}/pii-detail")
def get_request_pii_detail(
    request_id: str,
    db: Session = Depends(get_db),
) -> dict:
    """Return full PII entity detail for a single request, including before/after values."""
    from sqlalchemy import text as _text

    try:
        row = db.execute(
            _text(
                "SELECT r.request_id, r.org_id, r.project_id,"
                " r.pii_detected, r.pii_types, r.pii_action_taken,"
                " r.pii_severity, r.pii_entities_detected, r.pii_entities_masked,"
                " r.pii_detail, r.request_payload, r.created_at,"
                " resp.response_text, resp.output_pii_types"
                " FROM ai_requests r"
                " LEFT JOIN ai_responses resp ON resp.request_id = r.request_id"
                " WHERE r.request_id = :request_id"
            ),
            {"request_id": request_id},
        ).fetchone()
    except Exception as exc:
        _log.warning("pii_detail query failed for %s: %s", request_id, exc)
        raise HTTPException(status_code=500, detail="Query failed.")

    if not row:
        raise HTTPException(status_code=404, detail="Request not found.")

    return {
        "request_id":            row[0],
        "org_id":                row[1],
        "project_id":            row[2],
        "pii_detected":          row[3],
        "pii_types":             row[4] or [],
        "pii_action_taken":      row[5],
        "pii_severity":          row[6],
        "pii_entities_detected": row[7] or 0,
        "pii_entities_masked":   row[8] or 0,
        "pii_detail":            row[9] or [],
        "request_payload":       row[10],
        "created_at":            row[11].isoformat() if row[11] else None,
        "response_text":         row[12],
        "output_pii_types":      row[13] or [],
    }
