"""Regression test: requests carrying a `tools` field (empty or populated)
must never 500 through the proxy. Added after a report that `tools: []`
caused a 500 — verified not reproducible against current proxy.py, kept as
a permanent guard against regressions in request/response handling.

Run:
    python -m pytest tests/test_tools_repro.py -v
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.models import Organization, Project
from app.services.governance_key_service import create_governance_key

FAKE_AZURE_RESPONSE = {
    "id": "chatcmpl-tools-test",
    "object": "chat.completion",
    "model": "gpt-4o",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "Noted."}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
}


def _fake_http(body: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        request=httpx.Request("POST", "https://fake.azure.com"),
    )


def _mock_azure_client(response: httpx.Response):
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(return_value=response)
    return client


def _mock_deployment():
    return MagicMock(
        deployment_id="dep-tools",
        model_name="gpt-4o",
        deployment_name="gpt-4o",
        provider="azure",
        api_endpoint="https://fake.azure.com",
        api_version="2024-02-01",
    )


def _seed(db, org_id: str, project_id: str) -> str:
    db.add(Organization(id=org_id, org_name=f"Org {org_id}"))
    db.add(Project(id=project_id, org_id=org_id, project_name=f"Project {project_id}"))
    db.flush()
    result = create_governance_key(db, org_id=org_id, project_id=project_id, key_name="test-key")
    db.flush()
    return result["raw_key"]


def _proxy_patches():
    return [
        patch("app.routers.proxy.httpx.AsyncClient",
              return_value=_mock_azure_client(_fake_http(FAKE_AZURE_RESPONSE))),
        patch("app.routers.proxy.get_deployments_for_org",
              return_value=[_mock_deployment()]),
        patch("app.routers.proxy.build_provider_request",
              return_value=("https://fake.azure.com", {})),
    ]


def test_tools_empty_array(client, db_session):
    raw_key = _seed(db_session, "org-tools1", "proj-tools1")
    p = _proxy_patches()
    with p[0], p[1], p[2]:
        resp = client.post(
            "/proxy",
            headers={"X-Governance-Key": raw_key},
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [],
            },
        )
    print("STATUS (tools=[]):", resp.status_code)
    print("BODY (tools=[]):", resp.text)
    assert resp.status_code != 500, f"Got 500: {resp.text}"


def test_tools_populated(client, db_session):
    raw_key = _seed(db_session, "org-tools2", "proj-tools2")
    p = _proxy_patches()
    with p[0], p[1], p[2]:
        resp = client.post(
            "/proxy",
            headers={"X-Governance-Key": raw_key},
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {"name": "test", "parameters": {}}}],
            },
        )
    print("STATUS (tools populated):", resp.status_code)
    print("BODY (tools populated):", resp.text)
    assert resp.status_code != 500, f"Got 500: {resp.text}"


def test_no_tools_control(client, db_session):
    raw_key = _seed(db_session, "org-tools3", "proj-tools3")
    p = _proxy_patches()
    with p[0], p[1], p[2]:
        resp = client.post(
            "/proxy",
            headers={"X-Governance-Key": raw_key},
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    print("STATUS (no tools):", resp.status_code)
    print("BODY (no tools):", resp.text)
    assert resp.status_code == 200, f"Control failed: {resp.text}"
