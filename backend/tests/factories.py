"""Helpers for constructing valid test data."""
from datetime import datetime, timezone
from uuid import uuid4


def make_telemetry_event(**overrides) -> dict:
    """Return a minimal valid telemetry event payload."""
    base = {
        "event_id": str(uuid4()),
        "tool_name": "test_tool",
        "org_id": "test-org",
        "status": "success",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "latency_ms": 200,
        "input_data_size_mb": "0.1",
        "output_data_size_mb": "0.05",
    }
    base.update(overrides)
    return base
