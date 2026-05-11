"""TraceContext — context manager for manual, per-request telemetry capture.

When used inside a sdk.session() block, TraceContext automatically inherits
the session's parent_trace_id and pushes its own trace_id into context so
any nested LLM calls (via interceptors) are linked to it.
"""
from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Any, Optional

from .context import get_active_session_name, get_active_trace_id, push_context

if TYPE_CHECKING:
    from .client import GovernanceSDK


class TraceContext:
    """
    Usage::

        with sdk.trace(model_name="gpt-4o", provider="openai") as t:
            response = call_llm(...)
            t.record(input_tokens=100, output_tokens=50)

    Inside a session::

        with sdk.session(name="pipeline") as sess:
            with sdk.trace(model_name="claude-3-5-sonnet", provider="anthropic") as t:
                # t inherits sess.trace_id as parent; pushes its own trace_id
                # so further nested intercepted calls link to t
                t.record(input_tokens=200, output_tokens=80)
    """

    def __init__(self, sdk: "GovernanceSDK", **kwargs: Any) -> None:
        self._sdk = sdk
        self._start: Optional[float] = None
        self._frame = None

        # Capture the session parent before we push our own trace.
        parent_trace_id = get_active_trace_id()
        session_name = get_active_session_name()

        own_trace_id = kwargs.get("trace_id") or str(uuid.uuid4())

        metadata = dict(kwargs.get("metadata", {}))
        if parent_trace_id:
            metadata["parent_trace_id"] = parent_trace_id
        if session_name:
            metadata["session_name"] = session_name

        self._event: dict[str, Any] = {
            "org_id":       sdk.org_id,
            "project_id":   kwargs.get("project_id") or sdk.project_id,
            "user_id":      kwargs.get("user_id"),
            "trace_id":     own_trace_id,
            "provider":     kwargs.get("provider", "unknown"),
            "model_name":   kwargs.get("model_name"),
            "tool_name":    kwargs.get("tool_name") or sdk.tool_name,
            "service_type": kwargs.get("service_type"),
            "tags":         kwargs.get("tags", []),
            "status":       "success",
            "input_tokens":  0,
            "output_tokens": 0,
            "latency_ms":    0,
            "contains_pii":  False,
            "pii_type":      None,
            "stages":        [],
            "metadata":      metadata,
        }

    # ── context manager ────────────────────────────────────────────────────

    def __enter__(self) -> "TraceContext":
        self._start = time.time()
        # Push own trace_id so nested intercepted calls see this trace as parent.
        self._frame = push_context(self._event["trace_id"])
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self._event["latency_ms"] = int((time.time() - self._start) * 1000)
        if exc_type is not None:
            self._event["status"] = "error"
            self._event.setdefault("metadata", {})["error"] = str(exc_val)
        if self._frame is not None:
            self._frame.restore()
        self._sdk._send(self._event)
        return False

    # ── public helpers ─────────────────────────────────────────────────────

    def record(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        status: Optional[str] = None,
        contains_pii: bool = False,
        pii_type: Optional[str] = None,
        **extra: Any,
    ) -> None:
        """Call inside the `with` block after receiving the LLM response."""
        self._event["input_tokens"] = input_tokens
        self._event["output_tokens"] = output_tokens
        if status:
            self._event["status"] = status
        if contains_pii:
            self._event["contains_pii"] = True
            self._event["pii_type"] = pii_type
        self._event.update(extra)

    def add_stage(
        self,
        stage_name: str,
        *,
        latency_ms: int = 0,
        status: str = "success",
        details: Optional[dict] = None,
    ) -> None:
        """Record a pipeline stage within this trace."""
        self._event["stages"].append({
            "stage_order":      len(self._event["stages"]),
            "stage_name":       stage_name,
            "stage_latency_ms": latency_ms,
            "status":           status,
            "details":          details or {},
        })

    def set_error(self, message: Optional[str] = None) -> None:
        self._event["status"] = "error"
        if message:
            self._event.setdefault("metadata", {})["error"] = message
