"""Thread-safe trace context propagation using Python contextvars.

Any code that sets a trace context (sdk.session(), sdk.trace()) automatically
makes that trace_id visible to all intercepted LLM calls in the same async
task or thread — no manual wiring required.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

_active_trace_id: ContextVar[Optional[str]] = ContextVar("_active_trace_id", default=None)
_active_session_name: ContextVar[Optional[str]] = ContextVar("_active_session_name", default=None)


def get_active_trace_id() -> Optional[str]:
    """Return the trace_id pushed by the innermost active session/trace, or None."""
    return _active_trace_id.get()


def get_active_session_name() -> Optional[str]:
    return _active_session_name.get()


class _ContextFrame:
    """Holds reset tokens so the previous context is restored on exit."""

    __slots__ = ("_trace_token", "_session_token")

    def __init__(self, trace_token, session_token):
        self._trace_token = trace_token
        self._session_token = session_token

    def restore(self) -> None:
        if self._trace_token is not None:
            _active_trace_id.reset(self._trace_token)
        if self._session_token is not None:
            _active_session_name.reset(self._session_token)


def push_context(trace_id: str, session_name: Optional[str] = None) -> _ContextFrame:
    """Set a new active trace (and optionally session name) and return a frame to restore later."""
    t = _active_trace_id.set(trace_id)
    s = _active_session_name.set(session_name) if session_name is not None else None
    return _ContextFrame(t, s)
