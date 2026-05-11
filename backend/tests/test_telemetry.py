"""Basic tests for the telemetry ingestion endpoint."""
from tests.factories import make_telemetry_event


def test_create_telemetry_event(client):
    payload = make_telemetry_event()
    response = client.post("/telemetry/event", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["event_id"] == payload["event_id"]
    assert data["tool_name"] == payload["tool_name"]


def test_duplicate_event_returns_409(client):
    payload = make_telemetry_event()
    response1 = client.post("/telemetry/event", json=payload)
    assert response1.status_code == 200

    response2 = client.post("/telemetry/event", json=payload)
    assert response2.status_code == 409
