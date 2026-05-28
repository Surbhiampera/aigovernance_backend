"""
governance_logger — zero-dependency Python / FastAPI decorator for AI governance telemetry.

The logger is intentionally cost-free on the client side: it ships raw
observations (tokens, latency, model name, input/output) to the governance
platform and lets the platform's CostEngine calculate the actual cost from
its model_pricing database table.  This means:

  • No hardcoded model rates — works with any model, any provider, any tool.
  • Adding a new model to the platform automatically prices all historical calls.
  • One source of truth for cost across every tool your org uses.

Usage:
    from governance_logger import governance_logger, generate_trace_id

    # Single function — any tool, any language wrapper
    @router.post("/send-email")
    @governance_logger(organization="acme", project_name="email-agent")
    async def send_email(payload: dict): ...

    # Multi-step agent — share a trace_id to group calls into one session
    trace = generate_trace_id()

    @governance_logger(organization="acme", project_name="rag-pipeline", trace_id=trace)
    def retrieve_docs(query): ...

    @governance_logger(organization="acme", project_name="rag-pipeline", trace_id=trace)
    def generate_answer(docs, query): ...

Configuration (env vars — set once per service):
    GOV_ENDPOINT       — governance platform URL  (required)
    GOV_API_KEY        — API key for auth          (required)
    GOV_MAX_RETRIES    — HTTP retry attempts        (default 3)
    GOV_RETRY_DELAY_S  — base backoff seconds       (default 0.5)
"""
from __future__ import annotations

import asyncio
import base64
import functools
import inspect
import json
import os
import re
import sys
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

try:
    from fastapi import Request as _FastAPIRequest
    _HAS_FASTAPI = True
except ImportError:
    _FastAPIRequest = None  # type: ignore
    _HAS_FASTAPI = False

_LOGGER_VERSION = "1.2.0"

_MAX_RETRIES   = int(os.getenv("GOV_MAX_RETRIES",   "3"))
_RETRY_DELAY_S = float(os.getenv("GOV_RETRY_DELAY_S", "0.5"))


# ── Trace ID helper ───────────────────────────────────────────────────────────

def generate_trace_id() -> str:
    """Return a unique trace ID for grouping multi-step calls into one session."""
    return uuid.uuid4().hex


# ── PII detection (masking happens server-side too; this is client-side pre-mask) ──

_PII_PATTERNS: list = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                                    "[SSN]"),
    (re.compile(r"\b[A-Z]{1,2}\d{6,9}\b"),                                    "[PASSPORT]"),
    (re.compile(r"\b\d{9}\b"),                                                 "[NID]"),
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),   "[EMAIL]"),
    (re.compile(r"\b(?:\+1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"),   "[PHONE]"),
    (re.compile(r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|"
                r"3[47][0-9]{13})\b"),                                         "[CC]"),
    (re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7}(?:[A-Z0-9]{0,16})?\b"),   "[IBAN]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),                             "[IP]"),
    (re.compile(r"\b\d{3}-[A-Z]{2}-\d{4}\b"),                                "[MEDICAL_ID]"),
    (re.compile(r"\b(?:mrn|patient.?id|dob)[:\s]+[A-Za-z0-9\-/]+",
                re.IGNORECASE),                                                "[MEDICAL_FIELD]"),
]

_PII_LABEL_MAP = {
    "[SSN]": "ssn", "[PASSPORT]": "passport", "[NID]": "national_id",
    "[EMAIL]": "email", "[PHONE]": "phone", "[CC]": "credit_card",
    "[IBAN]": "iban", "[IP]": "ip_address",
    "[MEDICAL_ID]": "medical_id", "[MEDICAL_FIELD]": "medical_field",
}


def _mask_pii(text: str) -> tuple:
    found_types: list = []
    for pat, label in _PII_PATTERNS:
        if pat.search(text):
            found_types.append(_PII_LABEL_MAP.get(label, label))
            text = pat.sub(label, text)
    return text, bool(found_types), found_types


def _safe_preview(obj: Any, max_chars: int = 500) -> tuple:
    """Return (preview_str, size_bytes, pii_found, pii_types_list)."""
    try:
        if obj is None:
            return "", 0, False, []
        raw  = json.dumps(obj, default=str)
        size = len(raw.encode("utf-8"))
        preview, pii, types = _mask_pii(raw[:max_chars])
        return preview, size, pii, types
    except Exception:
        return "", 0, False, []


# ── Token extraction — works with any SDK response shape ─────────────────────

def _extract_tokens(response: Any) -> tuple:
    """Return (prompt_tokens, completion_tokens) from any SDK / dict response."""
    if response is None:
        return 0, 0

    # 1. SDK objects: response.usage.prompt_tokens / .input_tokens
    usage = getattr(response, "usage", None)
    if usage is not None:
        p = getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", None) or 0
        c = getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", None) or 0
        if p or c:
            return int(p), int(c)

    # 2. Dict with nested usage block
    if isinstance(response, dict):
        u = response.get("usage") or {}
        p = u.get("prompt_tokens") or u.get("input_tokens") or 0
        c = u.get("completion_tokens") or u.get("output_tokens") or 0
        if p or c:
            return int(p), int(c)
        # 3. Top-level token keys
        p = response.get("prompt_tokens") or response.get("input_tokens") or 0
        c = response.get("completion_tokens") or response.get("output_tokens") or 0
        if p or c:
            return int(p), int(c)
        # 4. Nested response/result wrapper
        nested = response.get("response") or response.get("result")
        if nested is not None and nested is not response:
            p, c = _extract_tokens(nested)
            if p or c:
                return p, c
        # 5. total_tokens only
        t = response.get("total_tokens") or 0
        if t:
            return int(t), 0
        return 0, 0

    # 6. Pydantic / dataclass with top-level token attributes
    p = getattr(response, "prompt_tokens", None) or getattr(response, "input_tokens", None) or 0
    c = getattr(response, "completion_tokens", None) or getattr(response, "output_tokens", None) or 0
    return int(p), int(c)


def _extract_model(response: Any, fallback: Optional[str] = None) -> Optional[str]:
    if isinstance(response, dict):
        model = response.get("model") or response.get("model_name")
        if model:
            return model
        nested = response.get("response") or response.get("result")
        if nested is not None and nested is not response:
            m = _extract_model(nested, None)
            if m:
                return m
        return fallback
    return getattr(response, "model", None) or getattr(response, "model_name", None) or fallback


# ── HTTP helpers with retry + backoff ─────────────────────────────────────────

def _build_headers(api_key: str) -> dict:
    h = {"Content-Type": "application/json"}
    if api_key:
        h["X-API-Key"] = api_key
    return h


def _http_post_sync(url: str, payload: dict, api_key: str) -> None:
    for attempt in range(_MAX_RETRIES):
        try:
            try:
                import requests as _req
                resp = _req.post(url, json=payload, headers=_build_headers(api_key), timeout=5)
                if resp.status_code < 500:
                    return
            except ImportError:
                import urllib.request as _ur
                data = json.dumps(payload, default=str).encode("utf-8")
                req  = _ur.Request(url, data=data, headers=_build_headers(api_key), method="POST")
                _ur.urlopen(req, timeout=5)
                return
        except Exception:
            pass
        if attempt < _MAX_RETRIES - 1:
            time.sleep(_RETRY_DELAY_S * (2 ** attempt))
    # silently drop after all retries — governance must never crash the caller


async def _http_post_async(url: str, payload: dict, api_key: str) -> None:
    for attempt in range(_MAX_RETRIES):
        try:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.post(url, json=payload, headers=_build_headers(api_key))
                    if resp.status_code < 500:
                        return
            except ImportError:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, _http_post_sync, url, payload, api_key)
                return
        except Exception:
            pass
        if attempt < _MAX_RETRIES - 1:
            await asyncio.sleep(_RETRY_DELAY_S * (2 ** attempt))


# ── Header / JWT extraction ───────────────────────────────────────────────────

def _extract_from_headers(request: Any) -> tuple:
    """Return (org_id, project_id, user_id) from request headers or JWT Bearer token."""
    if request is None:
        return None, None, None
    headers = getattr(request, "headers", {})
    org_id     = (headers.get("x-org-id")
                  or headers.get("x-organization-id")
                  or headers.get("x-tenant-id"))
    project_id = headers.get("x-project-id")
    user_id    = headers.get("x-user-id") or headers.get("x-user")

    if not org_id:
        auth = headers.get("authorization", "")
        if auth.startswith("Bearer ") and auth.count(".") == 2:
            try:
                b64 = auth[7:].split(".")[1]
                b64 += "=" * (-len(b64) % 4)
                claims = json.loads(base64.urlsafe_b64decode(b64))
                org_id     = org_id     or claims.get("org_id") or claims.get("organization_id")
                project_id = project_id or claims.get("project_id")
                user_id    = user_id    or claims.get("user_id") or claims.get("sub")
            except Exception:
                pass
    return org_id, project_id, user_id


# ── Main decorator ────────────────────────────────────────────────────────────

def governance_logger(
    project_name: str = "unknown",
    organization: str = "unknown",
    *,
    model_name:       Optional[str]       = None,
    provider:         Optional[str]       = None,
    tool_name:        Optional[str]       = None,
    trace_id:         Optional[str]       = None,
    user_id:          Optional[str]       = None,
    tags:             Optional[List[str]] = None,
    decorator_type:   Optional[str]       = None,
    capture_io:       bool                = True,
    max_preview_chars: int                = 500,
    execution_env:    str                 = "production",
    endpoint:         Optional[str]       = None,
    api_key:          Optional[str]       = None,
) -> Callable:
    """
    Wrap any Python function or FastAPI route to auto-ship governance telemetry.

    The decorator captures what it observes (latency, tokens, model, I/O) and
    sends it to the governance platform.  Cost is calculated server-side from
    the platform's model_pricing table — no rates are hardcoded here.

    Parameters
    ----------
    project_name      : project identifier in the governance platform
    organization      : org identifier in the governance platform
    model_name        : LLM model name — auto-extracted from response if omitted
    provider          : LLM provider ("openai", "anthropic", "google", …) — optional
    tool_name         : label for this tool; defaults to the function's qualified name
    trace_id          : shared ID to group multi-step calls into one session;
                        use generate_trace_id() at the top of your workflow
    user_id           : caller identity; auto-read from x-user-id header / JWT sub
    tags              : arbitrary string labels for filtering in the dashboard
    decorator_type    : "fastapi_route" | "function" — auto-detected if omitted
    capture_io        : capture PII-masked input/output previews (default True)
    max_preview_chars : truncate previews at this character length
    execution_env     : environment label ("production", "staging", "dev", …)
    endpoint          : governance platform URL; falls back to GOV_ENDPOINT env var
    api_key           : auth token; falls back to GOV_API_KEY env var
    """
    _endpoint   = (endpoint or os.getenv("GOV_ENDPOINT", "http://localhost:8000")).rstrip("/")
    _api_key    = api_key or os.getenv("GOV_API_KEY", "")
    _ingest_url = f"{_endpoint}/decorator/ingest"
    _tags       = tags or []

    def decorator(fn: Callable) -> Callable:
        fn_qualname = fn.__qualname__
        fn_module   = fn.__module__
        _tool       = tool_name or fn_qualname

        sig = inspect.signature(fn)
        has_request_param = _HAS_FASTAPI and any(
            p.annotation is _FastAPIRequest or p.name == "request"
            for p in sig.parameters.values()
        )

        def _build_payload(
            result, status, error_msg, latency_ms,
            http_method, http_path,
            input_preview, input_size, input_pii, input_pii_types,
            org_id, project_id, resolved_user_id,
            prompt_tokens, completion_tokens,
            resolved_model, resolved_trace_id,
        ) -> dict:
            out_preview, out_size, out_pii, out_pii_types = (
                _safe_preview(result, max_preview_chars) if capture_io else ("", 0, False, [])
            )
            pii       = input_pii or out_pii
            pii_types = list(dict.fromkeys(input_pii_types + out_pii_types))

            resolved_dtype = decorator_type or ("fastapi_route" if http_method else "function")

            meta: Dict[str, Any] = {
                "decorator_type": resolved_dtype,
                "function":       fn_qualname,
                "module":         fn_module,
                "project_name":   project_name,
                "organization":   organization,
                "python_version": (
                    f"{sys.version_info.major}.{sys.version_info.minor}"
                    f".{sys.version_info.micro}"
                ),
            }
            if http_method:
                meta["http_method"] = http_method
            if http_path:
                meta["http_path"] = http_path
            if error_msg:
                meta["error"] = error_msg
            if pii:
                meta["pii_detected_in_io"] = True
                meta["pii_types"] = pii_types

            return {
                # ── identity ─────────────────────────────────────────────────
                "org_id":        org_id or organization,
                "project_id":    project_id or project_name,
                "project_name":  project_name,
                "organization":  organization,
                "trace_id":      resolved_trace_id,
                "user_id":       resolved_user_id or user_id,
                "tags":          _tags,
                # ── what was called ──────────────────────────────────────────
                "tool_name":      _tool,
                "function_name":  fn_qualname,
                "module_path":    fn_module,
                "provider":       provider,           # None → platform infers
                "model_name":     resolved_model,     # None → platform stores as null
                "decorator_type": resolved_dtype,
                "http_method":    http_method,
                "http_path":      http_path,
                "service_type":   "fastapi_route" if http_method else "function",
                "execution_env":  execution_env,
                "logger_version": _LOGGER_VERSION,
                # ── raw observations (platform calculates cost from these) ───
                "input_tokens":   prompt_tokens,
                "output_tokens":  completion_tokens,
                "total_tokens":   prompt_tokens + completion_tokens,
                "latency_ms":     latency_ms,
                "status":         status,
                # estimated_cost intentionally omitted — CostEngine owns this
                "input_data_size_mb":  round(input_size / (1024 * 1024), 6),
                "output_data_size_mb": round(out_size   / (1024 * 1024), 6),
                "input_preview":       input_preview if capture_io else None,
                "output_preview":      out_preview   if capture_io else None,
                # ── security ─────────────────────────────────────────────────
                "contains_pii": pii,
                "pii_type":     ",".join(pii_types) if pii_types else None,
                # ── debug ────────────────────────────────────────────────────
                "metadata": meta,
            }

        # ── async path ───────────────────────────────────────────────────────
        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                start     = time.time()
                status    = "success"
                error_msg = None
                result    = None

                req = kwargs.get("request") if has_request_param else kwargs.pop("request", None)
                if req is None:
                    for a in args:
                        if _HAS_FASTAPI and isinstance(a, _FastAPIRequest):
                            req = a
                            break

                http_method = getattr(req, "method", None) if req else None
                http_path   = str(getattr(req, "url", "") or "") if req else None
                org_id, project_id, hdr_user_id = _extract_from_headers(req)
                resolved_trace_id = kwargs.pop("_trace_id", None) or trace_id

                input_preview, input_size, input_pii, input_pii_types = "", 0, False, []
                if capture_io:
                    input_data: Dict[str, Any] = {k: v for k, v in kwargs.items() if k != "request"}
                    if req is not None:
                        try:
                            body_bytes = await req.body()
                            if body_bytes:
                                input_data["_body"] = body_bytes.decode("utf-8", errors="replace")
                        except Exception:
                            pass
                    input_preview, input_size, input_pii, input_pii_types = _safe_preview(
                        input_data, max_preview_chars
                    )

                try:
                    result = await fn(*args, **kwargs)
                    return result
                except Exception as exc:
                    status    = "error"
                    error_msg = str(exc)
                    raise
                finally:
                    latency_ms     = int((time.time() - start) * 1000)
                    prompt_tokens, completion_tokens = _extract_tokens(result)
                    resolved_model = _extract_model(result, model_name)
                    payload = _build_payload(
                        result, status, error_msg, latency_ms,
                        http_method, http_path,
                        input_preview, input_size, input_pii, input_pii_types,
                        org_id, project_id, hdr_user_id,
                        prompt_tokens, completion_tokens,
                        resolved_model, resolved_trace_id,
                    )
                    try:
                        asyncio.get_running_loop().create_task(
                            _http_post_async(_ingest_url, payload, _api_key)
                        )
                    except RuntimeError:
                        threading.Thread(
                            target=_http_post_sync,
                            args=(_ingest_url, payload, _api_key),
                            daemon=True,
                        ).start()

            if _HAS_FASTAPI and not has_request_param:
                params = list(sig.parameters.values())
                params.insert(
                    0,
                    inspect.Parameter(
                        "request",
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        annotation=_FastAPIRequest,
                    ),
                )
                async_wrapper.__signature__ = sig.replace(parameters=params)

            return async_wrapper

        # ── sync path ────────────────────────────────────────────────────────
        else:
            @functools.wraps(fn)
            def sync_wrapper(*args, **kwargs):
                start     = time.time()
                status    = "success"
                error_msg = None
                result    = None

                req = kwargs.get("request") if has_request_param else kwargs.pop("request", None)
                if req is None:
                    for a in args:
                        if _HAS_FASTAPI and isinstance(a, _FastAPIRequest):
                            req = a
                            break

                http_method = getattr(req, "method", None) if req else None
                http_path   = str(getattr(req, "url", "") or "") if req else None
                org_id, project_id, hdr_user_id = _extract_from_headers(req)
                resolved_trace_id = kwargs.pop("_trace_id", None) or trace_id

                input_preview, input_size, input_pii, input_pii_types = "", 0, False, []
                if capture_io:
                    input_data = {k: v for k, v in kwargs.items() if k != "request"}
                    input_preview, input_size, input_pii, input_pii_types = _safe_preview(
                        input_data, max_preview_chars
                    )

                try:
                    result = fn(*args, **kwargs)
                    return result
                except Exception as exc:
                    status    = "error"
                    error_msg = str(exc)
                    raise
                finally:
                    latency_ms     = int((time.time() - start) * 1000)
                    prompt_tokens, completion_tokens = _extract_tokens(result)
                    resolved_model = _extract_model(result, model_name)
                    payload = _build_payload(
                        result, status, error_msg, latency_ms,
                        http_method, http_path,
                        input_preview, input_size, input_pii, input_pii_types,
                        org_id, project_id, hdr_user_id,
                        prompt_tokens, completion_tokens,
                        resolved_model, resolved_trace_id,
                    )
                    threading.Thread(
                        target=_http_post_sync,
                        args=(_ingest_url, payload, _api_key),
                        daemon=True,
                    ).start()

            if _HAS_FASTAPI and not has_request_param:
                params = list(sig.parameters.values())
                params.insert(
                    0,
                    inspect.Parameter(
                        "request",
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        annotation=_FastAPIRequest,
                    ),
                )
                sync_wrapper.__signature__ = sig.replace(parameters=params)

            return sync_wrapper

    return decorator
