"""GovernanceSDK — runtime telemetry + governance agent."""
from __future__ import annotations

import functools
import logging
import time
import uuid
from typing import Any, Callable, Optional

import requests

_logger = logging.getLogger(__name__)
_RETRY_DELAYS = (1, 2, 4)

from .batch import BatchBuffer
from .context import _ContextFrame, push_context
from .cost import CostEngine
from .policy import PolicyEngine
from .security import PIIScanner
from .tracer import TraceContext


class SessionContext:
    """
    Context manager that groups all LLM calls inside it under a shared parent trace.

    Usage::

        with sdk.session(name="user-chat") as sess:
            # Every patched LLM call inside here carries sess.trace_id as parent_trace_id.
            anthropic.messages.create(...)
            openai.chat.completions.create(...)

        print(sess.trace_id)  # same ID across all calls above
    """

    def __init__(self, sdk: "GovernanceSDK", *, name: Optional[str] = None) -> None:
        self._sdk = sdk
        self.name = name
        self.trace_id = str(uuid.uuid4())
        self._frame: Optional[_ContextFrame] = None

    def __enter__(self) -> "SessionContext":
        self._frame = push_context(self.trace_id, self.name)
        return self

    def __exit__(self, *_) -> bool:
        if self._frame is not None:
            self._frame.restore()
        return False


class GovernanceSDK:
    """
    Embed in any Python app to get real-time LLM governance.

    Minimal setup using human-readable names::

        sdk = GovernanceSDK(
            org_name="Acme Corp",
            project_name="chatbot-prod",
            tool_name="customer-support-bot",   # shows up in every dashboard
            endpoint="http://localhost:8000",
        )
        sdk.patch_openai()
        sdk.patch_anthropic()

    Using IDs directly (skips the resolution API call)::

        sdk = GovernanceSDK(
            org_id="org-uuid",
            project_id="proj-uuid",
            tool_name="code-assistant",
            endpoint="http://localhost:8000",
        )

    Session grouping (all calls share a parent trace_id)::

        with sdk.session(name="checkout-flow") as sess:
            openai.chat.completions.create(...)
            anthropic.messages.create(...)
            # both events linked by sess.trace_id
    """

    def __init__(
        self,
        # ── identity: prefer names; fall back to IDs ───────────────────────
        org_id: Optional[str] = None,
        *,
        org_name: Optional[str] = None,
        project_id: Optional[str] = None,
        project_name: Optional[str] = None,
        # ── what AI application this SDK instance represents ───────────────
        tool_name: str = "unknown",
        # ── network ────────────────────────────────────────────────────────
        endpoint: str = "http://localhost:8000",
        api_key: Optional[str] = None,
        # ── batching ───────────────────────────────────────────────────────
        batch_size: int = 1,
        flush_interval: float = 5.0,
        # ── feature flags ──────────────────────────────────────────────────
        detect_pii: bool = True,
        redact_pii: bool = False,
        enforce_policy: bool = False,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            self._headers["X-API-Key"] = api_key

        # Resolve org/project names → IDs when names are supplied but IDs are not.
        if (org_name or project_name) and not (org_id and project_id):
            from .resolver import resolve_org_project
            resolved_org, resolved_proj = resolve_org_project(
                self._endpoint, self._headers, org_name, project_name
            )
            org_id = org_id or resolved_org
            project_id = project_id or resolved_proj

        self.org_id: str = org_id or "default"
        self.project_id: Optional[str] = project_id
        # tool_name is the AI application name that appears in every dashboard chart.
        # It is separate from provider ("openai") and model_name ("gpt-4o").
        self.tool_name: str = tool_name

        self._detect_pii = detect_pii
        self._redact_pii = redact_pii
        self._enforce_policy = enforce_policy

        self._pii_scanner = PIIScanner() if detect_pii else None
        self._policy = PolicyEngine(self._endpoint, self.org_id, self._headers)
        self._cost_engine = CostEngine(self._endpoint, self._headers)

        self._callbacks: dict[str, list[Callable]] = {
            "on_alert": [],
            "on_pii_detected": [],
            "on_policy_block": [],
            "on_budget_warning": [],
        }

        self._buffer: Optional[BatchBuffer] = None
        if batch_size > 1:
            self._buffer = BatchBuffer(
                max_size=batch_size,
                flush_interval=flush_interval,
                flush_fn=self._send_batch,
            )

    # ── callback registration ────────────────────────────────────────────────

    def on_alert(self, fn: Callable) -> "GovernanceSDK":
        """Register a callback fired when a post-call governance rule is triggered."""
        self._callbacks["on_alert"].append(fn)
        return self

    def on_pii_detected(self, fn: Callable) -> "GovernanceSDK":
        """Register a callback fired when PII is found in a prompt."""
        self._callbacks["on_pii_detected"].append(fn)
        return self

    def on_policy_block(self, fn: Callable) -> "GovernanceSDK":
        """Register a callback fired when a pre-call policy blocks or warns."""
        self._callbacks["on_policy_block"].append(fn)
        return self

    def on_budget_warning(self, fn: Callable) -> "GovernanceSDK":
        """Register a callback fired when token/cost quota nears its limit."""
        self._callbacks["on_budget_warning"].append(fn)
        return self

    def _fire(self, event: str, payload: Any) -> None:
        for fn in self._callbacks.get(event, []):
            try:
                fn(payload)
            except Exception:
                pass

    # ── instrumentation ──────────────────────────────────────────────────────

    def patch_openai(self) -> "GovernanceSDK":
        from .interceptors.openai_patch import patch
        patch(self)
        return self

    def patch_anthropic(self) -> "GovernanceSDK":
        from .interceptors.anthropic_patch import patch
        patch(self)
        return self

    # ── session grouping ─────────────────────────────────────────────────────

    def session(self, *, name: Optional[str] = None) -> SessionContext:
        """
        Return a context manager that groups all LLM calls under a shared parent trace.

        Every intercepted call inside the `with` block automatically records the
        session's trace_id as `parent_trace_id`, linking them in the tracing dashboard.
        """
        return SessionContext(self, name=name)

    # ── manual tracing ───────────────────────────────────────────────────────

    def trace(self, **kwargs: Any) -> TraceContext:
        return TraceContext(self, **kwargs)

    def capture(self, **kwargs: Any) -> Callable:
        """Decorator — wraps any function so every call is automatically traced."""
        def decorator(fn: Callable) -> Callable:
            @functools.wraps(fn)
            def wrapper(*args, **kw):
                with self.trace(**kwargs):
                    return fn(*args, **kw)
            return wrapper
        return decorator

    # ── live stats + quota ───────────────────────────────────────────────────

    def stats(self) -> dict:
        """Real-time session stats: cost, tokens, call count, budget state, active rules."""
        return {
            **self._cost_engine.session_stats,
            "budget": self._policy.budget_status,
            "active_rules": len(self._policy.active_rules),
        }

    def check_quota(self, project_id: Optional[str] = None) -> dict:
        """Return live budget and token quota state (pulled from background cache)."""
        return self._policy.budget_status

    # ── flush ────────────────────────────────────────────────────────────────

    def flush(self) -> None:
        if self._buffer:
            self._buffer.flush()

    # ── internal ─────────────────────────────────────────────────────────────

    def _send(self, event: dict) -> None:
        if self._buffer:
            self._buffer.add(event)
        else:
            self._send_single(event)

    def _send_single(self, event: dict) -> None:
        url = f"{self._endpoint}/control/ingest"
        for attempt, delay in enumerate(_RETRY_DELAYS, 1):
            try:
                resp = requests.post(url, json=event, headers=self._headers, timeout=5)
                if resp.status_code < 500:
                    return
                _logger.warning(
                    "governance ingest HTTP %d (attempt %d/%d)",
                    resp.status_code, attempt, len(_RETRY_DELAYS),
                )
            except requests.exceptions.RequestException as exc:
                _logger.warning(
                    "governance ingest attempt %d/%d network error: %s",
                    attempt, len(_RETRY_DELAYS), exc,
                )
            if attempt < len(_RETRY_DELAYS):
                time.sleep(delay)
        _logger.error(
            "governance: event dropped after %d attempts (fn=%s)",
            len(_RETRY_DELAYS), event.get("function_name", "unknown"),
        )

    def _send_batch(self, events: list[dict]) -> None:
        url = f"{self._endpoint}/control/ingest/batch"
        for attempt, delay in enumerate(_RETRY_DELAYS, 1):
            try:
                resp = requests.post(
                    url, json={"events": events}, headers=self._headers, timeout=10,
                )
                if resp.status_code < 500:
                    return
                _logger.warning(
                    "governance batch ingest HTTP %d (attempt %d/%d)",
                    resp.status_code, attempt, len(_RETRY_DELAYS),
                )
            except requests.exceptions.RequestException as exc:
                _logger.warning(
                    "governance batch ingest attempt %d/%d network error: %s",
                    attempt, len(_RETRY_DELAYS), exc,
                )
            if attempt < len(_RETRY_DELAYS):
                time.sleep(delay)
        _logger.error(
            "governance: batch of %d events dropped after %d attempts",
            len(events), len(_RETRY_DELAYS),
        )
