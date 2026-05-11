"""
GovernanceDecorator — automatic telemetry capture via Python decorators.

External tools integrate with a single import and one instance::

    from governance_sdk import GovernanceDecorator

    gov = GovernanceDecorator(
        org_id="royal-sundaram",
        project_id="email-agent-prod",
        tool_name="email-agent",
        endpoint="http://governance-platform:8000",
        api_key="gvk-xxx",
    )

    # Generic function — captures latency, status, I/O size
    @gov.trace()
    def process_document(doc: str) -> dict:
        ...

    # LLM call — also extracts prompt/completion tokens from the response object
    @gov.llm_call(model_name="gpt-4o", provider="openai")
    def classify_email(body: str):
        return openai.chat.completions.create(model="gpt-4o", messages=[...])

    # Multi-stage pipeline — all child calls link to this trace automatically
    @gov.pipeline(name="email-pipeline")
    def run_email_pipeline(email: dict):
        intent = classify_email(email["body"])   # child span auto-linked
        ...

    # External API / tool call
    @gov.tool_call(tool_type="api", name="search-api")
    def call_search_api(query: str) -> list:
        ...

    # Async functions work the same way
    @gov.trace()
    async def async_classify(body: str) -> dict:
        ...

    # From environment variables (production pattern)
    gov = GovernanceDecorator.from_env()
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import json
import re
import sys
import time
import uuid
from typing import Any, Callable, Optional

from .client import GovernanceSDK

_SDK_VERSION = "1.1.0"

# ── PII masking ──────────────────────────────────────────────────────────────

_PII_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    (re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "[EMAIL]"),
    (re.compile(r"\b(?:\+1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"), "[PHONE]"),
    (re.compile(r"\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13})\b"), "[CC]"),
    (re.compile(r"\b(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\."
                r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b"), "[IP]"),
]


def _mask_pii(text: str) -> tuple[str, bool]:
    """Replace common PII patterns in place. Returns (masked_text, pii_found)."""
    found = False
    for pat, label in _PII_PATTERNS:
        if pat.search(text):
            found = True
            text = pat.sub(label, text)
    return text, found


# ── Serialization helpers ─────────────────────────────────────────────────────

def _safe_preview(obj: Any, max_chars: int, mask_pii: bool = True) -> tuple[str, int, bool]:
    """
    Serialize *obj* to a preview string.
    Returns (preview_text, raw_size_bytes, pii_detected).
    Never raises.
    """
    try:
        if obj is None:
            return "", 0, False
        raw = json.dumps(obj, default=str)
        size = len(raw.encode("utf-8"))
        preview = raw[:max_chars]
        if mask_pii:
            preview, pii = _mask_pii(preview)
        else:
            pii = False
        return preview, size, pii
    except Exception:
        return "", 0, False


def _args_to_dict(fn: Callable, args: tuple, kwargs: dict) -> dict:
    """Bind positional args to parameter names using the function's signature."""
    try:
        sig = inspect.signature(fn)
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except Exception:
        return {"args": list(args), "kwargs": kwargs}


def _extract_tokens(response: Any) -> tuple[int, int]:
    """
    Try to extract prompt/completion token counts from an OpenAI or Anthropic
    response object. Returns (prompt_tokens, completion_tokens).
    """
    if response is None:
        return 0, 0
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    prompt = (
        getattr(usage, "prompt_tokens", None)
        or getattr(usage, "input_tokens", None)
        or 0
    )
    completion = (
        getattr(usage, "completion_tokens", None)
        or getattr(usage, "output_tokens", None)
        or 0
    )
    return int(prompt), int(completion)


def _keys_summary(obj: Any) -> str:
    """Return a comma-separated list of top-level keys (dict) or type name."""
    try:
        if isinstance(obj, dict):
            return ", ".join(list(obj.keys())[:20])
        if isinstance(obj, (list, tuple)):
            return f"[{type(obj).__name__} len={len(obj)}]"
        return type(obj).__name__
    except Exception:
        return ""


# ── Core class ────────────────────────────────────────────────────────────────

class GovernanceDecorator:
    """
    Decorator-based governance integration layer.

    Wraps :class:`GovernanceSDK` and exposes four decorator factories:

    * ``@gov.trace()``       — any function
    * ``@gov.llm_call()``    — functions that call an LLM and return the response
    * ``@gov.pipeline()``    — multi-step pipelines (groups child calls by trace_id)
    * ``@gov.tool_call()``   — external API / tool invocations

    All decorators support both sync and async functions transparently.
    """

    def __init__(
        self,
        org_id: Optional[str] = None,
        *,
        org_name: Optional[str] = None,
        project_id: Optional[str] = None,
        project_name: Optional[str] = None,
        tool_name: str = "unknown",
        endpoint: str = "http://localhost:8000",
        api_key: Optional[str] = None,
        tool_version: str = "1.0.0",
        execution_env: str = "production",
        capture_io: bool = True,
        max_preview_chars: int = 500,
        detect_pii: bool = True,
        redact_pii: bool = True,
        batch_size: int = 1,
        flush_interval: float = 5.0,
        enforce_policy: bool = False,
    ) -> None:
        self._sdk = GovernanceSDK(
            org_id=org_id,
            org_name=org_name,
            project_id=project_id,
            project_name=project_name,
            tool_name=tool_name,
            endpoint=endpoint,
            api_key=api_key,
            detect_pii=detect_pii,
            redact_pii=redact_pii,
            enforce_policy=enforce_policy,
            batch_size=batch_size,
            flush_interval=flush_interval,
        )
        self._tool_version = tool_version
        self._execution_env = execution_env
        self._capture_io = capture_io
        self._max_preview_chars = max_preview_chars
        self._sdk_version = _SDK_VERSION
        self._python_version = (
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        )

    # ── Class factory ────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> "GovernanceDecorator":
        """
        Build a GovernanceDecorator from environment variables.

        Required::

            GOV_ORG_ID or GOV_ORG_NAME

        Optional (all have defaults)::

            GOV_PROJECT_ID, GOV_PROJECT_NAME
            GOV_TOOL_NAME        default "unknown"
            GOV_ENDPOINT         default "http://localhost:8000"
            GOV_API_KEY
            GOV_TOOL_VERSION     default "1.0.0"
            GOV_ENV              default "production"
            GOV_DETECT_PII       default "true"
            GOV_REDACT_PII       default "true"
            GOV_CAPTURE_IO       default "true"
            GOV_MAX_PREVIEW      default "500"
            GOV_BATCH_SIZE       default "1"
        """
        import os

        return cls(
            org_id=os.getenv("GOV_ORG_ID"),
            org_name=os.getenv("GOV_ORG_NAME"),
            project_id=os.getenv("GOV_PROJECT_ID"),
            project_name=os.getenv("GOV_PROJECT_NAME"),
            tool_name=os.getenv("GOV_TOOL_NAME", "unknown"),
            endpoint=os.getenv("GOV_ENDPOINT", "http://localhost:8000"),
            api_key=os.getenv("GOV_API_KEY"),
            tool_version=os.getenv("GOV_TOOL_VERSION", "1.0.0"),
            execution_env=os.getenv("GOV_ENV", "production"),
            capture_io=os.getenv("GOV_CAPTURE_IO", "true").lower() == "true",
            max_preview_chars=int(os.getenv("GOV_MAX_PREVIEW", "500")),
            detect_pii=os.getenv("GOV_DETECT_PII", "true").lower() == "true",
            redact_pii=os.getenv("GOV_REDACT_PII", "true").lower() == "true",
            batch_size=int(os.getenv("GOV_BATCH_SIZE", "1")),
        )

    # ── Decorator factories ──────────────────────────────────────────────────

    def trace(
        self,
        *,
        service_type: Optional[str] = None,
        capture_io: Optional[bool] = None,
        **extra: Any,
    ) -> Callable:
        """
        Wrap any function to auto-capture latency, status, and I/O size.

        Usage::

            @gov.trace()
            def my_function(arg1, arg2):
                ...

            @gov.trace(service_type="preprocessing", capture_io=False)
            def large_batch_processor(records: list):
                ...
        """
        return self._make_decorator(
            decorator_type="trace",
            service_type=service_type or "function",
            capture_io=capture_io if capture_io is not None else self._capture_io,
            **extra,
        )

    def llm_call(
        self,
        *,
        model_name: Optional[str] = None,
        provider: Optional[str] = None,
        service_type: Optional[str] = None,
        capture_io: Optional[bool] = None,
        **extra: Any,
    ) -> Callable:
        """
        Wrap a function that calls an LLM and returns its response.

        Token counts (prompt + completion) are extracted automatically from the
        return value when it is an OpenAI ``ChatCompletion`` or Anthropic
        ``Message`` object.

        Usage::

            @gov.llm_call(model_name="gpt-4o", provider="openai")
            def classify(body: str):
                return openai.chat.completions.create(
                    model="gpt-4o",
                    messages=[{"role": "user", "content": body}],
                )

            @gov.llm_call(model_name="claude-3-5-sonnet-20241022", provider="anthropic")
            async def summarise(text: str):
                return await anthropic_client.messages.create(...)
        """
        return self._make_decorator(
            decorator_type="llm_call",
            model_name=model_name,
            provider=provider,
            service_type=service_type or "llm",
            capture_io=capture_io if capture_io is not None else self._capture_io,
            **extra,
        )

    def pipeline(
        self,
        *,
        name: Optional[str] = None,
        service_type: Optional[str] = None,
        capture_io: Optional[bool] = None,
        **extra: Any,
    ) -> Callable:
        """
        Wrap a multi-stage pipeline function.

        Opens a session context so every child ``@gov.trace()``, ``@gov.llm_call()``,
        or ``patch_openai()`` call inside the function is automatically linked
        by the same ``trace_id``.

        Usage::

            @gov.pipeline(name="email-processing-pipeline")
            def process_email(email: dict):
                intent = classify_email(email["body"])   # child span
                draft = generate_reply(intent)           # child span
                return send_email(draft)                 # child span
        """
        return self._make_decorator(
            decorator_type="pipeline",
            service_type=service_type or "pipeline",
            create_session=True,
            session_name=name,
            capture_io=capture_io if capture_io is not None else self._capture_io,
            **extra,
        )

    def tool_call(
        self,
        *,
        tool_type: str = "api",
        name: Optional[str] = None,
        capture_io: Optional[bool] = None,
        **extra: Any,
    ) -> Callable:
        """
        Wrap an external API or tool invocation.

        Usage::

            @gov.tool_call(tool_type="api", name="search-api")
            def call_search(query: str) -> list:
                return requests.get("https://api.search.com", params={"q": query}).json()

            @gov.tool_call(tool_type="database")
            def fetch_customer(customer_id: str) -> dict:
                ...
        """
        return self._make_decorator(
            decorator_type="tool_call",
            service_type=f"tool:{tool_type}",
            capture_io=capture_io if capture_io is not None else self._capture_io,
            **extra,
        )

    # ── SDK proxy methods ────────────────────────────────────────────────────

    def patch_openai(self) -> "GovernanceDecorator":
        """Monkey-patch openai.chat.completions.create to auto-capture every call."""
        self._sdk.patch_openai()
        return self

    def patch_anthropic(self) -> "GovernanceDecorator":
        """Monkey-patch anthropic.messages.create to auto-capture every call."""
        self._sdk.patch_anthropic()
        return self

    def session(self, *, name: Optional[str] = None):
        """Return a context manager that groups all LLM calls under a shared trace_id."""
        return self._sdk.session(name=name)

    def flush(self) -> None:
        """Flush any buffered events immediately."""
        self._sdk.flush()

    @property
    def sdk(self) -> GovernanceSDK:
        return self._sdk

    # ── Internal: decorator factory ──────────────────────────────────────────

    def _make_decorator(
        self,
        *,
        decorator_type: str = "trace",
        model_name: Optional[str] = None,
        provider: Optional[str] = None,
        service_type: Optional[str] = None,
        capture_io: bool = True,
        create_session: bool = False,
        session_name: Optional[str] = None,
        **extra: Any,
    ) -> Callable:
        gov = self  # closure capture

        def decorator(fn: Callable) -> Callable:
            fn_qualname = fn.__qualname__
            fn_module = fn.__module__
            _session_name = session_name or fn_qualname

            if asyncio.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def async_wrapper(*args, **kwargs):
                    return await gov._run_async(
                        fn, args, kwargs,
                        fn_qualname, fn_module,
                        decorator_type, model_name, provider, service_type,
                        capture_io, create_session, _session_name, extra,
                    )
                return async_wrapper
            else:
                @functools.wraps(fn)
                def sync_wrapper(*args, **kwargs):
                    return gov._run_sync(
                        fn, args, kwargs,
                        fn_qualname, fn_module,
                        decorator_type, model_name, provider, service_type,
                        capture_io, create_session, _session_name, extra,
                    )
                return sync_wrapper

        return decorator

    # ── Internal: sync execution ─────────────────────────────────────────────

    def _run_sync(
        self, fn, args, kwargs,
        fn_qualname, fn_module,
        decorator_type, model_name, provider, service_type,
        capture_io, create_session, session_name, extra,
    ):
        start = time.time()
        status = "success"
        error_msg = None
        result = None

        input_preview, input_size, input_pii = self._capture_input(fn, args, kwargs, capture_io)

        try:
            if create_session:
                with self._sdk.session(name=session_name):
                    result = fn(*args, **kwargs)
            else:
                result = fn(*args, **kwargs)
            return result
        except Exception as exc:
            status = "error"
            error_msg = str(exc)
            raise
        finally:
            latency_ms = int((time.time() - start) * 1000)
            self._emit(
                result, status, error_msg, latency_ms,
                input_preview, input_size, input_pii,
                fn_qualname, fn_module,
                decorator_type, model_name, provider, service_type,
                capture_io, extra,
            )
            self._update_inventory(fn_qualname, fn_module, decorator_type, status, latency_ms)

    # ── Internal: async execution ────────────────────────────────────────────

    async def _run_async(
        self, fn, args, kwargs,
        fn_qualname, fn_module,
        decorator_type, model_name, provider, service_type,
        capture_io, create_session, session_name, extra,
    ):
        start = time.time()
        status = "success"
        error_msg = None
        result = None

        input_preview, input_size, input_pii = self._capture_input(fn, args, kwargs, capture_io)

        try:
            if create_session:
                with self._sdk.session(name=session_name):
                    result = await fn(*args, **kwargs)
            else:
                result = await fn(*args, **kwargs)
            return result
        except Exception as exc:
            status = "error"
            error_msg = str(exc)
            raise
        finally:
            latency_ms = int((time.time() - start) * 1000)
            self._emit(
                result, status, error_msg, latency_ms,
                input_preview, input_size, input_pii,
                fn_qualname, fn_module,
                decorator_type, model_name, provider, service_type,
                capture_io, extra,
            )
            self._update_inventory(fn_qualname, fn_module, decorator_type, status, latency_ms)

    # ── Internal: helpers ────────────────────────────────────────────────────

    def _capture_input(
        self, fn: Callable, args: tuple, kwargs: dict, capture_io: bool
    ) -> tuple[str, int, bool]:
        if not capture_io:
            return "", 0, False
        return _safe_preview(
            _args_to_dict(fn, args, kwargs),
            self._max_preview_chars,
        )

    def _emit(
        self,
        result, status, error_msg, latency_ms,
        input_preview, input_size, input_pii,
        fn_qualname, fn_module,
        decorator_type, model_name, provider, service_type,
        capture_io, extra,
    ) -> None:
        try:
            prompt_tokens, completion_tokens = _extract_tokens(result)

            output_preview, output_size, output_pii = "", 0, False
            if capture_io and result is not None:
                output_preview, output_size, output_pii = _safe_preview(
                    result, self._max_preview_chars
                )

            metadata: dict = {
                "decorator_type": decorator_type,
                "function": fn_qualname,
                "module": fn_module,
            }
            if error_msg:
                metadata["error"] = error_msg
            if input_pii or output_pii:
                metadata["pii_detected_in_io"] = True
            if extra:
                metadata.update({k: v for k, v in extra.items() if k not in metadata})

            self._sdk._send({
                "org_id":               self._sdk.org_id,
                "project_id":           self._sdk.project_id,
                "tool_name":            self._sdk.tool_name,
                "provider":             provider or "unknown",
                "model_name":           model_name,
                "service_type":         service_type or decorator_type,
                "function_name":        fn_qualname,
                "module_path":          fn_module,
                "decorator_type":       decorator_type,
                "execution_env":        self._execution_env,
                "sdk_version":          self._sdk_version,
                "tool_version":         self._tool_version,
                "input_tokens":         prompt_tokens,
                "output_tokens":        completion_tokens,
                "latency_ms":           latency_ms,
                "status":               status,
                "input_data_size_mb":   round(input_size / (1024 * 1024), 6),
                "output_data_size_mb":  round(output_size / (1024 * 1024), 6),
                "input_preview":        input_preview if capture_io else None,
                "output_preview":       output_preview if capture_io else None,
                "contains_pii":         input_pii or output_pii,
                "metadata":             metadata,
            })
        except Exception:
            pass

    def _update_inventory(
        self,
        fn_qualname: str,
        module: str,
        decorator_type: str,
        status: str,
        latency_ms: int,
    ) -> None:
        """
        Upsert tool_api_inventory via the backend API.
        Best-effort — failures are silently ignored to never affect the host app.
        """
        try:
            import requests as _req
            _req.post(
                f"{self._sdk._endpoint}/tools/inventory/upsert",
                json={
                    "org_id":         self._sdk.org_id,
                    "project_id":     self._sdk.project_id,
                    "tool_name":      self._sdk.tool_name,
                    "function_name":  fn_qualname,
                    "module_path":    module,
                    "decorator_type": decorator_type,
                    "status":         status,
                    "latency_ms":     latency_ms,
                    "sdk_version":    self._sdk_version,
                    "python_version": self._python_version,
                    "execution_env":  self._execution_env,
                },
                headers=self._sdk._headers,
                timeout=2,
            )
        except Exception:
            pass
