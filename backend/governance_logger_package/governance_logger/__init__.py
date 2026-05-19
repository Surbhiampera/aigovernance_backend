"""
governance_logger — zero-dependency FastAPI / Python decorator for AI governance telemetry.

Usage:
    from governance_logger import governance_logger

    @router.post("/send-email")
    @governance_logger(
        organization="rs",
        project_name="automated-email-agent",
        model_name="gpt-4o",
        provider="openai",
    )
    async def send_email(payload: dict):
        ...

Configuration (env vars):
    GOV_ENDPOINT   — governance backend URL   (required)
    GOV_API_KEY    — API key for auth          (required)
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
from typing import Any, Callable, Dict, Optional

try:
    from fastapi import Request as _FastAPIRequest
    _HAS_FASTAPI = True
except ImportError:
    _FastAPIRequest = None  # type: ignore
    _HAS_FASTAPI = False

_LOGGER_VERSION = "1.0.0"

# ── PII masking ───────────────────────────────────────────────────────────────

_PII_PATTERNS: list = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                                    "[SSN]"),
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),   "[EMAIL]"),
    (re.compile(r"\b(?:\+1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"),   "[PHONE]"),
    (re.compile(r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|"
                r"3[47][0-9]{13})\b"),                                         "[CC]"),
]


def _mask_pii(text: str) -> tuple:
    found = False
    for pat, label in _PII_PATTERNS:
        if pat.search(text):
            found = True
            text = pat.sub(label, text)
    return text, found


def _safe_preview(obj: Any, max_chars: int = 500) -> tuple:
    try:
        if obj is None:
            return "", 0, False
        raw = json.dumps(obj, default=str)
        size = len(raw.encode("utf-8"))
        preview, pii = _mask_pii(raw[:max_chars])
        return preview, size, pii
    except Exception:
        return "", 0, False


# ── Token / model extraction ──────────────────────────────────────────────────

def _extract_tokens(response: Any) -> tuple:
    """Best-effort extraction of (prompt_tokens, completion_tokens).

    Supports three response shapes, in order of preference:
      1. Nested usage object (OpenAI/Anthropic SDK response): response.usage.prompt_tokens
      2. Nested usage dict:                                   response["usage"]["prompt_tokens"]
      3. Top-level dict keys (custom FastAPI responses):      response["prompt_tokens"] / ["input_tokens"]
      4. Top-level object attributes (custom Pydantic models): response.prompt_tokens
    """
    if response is None:
        return 0, 0

    # 1. Object with .usage attribute (OpenAI/Anthropic native SDK objects)
    usage = getattr(response, "usage", None)
    if usage is not None:
        p = getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", None) or 0
        c = getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", None) or 0
        if p or c:
            return int(p), int(c)

    # 2 & 3. Dict-shaped response
    if isinstance(response, dict):
        u = response.get("usage") or {}
        p = u.get("prompt_tokens") or u.get("input_tokens") or 0
        c = u.get("completion_tokens") or u.get("output_tokens") or 0
        if p or c:
            return int(p), int(c)
        # Fallback: tokens at top level of the response dict
        p = response.get("prompt_tokens") or response.get("input_tokens") or 0
        c = response.get("completion_tokens") or response.get("output_tokens") or 0
        if p or c:
            return int(p), int(c)
        nested = response.get("response") or response.get("result")
        if nested is not None and nested is not response:
            p, c = _extract_tokens(nested)
            if p or c:
                return p, c
        # Sometimes only total_tokens is provided
        t = response.get("total_tokens") or 0
        if t:
            return int(t), 0
        return 0, 0

    # 4. Object with top-level token attributes (Pydantic / dataclass)
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
            nested_model = _extract_model(nested, None)
            if nested_model:
                return nested_model
        return fallback
    return getattr(response, "model", None) or getattr(response, "model_name", None) or fallback


# ── Cost estimation ───────────────────────────────────────────────────────────

_MODEL_RATES: Dict[str, tuple] = {
    "gpt-4o":             (5e-6,    15e-6),
    "gpt-4-turbo":        (10e-6,   30e-6),
    "gpt-4":              (30e-6,   60e-6),
    "gpt-3.5-turbo":      (5e-7,    15e-7),
    "claude-3-5-sonnet":  (3e-6,    15e-6),
    "claude-3-opus":      (15e-6,   75e-6),
    "claude-3-haiku":     (2.5e-7,  1.25e-6),
    "claude-sonnet":      (3e-6,    15e-6),
    "claude-haiku":       (2.5e-7,  1.25e-6),
    "gemini-1.5-pro":     (1.25e-6, 5e-6),
    "gemini-pro":         (5e-7,    1.5e-6),
    "llama":              (2e-7,    2e-7),
    "mistral":            (2e-7,    6e-7),
}


def _estimate_cost(model_name: str, prompt_tokens: int, completion_tokens: int) -> float:
    ml = (model_name or "").lower()
    for key, (in_r, out_r) in _MODEL_RATES.items():
        if key in ml:
            return round(prompt_tokens * in_r + completion_tokens * out_r, 8)
    return round((prompt_tokens + completion_tokens) * 1e-6, 8)


# ── Header / JWT extraction ───────────────────────────────────────────────────

def _extract_from_headers(request: Any) -> tuple:
    if request is None:
        return None, None
    headers = getattr(request, "headers", {})
    org_id     = (headers.get("x-org-id")
                  or headers.get("x-organization-id")
                  or headers.get("x-tenant-id"))
    project_id = headers.get("x-project-id")

    if not org_id:
        auth = headers.get("authorization", "")
        if auth.startswith("Bearer ") and auth.count(".") == 2:
            try:
                b64 = auth[7:].split(".")[1]
                b64 += "=" * (-len(b64) % 4)
                claims = json.loads(base64.urlsafe_b64decode(b64))
                org_id     = org_id     or claims.get("org_id") or claims.get("organization_id")
                project_id = project_id or claims.get("project_id")
            except Exception:
                pass
    return org_id, project_id


# ── HTTP send helpers ─────────────────────────────────────────────────────────

def _build_headers(api_key: str) -> dict:
    h = {"Content-Type": "application/json"}
    if api_key:
        h["X-API-Key"] = api_key
    return h


def _http_post_sync(url: str, payload: dict, api_key: str) -> None:
    try:
        try:
            import requests as _req
            _req.post(url, json=payload, headers=_build_headers(api_key), timeout=3)
        except ImportError:
            import urllib.request as _ur
            data = json.dumps(payload, default=str).encode("utf-8")
            req = _ur.Request(url, data=data, headers=_build_headers(api_key), method="POST")
            _ur.urlopen(req, timeout=3)
    except Exception:
        pass


async def _http_post_async(url: str, payload: dict, api_key: str) -> None:
    try:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.post(url, json=payload, headers=_build_headers(api_key))
        except ImportError:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _http_post_sync, url, payload, api_key)
    except Exception:
        pass


# ── Main decorator ────────────────────────────────────────────────────────────

def governance_logger(
    project_name: str = "unknown",
    organization: str = "unknown",
    *,
    model_name: Optional[str] = None,
    provider: Optional[str] = None,
    tool_name: Optional[str] = None,
    decorator_type: str = "fastapi_route",
    capture_io: bool = True,
    max_preview_chars: int = 500,
    execution_env: str = "production",
    endpoint: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Callable:
    """
    Wrap any FastAPI route or plain Python function to auto-ship governance telemetry.

    Parameters
    ----------
    project_name  : must match the project_id created in the governance platform
    organization  : must match the org_id created in the governance platform
    model_name    : LLM model (e.g. "gpt-4o"). Auto-read from response if omitted.
    provider      : LLM provider (e.g. "openai", "anthropic")
    tool_name     : defaults to the decorated function's qualified name
    capture_io    : capture PII-masked input/output previews (default True)
    execution_env : "production", "staging", etc.
    endpoint      : governance backend URL — falls back to GOV_ENDPOINT env var
    api_key       : auth token — falls back to GOV_API_KEY env var
    """
    _endpoint   = (endpoint or os.getenv("GOV_ENDPOINT", "http://localhost:8000")).rstrip("/")
    _api_key    = api_key or os.getenv("GOV_API_KEY", "")
    _ingest_url = f"{_endpoint}/decorator/ingest"

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
            input_preview, input_size, input_pii,
            org_id, project_id,
            prompt_tokens, completion_tokens,
            resolved_model,
        ) -> dict:
            out_preview, out_size, out_pii = (
                _safe_preview(result, max_preview_chars) if capture_io else ("", 0, False)
            )
            cost = _estimate_cost(resolved_model or "", prompt_tokens, completion_tokens)
            pii  = input_pii or out_pii

            meta: Dict[str, Any] = {
                "decorator_type": decorator_type,
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

            return {
                "org_id":              org_id or organization,
                "project_id":          project_id or project_name,
                "project_name":        project_name,
                "organization":        organization,
                "tool_name":           _tool,
                "function_name":       fn_qualname,
                "module_path":         fn_module,
                "provider":            provider or "unknown",
                "model_name":          resolved_model,
                "decorator_type":      decorator_type,
                "http_method":         http_method,
                "http_path":           http_path,
                "service_type":        "fastapi_route" if http_method else "function",
                "execution_env":       execution_env,
                "logger_version":      _LOGGER_VERSION,
                "input_tokens":        prompt_tokens,
                "output_tokens":       completion_tokens,
                "total_tokens":        prompt_tokens + completion_tokens,
                "latency_ms":          latency_ms,
                "status":              status,
                "estimated_cost":      cost,
                "input_data_size_mb":  round(input_size / (1024 * 1024), 6),
                "output_data_size_mb": round(out_size  / (1024 * 1024), 6),
                "input_preview":       input_preview if capture_io else None,
                "output_preview":      out_preview   if capture_io else None,
                "contains_pii":        pii,
                "metadata":            meta,
            }

        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                start     = time.time()
                status    = "success"
                error_msg = None
                result    = None

                if has_request_param:
                    req = kwargs.get("request")
                else:
                    req = kwargs.pop("request", None)

                if req is None:
                    for a in args:
                        if _HAS_FASTAPI and isinstance(a, _FastAPIRequest):
                            req = a
                            break

                http_method = getattr(req, "method", None) if req else None
                http_path   = str(getattr(req, "url", "") or "") if req else None
                org_id, project_id = _extract_from_headers(req)

                input_preview, input_size, input_pii = "", 0, False
                if capture_io:
                    input_data: Dict[str, Any] = {
                        k: v for k, v in kwargs.items() if k != "request"
                    }
                    if req is not None:
                        try:
                            body_bytes = await req.body()
                            if body_bytes:
                                input_data["_body"] = body_bytes.decode("utf-8", errors="replace")
                        except Exception:
                            pass
                    input_preview, input_size, input_pii = _safe_preview(
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
                        input_preview, input_size, input_pii,
                        org_id, project_id,
                        prompt_tokens, completion_tokens,
                        resolved_model,
                    )
                    try:
                        asyncio.get_running_loop().create_task(
                            _http_post_async(_ingest_url, payload, _api_key)
                        )
                    except RuntimeError:
                        pass

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

        else:
            @functools.wraps(fn)
            def sync_wrapper(*args, **kwargs):
                start     = time.time()
                status    = "success"
                error_msg = None
                result    = None

                if has_request_param:
                    req = kwargs.get("request")
                else:
                    req = kwargs.pop("request", None)

                if req is None:
                    for a in args:
                        if _HAS_FASTAPI and isinstance(a, _FastAPIRequest):
                            req = a
                            break

                http_method = getattr(req, "method", None) if req else None
                http_path   = str(getattr(req, "url", "") or "") if req else None
                org_id, project_id = _extract_from_headers(req)

                input_preview, input_size, input_pii = "", 0, False
                if capture_io:
                    input_data = {k: v for k, v in kwargs.items() if k != "request"}
                    input_preview, input_size, input_pii = _safe_preview(
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
                        input_preview, input_size, input_pii,
                        org_id, project_id,
                        prompt_tokens, completion_tokens,
                        resolved_model,
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
