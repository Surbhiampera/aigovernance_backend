"""Proxy / Wrapper Layer router.

Exposes an OpenAI-compatible endpoint that external teams point their
SDK at instead of calling OpenAI directly:

    client = OpenAI(
        api_key="sk-openai-...",
        base_url="http://your-server/proxy/openai",
        default_headers={"X-Governance-Key": "gov-..."},
    )

Request lifecycle (10 steps from the governance doc):
  1  Receive request, resolve org/project from X-Governance-Key header
  2  Generate request_id, request_type, capture metadata + timestamps
  3  Route to this proxy server (we ARE the proxy)
  4  PII scan — mask or block based on pii_policies
  5  Count input tokens
  6  Forward sanitized request to real AI provider
  7  AI model processes
  8  Receive response, count output tokens
  9  Calculate cost
 10  Store all data (ai_requests, ai_responses, token_usage, request_cost, audit_logs)
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.services.governance_key_service import verify_governance_key
from app.services.pii_engine import scan_and_mask
from app.services.audit_service import log_pii_detection, log_request_blocked, log_event
from app.services.token_counter import count_tokens
from app.models import (
    AiRequest, AiResponse, TokenUsage, RequestCost, RouteExecution,
    ProviderConfig, ProjectModelUsage,
)

router = APIRouter(prefix="/proxy", tags=["proxy"])

# ---------------------------------------------------------------------------
# Helper: generate IDs
# ---------------------------------------------------------------------------
def _req_id() -> str:
    return f"req-{uuid.uuid4().hex[:20]}"

def _resp_id() -> str:
    return f"resp-{uuid.uuid4().hex[:20]}"

def _exec_id() -> str:
    return f"exec-{uuid.uuid4().hex[:20]}"

def _tu_id() -> str:
    return f"tu-{uuid.uuid4().hex[:20]}"

def _cost_id() -> str:
    return f"cost-{uuid.uuid4().hex[:20]}"


# ---------------------------------------------------------------------------
# Helper: resolve provider config for the org
# ---------------------------------------------------------------------------
def _get_provider_config(db: Session, org_id: str, provider: str) -> Optional[ProviderConfig]:
    return (
        db.query(ProviderConfig)
        .filter(
            ProviderConfig.org_id == org_id,
            ProviderConfig.provider == provider,
            ProviderConfig.is_active == True,
        )
        .first()
    )


# ---------------------------------------------------------------------------
# Helper: classify request_type from URL path
# ---------------------------------------------------------------------------
def _classify_from_path(path: str, body: dict) -> str:
    if "chat/completions" in path:
        if body.get("tools") or body.get("functions"):
            return "tool_call"
        return "chat_completion"
    if "embeddings" in path:
        return "embedding"
    if "images/generations" in path or "images/edits" in path or "images/variations" in path:
        return "image_generation"
    if "audio/transcriptions" in path:
        return "audio_transcription"
    if "audio/translations" in path:
        return "audio_translation"
    if "audio/speech" in path:
        return "text_to_speech"
    if "completions" in path:
        return "completion"
    if "moderations" in path:
        return "moderation"
    if "fine_tuning" in path or "fine-tuning" in path:
        return "fine_tuning"
    return "api_call"


# ---------------------------------------------------------------------------
# Helper: extract text from any request type for PII scan + token count
# ---------------------------------------------------------------------------
def _extract_text_universal(body: dict, path: str) -> tuple[str, str]:
    """Returns (prompt_text, system_prompt) for any OpenAI endpoint."""
    if not body:
        return "", ""

    # Chat completions — parse messages array
    if "chat/completions" in path or "messages" in body:
        prompt_parts, system_parts = [], []
        for msg in body.get("messages", []):
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    c.get("text", "") for c in content
                    if isinstance(c, dict) and c.get("type") == "text"
                )
            if role == "system":
                system_parts.append(content)
            else:
                prompt_parts.append(content)
        return "\n".join(prompt_parts), "\n".join(system_parts)

    # Embeddings — input can be string or list of strings
    if "embeddings" in path:
        inp = body.get("input", "")
        if isinstance(inp, list):
            return " ".join(str(i) for i in inp), ""
        return str(inp), ""

    # Images — prompt field
    if "images" in path:
        return body.get("prompt", ""), ""

    # Moderations
    if "moderations" in path:
        inp = body.get("input", "")
        if isinstance(inp, list):
            return " ".join(inp), ""
        return str(inp), ""

    # Legacy completions
    if "completions" in path:
        prompt = body.get("prompt", "")
        if isinstance(prompt, list):
            return " ".join(prompt), ""
        return str(prompt), ""

    # TTS — input is the text to speak
    if "audio/speech" in path:
        return body.get("input", ""), ""

    return "", ""


# ---------------------------------------------------------------------------
# Helper: apply PII masking to outgoing request body
# ---------------------------------------------------------------------------
def _mask_body_pii(body: dict, path: str, org_id: str, db) -> dict:
    if not body:
        return body

    if "chat/completions" in path or "messages" in body:
        sanitized_messages = []
        for msg in body.get("messages", []):
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                result = scan_and_mask(content, org_id=org_id, db=db)
                msg = {**msg, "content": result.sanitized_text}
            sanitized_messages.append(msg)
        return {**body, "messages": sanitized_messages}

    if "embeddings" in path:
        inp = body.get("input", "")
        if isinstance(inp, str):
            result = scan_and_mask(inp, org_id=org_id, db=db)
            return {**body, "input": result.sanitized_text}
        if isinstance(inp, list):
            masked = [scan_and_mask(str(i), org_id=org_id, db=db).sanitized_text for i in inp]
            return {**body, "input": masked}

    if "images" in path and body.get("prompt"):
        result = scan_and_mask(body["prompt"], org_id=org_id, db=db)
        return {**body, "prompt": result.sanitized_text}

    return body


# ---------------------------------------------------------------------------
# Helper: calculate cost from token counts + model pricing
# ---------------------------------------------------------------------------
def _calculate_cost(db: Session, provider: str, model_name: str, prompt_tokens: int, completion_tokens: int) -> dict:
    from app.models import ModelPricing
    pricing = (
        db.query(ModelPricing)
        .filter(ModelPricing.model_name == model_name)
        .first()
    )
    if pricing:
        input_cost = Decimal(str(prompt_tokens)) / Decimal("1000000") * Decimal(str(pricing.input_cost_per_1k * 1000))
        output_cost = Decimal(str(completion_tokens)) / Decimal("1000000") * Decimal(str(pricing.output_cost_per_1k * 1000))
    else:
        # Fallback: GPT-4o rates
        input_cost = Decimal(str(prompt_tokens)) / Decimal("1000000") * Decimal("5.0")
        output_cost = Decimal(str(completion_tokens)) / Decimal("1000000") * Decimal("15.0")

    llm_cost = input_cost + output_cost
    return {
        "input_token_cost":  float(input_cost.quantize(Decimal("0.00000001"))),
        "output_token_cost": float(output_cost.quantize(Decimal("0.00000001"))),
        "llm_cost":          float(llm_cost.quantize(Decimal("0.00000001"))),
        "total_cost":        float(llm_cost.quantize(Decimal("0.00000001"))),
        "pricing_snapshot":  {
            "input_per_1m":  float(pricing.input_cost_per_1k * 1000) if pricing else 5.0,
            "output_per_1m": float(pricing.output_cost_per_1k * 1000) if pricing else 15.0,
        },
    }


# ---------------------------------------------------------------------------
# Universal proxy endpoint — forwards ANY OpenAI API call
# Captures: chat, embeddings, images, audio, completions, moderations, TTS
# ---------------------------------------------------------------------------
@router.api_route("/openai/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_openai_universal(
    request: Request,
    path: str,
    x_governance_key: Optional[str] = Header(None, alias="X-Governance-Key"),
    db: Session = Depends(get_db),
):
    received_at = datetime.utcnow()

    # ── Step 1: Authenticate ─────────────────────────────────────────────
    if not x_governance_key:
        raise HTTPException(status_code=401, detail="X-Governance-Key header required")

    key_info = verify_governance_key(db, x_governance_key)
    if key_info is None:
        raise HTTPException(status_code=401, detail="Invalid or expired governance key")

    org_id = key_info["org_id"]
    # X-Project-Id header lets external team tag requests to a specific project.
    # Falls back to the project baked into the governance key.
    project_id = request.headers.get("X-Project-Id") or key_info.get("project_id")
    api_key_id = key_info["key_id"]
    client_ip = request.client.host if request.client else None
    source_system = request.headers.get("X-Source-System") or request.headers.get("User-Agent", "")
    trace_id = request.headers.get("X-Trace-Id")
    request_id = _req_id()
    if not trace_id:
        trace_id = request_id

    # ── Step 2: Parse body ───────────────────────────────────────────────
    content_type = request.headers.get("content-type", "")
    is_multipart = "multipart/form-data" in content_type
    body: dict[str, Any] = {}
    raw_body: bytes = b""

    if is_multipart:
        raw_body = await request.body()
    else:
        try:
            body = await request.json()
        except Exception:
            raw_body = await request.body()

    # Force-disable streaming so we can fully capture the response for logging
    if body.get("stream"):
        body = {**body, "stream": False}

    model_name = body.get("model", "unknown")
    provider = "openai"
    request_type = _classify_from_path(path, body)
    prompt_text, system_prompt = _extract_text_universal(body, path)

    # ── Step 3: Save initial ai_request row ─────────────────────────────
    ai_req = AiRequest(
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        trace_id=trace_id,
        request_type=request_type,
        request_status="processing",
        api_key_id=api_key_id,
        client_ip=client_ip,
        user_agent=source_system,
        provider=provider,
        model_name=model_name,
        requested_model=model_name,
        source_system=source_system,
        prompt_text=prompt_text,
        system_prompt=system_prompt,
        messages=body.get("messages"),
        request_parameters={k: v for k, v in body.items() if k not in ("messages", "model")},
        num_messages=len(body.get("messages", [])),
        has_system_prompt=bool(system_prompt),
        has_tool_definitions=bool(body.get("tools") or body.get("functions")),
        received_at=received_at,
        processing_started_at=datetime.utcnow(),
    )
    db.add(ai_req)
    db.flush()

    pii_scan_at = datetime.utcnow()
    pii_result = None
    sanitized_body = body

    # ── Step 4: PII scan (text-based requests only) ──────────────────────
    if prompt_text and not is_multipart:
        full_text = "\n".join(filter(None, [system_prompt, prompt_text]))
        pii_result = scan_and_mask(full_text, org_id=org_id, db=db)

        if pii_result.pii_detected and pii_result.audit_required:
            log_pii_detection(db, org_id=org_id, project_id=project_id,
                              request_id=request_id, pii_types=pii_result.pii_types,
                              action_taken=pii_result.action_taken, actor_ip=client_ip)

        if pii_result.should_block:
            log_request_blocked(db, org_id=org_id, project_id=project_id,
                                request_id=request_id,
                                reason=f"PII: {', '.join(pii_result.pii_types)}",
                                actor_ip=client_ip)
            ai_req.request_status = "blocked"
            ai_req.pii_detected = True
            ai_req.pii_types = pii_result.pii_types
            ai_req.pii_action_taken = "block"
            db.commit()
            raise HTTPException(status_code=403, detail={
                "error": "request_blocked",
                "reason": "Request contains restricted PII",
                "pii_types": pii_result.pii_types,
            })

        if pii_result.pii_masked:
            sanitized_body = _mask_body_pii(body, path, org_id, db)

        ai_req.pii_detected = pii_result.pii_detected
        ai_req.pii_types = pii_result.pii_types if pii_result.pii_detected else None
        ai_req.pii_masked = pii_result.pii_masked
        ai_req.pii_action_taken = pii_result.action_taken
        ai_req.sanitized_prompt_text = pii_result.sanitized_text if pii_result.pii_masked else None

    # ── Step 5: Count input tokens ───────────────────────────────────────
    prompt_tokens_estimate = count_tokens((prompt_text or "") + " " + (system_prompt or ""), model_name)
    ai_req.input_token_estimate = prompt_tokens_estimate
    ai_req.prompt_char_count = len(prompt_text or "")
    db.flush()

    # ── Step 6: Get provider API key and build upstream URL ───────────────
    provider_cfg = _get_provider_config(db, org_id, provider)
    caller_api_key = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    real_api_key = (provider_cfg.api_key if provider_cfg else None) or caller_api_key

    if not real_api_key:
        raise HTTPException(status_code=400, detail="No provider API key configured for this org")

    upstream_base = (provider_cfg.base_url if provider_cfg and provider_cfg.base_url
                     else "https://api.openai.com/v1")
    upstream_url = f"{upstream_base}/{path}"

    # Build forward headers — strip governance-specific headers
    _skip = {"x-governance-key", "host", "content-length", "authorization",
              "x-source-system", "x-trace-id"}
    forward_headers: dict[str, str] = {"Authorization": f"Bearer {real_api_key}"}
    for k, v in request.headers.items():
        if k.lower() not in _skip:
            forward_headers[k] = v

    forward_at = datetime.utcnow()
    route_exec = RouteExecution(
        execution_id=_exec_id(),
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        execution_status="proxying",
        selected_provider=provider,
        selected_model=model_name,
        proxy_type="passthrough",
        upstream_url=upstream_url,
        pipeline_stages=[
            {"stage": "received", "status": "pass", "ts": received_at.isoformat(),
             "request_type": request_type},
            {"stage": "pii_scan", "status": "masked" if (pii_result and pii_result.pii_masked) else "pass",
             "ts": pii_scan_at.isoformat(),
             "pii_types": pii_result.pii_types if pii_result else []},
            {"stage": "forwarded", "status": "pass", "ts": forward_at.isoformat(),
             "upstream_url": upstream_url},
        ],
        started_at=received_at,
    )
    db.add(route_exec)
    db.flush()

    # ── Step 7: Forward to real OpenAI ───────────────────────────────────
    try:
        async with httpx.AsyncClient(timeout=120.0) as http_client:
            if is_multipart:
                upstream_resp = await http_client.post(
                    upstream_url, content=raw_body,
                    headers={**forward_headers, "content-type": content_type},
                )
            elif raw_body:
                upstream_resp = await http_client.request(
                    method=request.method, url=upstream_url,
                    content=raw_body, headers=forward_headers,
                )
            else:
                upstream_resp = await http_client.request(
                    method=request.method, url=upstream_url,
                    json=sanitized_body, headers=forward_headers,
                )
    except httpx.TimeoutException:
        ai_req.request_status = "timeout"
        db.commit()
        raise HTTPException(status_code=504, detail="Upstream AI provider timed out")
    except Exception as exc:
        ai_req.request_status = "failed"
        db.commit()
        raise HTTPException(status_code=502, detail=f"Upstream error: {str(exc)}")

    response_at = datetime.utcnow()
    latency_ms = int((response_at - forward_at).total_seconds() * 1000)

    # ── Step 8: Parse response ────────────────────────────────────────────
    upstream_content_type = upstream_resp.headers.get("content-type", "")
    is_json_response = "application/json" in upstream_content_type

    upstream_data: dict | None = None
    response_text = ""
    finish_reason = None
    tool_calls = None

    if is_json_response:
        try:
            upstream_data = upstream_resp.json()
        except Exception:
            upstream_data = None

    if upstream_resp.status_code != 200:
        ai_req.request_status = "failed"
        db.commit()
        if upstream_data:
            return JSONResponse(status_code=upstream_resp.status_code, content=upstream_data)
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            media_type=upstream_content_type,
        )

    # Extract content from JSON responses
    if upstream_data:
        choices = upstream_data.get("choices", [])
        if choices:
            choice = choices[0]
            finish_reason = choice.get("finish_reason")
            msg = choice.get("message", {})
            response_text = msg.get("content") or ""
            tool_calls = msg.get("tool_calls")
            # Legacy completions
            if not response_text:
                response_text = choice.get("text", "")

        # Embeddings — response_text is not applicable, but log dimension count
        if request_type == "embedding":
            data_items = upstream_data.get("data", [])
            response_text = f"[{len(data_items)} embedding(s) returned]"

        # Images — log URL or b64 indicator
        if request_type == "image_generation":
            data_items = upstream_data.get("data", [])
            response_text = f"[{len(data_items)} image(s) generated]"

        # Moderations
        if request_type == "moderation":
            results = upstream_data.get("results", [])
            flagged = sum(1 for r in results if r.get("flagged"))
            response_text = f"[{len(results)} item(s) moderated, {flagged} flagged]"

        # Extract token counts from response (most reliable source)
        usage = upstream_data.get("usage", {})
        prompt_tokens_final = (
            usage.get("prompt_tokens") or
            usage.get("input_tokens") or
            prompt_tokens_estimate
        )
        completion_tokens_final = (
            usage.get("completion_tokens") or
            usage.get("output_tokens") or
            count_tokens(response_text, model_name)
        )
        total_tokens = usage.get("total_tokens") or (prompt_tokens_final + completion_tokens_final)
    else:
        # Binary response (audio file, image bytes, etc.) — count from input only
        usage = {}
        prompt_tokens_final = prompt_tokens_estimate
        completion_tokens_final = 0
        total_tokens = prompt_tokens_estimate
        response_text = f"[binary response: {len(upstream_resp.content)} bytes]"

    # ── Step 9: Save response + token usage + cost ────────────────────────
    response_id = _resp_id()
    ai_resp = AiResponse(
        response_id=response_id,
        request_id=request_id,
        org_id=org_id,
        project_id=project_id,
        provider=provider,
        model_name=upstream_data.get("model", model_name) if upstream_data else model_name,
        response_status="completed",
        finish_reason=finish_reason,
        response_payload=upstream_data,
        response_text=response_text,
        tool_calls=tool_calls,
        output_char_count=len(response_text),
        num_tool_calls=len(tool_calls) if tool_calls else 0,
        provider_request_id=upstream_data.get("id") if upstream_data else None,
        response_started_at=forward_at,
        response_completed_at=response_at,
        latency_ms=latency_ms,
    )
    db.add(ai_resp)
    db.flush()

    cost_data = _calculate_cost(db, provider, model_name, prompt_tokens_final, completion_tokens_final)

    db.add(TokenUsage(
        token_usage_id=_tu_id(),
        request_id=request_id,
        response_id=response_id,
        org_id=org_id,
        project_id=project_id,
        provider=provider,
        model_name=model_name,
        prompt_tokens=prompt_tokens_final,
        completion_tokens=completion_tokens_final,
        total_tokens=total_tokens,
        input_tokens=prompt_tokens_final,
        output_tokens=completion_tokens_final,
        cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
        raw_usage=usage,
    ))

    db.add(RequestCost(
        cost_id=_cost_id(),
        request_id=request_id,
        response_id=response_id,
        org_id=org_id,
        project_id=project_id,
        provider=provider,
        model_name=model_name,
        input_token_cost=cost_data["input_token_cost"],
        output_token_cost=cost_data["output_token_cost"],
        llm_cost=cost_data["llm_cost"],
        total_cost=cost_data["total_cost"],
        adjusted_total_cost=cost_data["total_cost"],
        pricing_snapshot=cost_data["pricing_snapshot"],
        cost_model_type="per_token",
        currency="USD",
    ))

    # ── Upsert ProjectModelUsage (daily aggregate per project × model) ───
    _today = received_at.date()
    _pid_filter = (
        ProjectModelUsage.project_id == project_id
        if project_id is not None
        else ProjectModelUsage.project_id.is_(None)
    )
    pmu = db.query(ProjectModelUsage).filter(
        ProjectModelUsage.org_id == org_id,
        _pid_filter,
        ProjectModelUsage.model_name == model_name,
        ProjectModelUsage.date == _today,
    ).first()
    if pmu:
        pmu.call_count = (pmu.call_count or 0) + 1
        pmu.total_prompt_tokens = (pmu.total_prompt_tokens or 0) + prompt_tokens_final
        pmu.total_completion_tokens = (pmu.total_completion_tokens or 0) + completion_tokens_final
        pmu.total_tokens = (pmu.total_tokens or 0) + total_tokens
        pmu.input_tokens = (pmu.input_tokens or 0) + prompt_tokens_final
        pmu.output_tokens = (pmu.output_tokens or 0) + completion_tokens_final
        pmu.input_token_cost = float(pmu.input_token_cost or 0) + cost_data["input_token_cost"]
        pmu.output_token_cost = float(pmu.output_token_cost or 0) + cost_data["output_token_cost"]
        pmu.total_token_cost = float(pmu.total_token_cost or 0) + cost_data["llm_cost"]
        pmu.total_cost = float(pmu.total_cost or 0) + cost_data["total_cost"]
        pmu.success_count = (pmu.success_count or 0) + 1
    else:
        db.add(ProjectModelUsage(
            org_id=org_id,
            project_id=project_id,
            model_name=model_name,
            provider=provider,
            date=_today,
            call_count=1,
            total_prompt_tokens=prompt_tokens_final,
            total_completion_tokens=completion_tokens_final,
            total_tokens=total_tokens,
            input_tokens=prompt_tokens_final,
            output_tokens=completion_tokens_final,
            input_token_cost=cost_data["input_token_cost"],
            output_token_cost=cost_data["output_token_cost"],
            total_token_cost=cost_data["llm_cost"],
            total_cost=cost_data["total_cost"],
            success_count=1,
        ))

    # ── Step 10: Finalise ────────────────────────────────────────────────
    ai_req.request_status = "completed"
    route_exec.execution_status = "completed"
    route_exec.upstream_request_id = upstream_data.get("id") if upstream_data else None
    route_exec.total_latency_ms = latency_ms
    route_exec.upstream_latency_ms = latency_ms
    route_exec.completed_at = response_at
    route_exec.pipeline_stages.append({
        "stage": "completed", "status": "pass",
        "ts": datetime.utcnow().isoformat(),
        "prompt_tokens": prompt_tokens_final,
        "completion_tokens": completion_tokens_final,
        "total_tokens": total_tokens,
        "total_cost": cost_data["total_cost"],
        "latency_ms": latency_ms,
    })

    log_event(
        db,
        org_id=org_id,
        project_id=project_id,
        audit_category="request",
        audit_action="completed",
        audit_status="success",
        actor_type="api_key",
        actor_id=api_key_id,
        actor_ip=client_ip,
        entity_type="ai_request",
        entity_id=request_id,
        request_id=request_id,
        change_summary=(
            f"type={request_type} model={model_name} "
            f"prompt_tokens={prompt_tokens_final} completion_tokens={completion_tokens_final} "
            f"cost=${cost_data['total_cost']:.8f} latency={latency_ms}ms"
        ),
        metadata={
            "request_type": request_type,
            "prompt_tokens": prompt_tokens_final,
            "completion_tokens": completion_tokens_final,
            "total_tokens": total_tokens,
            "total_cost": cost_data["total_cost"],
            "latency_ms": latency_ms,
        },
    )

    db.commit()

    # Return response to caller (passthrough)
    if upstream_data:
        return JSONResponse(content=upstream_data)
    return Response(
        content=upstream_resp.content,
        status_code=200,
        media_type=upstream_content_type,
    )


# ---------------------------------------------------------------------------
# Governance Key management endpoints
# ---------------------------------------------------------------------------
@router.post("/keys")
def create_key(
    payload: dict,
    db: Session = Depends(get_db),
):
    """Create a governance key for an external team.
    Body: {org_id, key_name, project_id (optional), expires_at (optional ISO string)}
    """
    from app.services.governance_key_service import create_governance_key

    org_id = payload.get("org_id")
    key_name = payload.get("key_name")
    if not org_id or not key_name:
        raise HTTPException(status_code=400, detail="org_id and key_name are required")

    expires_at = None
    if payload.get("expires_at"):
        from datetime import datetime
        expires_at = datetime.fromisoformat(payload["expires_at"])

    result = create_governance_key(
        db,
        org_id=org_id,
        project_id=payload.get("project_id"),
        key_name=key_name,
        created_by=payload.get("created_by"),
        expires_at=expires_at,
    )
    db.commit()
    return result


@router.get("/keys/{org_id}")
def list_keys(org_id: str, db: Session = Depends(get_db)):
    from app.services.governance_key_service import list_governance_keys
    return list_governance_keys(db, org_id)


@router.delete("/keys/{key_id}")
def revoke_key(key_id: str, db: Session = Depends(get_db)):
    from app.services.governance_key_service import revoke_governance_key
    ok = revoke_governance_key(db, key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Key not found")
    db.commit()
    return {"revoked": True, "key_id": key_id}


# ---------------------------------------------------------------------------
# Provider config management endpoints
# ---------------------------------------------------------------------------
@router.post("/provider-configs")
def create_provider_config(payload: dict, db: Session = Depends(get_db)):
    """Store an AI provider API key for an org.
    Body: {org_id, provider, api_key, base_url (optional), model_allowlist (optional)}
    """
    import uuid as _uuid

    required = ("org_id", "provider", "api_key")
    for f in required:
        if not payload.get(f):
            raise HTTPException(status_code=400, detail=f"{f} is required")

    raw_key = payload["api_key"]
    hint = raw_key[:6] + "..." + raw_key[-4:] if len(raw_key) > 10 else "***"

    row = ProviderConfig(
        config_id=f"pc-{_uuid.uuid4().hex[:16]}",
        org_id=payload["org_id"],
        project_id=payload.get("project_id"),
        provider=payload["provider"],
        api_key=raw_key,           # TODO: encrypt before storing in production
        api_key_hint=hint,
        base_url=payload.get("base_url"),
        model_allowlist=payload.get("model_allowlist"),
        is_active=True,
        created_by=payload.get("created_by"),
    )
    db.add(row)
    db.commit()

    return {
        "config_id":      row.config_id,
        "org_id":         row.org_id,
        "provider":       row.provider,
        "api_key_hint":   hint,
        "base_url":       row.base_url,
        "model_allowlist": row.model_allowlist,
        "is_active":      True,
    }


@router.get("/provider-configs/{org_id}")
def list_provider_configs(org_id: str, db: Session = Depends(get_db)):
    rows = db.query(ProviderConfig).filter(ProviderConfig.org_id == org_id).all()
    return [
        {
            "config_id":      r.config_id,
            "provider":       r.provider,
            "api_key_hint":   r.api_key_hint,
            "base_url":       r.base_url,
            "model_allowlist": r.model_allowlist,
            "is_active":      r.is_active,
            "created_at":     r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.delete("/provider-configs/{config_id}")
def delete_provider_config(config_id: str, db: Session = Depends(get_db)):
    row = db.query(ProviderConfig).filter(ProviderConfig.config_id == config_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Config not found")
    row.is_active = False
    db.commit()
    return {"deactivated": True, "config_id": config_id}


# ---------------------------------------------------------------------------
# PII policy management endpoints
# ---------------------------------------------------------------------------
@router.get("/pii-policies/{org_id}")
def list_pii_policies(org_id: str, db: Session = Depends(get_db)):
    from app.models import PiiPolicy
    rows = (
        db.query(PiiPolicy)
        .filter((PiiPolicy.org_id == org_id) | (PiiPolicy.org_id == None))
        .order_by(PiiPolicy.priority.desc())
        .all()
    )
    return [
        {
            "policy_id":   r.policy_id,
            "org_id":      r.org_id,
            "pii_type":    r.pii_type,
            "risk_level":  r.risk_level,
            "action":      r.action,
            "mask_pattern": r.mask_pattern,
            "priority":    r.priority,
            "is_active":   r.is_active,
            "description": r.description,
        }
        for r in rows
    ]


@router.post("/pii-policies")
def create_pii_policy(payload: dict, db: Session = Depends(get_db)):
    import uuid as _uuid
    from app.models import PiiPolicy

    required = ("pii_type", "risk_level", "action")
    for f in required:
        if not payload.get(f):
            raise HTTPException(status_code=400, detail=f"{f} is required")

    row = PiiPolicy(
        policy_id=f"pp-{_uuid.uuid4().hex[:16]}",
        org_id=payload.get("org_id"),
        pii_type=payload["pii_type"],
        risk_level=payload["risk_level"],
        action=payload["action"],
        mask_pattern=payload.get("mask_pattern", f"[{payload['pii_type'].upper()}]"),
        priority=payload.get("priority", 0),
        description=payload.get("description"),
        is_active=True,
    )
    db.add(row)
    db.commit()
    return {"policy_id": row.policy_id, "pii_type": row.pii_type, "action": row.action}


# ---------------------------------------------------------------------------
# Reporting endpoints — all data from proxy tables only
# ---------------------------------------------------------------------------
from datetime import timedelta
from sqlalchemy import func, case


def _date_filter(q, model, org_id, days):
    if days and days > 0:
        cutoff = datetime.utcnow() - timedelta(days=int(days))
        q = q.filter(model.received_at >= cutoff)
    if org_id:
        q = q.filter(model.org_id == org_id)
    return q


@router.get("/stats/overview")
def proxy_stats_overview(
    org_id: Optional[str] = None,
    days: int = 30,
    db: Session = Depends(get_db),
):
    from app.models import AiRequest, AiResponse, TokenUsage, RequestCost

    cutoff = datetime.utcnow() - timedelta(days=days)

    req_q = db.query(
        func.count(AiRequest.id).label("total"),
        func.sum(case((AiRequest.request_status == "completed", 1), else_=0)).label("completed"),
        func.sum(case((AiRequest.request_status == "blocked", 1), else_=0)).label("blocked"),
        func.sum(case((AiRequest.pii_detected == True, 1), else_=0)).label("pii_hits"),
    ).filter(AiRequest.received_at >= cutoff)
    if org_id:
        req_q = req_q.filter(AiRequest.org_id == org_id)
    rs = req_q.first()

    cost_q = db.query(
        func.sum(RequestCost.total_cost).label("total_cost"),
        func.sum(RequestCost.llm_cost).label("llm_cost"),
    ).join(AiRequest, RequestCost.request_id == AiRequest.request_id).filter(AiRequest.received_at >= cutoff)
    if org_id:
        cost_q = cost_q.filter(RequestCost.org_id == org_id)
    cs = cost_q.first()

    tok_q = db.query(
        func.sum(TokenUsage.total_tokens).label("total_tokens"),
        func.sum(TokenUsage.prompt_tokens).label("prompt_tokens"),
        func.sum(TokenUsage.completion_tokens).label("completion_tokens"),
    ).join(AiRequest, TokenUsage.request_id == AiRequest.request_id).filter(AiRequest.received_at >= cutoff)
    if org_id:
        tok_q = tok_q.filter(TokenUsage.org_id == org_id)
    ts = tok_q.first()

    lat_q = db.query(func.avg(AiResponse.latency_ms).label("avg_lat")).join(
        AiRequest, AiResponse.request_id == AiRequest.request_id
    ).filter(AiRequest.received_at >= cutoff)
    if org_id:
        lat_q = lat_q.filter(AiRequest.org_id == org_id)
    ls = lat_q.first()

    total = rs.total or 0
    completed = rs.completed or 0
    return {
        "total_requests": total,
        "completed": completed,
        "blocked": rs.blocked or 0,
        "pii_detections": rs.pii_hits or 0,
        "success_rate": round((completed / total * 100), 1) if total else 0.0,
        "total_cost": round(float(cs.total_cost or 0), 6),
        "llm_cost": round(float(cs.llm_cost or 0), 6),
        "total_tokens": int(ts.total_tokens or 0),
        "prompt_tokens": int(ts.prompt_tokens or 0),
        "completion_tokens": int(ts.completion_tokens or 0),
        "avg_latency_ms": round(float(ls.avg_lat or 0), 0),
    }


@router.get("/stats/trends")
def proxy_stats_trends(
    org_id: Optional[str] = None,
    days: int = 30,
    db: Session = Depends(get_db),
):
    from app.models import AiRequest, AiResponse, TokenUsage, RequestCost

    cutoff = datetime.utcnow() - timedelta(days=days)

    req_rows = db.query(
        func.date(AiRequest.received_at).label("d"),
        func.count(AiRequest.id).label("cnt"),
    ).filter(AiRequest.received_at >= cutoff)
    if org_id:
        req_rows = req_rows.filter(AiRequest.org_id == org_id)
    req_map = {str(r.d): r.cnt for r in req_rows.group_by(func.date(AiRequest.received_at)).all()}

    cost_rows = db.query(
        func.date(AiRequest.received_at).label("d"),
        func.sum(RequestCost.total_cost).label("cost"),
    ).join(AiRequest, RequestCost.request_id == AiRequest.request_id).filter(AiRequest.received_at >= cutoff)
    if org_id:
        cost_rows = cost_rows.filter(RequestCost.org_id == org_id)
    cost_map = {str(r.d): float(r.cost or 0) for r in cost_rows.group_by(func.date(AiRequest.received_at)).all()}

    tok_rows = db.query(
        func.date(AiRequest.received_at).label("d"),
        func.sum(TokenUsage.total_tokens).label("tokens"),
    ).join(AiRequest, TokenUsage.request_id == AiRequest.request_id).filter(AiRequest.received_at >= cutoff)
    if org_id:
        tok_rows = tok_rows.filter(TokenUsage.org_id == org_id)
    tok_map = {str(r.d): int(r.tokens or 0) for r in tok_rows.group_by(func.date(AiRequest.received_at)).all()}

    lat_rows = db.query(
        func.date(AiRequest.received_at).label("d"),
        func.avg(AiResponse.latency_ms).label("lat"),
    ).join(AiRequest, AiResponse.request_id == AiRequest.request_id).filter(AiRequest.received_at >= cutoff)
    if org_id:
        lat_rows = lat_rows.filter(AiRequest.org_id == org_id)
    lat_map = {str(r.d): round(float(r.lat or 0), 0) for r in lat_rows.group_by(func.date(AiRequest.received_at)).all()}

    all_dates = sorted(set(list(req_map) + list(cost_map) + list(tok_map)))
    return [
        {
            "date": d,
            "total_requests": req_map.get(d, 0),
            "total_cost": round(cost_map.get(d, 0), 6),
            "total_tokens": tok_map.get(d, 0),
            "avg_latency_ms": lat_map.get(d, 0),
        }
        for d in all_dates
    ]


@router.get("/stats/by-project")
def proxy_stats_by_project(
    org_id: Optional[str] = None,
    days: int = 90,
    db: Session = Depends(get_db),
):
    from app.models import AiRequest, TokenUsage, RequestCost
    from app.models import Project

    cutoff = datetime.utcnow() - timedelta(days=days)

    rows = db.query(
        AiRequest.org_id,
        AiRequest.project_id,
        func.count(AiRequest.id).label("total_requests"),
        func.sum(case((AiRequest.pii_detected == True, 1), else_=0)).label("pii_hits"),
    ).filter(AiRequest.received_at >= cutoff)
    if org_id:
        rows = rows.filter(AiRequest.org_id == org_id)
    req_data = {(r.org_id, r.project_id): {"total_requests": r.total_requests, "pii_hits": r.pii_hits}
                for r in rows.group_by(AiRequest.org_id, AiRequest.project_id).all()}

    cost_rows = db.query(
        RequestCost.org_id,
        RequestCost.project_id,
        func.sum(RequestCost.total_cost).label("total_cost"),
        func.sum(RequestCost.llm_cost).label("llm_cost"),
    ).join(AiRequest, RequestCost.request_id == AiRequest.request_id).filter(AiRequest.received_at >= cutoff)
    if org_id:
        cost_rows = cost_rows.filter(RequestCost.org_id == org_id)
    cost_data = {(r.org_id, r.project_id): {"total_cost": float(r.total_cost or 0), "llm_cost": float(r.llm_cost or 0)}
                 for r in cost_rows.group_by(RequestCost.org_id, RequestCost.project_id).all()}

    tok_rows = db.query(
        TokenUsage.org_id,
        TokenUsage.project_id,
        func.sum(TokenUsage.total_tokens).label("total_tokens"),
        func.sum(TokenUsage.prompt_tokens).label("input_tokens"),
        func.sum(TokenUsage.completion_tokens).label("output_tokens"),
    ).join(AiRequest, TokenUsage.request_id == AiRequest.request_id).filter(AiRequest.received_at >= cutoff)
    if org_id:
        tok_rows = tok_rows.filter(TokenUsage.org_id == org_id)
    tok_data = {
        (r.org_id, r.project_id): {
            "total_tokens": int(r.total_tokens or 0),
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
        }
        for r in tok_rows.group_by(TokenUsage.org_id, TokenUsage.project_id).all()
    }

    lat_rows = db.query(
        AiRequest.org_id,
        AiRequest.project_id,
        func.avg(AiResponse.latency_ms).label("avg_lat"),
    ).join(AiResponse, AiResponse.request_id == AiRequest.request_id).filter(AiRequest.received_at >= cutoff)
    if org_id:
        lat_rows = lat_rows.filter(AiRequest.org_id == org_id)
    lat_data = {(r.org_id, r.project_id): round(float(r.avg_lat or 0), 0)
                for r in lat_rows.group_by(AiRequest.org_id, AiRequest.project_id).all()}

    all_keys = set(list(req_data) + list(cost_data) + list(tok_data))

    # Resolve project names
    proj_names = {}
    try:
        projs = db.query(Project).all()
        proj_names = {p.id: p.name for p in projs}
    except Exception:
        pass

    result = []
    for (oid, pid) in all_keys:
        rd = req_data.get((oid, pid), {})
        cd = cost_data.get((oid, pid), {})
        td = tok_data.get((oid, pid), {})
        result.append({
            "org_id":          oid,
            "project_id":      pid,
            "project_name":    proj_names.get(pid, pid or "Unassigned"),
            "total_requests":  rd.get("total_requests", 0),
            "pii_hits":        rd.get("pii_hits", 0),
            "total_cost":      round(cd.get("total_cost", 0), 6),
            "llm_cost":        round(cd.get("llm_cost", 0), 6),
            "total_tokens":    td.get("total_tokens", 0),
            "input_tokens":    td.get("input_tokens", 0),
            "output_tokens":   td.get("output_tokens", 0),
            "avg_latency_ms":  lat_data.get((oid, pid), 0),
        })
    result.sort(key=lambda x: x["total_cost"], reverse=True)
    return result


@router.get("/stats/by-model")
def proxy_stats_by_model(
    org_id: Optional[str] = None,
    project_id: Optional[str] = None,
    days: int = 30,
    db: Session = Depends(get_db),
):
    from app.models import AiRequest, TokenUsage, RequestCost

    cutoff = datetime.utcnow() - timedelta(days=days)

    rows = db.query(
        RequestCost.model_name,
        RequestCost.provider,
        func.count(RequestCost.id).label("total_requests"),
        func.sum(RequestCost.total_cost).label("total_cost"),
        func.sum(RequestCost.llm_cost).label("llm_cost"),
        func.sum(RequestCost.input_token_cost).label("input_cost"),
        func.sum(RequestCost.output_token_cost).label("output_cost"),
    ).join(AiRequest, RequestCost.request_id == AiRequest.request_id).filter(AiRequest.received_at >= cutoff)
    if org_id:
        rows = rows.filter(RequestCost.org_id == org_id)
    if project_id:
        rows = rows.filter(RequestCost.project_id == project_id)
    rows = rows.group_by(RequestCost.model_name, RequestCost.provider).order_by(func.sum(RequestCost.total_cost).desc()).all()

    tok_rows = db.query(
        TokenUsage.model_name,
        func.sum(TokenUsage.total_tokens).label("tokens"),
        func.sum(TokenUsage.prompt_tokens).label("input_tokens"),
        func.sum(TokenUsage.completion_tokens).label("output_tokens"),
    ).join(AiRequest, TokenUsage.request_id == AiRequest.request_id).filter(AiRequest.received_at >= cutoff)
    if org_id:
        tok_rows = tok_rows.filter(TokenUsage.org_id == org_id)
    if project_id:
        tok_rows = tok_rows.filter(TokenUsage.project_id == project_id)
    tok_map = {
        r.model_name: {
            "tokens":        int(r.tokens or 0),
            "input_tokens":  int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
        }
        for r in tok_rows.group_by(TokenUsage.model_name).all()
    }

    return [
        {
            "model_name":        r.model_name or "unknown",
            "provider":          r.provider or "unknown",
            "total_requests":    r.total_requests,
            "total_cost":        round(float(r.total_cost or 0), 6),
            "input_cost":        round(float(r.input_cost or 0), 6),
            "output_cost":       round(float(r.output_cost or 0), 6),
            "total_tokens":      tok_map.get(r.model_name, {}).get("tokens", 0),
            "input_tokens":      tok_map.get(r.model_name, {}).get("input_tokens", 0),
            "output_tokens":     tok_map.get(r.model_name, {}).get("output_tokens", 0),
        }
        for r in rows
    ]


@router.get("/requests")
def proxy_request_list(
    org_id: Optional[str] = None,
    project_id: Optional[str] = None,
    status: Optional[str] = None,
    pii_only: bool = False,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    from app.models import AiRequest, TokenUsage, RequestCost

    q = db.query(AiRequest).order_by(AiRequest.received_at.desc())
    if org_id:
        q = q.filter(AiRequest.org_id == org_id)
    if project_id:
        q = q.filter(AiRequest.project_id == project_id)
    if status:
        q = q.filter(AiRequest.request_status == status)
    if pii_only:
        q = q.filter(AiRequest.pii_detected == True)

    total = q.count()
    reqs = q.offset(offset).limit(limit).all()

    req_ids = [r.request_id for r in reqs]
    cost_map = {r.request_id: r for r in db.query(RequestCost).filter(RequestCost.request_id.in_(req_ids)).all()}
    tok_map = {r.request_id: r for r in db.query(TokenUsage).filter(TokenUsage.request_id.in_(req_ids)).all()}

    items = []
    for r in reqs:
        c = cost_map.get(r.request_id)
        t = tok_map.get(r.request_id)
        items.append({
            "request_id": r.request_id,
            "org_id": r.org_id,
            "project_id": r.project_id,
            "provider": r.provider,
            "model_name": r.model_name,
            "request_type": r.request_type,
            "request_status": r.request_status,
            "pii_detected": r.pii_detected,
            "pii_types": r.pii_types,
            "pii_action_taken": r.pii_action_taken,
            "prompt_tokens": t.prompt_tokens if t else 0,
            "completion_tokens": t.completion_tokens if t else 0,
            "total_tokens": t.total_tokens if t else 0,
            "total_cost": round(float(c.total_cost or 0), 8) if c else 0,
            "llm_cost": round(float(c.llm_cost or 0), 8) if c else 0,
            "received_at": r.received_at.isoformat() if r.received_at else None,
            "client_ip": r.client_ip,
            "source_system": r.source_system,
        })
    return {"total": total, "items": items}


@router.get("/stats/by-project-model")
def proxy_stats_by_project_model(
    org_id: Optional[str] = None,
    days: int = 30,
    db: Session = Depends(get_db),
):
    """Usage breakdown grouped by (project, model) — reads from pre-aggregated ProjectModelUsage table."""
    cutoff = datetime.utcnow().date() - timedelta(days=int(days))

    q = db.query(
        ProjectModelUsage.org_id,
        ProjectModelUsage.project_id,
        ProjectModelUsage.model_name,
        ProjectModelUsage.provider,
        func.sum(ProjectModelUsage.call_count).label("total_requests"),
        func.sum(ProjectModelUsage.input_tokens).label("input_tokens"),
        func.sum(ProjectModelUsage.output_tokens).label("output_tokens"),
        func.sum(ProjectModelUsage.total_tokens).label("total_tokens"),
        func.sum(ProjectModelUsage.input_token_cost).label("input_cost"),
        func.sum(ProjectModelUsage.output_token_cost).label("output_cost"),
        func.sum(ProjectModelUsage.total_cost).label("total_cost"),
        func.sum(ProjectModelUsage.success_count).label("success_count"),
    ).filter(ProjectModelUsage.date >= cutoff)
    if org_id:
        q = q.filter(ProjectModelUsage.org_id == org_id)
    rows = q.group_by(
        ProjectModelUsage.org_id,
        ProjectModelUsage.project_id,
        ProjectModelUsage.model_name,
        ProjectModelUsage.provider,
    ).order_by(
        ProjectModelUsage.project_id,
        func.sum(ProjectModelUsage.total_cost).desc(),
    ).all()

    # Resolve project names
    proj_names = {}
    try:
        from app.models import Project
        projs = db.query(Project).all()
        proj_names = {p.id: (p.project_name or p.id) for p in projs}
    except Exception:
        pass

    return [
        {
            "org_id":          r.org_id,
            "project_id":      r.project_id,
            "project_name":    proj_names.get(r.project_id, r.project_id or "Unassigned"),
            "model_name":      r.model_name or "unknown",
            "provider":        r.provider or "openai",
            "total_requests":  int(r.total_requests or 0),
            "input_tokens":    int(r.input_tokens or 0),
            "output_tokens":   int(r.output_tokens or 0),
            "total_tokens":    int(r.total_tokens or 0),
            "input_cost":      round(float(r.input_cost or 0), 8),
            "output_cost":     round(float(r.output_cost or 0), 8),
            "total_cost":      round(float(r.total_cost or 0), 8),
            "success_count":   int(r.success_count or 0),
        }
        for r in rows
    ]


@router.get("/stats/pii")
def proxy_pii_summary(
    org_id: Optional[str] = None,
    days: int = 30,
    db: Session = Depends(get_db),
):
    from app.models import AiRequest

    cutoff = datetime.utcnow() - timedelta(days=days)

    q = db.query(AiRequest).filter(
        AiRequest.pii_detected == True,
        AiRequest.received_at >= cutoff,
    )
    if org_id:
        q = q.filter(AiRequest.org_id == org_id)
    rows = q.order_by(AiRequest.received_at.desc()).all()

    pii_type_counts: dict = {}
    for r in rows:
        for pt in (r.pii_types or []):
            pii_type_counts[pt] = pii_type_counts.get(pt, 0) + 1

    action_counts: dict = {}
    for r in rows:
        a = r.pii_action_taken or "unknown"
        action_counts[a] = action_counts.get(a, 0) + 1

    blocked_q = db.query(func.count(AiRequest.id)).filter(
        AiRequest.request_status == "blocked",
        AiRequest.received_at >= cutoff,
    )
    if org_id:
        blocked_q = blocked_q.filter(AiRequest.org_id == org_id)
    blocked_count = blocked_q.scalar() or 0

    return {
        "total_pii_requests": len(rows),
        "blocked_requests": blocked_count,
        "pii_type_breakdown": [{"pii_type": k, "count": v} for k, v in sorted(pii_type_counts.items(), key=lambda x: -x[1])],
        "action_breakdown": [{"action": k, "count": v} for k, v in sorted(action_counts.items(), key=lambda x: -x[1])],
    }
