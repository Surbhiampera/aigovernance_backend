"""
Verify per-user tracking on top of the shared-project X-Governance-Key:

- X-User-Id / X-User-Email / X-User-Role are captured onto AiRequest
- GET /costs/by-user aggregates input/output tokens and cost per user
- Requests without X-User-Id remain unaffected (user_id stays null)

Run:
    python -m pytest tests/test_user_tracking.py -v
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.models import AiRequest, Organization, Project, RequestCost
from app.services.governance_key_service import create_governance_key

FAKE_AZURE_RESPONSE = {
    "id": "chatcmpl-test123",
    "object": "chat.completion",
    "model": "gpt-4o",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello! I am an AI assistant."},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 25, "completion_tokens": 10, "total_tokens": 35},
}


def _fake_httpx_response(body: dict, status: int = 200) -> httpx.Response:
    request = httpx.Request("POST", "https://fake.azure.com")
    return httpx.Response(
        status_code=status,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        request=request,
    )


def _seed_org_project_key(db, org_id: str, project_id: str) -> str:
    db.add(Organization(id=org_id, org_name=f"Org {org_id}"))
    db.add(Project(id=project_id, org_id=org_id, project_name=f"Project {project_id}"))
    db.flush()
    result = create_governance_key(db, org_id=org_id, project_id=project_id, key_name="test-key")
    db.flush()
    return result["raw_key"]


def _mock_deployment():
    return MagicMock(
        deployment_id="dep-001",
        model_name="gpt-4o",
        deployment_name="gpt-4o",
        provider="azure",
        api_endpoint="https://fake.azure.com",
        api_version="2024-02-01",
    )


def _mock_azure_client(response: httpx.Response):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=response)
    return client


def _post_proxy(client, raw_key: str, extra_headers: dict | None = None):
    headers = {"X-Governance-Key": raw_key}
    headers.update(extra_headers or {})
    with patch(
        "app.routers.proxy.httpx.AsyncClient",
        return_value=_mock_azure_client(_fake_httpx_response(FAKE_AZURE_RESPONSE)),
    ), patch(
        "app.routers.proxy.get_deployments_for_org",
        return_value=[_mock_deployment()],
    ), patch(
        "app.routers.proxy.build_provider_request",
        return_value=("https://fake.azure.com", {}),
    ):
        return client.post(
            "/proxy",
            headers=headers,
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "Hello"}]},
        )


def test_user_id_and_email_captured_on_ai_request(client, db_session):
    raw_key = _seed_org_project_key(db_session, "org-u01", "proj-u01")

    resp = _post_proxy(
        client,
        raw_key,
        {
            "X-User-Id": "alice@theirapp.com",
            "X-User-Email": "alice@theirapp.com",
            "X-User-Role": "developer",
        },
    )
    assert resp.status_code == 200

    row = db_session.query(AiRequest).filter(AiRequest.org_id == "org-u01").first()
    assert row is not None
    assert row.user_id == "alice@theirapp.com"
    assert row.user_email == "alice@theirapp.com"
    assert row.user_role == "developer"


def test_request_without_user_id_is_unaffected(client, db_session):
    """Backward compatibility: omitting X-User-Id must not break the proxy flow."""
    raw_key = _seed_org_project_key(db_session, "org-u02", "proj-u02")

    resp = _post_proxy(client, raw_key)
    assert resp.status_code == 200

    row = db_session.query(AiRequest).filter(AiRequest.org_id == "org-u02").first()
    assert row is not None
    assert row.user_id is None

    cost_row = db_session.query(RequestCost).filter(RequestCost.org_id == "org-u02").first()
    assert cost_row is not None
    assert cost_row.total_cost > 0


def test_costs_by_user_endpoint(client, db_session):
    raw_key = _seed_org_project_key(db_session, "org-u03", "proj-u03")

    # Two calls from Alice, one from Bob — all sharing the same project key.
    _post_proxy(client, raw_key, {"X-User-Id": "alice@theirapp.com"})
    _post_proxy(client, raw_key, {"X-User-Id": "alice@theirapp.com"})
    _post_proxy(client, raw_key, {"X-User-Id": "bob@theirapp.com"})

    resp = client.get(
        "/costs/by-user",
        params={"org_id": "org-u03", "project_id": "proj-u03"},
    )
    assert resp.status_code == 200

    rows = {r["user_id"]: r for r in resp.json()}
    assert set(rows) == {"alice@theirapp.com", "bob@theirapp.com"}

    _usage = FAKE_AZURE_RESPONSE["usage"]
    assert rows["alice@theirapp.com"]["total_requests"] == 2
    assert rows["alice@theirapp.com"]["input_tokens"] == _usage["prompt_tokens"] * 2
    assert rows["alice@theirapp.com"]["output_tokens"] == _usage["completion_tokens"] * 2
    assert rows["alice@theirapp.com"]["total_cost"] > 0

    assert rows["bob@theirapp.com"]["total_requests"] == 1
    assert rows["bob@theirapp.com"]["input_tokens"] == _usage["prompt_tokens"]


def test_proxy_requests_filters_by_user_id(client, db_session):
    raw_key = _seed_org_project_key(db_session, "org-u04", "proj-u04")

    _post_proxy(client, raw_key, {"X-User-Id": "alice@theirapp.com"})
    _post_proxy(client, raw_key, {"X-User-Id": "bob@theirapp.com"})

    resp = client.get(
        "/proxy/v1/requests",
        params={"org_id": "org-u04", "user_id": "alice@theirapp.com"},
    )
    assert resp.status_code == 200

    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["user_id"] == "alice@theirapp.com"
