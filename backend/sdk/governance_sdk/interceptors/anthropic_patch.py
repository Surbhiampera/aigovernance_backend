"""Auto-instrumentation for anthropic Messages.create.

Each call automatically:
  - Reads the model name from kwargs (always known at call time)
  - Reads input/output token counts from the response usage object
  - Inherits the active parent_trace_id from context (set by sdk.session())
  - Uses sdk.tool_name as the application-level tool identifier
  - Emits a structured event to /control/ingest
"""
from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING

from governance_sdk.context import get_active_session_name, get_active_trace_id

if TYPE_CHECKING:
    from governance_sdk.client import GovernanceSDK


def patch(sdk: "GovernanceSDK") -> None:
    try:
        from anthropic.resources.messages import Messages
    except ImportError:
        raise ImportError("anthropic package not installed. Run: pip install anthropic")

    _original = Messages.create

    def _patched(self_messages, *args, **kwargs):
        model = kwargs.get("model", "unknown")
        messages = kwargs.get("messages", [])

        # Each LLM call gets its own trace_id.
        # If sdk.session() is active, that session's trace_id becomes parent_trace_id,
        # linking all calls in the session without overwriting per-call identity.
        own_trace_id = str(uuid.uuid4())
        parent_trace_id = get_active_trace_id()
        session_name = get_active_session_name()

        # ── PRE-CALL: PII scan ─────────────────────────────────────────────
        contains_pii, pii_type, risk_score = False, None, 0
        if sdk._detect_pii and sdk._pii_scanner:
            contains_pii, pii_type, risk_score = sdk._pii_scanner.scan_messages(messages)
            if contains_pii:
                sdk._fire("on_pii_detected", {
                    "pii_type": pii_type,
                    "risk_score": risk_score,
                    "model": model,
                    "provider": "anthropic",
                })
                if sdk._redact_pii:
                    kwargs = {**kwargs, "messages": sdk._pii_scanner.redact_messages(messages)}

        # ── PRE-CALL: policy gate ──────────────────────────────────────────
        decision = sdk._policy.evaluate_pre_call(
            model=model,
            provider="anthropic",
            messages=messages,
            project_id=sdk.project_id,
        )
        if not decision.allowed:
            sdk._fire("on_policy_block", {
                "reason": decision.reason,
                "action": decision.action,
                "model": model,
            })
            if sdk._enforce_policy:
                raise RuntimeError(f"[GovernanceSDK] Blocked by policy: {decision.reason}")

        # ── CALL ───────────────────────────────────────────────────────────
        start = time.time()
        try:
            response = _original(self_messages, *args, **kwargs)
            elapsed = int((time.time() - start) * 1000)

            usage = getattr(response, "usage", None)
            input_tokens = usage.input_tokens if usage else 0
            output_tokens = usage.output_tokens if usage else 0

            # ── POST-CALL: cost computation ────────────────────────────────
            cost = sdk._cost_engine.compute(model, input_tokens, output_tokens)

            # ── POST-CALL: alert evaluation ────────────────────────────────
            alerts = sdk._policy.evaluate_post_call(
                model=model,
                cost=cost,
                total_tokens=input_tokens + output_tokens,
                contains_pii=contains_pii,
                pii_type=pii_type,
                latency_ms=elapsed,
            )
            for alert in alerts:
                sdk._fire("on_alert", alert)

            metadata: dict = {
                "policy_action": decision.action,
                "risk_score": risk_score,
                "alerts_triggered": len(alerts),
            }
            if parent_trace_id:
                metadata["parent_trace_id"] = parent_trace_id
            if session_name:
                metadata["session_name"] = session_name

            sdk._send({
                "org_id":        sdk.org_id,
                "project_id":    sdk.project_id,
                "provider":      "anthropic",
                "model_name":    model,
                "tool_name":     sdk.tool_name,
                "input_tokens":  input_tokens,
                "output_tokens": output_tokens,
                "latency_ms":    elapsed,
                "status":        "success",
                "trace_id":      own_trace_id,
                "contains_pii":  contains_pii,
                "pii_type":      pii_type,
                "cost":          cost,
                "metadata":      metadata,
            })
            return response

        except Exception as exc:
            elapsed = int((time.time() - start) * 1000)
            meta: dict = {"error": str(exc)}
            if parent_trace_id:
                meta["parent_trace_id"] = parent_trace_id
            sdk._send({
                "org_id":     sdk.org_id,
                "project_id": sdk.project_id,
                "provider":   "anthropic",
                "model_name": model,
                "tool_name":  sdk.tool_name,
                "latency_ms": elapsed,
                "status":     "error",
                "trace_id":   own_trace_id,
                "metadata":   meta,
            })
            raise

    Messages.create = _patched
