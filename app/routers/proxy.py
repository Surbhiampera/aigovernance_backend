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
import time
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, AsyncIterator, Optional

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
from app.services.governance_key_service import verify_governance_key
from app.services.pii_engine import scan_and_mask
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
        input_tokens=input_tokens,
        source_ip=source_ip,
        user_agent=user_agent,
        status="pending",
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
) -> tuple[Decimal, Decimal, Decimal]:
    from app.models import ModelPricing
    from app.services.ai_model_pricing import calculate_token_cost

    result = calculate_token_cost(model, input_tokens, output_tokens)
    input_cost = Decimal(str(result.get("input_token_cost", 0)))
    output_cost = Decimal(str(result.get("output_token_cost", 0)))

    if input_cost == 0 and output_cost == 0 and (input_tokens + output_tokens) > 0:
        db_price = (
            db.query(ModelPricing)
            .filter(ModelPricing.model_name == model)
            .first()
        )
        if db_price:
            input_cost = (
                Decimal(str(input_tokens)) / Decimal("1000")
                * Decimal(str(db_price.input_cost_per_1k))
            ).quantize(Decimal("0.000001"))
            output_cost = (
                Decimal(str(output_tokens)) / Decimal("1000")
                * Decimal(str(db_price.output_cost_per_1k))
            ).quantize(Decimal("0.000001"))

    total_cost = (input_cost + output_cost).quantize(Decimal("0.000001"))
    return input_cost, output_cost, total_cost


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
) -> None:
    input_cost, output_cost, total_cost = _calculate_cost(
        db=db,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    db.add(AiResponse(
        response_id=_new_response_id(),
        request_id=request_id,
        response_payload=response_payload,
        output_tokens=output_tokens,
        finish_reason=finish_reason,
        response_time_ms=latency_ms,
        status=status,
        created_at=datetime.utcnow(),
    ))

    db.add(TokenUsage(
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        model_name=model,
        deployment_name=deployment,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
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
        input_cost=input_cost,
        output_cost=output_cost,
        total_cost=total_cost,
        currency="USD",
        created_at=datetime.utcnow(),
    ))

    req_row = db.query(AiRequest).filter(AiRequest.request_id == request_id).first()
    if req_row:
        req_row.status = status
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

    latency_ms = int((time.time() - t_start) * 1000)

    combined = "".join(
        json.loads(c).get("choices", [{}])[0].get("delta", {}).get("content") or ""
        for c in chunks_raw
        if _is_valid_json(c)
    )
    output_tokens = count_tokens(text=combined, model=model) if combined else 0

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

    # Step 2: Parse body
    request_id = _new_request_id()
    try:
        body: dict = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body.")

    messages: list[dict] = body.get("messages", [])
    stream: bool = bool(body.get("stream", False))
    deployment = get_azure_openai_deployment()
    api_version = get_azure_openai_api_version()
    model: str = body.get("model") or deployment

    if not deployment:
        raise HTTPException(status_code=503, detail="Azure deployment not configured on this server.")

    # Step 4: PII scan
    clean_messages = _scan_messages(
        messages=messages,
        org_id=org_id,
        project_id=project_id,
        db=db,
    )
    forward_body = {k: v for k, v in body.items() if k != "stream"}
    forward_body["messages"] = clean_messages

    # Step 5: Count input tokens, store AiRequest
    prompt_text = " ".join(
        m.get("content", "") for m in clean_messages
        if isinstance(m.get("content"), str)
    )
    input_tokens = count_tokens(text=prompt_text, model=model)

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
            detail=str(exc),
        )
        db.commit()
        raise HTTPException(status_code=exc.response.status_code, detail=str(exc))
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Azure OpenAI unreachable: {exc}")

    latency_ms = int((time.time() - t_start) * 1000)
    response_data: dict = azure_resp.json()

    # Step 8: Extract output tokens
    usage = response_data.get("usage", {})
    output_tokens = int(usage.get("completion_tokens", 0))
    if output_tokens == 0:
        output_text = "".join(
            (c.get("message", {}).get("content") or "")
            for c in response_data.get("choices", [])
        )
        output_tokens = count_tokens(text=output_text, model=model)

    finish_reason: Optional[str] = None
    choices = response_data.get("choices", [])
    if choices:
        finish_reason = choices[0].get("finish_reason")

    # Steps 9-10: Store cost + audit
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
                "request_id": r.request_id,
                "org_id": r.org_id,
                "project_id": r.project_id,
                "request_type": r.request_type,
                "model_name": r.model_name,
                "deployment_name": r.deployment_name,
                "input_tokens": r.input_tokens,
                "status": r.status,
                "source_ip": r.source_ip,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            }
            for r in rows
        ],
    }
