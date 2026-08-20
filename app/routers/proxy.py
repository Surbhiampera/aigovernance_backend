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

import asyncio
import json
import logging
import time
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, AsyncIterator, Optional

_log = logging.getLogger(__name__)

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import Integer, cast, func
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import (
    get_azure_circuit_failure_threshold,
    get_azure_circuit_reset_seconds,
    get_azure_max_concurrent_requests,
    get_azure_read_timeout_seconds,
    get_azure_retry_backoff_seconds,
    get_azure_retry_max_attempts,
    get_azure_total_deadline_seconds,
)
from app.core.deps import get_db
from app.models import AiRequest, AiResponse, RequestCost, RouteExecution, TokenUsage
from app.services.audit_service import log_event
from app.services.budget_service import check_budget
from app.services.circuit_breaker import CircuitBreaker
from app.services.cost_lookup import calculate_cost as _calculate_cost
from app.services.deployment_service import build_provider_request, get_deployments_for_org
from app.services.provider_translation import (
    extract_usage,
    needs_translation,
    stream_delta_text,
    stream_finish_reason,
    stream_is_terminal,
    translate_outbound_body,
)
from app.services.governance_key_service import verify_governance_key

from app.services.pii_engine import scan_and_mask
from app.services.rate_limit_service import check_rate_limit, record_tokens_used
from app.services.token_counter import count_tokens

router = APIRouter(prefix="/proxy", tags=["proxy"])


class _ConcurrencyLimiter:
    """Bounded, non-blocking concurrency cap.

    Unlike asyncio.Semaphore, try_acquire() never waits — callers over the
    limit get an immediate False so they can fail fast (503) instead of
    queuing behind a slow/saturated upstream and holding a DB connection
    while they wait.
    """

    def __init__(self, max_concurrent: int) -> None:
        self._max = max_concurrent
        self._count = 0
        self._lock = asyncio.Lock()

    async def try_acquire(self) -> bool:
        async with self._lock:
            if self._count >= self._max:
                return False
            self._count += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            self._count -= 1

    @property
    def in_flight(self) -> int:
        return self._count

    @property
    def max_concurrent(self) -> int:
        return self._max


# Backpressure valve: caps in-flight Azure calls per process. Once full,
# new requests get a fast 503 instead of queuing indefinitely behind a
# slow/saturated upstream and tying up DB connections while they wait.
_azure_limiter = _ConcurrencyLimiter(get_azure_max_concurrent_requests())

# Stops hammering an already-failing deployment: opens after repeated
# failures, fails fast while open, then probes once before fully closing.
# Keyed per (provider, deployment) — not global — so a failing primary
# deployment doesn't trip the breaker for an unrelated fallback deployment.
_circuit_breakers: dict[str, CircuitBreaker] = {}


def _circuit_key(provider: str, deployment: str) -> str:
    return f"{provider or 'azure_openai'}:{deployment}"


def _get_circuit_breaker(key: str) -> CircuitBreaker:
    cb = _circuit_breakers.get(key)
    if cb is None:
        cb = CircuitBreaker(
            failure_threshold=get_azure_circuit_failure_threshold(),
            reset_seconds=get_azure_circuit_reset_seconds(),
        )
        _circuit_breakers[key] = cb
    return cb


def _azure_httpx_timeout() -> httpx.Timeout:
    """Shared timeout config for every outbound call to a provider.

    read is configurable (AZURE_READ_TIMEOUT_SECONDS) — see
    get_azure_read_timeout_seconds for why it bounds worst-case latency.
    """
    return httpx.Timeout(connect=10.0, read=get_azure_read_timeout_seconds(), write=30.0, pool=5.0)


async def _post_to_azure_with_retry(
    client: httpx.AsyncClient, *, url: str, headers: dict, json_body: dict, deadline: float,
) -> httpx.Response:
    """POST to Azure, retrying transient failures (timeouts, connection
    errors, 5xx) with exponential backoff. Does not retry 4xx — those are
    deterministic (bad request, rate limited, content filtered) and retrying
    them would just waste the budget without changing the outcome.

    `deadline` is an absolute time.time() value shared across every retry AND
    every failover candidate (see get_azure_total_deadline_seconds) — once
    passed, no further retry is attempted here even if max_attempts hasn't
    been reached, so this can't itself blow the overall budget.
    """
    max_attempts = get_azure_retry_max_attempts()
    backoff = get_azure_retry_backoff_seconds()
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            resp = await client.post(url=url, headers=headers, json=json_body)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                raise
            last_exc = exc
        except httpx.RequestError as exc:
            last_exc = exc
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        if attempt < max_attempts - 1:
            await asyncio.sleep(min(backoff * (2 ** attempt), remaining))
        if time.time() >= deadline:
            break
    raise last_exc


# ---------------------------------------------------------------------------
# ID generators
# ---------------------------------------------------------------------------

def _new_request_id() -> str:
    return f"req-{uuid.uuid4().hex[:20]}"


def _new_response_id() -> str:
    return f"resp-{uuid.uuid4().hex[:20]}"


def _new_execution_id() -> str:
    return f"exec-{uuid.uuid4().hex[:20]}"


def _image_dimensions(data: bytes) -> Optional[tuple[int, int]]:
    """Best-effort (width, height) from raw image bytes — PNG/JPEG/GIF/WEBP headers only."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
            return w, h
        if data[:6] in (b"GIF87a", b"GIF89a"):
            w, h = int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
            return w, h
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            if data[12:16] == b"VP8X":
                w = 1 + int.from_bytes(data[24:27], "little")
                h = 1 + int.from_bytes(data[27:30], "little")
                return w, h
        if data[:2] == b"\xff\xd8":  # JPEG — scan markers for SOFx frame
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h = int.from_bytes(data[i + 5:i + 7], "big")
                    w = int.from_bytes(data[i + 7:i + 9], "big")
                    return w, h
                seg_len = int.from_bytes(data[i + 2:i + 4], "big")
                i += 2 + seg_len
    except Exception:
        pass
    return None


def _image_block_tokens(image_url_block: dict) -> int:
    """OpenAI vision token formula (https://platform.openai.com/docs/guides/vision):

    low detail  → flat 85 tokens.
    high/auto   → scale to fit 2048x2048, shortest side to 768, tile in 512x512
                  blocks, tokens = 85 + 170 * num_tiles.
    Dimensions come from decoding embedded base64 image bytes; remote URLs
    without inline data can't be measured without fetching them, so they fall
    back to a single-tile (1024x1024) estimate rather than a 1-tile minimum.
    """
    info = image_url_block.get("image_url") or {}
    if info.get("detail") == "low":
        return 85

    url = info.get("url", "")
    dims = None
    if url.startswith("data:") and "base64," in url:
        import base64
        try:
            dims = _image_dimensions(base64.b64decode(url.split("base64,", 1)[1]))
        except Exception:
            dims = None

    width, height = dims or (1024, 1024)

    if width > 2048 or height > 2048:
        scale = 2048 / max(width, height)
        width, height = width * scale, height * scale

    scale = 768 / min(width, height) if min(width, height) > 768 else 1
    width, height = width * scale, height * scale

    tiles_w = -(-int(width) // 512)  # ceil div
    tiles_h = -(-int(height) // 512)
    return 85 + 170 * tiles_w * tiles_h


def _content_block_tokens(block: dict, model: str) -> int:
    block_type = block.get("type")
    if block_type == "text":
        return count_tokens(text=block.get("text", ""), model_name=model)
    if block_type == "image_url":
        return _image_block_tokens(block)
    return 0


def _estimate_input_tokens(messages: list[dict], model: str) -> int:
    """Tiktoken fallback — only called when Azure does not return prompt_tokens."""
    tokens = 2
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            tokens += 4 + count_tokens(text=content, model_name=model)
        elif isinstance(content, list):
            tokens += 4 + sum(
                _content_block_tokens(block, model)
                for block in content if isinstance(block, dict)
            )
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


def _flush_success_writes(
    *,
    request_id: str,
    org_id: str,
    project_id: Optional[str],
    key_id: str,
    model: str,
    deployment: str,
    provider: str,
    response_payload: dict,
    input_tokens: int,
    output_tokens: int,
    finish_reason: Optional[str],
    latency_ms: int,
    input_token_source: str,
    output_token_source: str,
    source_ip: Optional[str],
    user_agent: Optional[str],
    ctx: dict,
    upstream_ms: int,
    total_ms: int,
) -> None:
    """Write response/cost/audit/route-execution rows after the client already
    has its response. Runs on its own DB session/connection so it never holds
    up the request that triggered it; failures here are logged, not raised.
    """
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        _store_response_and_cost(
            db=db, request_id=request_id, org_id=org_id, project_id=project_id,
            model=model, deployment=deployment, provider=provider,
            response_payload=response_payload, input_tokens=input_tokens,
            output_tokens=output_tokens, finish_reason=finish_reason,
            latency_ms=latency_ms, status="success",
            input_token_source=input_token_source, output_token_source=output_token_source,
        )
        _store_audit(
            db=db, request_id=request_id, org_id=org_id, project_id=project_id,
            key_id=key_id, action="proxy_request_complete", status="success",
            source_ip=source_ip, user_agent=user_agent,
        )
        _store_route_execution(
            db=db, ctx=ctx, upstream_ms=upstream_ms, total_ms=total_ms, status="success",
        )
        db.commit()
    except Exception:
        db.rollback()
        _log.exception("Background write failed for request_id=%s", request_id)
    finally:
        db.close()


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

    def _scan_text(text: str, *, idx: int, role: str) -> str:
        result = scan_and_mask(
            text=text,
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
                "role": role,
            })
        combined.entities_detected += result.entities_detected
        combined.entities_masked += result.entities_masked
        return result.sanitized_text

    for idx, msg in enumerate(messages):
        content = msg.get("content", "")
        role = msg.get("role", "user")
        if isinstance(content, str):
            sanitised.append({**msg, "content": _scan_text(content, idx=idx, role=role)})
        elif isinstance(content, list):
            scanned_blocks = [
                {**block, "text": _scan_text(block["text"], idx=idx, role=role)}
                if isinstance(block, dict) and block.get("type") == "text"
                else block
                for block in content
            ]
            sanitised.append({**msg, "content": scanned_blocks})
        else:
            sanitised.append(msg)
    combined.severity = compute_pii_severity([e["pii_type"] for e in combined.entity_details])
    return sanitised, combined


# ---------------------------------------------------------------------------
# Step 5: Store AiRequest
# ---------------------------------------------------------------------------

def _concat_message_text(messages: list[dict]) -> str:
    return "\n".join(
        m.get("content", "") for m in messages
        if isinstance(m, dict) and isinstance(m.get("content"), str)
    )


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
    original_messages: Optional[list[dict]] = None,
    sanitized_messages: Optional[list[dict]] = None,
    pii_detected: bool = False,
    pii_types: list = None,
    pii_masked: bool = False,
    pii_action_taken: Optional[str] = None,
    pii_severity: str = "",
    pii_entities_detected: int = 0,
    pii_entities_masked: int = 0,
    pii_detail: Optional[list] = None,
    entry_point: Optional[str] = None,
    trace_id: Optional[str] = None,
    parent_request_id: Optional[str] = None,
    has_tool_definitions: bool = False,
    user_id: Optional[str] = None,
    user_email: Optional[str] = None,
    user_role: Optional[str] = None,
) -> AiRequest:
    prompt_text_val = _concat_message_text(original_messages) if original_messages else None
    system_prompt_val = "\n".join(
        m.get("content", "") for m in (original_messages or [])
        if isinstance(m, dict) and m.get("role") == "system" and isinstance(m.get("content"), str)
    ) or None

    row = AiRequest(
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        governance_key_id=key_id,
        request_type=request_type,
        model_name=model,
        deployment_name=deployment,
        request_payload=payload,
        has_tool_definitions=has_tool_definitions,
        trace_id=trace_id,
        parent_request_id=parent_request_id,
        user_id=user_id,
        user_email=user_email,
        user_role=user_role,
        # Original (pre-mask) messages and masked text kept side-by-side for
        # audit/traceability — `request_payload` only holds the masked body
        # actually forwarded upstream.
        messages=original_messages,
        prompt_text=prompt_text_val,
        sanitized_prompt_text=_concat_message_text(sanitized_messages) if sanitized_messages else None,
        num_messages=len(original_messages or []),
        has_system_prompt=system_prompt_val is not None,
        prompt_char_count=len(prompt_text_val or ""),
        system_prompt=system_prompt_val,
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


def _token_usage_extras(
    *,
    req_row: Optional[AiRequest],
    model: str,
    input_tokens: int,
    output_tokens: int,
    response_payload: Optional[dict] = None,
) -> dict:
    """Best-effort enrichment for TokenUsage rows — system/tool-definition token
    counts, context-window utilization, and cache stats. Feeds the optimization
    tips engine (app/services/optimization/). Never raises: a token-count or
    pricing-catalogue failure here must not fail request accounting.
    """
    extras = {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "system_tokens": 0,
        "tool_definition_tokens": 0,
        "cached_tokens": 0,
        "uncached_tokens": input_tokens,
        "context_window_limit": None,
        "context_utilization_pct": None,
    }
    try:
        from app.services.ai_model_pricing import get_model_pricing
        from app.services.token_counter import count_tokens

        if req_row is not None:
            if req_row.system_prompt:
                extras["system_tokens"] = count_tokens(req_row.system_prompt, model)
            if req_row.has_tool_definitions and isinstance(req_row.request_payload, dict):
                tools = req_row.request_payload.get("tools")
                if tools:
                    extras["tool_definition_tokens"] = count_tokens(json.dumps(tools), model)

        if isinstance(response_payload, dict):
            usage = response_payload.get("usage") or {}
            cached = 0
            details = usage.get("prompt_tokens_details")
            if isinstance(details, dict):
                cached = int(details.get("cached_tokens", 0) or 0)
            elif "cache_read_input_tokens" in usage:
                cached = int(usage.get("cache_read_input_tokens", 0) or 0)
            extras["cached_tokens"] = cached
            extras["uncached_tokens"] = max(input_tokens - cached, 0)

        pricing = get_model_pricing(model)
        if pricing and pricing.context_window:
            extras["context_window_limit"] = pricing.context_window
            total = input_tokens + output_tokens
            extras["context_utilization_pct"] = round(min(total / pricing.context_window, 1) * 100, 3)
    except Exception:
        _log.warning("token_usage enrichment failed for model=%s", model, exc_info=True)

    return extras


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

    tool_calls = (
        response_payload.get("choices", [{}])[0].get("message", {}).get("tool_calls")
        if isinstance(response_payload, dict)
        else None
    )
    num_tool_calls = len(tool_calls) if tool_calls else 0

    req_row = db.query(AiRequest).filter(AiRequest.request_id == request_id).first()

    db.add(AiResponse(
        response_id=_new_response_id(),
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        model_name=model,
        response_payload=response_payload,
        tool_calls=tool_calls,
        num_tool_calls=num_tool_calls,
        finish_reason=finish_reason,
        latency_ms=latency_ms,
        response_status=status,
        created_at=datetime.utcnow(),
    ))

    token_usage_extras = _token_usage_extras(
        req_row=req_row, model=model, input_tokens=input_tokens,
        output_tokens=output_tokens, response_payload=response_payload,
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
        **token_usage_extras,
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

    record_tokens_used(org_id=org_id, project_id=project_id, tokens=input_tokens + output_tokens)

    if req_row:
        req_row.request_status = status
        req_row.completed_at = datetime.utcnow()
        req_row.input_token_estimate = input_tokens
        # Reflects whichever deployment actually served the request — may
        # differ from the primary if failover happened, so the request row
        # never disagrees with its own AiResponse/TokenUsage/RequestCost rows.
        req_row.model_name = model
        req_row.deployment_name = deployment

        if req_row.has_tool_definitions and num_tool_calls == 0:
            log_event(
                db,
                org_id=org_id,
                project_id=project_id,
                audit_category="governance",
                audit_action="tool_call_reliability_flag",
                audit_status="failure",
                actor_type="governance_engine",
                entity_type="ai_response",
                entity_id=request_id,
                request_id=request_id,
                policy_triggered=False,
                compliance_relevant=False,
                requires_review=True,
                change_summary=(
                    f"Model '{model}' was given tool definitions but invoked zero tools "
                    f"(finish_reason={finish_reason})."
                ),
                metadata={"model": model, "finish_reason": finish_reason},
                flush=False,
            )

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
    trace_id: Optional[str] = None,
    parent_request_id: Optional[str] = None,
    user_id: Optional[str] = None,
    user_email: Optional[str] = None,
    user_role: Optional[str] = None,
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
        trace_id=trace_id,
        parent_request_id=parent_request_id,
        user_id=user_id,
        user_email=user_email,
        user_role=user_role,
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
    model: Optional[str] = None,
    deployment: Optional[str] = None,
) -> None:
    """Mark ai_requests.status and write a failure audit row.

    Use this only when NO tokens were consumed (e.g. 429 rate limit, auth
    failure, PII block). Does not create request_costs or token_usage.
    For errors where Azure consumed tokens, use _mark_request_failed_with_cost().

    model/deployment are optional — pass them when known (e.g. after a
    failover attempt) so the request row shows which deployment actually
    issued the error, not just whichever was selected before the call.
    """
    req_row = db.query(AiRequest).filter(AiRequest.request_id == request_id).first()
    if req_row:
        req_row.request_status = req_status
        req_row.failure_code = failure_code
        req_row.failure_reason = detail
        req_row.completed_at = datetime.utcnow()
        if model:
            req_row.model_name = model
        if deployment:
            req_row.deployment_name = deployment

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
        # Reflects whichever deployment was actually attempted last — may
        # differ from the primary if failover happened, so the request row
        # never disagrees with its own TokenUsage/RequestCost rows.
        req_row.model_name = model
        req_row.deployment_name = deployment

    input_cost, output_cost, total_cost, pricing_source, pricing_snapshot, pricing_version = _calculate_cost(
        db=db,
        model=model,
        provider=provider,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    token_usage_extras = _token_usage_extras(
        req_row=req_row, model=model, input_tokens=input_tokens, output_tokens=output_tokens,
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
        **token_usage_extras,
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

    record_tokens_used(org_id=org_id, project_id=project_id, tokens=input_tokens + output_tokens)

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
    attempts: list[dict],
    request_id: str,
    org_id: str,
    project_id: Optional[str],
    key_id: str,
    input_tokens: int,
    messages: list[dict],
    source_ip: Optional[str],
    user_agent: Optional[str],
    db: Session,
    t_start: float,
    ctx: Optional[dict] = None,
) -> AsyncIterator[bytes]:
    """Stream Azure/OpenAI-shaped SSE to the client, failing over to the next
    same-class candidate in `attempts` if the connection itself fails (5xx,
    429, or unreachable) before any bytes have been sent downstream. Once a
    chunk has been yielded, no more switching — same as the original
    single-deployment behavior below that point.
    """
    chunks_raw: list[str] = []
    finish_reason: Optional[str] = None
    model = deployment = provider = ""

    for i, attempt in enumerate(attempts):
        is_last = i == len(attempts) - 1
        url, headers, body = attempt["url"], attempt["azure_hdrs"], attempt["outbound_body"]
        model, deployment, provider = attempt["model"], attempt["deployment"], attempt["provider"]
        circuit = _get_circuit_breaker(_circuit_key(provider, deployment))

        if not await circuit.allow_request():
            if not is_last:
                continue
            if ctx:
                ctx.update(attempt)
            _circuit_msg = f"{provider or 'Provider'} circuit breaker open"
            yield f"data: {json.dumps({'error': _circuit_msg})}\n\n".encode()
            _mark_request_failed(
                db=db, request_id=request_id, org_id=org_id, project_id=project_id,
                key_id=key_id, source_ip=source_ip, user_agent=user_agent,
                detail=_circuit_msg,
                failure_code="circuit_open",
                model=model, deployment=deployment,
            )
            if ctx:
                _store_route_execution(db=db, ctx=ctx, upstream_ms=int((time.time() - t_start) * 1000), total_ms=int((time.time() - ctx["t_request_start"]) * 1000), status="failed")
            db.commit()
            return

        try:
            async with httpx.AsyncClient(
                timeout=_azure_httpx_timeout()
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
            await circuit.record_success()
            if ctx:
                ctx.update(attempt)
            break
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if not chunks_raw and not is_last and (status_code >= 500 or status_code == 429):
                await circuit.record_failure()
                continue
            if status_code >= 500:
                await circuit.record_failure()
            if ctx:
                ctx.update(attempt)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n".encode()
            partial_output = "".join(
                json.loads(c).get("choices", [{}])[0].get("delta", {}).get("content") or ""
                for c in chunks_raw if _is_valid_json(c)
            )
            partial_output_tokens = count_tokens(text=partial_output, model_name=model) if partial_output else 0
            if status_code == 429 and partial_output_tokens == 0:
                _mark_request_failed(
                    db=db, request_id=request_id, org_id=org_id, project_id=project_id,
                    key_id=key_id, source_ip=source_ip, user_agent=user_agent,
                    detail=f"Azure stream 429 rate limit: {exc}",
                    failure_code="upstream_rate_limited",
                    model=model, deployment=deployment,
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
                    key_id=key_id, model=model, deployment=deployment, provider=provider,
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
            if not chunks_raw and not is_last:
                await circuit.record_failure()
                continue
            await circuit.record_failure()
            if ctx:
                ctx.update(attempt)
            yield f"data: {json.dumps({'error': f'Azure unreachable: {exc}'})}\n\n".encode()
            _err_upstream_ms = int((time.time() - t_start) * 1000)
            _fallback_tokens = _estimate_input_tokens(messages, model)
            _mark_request_failed_with_cost(
                db=db, request_id=request_id, org_id=org_id, project_id=project_id,
                key_id=key_id, model=model, deployment=deployment, provider=provider,
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
            key_id=key_id, model=model, deployment=deployment, provider=provider,
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


async def _stream_provider_native(
    *,
    attempts: list[dict],
    request_id: str,
    org_id: str,
    project_id: Optional[str],
    key_id: str,
    messages: list[dict],
    source_ip: Optional[str],
    user_agent: Optional[str],
    db: Session,
    t_start: float,
    ctx: Optional[dict] = None,
) -> AsyncIterator[bytes]:
    """Stream Anthropic/Google responses to the client byte-for-byte (native
    passthrough — no chunk reshaping) while parsing the same SSE events on
    the side, via provider_translation.stream_*, purely for internal
    accounting (tokens are always tiktoken-estimated on the stream path,
    same as the existing Azure stream behavior above).

    Fails over to the next same-class candidate in `attempts` if the
    connection itself fails (5xx, 429, unreachable) before any raw bytes
    have been sent downstream. Once a byte has been yielded, no more
    switching — same safety boundary as _stream_azure.
    """
    text_chunks: list[str] = []
    finish_reason: Optional[str] = None
    saw_any_chunk = False
    buffer = ""
    model = deployment = provider = ""

    for i, attempt in enumerate(attempts):
        is_last = i == len(attempts) - 1
        url, headers, body = attempt["url"], attempt["azure_hdrs"], attempt["outbound_body"]
        model, deployment, provider = attempt["model"], attempt["deployment"], attempt["provider"]
        circuit = _get_circuit_breaker(_circuit_key(provider, deployment))
        started_streaming = False

        if not await circuit.allow_request():
            if not is_last:
                continue
            if ctx:
                ctx.update(attempt)
            _circuit_msg = f"{provider or 'Provider'} circuit breaker open"
            _mark_request_failed(
                db=db, request_id=request_id, org_id=org_id, project_id=project_id,
                key_id=key_id, source_ip=source_ip, user_agent=user_agent,
                detail=_circuit_msg,
                failure_code="circuit_open",
                model=model, deployment=deployment,
            )
            if ctx:
                _store_route_execution(db=db, ctx=ctx, upstream_ms=int((time.time() - t_start) * 1000), total_ms=int((time.time() - ctx["t_request_start"]) * 1000), status="failed")
            db.commit()
            return

        try:
            async with httpx.AsyncClient(
                timeout=_azure_httpx_timeout()
            ) as client:
                async with client.stream("POST", url=url, headers=headers, json=body) as resp:
                    resp.raise_for_status()
                    async for raw in resp.aiter_bytes():
                        if not raw:
                            continue
                        started_streaming = True
                        yield raw
                        buffer += raw.decode("utf-8", errors="ignore")
                        while "\n\n" in buffer:
                            block, buffer = buffer.split("\n\n", 1)
                            for line in block.split("\n"):
                                if not line.startswith("data:"):
                                    continue
                                json_str = line[5:].strip()
                                if not json_str:
                                    continue
                                try:
                                    chunk = json.loads(json_str)
                                except (json.JSONDecodeError, TypeError):
                                    continue
                                saw_any_chunk = True
                                delta = stream_delta_text(provider, chunk)
                                if delta:
                                    text_chunks.append(delta)
                                fr = stream_finish_reason(provider, chunk)
                                if fr:
                                    finish_reason = fr
                                if stream_is_terminal(provider, chunk):
                                    finish_reason = finish_reason or "stop"
            await circuit.record_success()
            if ctx:
                ctx.update(attempt)
            break
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if not started_streaming and not is_last and (status_code >= 500 or status_code == 429):
                await circuit.record_failure()
                continue
            if status_code >= 500:
                await circuit.record_failure()
            if ctx:
                ctx.update(attempt)
            partial_output = "".join(text_chunks)
            partial_output_tokens = count_tokens(text=partial_output, model_name=model) if partial_output else 0
            if status_code == 429 and partial_output_tokens == 0:
                _mark_request_failed(
                    db=db, request_id=request_id, org_id=org_id, project_id=project_id,
                    key_id=key_id, source_ip=source_ip, user_agent=user_agent,
                    detail=f"{provider} stream 429 rate limit: {exc}",
                    failure_code="upstream_rate_limited",
                    model=model, deployment=deployment,
                )
                if ctx:
                    _store_route_execution(db=db, ctx=ctx, upstream_ms=int((time.time() - t_start) * 1000), total_ms=int((time.time() - ctx["t_request_start"]) * 1000), status="failed")
            else:
                _err_upstream_ms = int((time.time() - t_start) * 1000)
                _billed_input = _estimate_input_tokens(messages, model)
                _mark_request_failed_with_cost(
                    db=db, request_id=request_id, org_id=org_id, project_id=project_id,
                    key_id=key_id, model=model, deployment=deployment, provider=provider,
                    input_tokens=_billed_input, output_tokens=partial_output_tokens,
                    latency_ms=_err_upstream_ms,
                    source_ip=source_ip, user_agent=user_agent,
                    detail=f"{provider} stream error {status_code} after {len(text_chunks)} text chunks: {exc}",
                    failure_code=f"upstream_error_{status_code}",
                    input_token_source="tiktoken_estimate",
                    output_token_source="tiktoken_estimate",
                )
                if ctx:
                    _store_route_execution(db=db, ctx=ctx, upstream_ms=_err_upstream_ms, total_ms=int((time.time() - ctx["t_request_start"]) * 1000), status="failed")
            db.commit()
            return
        except httpx.RequestError as exc:
            if not started_streaming and not is_last:
                await circuit.record_failure()
                continue
            await circuit.record_failure()
            if ctx:
                ctx.update(attempt)
            _err_upstream_ms = int((time.time() - t_start) * 1000)
            _fallback_tokens = _estimate_input_tokens(messages, model)
            _mark_request_failed_with_cost(
                db=db, request_id=request_id, org_id=org_id, project_id=project_id,
                key_id=key_id, model=model, deployment=deployment, provider=provider,
                input_tokens=_fallback_tokens, output_tokens=0,
                latency_ms=_err_upstream_ms,
                source_ip=source_ip, user_agent=user_agent,
                detail=f"{provider} unreachable: {exc}",
                failure_code="upstream_unreachable",
                input_token_source="tiktoken_estimate",
                output_token_source="tiktoken_estimate",
            )
            if ctx:
                _store_route_execution(db=db, ctx=ctx, upstream_ms=_err_upstream_ms, total_ms=int((time.time() - ctx["t_request_start"]) * 1000), status="failed")
            db.commit()
            return

    latency_ms = int((time.time() - t_start) * 1000)
    combined = "".join(text_chunks)
    output_tokens = count_tokens(text=combined, model_name=model) if combined else 0

    if finish_reason is None and saw_any_chunk:
        _partial_input = _estimate_input_tokens(messages, model)
        _mark_request_failed_with_cost(
            db=db, request_id=request_id, org_id=org_id, project_id=project_id,
            key_id=key_id, model=model, deployment=deployment, provider=provider,
            input_tokens=_partial_input, output_tokens=output_tokens,
            latency_ms=latency_ms,
            source_ip=source_ip, user_agent=user_agent,
            detail=f"{provider} stream ended without a terminal event after {len(text_chunks)} text chunks",
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
        response_payload={"streamed": True, "text_chunks": len(text_chunks)},
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
    stream: bool = False,
    trace_id: Optional[str] = None,
    user_id: Optional[str] = None,
    user_email: Optional[str] = None,
    user_role: Optional[str] = None,
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

    # First call with a given trace_id is the parent (parent_request_id stays
    # NULL); later calls in the same multi-step pipeline point back at it, so
    # the request list can show one row per user turn while every call keeps
    # its own tokens/payload for audit.
    parent_request_id: Optional[str] = None
    if trace_id:
        parent_row = (
            db.query(AiRequest.request_id)
            .filter(AiRequest.trace_id == trace_id, AiRequest.parent_request_id.is_(None))
            .order_by(AiRequest.created_at.asc())
            .first()
        )
        if parent_row:
            parent_request_id = parent_row[0]

    try:
        check_rate_limit(
            db=db, org_id=org_id, project_id=project_id, key_id=key_id,
            model="", request_id=request_id, source_ip=source_ip,
        )
    except HTTPException as exc:
        _log_blocked_request(
            db=db, request_id=request_id, org_id=org_id, project_id=project_id,
            key_id=key_id, source_ip=source_ip, user_agent=user_agent, trace_id=trace_id,
            parent_request_id=parent_request_id, user_id=user_id,
            user_email=user_email, user_role=user_role,
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
            key_id=key_id, source_ip=source_ip, user_agent=user_agent, trace_id=trace_id,
            parent_request_id=parent_request_id, user_id=user_id,
            user_email=user_email, user_role=user_role,
            failure_code="invalid_request", failure_reason="Invalid JSON body.",
        )
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    model_name = model_override or body.get("model")
    if not model_name:
        _reason = "Model must be specified via ?model= query param or 'model' in the request body."
        _log_blocked_request(
            db=db, request_id=request_id, org_id=org_id, project_id=project_id,
            key_id=key_id, source_ip=source_ip, user_agent=user_agent, trace_id=trace_id,
            parent_request_id=parent_request_id, user_id=user_id,
            user_email=user_email, user_role=user_role,
            failure_code="invalid_request", failure_reason=_reason, request_payload=body,
        )
        raise HTTPException(status_code=400, detail=_reason)

    candidate_deployments = get_deployments_for_org(
        db, org_id=org_id, project_id=project_id, requested_model=model_name,
    )
    if not candidate_deployments:
        _reason = f"Model '{model_name}' is not configured or not authorized for this project."
        _log_blocked_request(
            db=db, request_id=request_id, org_id=org_id, project_id=project_id,
            key_id=key_id, source_ip=source_ip, user_agent=user_agent, trace_id=trace_id,
            parent_request_id=parent_request_id, user_id=user_id,
            user_email=user_email, user_role=user_role,
            failure_code="model_not_authorized", failure_reason=_reason, request_payload=body,
        )
        raise HTTPException(status_code=404, detail=_reason)

    depl = candidate_deployments[0]
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
            key_id=key_id, source_ip=source_ip, user_agent=user_agent, trace_id=trace_id,
            parent_request_id=parent_request_id, user_id=user_id,
            user_email=user_email, user_role=user_role,
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
        # _scan_messages runs Presidio/spaCy NER (CPU-bound) plus a sync DB
        # policy lookup. Both are blocking calls; running them inline on this
        # async def's event loop thread would stall every other coroutine on
        # this worker — including unrelated dashboard endpoints — for the
        # duration of the scan. Thread-offloading keeps the loop free.
        clean_messages, pii_result = await run_in_threadpool(
            _scan_messages, messages=messages, org_id=org_id, project_id=project_id, db=db,
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
            key_id=key_id, source_ip=source_ip, user_agent=user_agent, trace_id=trace_id,
            parent_request_id=parent_request_id, user_id=user_id,
            user_email=user_email, user_role=user_role,
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
        original_messages=messages,
        sanitized_messages=clean_messages,
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
        trace_id=trace_id,
        parent_request_id=parent_request_id,
        has_tool_definitions=bool(forward_body.get("tools") or forward_body.get("functions")),
        user_id=user_id,
        user_email=user_email,
        user_role=user_role,
    )
    db.commit()

    # Build one ready-to-send attempt per candidate deployment (primary first),
    # so the outbound call site can fail over to the next entry without
    # redoing auth/budget/PII/governance checks above.
    attempts: list[dict] = []
    for cand in candidate_deployments:
        cand_url, cand_hdrs = build_provider_request(cand, stream=stream)
        cand_provider = getattr(cand, "provider", None) or ""
        attempts.append(dict(
            depl_id=getattr(cand, "deployment_id", None),
            url=cand_url,
            azure_hdrs=cand_hdrs,
            outbound_body=translate_outbound_body(cand_provider, forward_body),
            provider=cand_provider,
            model=cand.model_name,
            deployment=cand.deployment_name or cand.model_name,
        ))

    primary = attempts[0]

    return dict(
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        key_id=key_id,
        model=primary["model"],
        deployment=primary["deployment"],
        provider=primary["provider"],
        forward_body=forward_body,
        outbound_body=primary["outbound_body"],
        input_tokens=input_tokens,
        source_ip=source_ip,
        user_agent=user_agent,
        url=primary["url"],
        azure_hdrs=primary["azure_hdrs"],
        attempts=attempts,
        entry_point=entry_point,
        t_request_start=t_request_start,
        routing_ms=int((t_routing_done - t_request_start) * 1000),
        governance_ms=int((t_governance_done - t_routing_done) * 1000),
    )


# ---------------------------------------------------------------------------
# Outbound call with failover across candidate deployments
# ---------------------------------------------------------------------------

async def _call_with_failover(*, ctx: dict) -> httpx.Response:
    """Try each candidate deployment in ctx['attempts'] (primary first) until
    one responds successfully.

    Skips a candidate whose circuit breaker is open. Fails over to the next
    candidate on connection errors, 5xx, and 429 (capacity/availability
    problems with that specific deployment) — not on other 4xx, since those
    are deterministic outcomes for the request itself (bad request, content
    filter, etc.) that a different provider wouldn't fix, and silently
    retrying a content-filter block against a laxer provider would be a
    governance decision, not just an engineering one.

    Mutates ctx in place with whichever attempt actually served the request
    (or was the last one tried, on total failure), so all downstream cost/
    token/audit logging — none of which changes — reflects what really
    happened.

    A hard wall-clock deadline (get_azure_total_deadline_seconds) spans every
    retry across every candidate here. Without it, retries-per-candidate ×
    candidates can exceed platform-level gateway timeouts this app doesn't
    control (e.g. Azure App Service's fixed 230s front-end timeout) — if that
    fires first, our own except blocks below never run and the caller gets
    an opaque platform error instead of our own audited 502/503.
    """
    attempts: list[dict] = ctx["attempts"]
    last_exc: Optional[Exception] = None
    deadline = time.time() + get_azure_total_deadline_seconds()

    for i, attempt in enumerate(attempts):
        is_last = (i == len(attempts) - 1)
        circuit = _get_circuit_breaker(_circuit_key(attempt["provider"], attempt["deployment"]))

        if time.time() >= deadline:
            ctx.update(attempt)
            if last_exc:
                raise last_exc
            raise HTTPException(
                status_code=502,
                detail="Azure OpenAI unreachable: exceeded internal retry deadline.",
            )

        if not await circuit.allow_request():
            if is_last:
                ctx.update(attempt)
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"{attempt['provider'] or 'Provider'} is failing repeatedly; "
                        "circuit breaker open. Please retry shortly."
                    ),
                )
            continue

        try:
            async with httpx.AsyncClient(
                timeout=_azure_httpx_timeout()
            ) as client:
                resp = await _post_to_azure_with_retry(
                    client, url=attempt["url"], headers=attempt["azure_hdrs"],
                    json_body=attempt["outbound_body"], deadline=deadline,
                )
            await circuit.record_success()
            ctx.update(attempt)
            return resp
        except httpx.HTTPStatusError as exc:
            ctx.update(attempt)
            status = exc.response.status_code
            if status >= 500 or status == 429:
                await circuit.record_failure()
                last_exc = exc
                if is_last:
                    raise
                continue
            raise
        except httpx.RequestError as exc:
            await circuit.record_failure()
            ctx.update(attempt)
            last_exc = exc
            if is_last:
                raise
            continue

    if last_exc:
        raise last_exc
    raise HTTPException(status_code=503, detail="No deployment available.")


# ---------------------------------------------------------------------------
# POST /proxy — non-streaming
# ---------------------------------------------------------------------------

@router.post("")
async def proxy_chat(
    request: Request,
    background_tasks: BackgroundTasks,
    model: Optional[str] = Query(None, description="AI model name (overrides body 'model' field)"),
    x_governance_key: str = Header(..., alias="X-Governance-Key"),
    x_trace_id: Optional[str] = Header(None, alias="X-Trace-Id"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
    db: Session = Depends(get_db),
) -> Any:
    ctx = await _run_pre_flight(
        request=request, x_governance_key=x_governance_key, model_override=model, db=db,
        trace_id=x_trace_id, user_id=x_user_id, user_email=x_user_email, user_role=x_user_role,
    )
    t_start = time.time()

    if not await _azure_limiter.try_acquire():
        raise HTTPException(
            status_code=503,
            detail="Server busy: too many concurrent requests to Azure OpenAI. Please retry shortly.",
        )
    try:
        try:
            azure_resp = await _call_with_failover(ctx=ctx)
        finally:
            await _azure_limiter.release()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if status_code == 429:
            _mark_request_failed(
                db=db, request_id=ctx["request_id"], org_id=ctx["org_id"],
                project_id=ctx["project_id"], key_id=ctx["key_id"],
                source_ip=ctx["source_ip"], user_agent=ctx["user_agent"],
                detail=f"Azure 429 rate limit: {exc}",
                failure_code="upstream_rate_limited",
                model=ctx["model"], deployment=ctx["deployment"],
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
                model=ctx["model"], deployment=ctx["deployment"], provider=ctx.get("provider", ""),
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
            model=ctx["model"], deployment=ctx["deployment"], provider=ctx.get("provider", ""),
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

    provider_usage = extract_usage(ctx.get("provider", ""), response_data)

    if provider_usage.input_tokens_known:
        input_tokens = provider_usage.input_tokens
        in_src = "azure"
    else:
        input_tokens = _estimate_input_tokens(ctx["forward_body"].get("messages", []), ctx["model"])
        in_src = "tiktoken_estimate"

    if provider_usage.output_tokens_known:
        output_tokens = provider_usage.output_tokens
        out_src = "azure"
    else:
        output_tokens = count_tokens(text=provider_usage.output_text, model_name=ctx["model"])
        out_src = "tiktoken_estimate"

    finish_reason: Optional[str] = provider_usage.finish_reason

    total_ms = int((time.time() - ctx["t_request_start"]) * 1000)
    background_tasks.add_task(
        _flush_success_writes,
        request_id=ctx["request_id"],
        org_id=ctx["org_id"],
        project_id=ctx["project_id"],
        key_id=ctx["key_id"],
        model=ctx["model"],
        deployment=ctx["deployment"],
        provider=ctx.get("provider", ""),
        response_payload=response_data,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        finish_reason=finish_reason,
        latency_ms=latency_ms,
        input_token_source=in_src,
        output_token_source=out_src,
        source_ip=ctx["source_ip"],
        user_agent=ctx["user_agent"],
        ctx=ctx,
        upstream_ms=latency_ms,
        total_ms=total_ms,
    )

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
    background_tasks: BackgroundTasks,
    model: Optional[str] = Query(None, description="AI model name (overrides body 'model' field)"),
    x_governance_key: str = Header(..., alias="X-Governance-Key"),
    x_trace_id: Optional[str] = Header(None, alias="X-Trace-Id"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
    db: Session = Depends(get_db),
) -> Any:
    return await proxy_chat(
        request=request, background_tasks=background_tasks, model=model,
        x_governance_key=x_governance_key, x_trace_id=x_trace_id, x_user_id=x_user_id,
        x_user_email=x_user_email, x_user_role=x_user_role, db=db,
    )


# ---------------------------------------------------------------------------
# POST /proxy/stream — SSE streaming
# ---------------------------------------------------------------------------

@router.post("/stream")
async def proxy_chat_stream(
    request: Request,
    model: Optional[str] = Query(None, description="AI model name (overrides body 'model' field)"),
    x_governance_key: str = Header(..., alias="X-Governance-Key"),
    x_trace_id: Optional[str] = Header(None, alias="X-Trace-Id"),
    x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
    x_user_email: Optional[str] = Header(None, alias="X-User-Email"),
    x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
    db: Session = Depends(get_db),
) -> Any:
    ctx = await _run_pre_flight(
        request=request, x_governance_key=x_governance_key, model_override=model, db=db,
        stream=True, trace_id=x_trace_id, user_id=x_user_id, user_email=x_user_email, user_role=x_user_role,
    )
    t_start = time.time()

    if not await _azure_limiter.try_acquire():
        raise HTTPException(
            status_code=503,
            detail="Server busy: too many concurrent requests to Azure OpenAI. Please retry shortly.",
        )
    release_tasks = BackgroundTasks()
    release_tasks.add_task(_azure_limiter.release)

    # Same-class failover only (see _stream_azure / _stream_provider_native
    # docstrings): the primary's provider class decides which streaming
    # handler runs; only candidates of that same class are eligible
    # fallbacks for this stream. Cross-class streaming failover (e.g. Azure
    # primary -> Anthropic secondary) isn't supported yet.
    primary_provider = ctx["provider"]
    primary_is_translated = needs_translation(primary_provider)
    same_class_attempts = [
        {**a, "outbound_body": {**a["outbound_body"], "stream": True}}
        for a in ctx["attempts"]
        if needs_translation(a["provider"]) == primary_is_translated
    ]

    stream_generator = (
        _stream_provider_native(
            attempts=same_class_attempts,
            request_id=ctx["request_id"],
            org_id=ctx["org_id"],
            project_id=ctx["project_id"],
            key_id=ctx["key_id"],
            messages=ctx["forward_body"].get("messages", []),
            source_ip=ctx["source_ip"],
            user_agent=ctx["user_agent"],
            db=db,
            t_start=t_start,
            ctx=ctx,
        )
        if primary_is_translated else
        _stream_azure(
            attempts=same_class_attempts,
            request_id=ctx["request_id"],
            org_id=ctx["org_id"],
            project_id=ctx["project_id"],
            key_id=ctx["key_id"],
            input_tokens=ctx["input_tokens"],
            messages=ctx["forward_body"].get("messages", []),
            source_ip=ctx["source_ip"],
            user_agent=ctx["user_agent"],
            db=db,
            t_start=t_start,
            ctx=ctx,
        )
    )
    return StreamingResponse(
        stream_generator,
        media_type="text/event-stream",
        background=release_tasks,
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
        # Count distinct pipeline groups (parent + its child LLM calls count
        # once), not raw call rows, while token/cost sums below still cover
        # every call — real usage/spend stays accurate.
        func.count(
            func.distinct(func.coalesce(AiRequest.parent_request_id, AiRequest.request_id))
        ).label("total_requests"),
        func.sum(RequestCost.input_tokens).label("input_tokens"),
        func.sum(RequestCost.output_tokens).label("output_tokens"),
        func.sum(RequestCost.total_tokens).label("total_tokens"),
        func.sum(RequestCost.input_token_cost).label("input_cost"),
        func.sum(RequestCost.output_token_cost).label("output_cost"),
        func.sum(RequestCost.llm_cost).label("llm_cost"),
        func.sum(RequestCost.total_cost).label("total_cost"),
    ).join(AiRequest, AiRequest.request_id == RequestCost.request_id)
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
    project_id: Optional[str] = Query(None),
    provider: Optional[str] = Query(None),
    model_name: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    cutoff = datetime.utcnow().date() - timedelta(days=days - 1)
    # Requests that never reached a terminal status (crashed/timed out before
    # the handler could mark them success/failed/partial/blocked) stay stuck
    # at "pending" forever. Treat any pending row older than this as failed.
    stale_pending_cutoff = datetime.utcnow() - timedelta(minutes=5)

    cost_q = db.query(
        # One pipeline (parent + child LLM calls) counts as one request;
        # token/cost sums still cover every call.
        func.count(
            func.distinct(func.coalesce(AiRequest.parent_request_id, AiRequest.request_id))
        ).label("total_requests"),
        func.sum(RequestCost.input_tokens).label("prompt_tokens"),
        func.sum(RequestCost.output_tokens).label("completion_tokens"),
        func.sum(RequestCost.total_tokens).label("total_tokens"),
        func.sum(RequestCost.llm_cost).label("llm_cost"),
        func.sum(RequestCost.total_cost).label("total_cost"),
    ).join(AiRequest, AiRequest.request_id == RequestCost.request_id).filter(
        func.date(RequestCost.created_at) >= cutoff
    )
    if org_id:
        cost_q = cost_q.filter(RequestCost.org_id == org_id)
    if project_id:
        cost_q = cost_q.filter(RequestCost.project_id == project_id)
    if provider:
        cost_q = cost_q.filter(RequestCost.provider == provider)
    if model_name:
        cost_q = cost_q.filter(RequestCost.model_name == model_name)
    cost = cost_q.first()

    req_q = db.query(
        func.count(
            func.distinct(func.coalesce(AiRequest.parent_request_id, AiRequest.request_id))
        ).label("total"),
        func.sum(
            cast(
                (AiRequest.parent_request_id.is_(None)) & (AiRequest.request_status == "success"),
                Integer,
            )
        ).label("completed"),
        func.sum(
            cast(
                (AiRequest.parent_request_id.is_(None))
                & (
                    (AiRequest.request_status == "failed")
                    | (
                        (AiRequest.request_status == "pending")
                        & (AiRequest.created_at < stale_pending_cutoff)
                    )
                ),
                Integer,
            )
        ).label("failed"),
        func.sum(
            cast(
                (AiRequest.parent_request_id.is_(None)) & (AiRequest.request_status == "partial"),
                Integer,
            )
        ).label("partial"),
        func.sum(
            cast(
                (AiRequest.parent_request_id.is_(None)) & (AiRequest.request_status == "blocked"),
                Integer,
            )
        ).label("blocked"),
    ).filter(func.date(AiRequest.created_at) >= cutoff)
    if org_id:
        req_q = req_q.filter(AiRequest.org_id == org_id)
    if project_id:
        req_q = req_q.filter(AiRequest.project_id == project_id)
    if provider:
        req_q = req_q.filter(AiRequest.provider == provider)
    if model_name:
        req_q = req_q.filter(AiRequest.model_name == model_name)
    req_stats = req_q.first()
    total_reqs = int(req_stats.total or 0)
    completed = int(req_stats.completed or 0)
    failed = int(req_stats.failed or 0)
    partial = int(req_stats.partial or 0)
    blocked = int(req_stats.blocked or 0)

    latency_q = db.query(func.avg(AiResponse.latency_ms)).filter(
        func.date(AiResponse.created_at) >= cutoff,
    )
    if org_id:
        latency_q = latency_q.filter(AiResponse.org_id == org_id)
    if project_id:
        latency_q = latency_q.filter(AiResponse.project_id == project_id)
    if provider:
        latency_q = latency_q.filter(AiResponse.provider == provider)
    if model_name:
        latency_q = latency_q.filter(AiResponse.model_name == model_name)
    avg_latency = latency_q.scalar() or 0

    pii_q = db.query(func.count(AiRequest.id)).filter(
        AiRequest.pii_detected == True,
        AiRequest.parent_request_id.is_(None),
        func.date(AiRequest.created_at) >= cutoff,
    )
    if org_id:
        pii_q = pii_q.filter(AiRequest.org_id == org_id)
    if project_id:
        pii_q = pii_q.filter(AiRequest.project_id == project_id)
    if provider:
        pii_q = pii_q.filter(AiRequest.provider == provider)
    if model_name:
        pii_q = pii_q.filter(AiRequest.model_name == model_name)
    pii_detections = pii_q.scalar() or 0

    success_rate = round((completed / total_reqs * 100), 2) if total_reqs > 0 else 0

    return {
        "total_requests": total_reqs,
        "completed": completed,
        "failed": failed,
        "partial": partial,
        "blocked": blocked,
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
    project_id: Optional[str] = Query(None),
    provider: Optional[str] = Query(None),
    model_name: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> list[dict]:
    cutoff = datetime.utcnow().date() - timedelta(days=days - 1)

    q = db.query(
        func.date(RequestCost.created_at).label("date"),
        func.count(
            func.distinct(func.coalesce(AiRequest.parent_request_id, AiRequest.request_id))
        ).label("total_requests"),
        func.sum(RequestCost.total_tokens).label("total_tokens"),
        func.sum(RequestCost.input_tokens).label("prompt_tokens"),
        func.sum(RequestCost.output_tokens).label("completion_tokens"),
        func.sum(RequestCost.llm_cost).label("llm_cost"),
        func.sum(RequestCost.total_cost).label("total_cost"),
    ).join(AiRequest, AiRequest.request_id == RequestCost.request_id).filter(
        func.date(RequestCost.created_at) >= cutoff
    )
    if org_id:
        q = q.filter(RequestCost.org_id == org_id)
    if project_id:
        q = q.filter(RequestCost.project_id == project_id)
    if provider:
        q = q.filter(RequestCost.provider == provider)
    if model_name:
        q = q.filter(RequestCost.model_name == model_name)

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
            "prompt_tokens": r.prompt_tokens or 0,
            "completion_tokens": r.completion_tokens or 0,
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
    project_id: Optional[str] = Query(None),
    provider: Optional[str] = Query(None),
    model_name: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    from sqlalchemy import text as _text

    cutoff = (datetime.utcnow().date() - timedelta(days=days - 1)).isoformat()

    org_filter = "AND org_id = :org_id" if org_id else ""
    project_filter = "AND project_id = :project_id" if project_id else ""
    provider_filter = "AND provider = :provider" if provider else ""
    model_filter = "AND model_name = :model_name" if model_name else ""

    params = {"cutoff": cutoff}
    if org_id:
        params["org_id"] = org_id
    if project_id:
        params["project_id"] = project_id
    if provider:
        params["provider"] = provider
    if model_name:
        params["model_name"] = model_name

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
          AND parent_request_id IS NULL
          AND DATE(created_at) >= :cutoff
          {org_filter}
          {project_filter}
          {provider_filter}
          {model_filter}
        GROUP BY unnested_type
        ORDER BY count DESC
    """), params).fetchall()

    action_rows = db.execute(_text(f"""
        SELECT pii_action_taken AS action, COUNT(*) AS count
        FROM ai_requests
        WHERE pii_detected = TRUE
          AND pii_action_taken IS NOT NULL
          AND parent_request_id IS NULL
          AND DATE(created_at) >= :cutoff
          {org_filter}
          {project_filter}
          {provider_filter}
          {model_filter}
        GROUP BY pii_action_taken
        ORDER BY count DESC
    """), params).fetchall()

    total_pii = sum(r.count for r in action_rows)

    return {
        "total_pii_requests": total_pii,
        "pii_type_breakdown": [{"pii_type": r.pii_type, "count": r.count} for r in type_rows],
        "action_breakdown": [{"action": r.action, "count": r.count} for r in action_rows],
    }


@router.get("/stats/tool-call-reliability")
def proxy_stats_tool_call_reliability(
    *,
    org_id: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Per-model breakdown of tool-equipped requests vs. requests where the
    model actually invoked a tool. A model with tools provided but a high
    zero-tool-call rate is unreliable for agentic/tool-calling workloads —
    this is the pattern behind silent agent failures that hallucinate an
    excuse instead of calling any tool.
    """
    cutoff = datetime.utcnow().date() - timedelta(days=days - 1)

    q = (
        db.query(
            AiRequest.model_name.label("model"),
            func.count(AiRequest.id).label("tool_equipped_requests"),
            func.sum(cast(AiResponse.num_tool_calls > 0, Integer)).label("requests_with_tool_calls"),
        )
        .join(AiResponse, AiResponse.request_id == AiRequest.request_id)
        .filter(
            AiRequest.has_tool_definitions.is_(True),
            func.date(AiRequest.created_at) >= cutoff,
        )
    )
    if org_id:
        q = q.filter(AiRequest.org_id == org_id)

    rows = q.group_by(AiRequest.model_name).order_by(func.count(AiRequest.id).desc()).all()

    result = []
    for r in rows:
        equipped = r.tool_equipped_requests or 0
        with_calls = r.requests_with_tool_calls or 0
        zero_calls = equipped - with_calls
        result.append({
            "model": r.model,
            "tool_equipped_requests": equipped,
            "requests_with_tool_calls": with_calls,
            "requests_with_zero_tool_calls": zero_calls,
            "zero_tool_call_rate_pct": round((zero_calls / equipped) * 100, 1) if equipped else 0.0,
        })
    return result


# ---------------------------------------------------------------------------
# Request search — dashboard traceability
# ---------------------------------------------------------------------------

@router.get("/v1/requests")
def list_proxy_requests(
    *,
    request_id: Optional[str] = None,
    org_id: Optional[str] = None,
    project_id: Optional[str] = None,
    user_id: Optional[str] = None,
    request_type: Optional[str] = None,
    status: Optional[str] = None,
    pii_only: Optional[bool] = None,
    pii_severity: Optional[str] = None,
    pii_action_taken: Optional[str] = None,
    failure_only: Optional[bool] = None,
    failure_code: Optional[str] = None,
    trace_id: Optional[str] = None,
    group_by: Optional[str] = None,
    parent_request_id: Optional[str] = None,
    include_children: bool = False,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
) -> dict:
    # Raw SQL avoids ORM column-mapping failures when proxy-layer columns
    # (governance_key_id, source_ip, deployment_name, completed_at) have not
    # yet been applied to the deployed DB from schema_clean.sql.
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
    if user_id:
        filters.append("r.user_id = :user_id")
        count_filters.append("user_id = :user_id")
        params["user_id"] = user_id
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
    if trace_id:
        filters.append("r.trace_id = :trace_id")
        count_filters.append("trace_id = :trace_id")
        params["trace_id"] = trace_id
    if parent_request_id:
        filters.append("(r.request_id = :parent_request_id OR r.parent_request_id = :parent_request_id)")
        count_filters.append("(request_id = :parent_request_id OR parent_request_id = :parent_request_id)")
        params["parent_request_id"] = parent_request_id

    if group_by == "trace_id":
        where = ("WHERE " + " AND ".join(filters)) if filters else ""
        return _list_proxy_requests_grouped_by_trace(
            db=db, where=where, params=dict(params), limit=limit, offset=offset,
        )

    # Default view: one row per multi-call pipeline (the parent), with totals
    # summed across its child calls — a single user question that fans out
    # into several internal LLM calls is reported as one request. Pass
    # ?parent_request_id=<id> to drill into a specific group's individual
    # calls, or ?include_children=true for the old flat per-call list.
    collapse_to_parents = not (request_id or parent_request_id or include_children)
    if collapse_to_parents:
        filters.append("r.parent_request_id IS NULL")
        count_filters.append("parent_request_id IS NULL")

    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    count_where = ("WHERE " + " AND ".join(count_filters)) if count_filters else ""

    try:
        params["limit"] = limit
        params["offset"] = offset

        if collapse_to_parents:
            # Page first (filtered, indexed, capped at `limit`), then a LATERAL
            # join computes token/cost totals only for each page row's own
            # group — never scanning/aggregating the whole table — all in a
            # single round trip to the DB (cuts a network round trip vs. a
            # separate totals query, which matters most on high-latency
            # connections to a managed Postgres instance).
            rows = db.execute(
                _text(
                    f"SELECT r.request_id, r.org_id, r.project_id, r.request_type, r.model_name,"
                    f" r.input_token_estimate, r.request_status, r.created_at,"
                    f" r.pii_detected, r.pii_types, r.pii_action_taken,"
                    f" r.client_ip, r.source_ip, r.received_at, r.provider, r.source_system,"
                    f" gt.g_total_tokens, gt.g_total_cost,"
                    f" gt.g_input_tokens AS prompt_tokens,"
                    f" gt.g_output_tokens AS completion_tokens,"
                    f" gt.g_llm_cost,"
                    f" gt.g_input_cost, gt.g_output_cost,"
                    f" r.entry_point,"
                    f" r.pii_severity, r.pii_entities_detected, r.pii_entities_masked,"
                    f" r.failure_code, r.failure_reason, r.completed_at, r.request_payload,"
                    f" r.trace_id, gt.request_count,"
                    f" r.user_id, r.user_email, r.user_role,"
                    f" COUNT(*) OVER() AS total_count"
                    f" FROM ai_requests r"
                    f" LEFT JOIN LATERAL ("
                    f"   SELECT COUNT(*) AS request_count,"
                    f"   SUM(COALESCE(tu2.input_tokens, r2.input_token_estimate)) AS g_input_tokens,"
                    f"   SUM(tu2.output_tokens) AS g_output_tokens,"
                    f"   SUM(tu2.total_tokens) AS g_total_tokens,"
                    f"   SUM(rc2.total_cost) AS g_total_cost,"
                    f"   SUM(rc2.llm_cost) AS g_llm_cost,"
                    f"   SUM(rc2.input_token_cost) AS g_input_cost,"
                    f"   SUM(rc2.output_token_cost) AS g_output_cost"
                    f"   FROM ai_requests r2"
                    f"   LEFT JOIN token_usage tu2 ON tu2.request_id = r2.request_id"
                    f"   LEFT JOIN request_cost rc2 ON rc2.request_id = r2.request_id"
                    f"   WHERE COALESCE(r2.parent_request_id, r2.request_id) = r.request_id"
                    f" ) gt ON TRUE"
                    f" {where}"
                    f" ORDER BY r.created_at DESC LIMIT :limit OFFSET :offset"
                ),
                params,
            ).fetchall()
            if rows:
                total = rows[0].total_count
            else:
                total = (
                    db.execute(_text(f"SELECT COUNT(*) FROM ai_requests {count_where}"), params).scalar() or 0
                )
        else:
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
                    f" r.failure_code, r.failure_reason, r.completed_at, r.request_payload,"
                    f" r.trace_id, NULL AS request_count, r.parent_request_id,"
                    f" r.user_id, r.user_email, r.user_role,"
                    f" COUNT(*) OVER() AS total_count"
                    f" FROM ai_requests r"
                    f" LEFT JOIN token_usage tu ON tu.request_id = r.request_id"
                    f" LEFT JOIN request_cost rc ON rc.request_id = r.request_id"
                    f" {where}"
                    f" ORDER BY r.created_at DESC LIMIT :limit OFFSET :offset"
                ),
                params,
            ).fetchall()
            if rows:
                total = rows[0].total_count
            else:
                total = (
                    db.execute(_text(f"SELECT COUNT(*) FROM ai_requests {count_where}"), params).scalar() or 0
                )
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
                "received_at":      (r[13].isoformat() + "Z") if r[13] else ((r[7].isoformat() + "Z") if r[7] else None),
                "provider":         r[14],
                "source_system":    r[15],
                "user_id":          r.user_id,
                "user_email":       r.user_email,
                "user_role":        r.user_role,
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
                "trace_id":              r[31],
                "request_count":         r[32] or 1,
                "parent_request_id":     getattr(r, "parent_request_id", None),
            }
            for r in rows
        ],
    }


def _list_proxy_requests_grouped_by_trace(
    *, db: Session, where: str, params: dict, limit: int, offset: int,
) -> dict:
    """Aggregate ai_requests by trace_id — one summary row per user question
    (e.g. the classify -> tool-select -> response-generation LLM calls a
    chatbot issues for a single turn, when it sends the same X-Trace-Id on
    each). Callers expand a group by re-querying with ?trace_id=<id>.
    """
    from sqlalchemy import text as _text

    trace_where = where or "WHERE 1=1"
    trace_where += " AND r.trace_id IS NOT NULL"

    try:
        total = (
            db.execute(
                _text(
                    f"SELECT COUNT(DISTINCT r.trace_id) FROM ai_requests r {trace_where}"
                ),
                params,
            ).scalar() or 0
        )
        params["limit"] = limit
        params["offset"] = offset
        rows = db.execute(
            _text(
                f"SELECT r.trace_id,"
                f" COUNT(*) AS request_count,"
                f" MIN(r.created_at) AS first_created_at,"
                f" MAX(r.created_at) AS last_created_at,"
                f" SUM(COALESCE(tu.input_tokens, r.input_token_estimate)) AS total_input_tokens,"
                f" SUM(tu.output_tokens) AS total_output_tokens,"
                f" SUM(tu.total_tokens) AS total_tokens,"
                f" SUM(rc.total_cost) AS total_cost,"
                f" MAX(r.org_id) AS org_id,"
                f" MAX(r.project_id) AS project_id"
                f" FROM ai_requests r"
                f" LEFT JOIN token_usage tu ON tu.request_id = r.request_id"
                f" LEFT JOIN request_cost rc ON rc.request_id = r.request_id"
                f" {trace_where}"
                f" GROUP BY r.trace_id"
                f" ORDER BY MAX(r.created_at) DESC LIMIT :limit OFFSET :offset"
            ),
            params,
        ).fetchall()
    except Exception as exc:
        _log.warning("list_proxy_requests grouped query failed: %s", exc)
        return {"total": 0, "offset": offset, "items": []}

    return {
        "total": total,
        "offset": offset,
        "items": [
            {
                "trace_id":           r[0],
                "request_count":      r[1],
                "first_created_at":   r[2].isoformat() if r[2] else None,
                "last_created_at":    r[3].isoformat() if r[3] else None,
                "total_input_tokens": r[4],
                "total_output_tokens": r[5],
                "total_tokens":       r[6],
                "total_cost":         float(r[7]) if r[7] is not None else None,
                "org_id":             r[8],
                "project_id":         r[9],
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
                " r.messages, r.prompt_text, r.sanitized_prompt_text,"
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
        "original_messages":     row[12] or [],
        "original_prompt_text":  row[13],
        "sanitized_prompt_text": row[14],
        "response_text":         row[15],
        "output_pii_types":      row[16] or [],
    }
