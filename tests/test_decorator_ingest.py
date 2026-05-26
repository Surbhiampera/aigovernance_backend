"""Tests for decorator telemetry normalization."""

from app.models import ApiKey, TelemetryEvent


def test_decorator_ingest_accepts_logged_token_field_names(client, db_session):
    db_session.add(ApiKey(id="test-key", org_id="rs", project_id="automated-email-agent"))
    db_session.commit()

    payload = {
        "timestamp": "2026-05-15T10:17:05.215870+00:00",
        "route": "draft-email",
        "model": "gpt-5-nano",
        "provider": "openai",
        "prompt_tokens": 164,
        "completion_tokens": 848,
        "total_tokens": 1012,
        "latency_seconds": 5.8081,
        "estimated_cost_usd": 0.0003474,
        "org_id": "rs",
        "project_id": "automated-email-agent",
    }

    response = client.post(
        "/decorator/ingest",
        json=payload,
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["function"] == "draft-email"
    assert data["model_name"] == "gpt-5-nano"
    assert data["total_tokens"] == 1012
    assert data["estimated_cost"] == 0.0003474
    assert data["latency_ms"] == 5808

    event = db_session.query(TelemetryEvent).filter(TelemetryEvent.event_id.like("dec-%")).one()
    assert event.prompt_tokens == 164
    assert event.completion_tokens == 848
    assert event.total_tokens == 1012
    assert event.latency_ms == 5808
