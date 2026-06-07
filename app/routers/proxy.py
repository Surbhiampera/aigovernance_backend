"""Governance Proxy Server — the single entry point for all AI requests.

External teams configure only:
    GOVERNANCE_KEY=gov-xxxx
    GOVERNANCE_BASE_URL=https://governance.company.com

They point any OpenAI-compatible SDK at this proxy:
    client = OpenAI(
        api_key="ignored",
        base_url="https://governance.company.com/proxy/v1",
        default_headers={"X-Governance-Key": "gov-xxxx"},
    )

Azure OpenAI credentials (API key, endpoint, deployment) live exclusively on
the admin server — read from environment variables via config.py.
External teams never see them.

Request lifecycle (10 steps):
  1  Authenticate X-Governance-Key → resolve org_id + project_id
  2  Generate request_id, capture metadata and timestamp
  3  Receive and validate request body
  4  PII scan — mask or block based on pii_policies
  5  Count input tokens, store AiRequest row
  6  Forward sanitised request to Azure OpenAI (admin credentials only)
  7  Azure OpenAI processes the request
  8  Capture output tokens and latency
  9  Calculate cost (input + output)
 10  Store AiResponse, TokenUsage, RequestCost, AuditLog — return to caller
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, AsyncIterator, Optional

_log = logging.getLogger(__name__)

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.config import (
    get_azure_openai_api_key,
    get_azure_openai_api_version,
    get_azure_openai_deployment,
    get_azure_openai_endpoint,
)
from app.core.deps import get_db
from app.models import AiRequest, AiResponse, RequestCost, TokenUsage
from app.services.audit_service import log_event
from app.services.budget_service import check_budget
from app.services.governance_key_service import verify_governance_key
from app.services.governance_rule_service import check_governance_rules, check_max_input_tokens
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
) -> list[dict]:
    sanitised: list[dict] = []
    for msg in messages:
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
        sanitised.append({**msg, "content": result.get("masked_text", content)})
    return sanitised


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
        created_at=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    return row


# ---------------------------------------------------------------------------
# Step 6: Azure OpenAI URL and headers (admin credentials only)
# ---------------------------------------------------------------------------

def _azure_url(*, deployment: str, api_version: str) -> str:
    endpoint = get_azure_openai_endpoint().rstrip("/")
    return (
        f"{endpoint}/openai/deployments/{deployment}"
        f"/chat/completions?api-version={api_version}"
    )


def _azure_headers() -> dict[str, str]:
    return {
        "api-key": get_azure_openai_api_key(),
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Steps 9-10: Cost calculation, store response, store audit
# ---------------------------------------------------------------------------

def _calculate_cost(
    *,
    db: Session,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> tuple[Decimal, Decimal, Decimal, str, dict, str]:
    """Return (input_cost, output_cost, total_cost, pricing_source, pricing_snapshot, pricing_version).

    Lookup order: DB model_pricing (most recent effective_from ≤ now) → static catalogue.
    DB row wins unconditionally so admins can update pricing without a code deploy.
    pricing_snapshot records the exact rates used so historical audits remain accurate
    even after future price changes.
    """
    from app.models import ModelPricing as ModelPricingRow
    from app.services.ai_model_pricing import get_model_pricing, normalize_model_name

    canonical = normalize_model_name(model)

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
            "currency": db_price.currency or "USD",
            "effective_from": eff_date,
            "source": "database",
        }
        pricing_version = f"{canonical}@{eff_date}"
    else:
        catalogue_entry = get_model_pricing(canonical)
        if catalogue_entry:
            input_cost = Decimal(str(round((input_tokens / 1_000_000) * catalogue_entry.input_per_1m, 6)))
            output_cost = Decimal(str(round((output_tokens / 1_000_000) * catalogue_entry.output_per_1m, 6)))
            pricing_source = "catalogue"
            pricing_snapshot = {
                "input_cost_per_1k": catalogue_entry.input_per_1m / 1000,
                "output_cost_per_1k": catalogue_entry.output_per_1m / 1000,
                "currency": "USD",
                "effective_from": None,
                "source": "catalogue",
            }
            pricing_version = "catalogue"
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
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        input_token_cost=input_cost,
        output_token_cost=output_cost,
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
) -> None:
    """Mark ai_requests.status and write a failure audit row.

    Use this only when NO tokens were consumed (e.g. 429 rate limit, auth
    failure, PII block). Does not create request_costs or token_usage.
    For errors where Azure consumed tokens, use _mark_request_failed_with_cost().
    """
    req_row = db.query(AiRequest).filter(AiRequest.request_id == request_id).first()
    if req_row:
        req_row.request_status = req_status
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
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    source_ip: Optional[str],
    user_agent: Optional[str],
    detail: str,
    req_status: str = "failed",
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
        req_row.completed_at = datetime.utcnow()

    input_cost, output_cost, total_cost, pricing_source, pricing_snapshot, pricing_version = _calculate_cost(
        db=db,
        model=model,
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
    input_tokens: int,
    source_ip: Optional[str],
    user_agent: Optional[str],
    db: Session,
    t_start: float,
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
            )
        else:
            # Use Azure's error body usage if available (e.g. content filter mid-stream),
            # otherwise use tiktoken on received chunks for input and partial output.
            azure_input, _ = _azure_usage_from_error(exc)
            billed_input = azure_input if azure_input > 0 else input_tokens
            in_src = "azure" if azure_input > 0 else "tiktoken_estimate"
            _mark_request_failed_with_cost(
                db=db, request_id=request_id, org_id=org_id, project_id=project_id,
                key_id=key_id, model=model, deployment=deployment,
                input_tokens=billed_input, output_tokens=partial_output_tokens,
                latency_ms=int((time.time() - t_start) * 1000),
                source_ip=source_ip, user_agent=user_agent,
                detail=f"Azure stream error {status_code} after {len(chunks_raw)} chunks: {exc}",
                input_token_source=in_src,
                output_token_source="tiktoken_estimate",
            )
        return
    except httpx.RequestError as exc:
        yield f"data: {json.dumps({'error': f'Azure unreachable: {exc}'})}\n\n".encode()
        _mark_request_failed_with_cost(
            db=db, request_id=request_id, org_id=org_id, project_id=project_id,
            key_id=key_id, model=model, deployment=deployment,
            input_tokens=input_tokens, output_tokens=0,
            latency_ms=int((time.time() - t_start) * 1000),
            source_ip=source_ip, user_agent=user_agent,
            detail=f"Azure stream unreachable (timeout/connection): {exc}",
            input_token_source="tiktoken_estimate",
            output_token_source="tiktoken_estimate",
        )
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
        _mark_request_failed_with_cost(
            db=db, request_id=request_id, org_id=org_id, project_id=project_id,
            key_id=key_id, model=model, deployment=deployment,
            input_tokens=input_tokens, output_tokens=output_tokens,
            latency_ms=latency_ms,
            source_ip=source_ip, user_agent=user_agent,
            detail=f"Stream ended without [DONE] after {len(chunks_raw)} chunks",
            req_status="partial",
            input_token_source="tiktoken_estimate",
            output_token_source="tiktoken_estimate",
        )
        return

    _store_response_and_cost(
        db=db,
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        model=model,
        deployment=deployment,
        response_payload={"streamed": True, "chunks": len(chunks_raw)},
        input_tokens=input_tokens,
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
    db.commit()


# ---------------------------------------------------------------------------
# Main endpoint — OpenAI-compatible chat completions
# ---------------------------------------------------------------------------

@router.post("/v1/chat/completions")
async def proxy_chat_completions(
    request: Request,
    x_governance_key: str = Header(..., alias="X-Governance-Key"),
    db: Session = Depends(get_db),
) -> Any:
    # Step 1: Authenticate
    identity = _authenticate(db=db, raw_key=x_governance_key)
    org_id: str = identity["org_id"]
    project_id: Optional[str] = identity["project_id"]
    key_id: str = identity["key_id"]

    source_ip: Optional[str] = request.client.host if request.client else None
    user_agent: Optional[str] = request.headers.get("user-agent")

    # request_id is generated immediately after auth so every blocked-request
    # audit row and every error response body carries a consistent identifier.
    request_id = _new_request_id()

    # Step 2a: Rate limit — cheapest check, requires no body parse.
    # Wrapped to fail open: if DB schema is missing columns the check is skipped
    # rather than crashing the entire request with a 500.
    try:
        check_rate_limit(
            db=db, org_id=org_id, project_id=project_id, key_id=key_id,
            model="",  # model unknown until body parsed; org/project limits still apply
            request_id=request_id, source_ip=source_ip,
        )
    except HTTPException:
        raise
    except Exception as _rl_err:
        _log.warning("Rate limit check skipped due to error: %s", _rl_err)

    # Step 2: Parse body
    try:
        body: dict = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    messages: list[dict] = body.get("messages", [])
    stream: bool = bool(body.get("stream", False))
    deployment = get_azure_openai_deployment()
    api_version = get_azure_openai_api_version()
    # model is the canonical name used for pricing lookups (e.g. "gpt-4o").
    # deployment is the Azure-specific resource name (e.g. "my-company-gpt4o-prod").
    # Callers should pass the canonical model name in the request body.
    # If omitted, we fall back to the deployment name, which may not resolve in
    # the pricing catalogue — in that case, add an alias in ai_model_pricing.py.
    model: str = body.get("model") or deployment

    if not deployment:
        raise HTTPException(status_code=503, detail="Azure deployment not configured on this server.")

    # Step 2b: Budget — current-month spend vs configured limits.
    try:
        check_budget(
            db=db, org_id=org_id, project_id=project_id,
            request_id=request_id, source_ip=source_ip, key_id=key_id,
        )
    except HTTPException:
        raise
    except Exception as _budget_err:
        _log.warning("Budget check skipped due to error: %s", _budget_err)

    # Step 2c: Governance rules — model allow/block, pricing, max_output_tokens.
    try:
        check_governance_rules(
            db=db, org_id=org_id, project_id=project_id,
            model=model,
            max_output_tokens_requested=body.get("max_tokens"),
            request_id=request_id, source_ip=source_ip,
        )
    except HTTPException:
        raise
    except Exception as _gov_err:
        _log.warning("Governance rules check skipped due to error: %s", _gov_err)

    # Step 4: PII scan
    clean_messages = _scan_messages(
        messages=messages,
        org_id=org_id,
        project_id=project_id,
        db=db,
    )
    forward_body = {k: v for k, v in body.items() if k != "stream"}
    forward_body["messages"] = clean_messages

    # Step 5: Count input tokens using chat-completions format overhead.
    # Each message carries ~4 tokens of structural overhead (role, separators).
    # A further 2 tokens prime the reply. This matches Azure's billing model.
    input_tokens = 2  # reply priming
    for msg in clean_messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            input_tokens += 4 + count_tokens(text=content, model_name=model)
        else:
            input_tokens += 4  # non-text content (images etc.) — best effort

    # Step 4a: Max input tokens governance check (requires token count).
    check_max_input_tokens(
        db=db, org_id=org_id, project_id=project_id,
        model=model, input_tokens=input_tokens,
        request_id=request_id, source_ip=source_ip,
    )

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
        user_agent=user_agent,
    )
    db.commit()

    url = _azure_url(deployment=deployment, api_version=api_version)
    azure_hdrs = _azure_headers()
    t_start = time.time()

    # Step 6-10: Streaming path
    if stream:
        return StreamingResponse(
            _stream_azure(
                url=url,
                headers=azure_hdrs,
                body={**forward_body, "stream": True},
                request_id=request_id,
                org_id=org_id,
                project_id=project_id,
                key_id=key_id,
                model=model,
                deployment=deployment,
                input_tokens=input_tokens,
                source_ip=source_ip,
                user_agent=user_agent,
                db=db,
                t_start=t_start,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Request-Id": request_id},
        )

    # Step 6-10: Non-streaming path
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=5.0)
        ) as client:
            azure_resp = await client.post(url=url, headers=azure_hdrs, json=forward_body)
            azure_resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if status_code == 429:
            # Rate limit — Azure rejected before processing; no tokens consumed.
            _mark_request_failed(
                db=db, request_id=request_id, org_id=org_id, project_id=project_id,
                key_id=key_id, source_ip=source_ip, user_agent=user_agent,
                detail=f"Azure 429 rate limit: {exc}",
            )
        else:
            # Content filter (400), context length (400), server error (5xx), etc.
            # Azure received and processed the input — input tokens were consumed.
            # Prefer Azure's own usage count from the error body; fall back to
            # our tiktoken estimate when Azure doesn't include usage.
            azure_input, azure_output = _azure_usage_from_error(exc)
            billed_input = azure_input if azure_input > 0 else input_tokens
            billed_output = azure_output
            in_src = "azure" if azure_input > 0 else "tiktoken_estimate"
            out_src = "azure" if azure_output > 0 else "tiktoken_estimate"
            _mark_request_failed_with_cost(
                db=db, request_id=request_id, org_id=org_id, project_id=project_id,
                key_id=key_id, model=model, deployment=deployment,
                input_tokens=billed_input, output_tokens=billed_output,
                latency_ms=int((time.time() - t_start) * 1000),
                source_ip=source_ip, user_agent=user_agent,
                detail=f"Azure error {status_code}: {exc}",
                input_token_source=in_src,
                output_token_source=out_src,
            )
        raise HTTPException(status_code=status_code, detail=str(exc))
    except httpx.RequestError as exc:
        # Timeout or connection failure — Azure received the request body and
        # started processing before the connection dropped; input tokens consumed.
        _mark_request_failed_with_cost(
            db=db, request_id=request_id, org_id=org_id, project_id=project_id,
            key_id=key_id, model=model, deployment=deployment,
            input_tokens=input_tokens, output_tokens=0,
            latency_ms=int((time.time() - t_start) * 1000),
            source_ip=source_ip, user_agent=user_agent,
            detail=f"Azure unreachable (timeout/connection): {exc}",
            input_token_source="tiktoken_estimate",
            output_token_source="tiktoken_estimate",
        )
        raise HTTPException(status_code=502, detail=f"Azure OpenAI unreachable: {exc}")

    latency_ms = int((time.time() - t_start) * 1000)
    response_data: dict = azure_resp.json()

    # Step 8: Extract tokens — prefer Azure's usage field (exact, from provider).
    # prompt_tokens overwrites our pre-send tiktoken estimate when Azure returns it.
    usage = response_data.get("usage", {})
    azure_prompt = int(usage.get("prompt_tokens", 0))
    azure_completion = int(usage.get("completion_tokens", 0))

    if azure_prompt > 0:
        input_tokens = azure_prompt
        in_src = "azure"
    else:
        in_src = "tiktoken_estimate"  # keep the estimate we counted before sending

    if azure_completion > 0:
        output_tokens = azure_completion
        out_src = "azure"
    else:
        output_text = "".join(
            (c.get("message", {}).get("content") or "")
            for c in response_data.get("choices", [])
        )
        output_tokens = count_tokens(text=output_text, model_name=model)
        out_src = "tiktoken_estimate"

    finish_reason: Optional[str] = None
    choices = response_data.get("choices", [])
    if choices:
        finish_reason = choices[0].get("finish_reason")

    # Steps 9-10: Store cost + audit
    # Both token sources reflect whether Azure returned a usage field.
    # in_src = "azure" when prompt_tokens was present, "tiktoken_estimate" otherwise.
    _store_response_and_cost(
        db=db,
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        model=model,
        deployment=deployment,
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
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        key_id=key_id,
        action="proxy_request_complete",
        status="success",
        source_ip=source_ip,
        user_agent=user_agent,
    )
    db.commit()

    return JSONResponse(
        content=response_data,
        headers={"X-Request-Id": request_id},
    )


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
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> dict:
    try:
        q = db.query(AiRequest)
        if request_id:
            q = q.filter(AiRequest.request_id == request_id)
        if org_id:
            q = q.filter(AiRequest.org_id == org_id)
        if project_id:
            q = q.filter(AiRequest.project_id == project_id)
        if request_type:
            q = q.filter(AiRequest.request_type == request_type)

        total = q.count()
        rows = q.order_by(AiRequest.created_at.desc()).offset(offset).limit(limit).all()

        return {
            "total": total,
            "offset": offset,
            "items": [
                {
                    "request_id":    r.request_id,
                    "org_id":        r.org_id,
                    "project_id":    r.project_id,
                    "request_type":  r.request_type,
                    "model_name":    r.model_name,
                    "deployment_name": getattr(r, "deployment_name", None),
                    "input_tokens":  r.input_token_estimate,
                    "status":        r.request_status,
                    "source_ip":     getattr(r, "source_ip", None),
                    "created_at":    r.created_at.isoformat() if r.created_at else None,
                    "completed_at":  getattr(r, "completed_at", None) and r.completed_at.isoformat(),
                }
                for r in rows
            ],
        }
    except Exception as exc:
        _log.error("list_proxy_requests DB error: %s", exc)
        raise HTTPException(status_code=503, detail="Request history temporarily unavailable.")
