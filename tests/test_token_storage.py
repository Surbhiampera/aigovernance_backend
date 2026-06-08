"""
Verify that proxy requests correctly store:

- input_tokens
- output_tokens
- total_tokens
- input_cost
- output_cost
- total_cost
- model_name
- project_id

and that cost calculations use model_pricing values.

Run:
    python -m pytest tests/test_token_storage.py -v
"""

import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.models import (
    Organization,
    Project,
    RequestCost,
    TokenUsage,
    ModelPricing,
)
from app.services.governance_key_service import create_governance_key


# ---------------------------------------------------------------------------
# Fake Azure response
# ---------------------------------------------------------------------------

FAKE_AZURE_RESPONSE = {
    "id": "chatcmpl-test123",
    "object": "chat.completion",
    "model": "gpt-4o",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Hello! I am an AI assistant."
            },
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 25,
        "completion_tokens": 10,
        "total_tokens": 35,
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_httpx_response(body: dict, status: int = 200) -> httpx.Response:
    request = httpx.Request("POST", "https://fake.azure.com")

    return httpx.Response(
        status_code=status,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        request=request,
    )


def _seed_org_project_key(db, org_id: str, project_id: str) -> str:
    db.add(
        Organization(
            id=org_id,
            org_name=f"Org {org_id}",
        )
    )

    db.add(
        Project(
            id=project_id,
            org_id=org_id,
            project_name=f"Project {project_id}",
        )
    )

    db.flush()

    result = create_governance_key(
        db,
        org_id=org_id,
        project_id=project_id,
        key_name="test-key",
    )

    db.flush()

    return result["raw_key"]


def _seed_model_pricing(db):
    """
    Pricing per 1M tokens.
    """

    pricing = ModelPricing(
        model_name="gpt-4o",
        input_cost_per_million=Decimal("5.00"),
        output_cost_per_million=Decimal("15.00"),
        provider="azure",
    )

    db.add(pricing)
    db.flush()

    return pricing


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_tokens_and_costs_are_stored(client, db_session):
    """
    Verify:

    - tokens stored
    - costs stored
    - model stored
    - project stored
    """

    raw_key = _seed_org_project_key(
        db_session,
        "org-t01",
        "proj-t01",
    )

    _seed_model_pricing(db_session)

    with patch(
        "app.routers.proxy.httpx.AsyncClient",
        return_value=_mock_azure_client(
            _fake_httpx_response(FAKE_AZURE_RESPONSE)
        ),
    ), patch(
        "app.routers.proxy.get_deployment_for_org",
        return_value=_mock_deployment(),
    ), patch(
        "app.routers.proxy.build_provider_request",
        return_value=("https://fake.azure.com", {}),
    ):

        resp = client.post(
            "/proxy",
            headers={
                "X-Governance-Key": raw_key
            },
            json={
                "model": "gpt-4o",
                "messages": [
                    {
                        "role": "user",
                        "content": "Hello"
                    }
                ],
            },
        )

    assert resp.status_code == 200

    row = (
        db_session.query(RequestCost)
        .filter(RequestCost.org_id == "org-t01")
        .first()
    )

    assert row is not None

    # ------------------------------------------------------------------
    # Tokens
    # ------------------------------------------------------------------

    assert row.input_tokens == 25
    assert row.output_tokens == 10
    assert row.total_tokens == 35

    # ------------------------------------------------------------------
    # Model + Project
    # ------------------------------------------------------------------

    assert row.project_id == "proj-t01"
    assert row.model_name == "gpt-4o"

    # ------------------------------------------------------------------
    # Expected Costs
    # ------------------------------------------------------------------

    expected_input_cost = (
        Decimal("25") / Decimal("1000000")
    ) * Decimal("5.00")

    expected_output_cost = (
        Decimal("10") / Decimal("1000000")
    ) * Decimal("15.00")

    expected_total_cost = (
        expected_input_cost
        + expected_output_cost
    )

    assert Decimal(str(row.input_cost)) == expected_input_cost
    assert Decimal(str(row.output_cost)) == expected_output_cost
    assert Decimal(str(row.total_cost)) == expected_total_cost

    # ------------------------------------------------------------------
    # Token Usage Table
    # ------------------------------------------------------------------

    usage = (
        db_session.query(TokenUsage)
        .filter(TokenUsage.org_id == "org-t01")
        .first()
    )

    assert usage is not None

    assert usage.input_tokens == 25
    assert usage.output_tokens == 10
    assert usage.total_tokens == 35

    assert usage.project_id == "proj-t01"
    assert usage.model_name == "gpt-4o"


def test_tiktoken_fallback_when_usage_missing(client, db_session):
    """
    If Azure doesn't send usage,
    proxy must estimate tokens.
    """

    raw_key = _seed_org_project_key(
        db_session,
        "org-t02",
        "proj-t02",
    )

    _seed_model_pricing(db_session)

    body = dict(FAKE_AZURE_RESPONSE)
    body["usage"] = {}

    with patch(
        "app.routers.proxy.httpx.AsyncClient",
        return_value=_mock_azure_client(
            _fake_httpx_response(body)
        ),
    ), patch(
        "app.routers.proxy.get_deployment_for_org",
        return_value=_mock_deployment(),
    ), patch(
        "app.routers.proxy.build_provider_request",
        return_value=("https://fake.azure.com", {}),
    ):

        resp = client.post(
            "/proxy",
            headers={
                "X-Governance-Key": raw_key
            },
            json={
                "model": "gpt-4o",
                "messages": [
                    {
                        "role": "user",
                        "content": "Hello"
                    }
                ],
            },
        )

    assert resp.status_code == 200

    row = (
        db_session.query(RequestCost)
        .filter(RequestCost.org_id == "org-t02")
        .first()
    )

    assert row is not None

    assert row.input_tokens > 0
    assert row.total_tokens == (
        row.input_tokens + row.output_tokens
    )

    assert row.total_cost > 0


def test_costs_by_project_endpoint(client, db_session):
    """
    Verify /costs/by-project returns:

    - model
    - project
    - input tokens
    - output tokens
    - total tokens
    - input cost
    - output cost
    - total cost
    """

    raw_key = _seed_org_project_key(
        db_session,
        "org-t03",
        "proj-t03",
    )

    _seed_model_pricing(db_session)

    with patch(
        "app.routers.proxy.httpx.AsyncClient",
        return_value=_mock_azure_client(
            _fake_httpx_response(
                FAKE_AZURE_RESPONSE
            )
        ),
    ), patch(
        "app.routers.proxy.get_deployment_for_org",
        return_value=_mock_deployment(),
    ), patch(
        "app.routers.proxy.build_provider_request",
        return_value=("https://fake.azure.com", {}),
    ):

        client.post(
            "/proxy",
            headers={
                "X-Governance-Key": raw_key
            },
            json={
                "model": "gpt-4o",
                "messages": [
                    {
                        "role": "user",
                        "content": "Hello"
                    }
                ],
            },
        )

    resp = client.get(
        "/costs/by-project",
        params={
            "org_id": "org-t03"
        },
    )

    assert resp.status_code == 200

    rows = resp.json()

    assert len(rows) == 1

    row = rows[0]

    assert row["project_id"] == "proj-t03"
    assert row["model_name"] == "gpt-4o"

    assert row["input_tokens"] == 25
    assert row["output_tokens"] == 10
    assert row["total_tokens"] == 35

    assert "input_cost" in row
    assert "output_cost" in row
    assert "total_cost" in row

    print("\n")
    print("===================================================")
    print(f"Project      : {row['project_id']}")
    print(f"Model        : {row['model_name']}")
    print(f"Input Tokens : {row['input_tokens']}")
    print(f"Output Tokens: {row['output_tokens']}")
    print(f"Total Tokens : {row['total_tokens']}")
    print(f"Input Cost   : {row['input_cost']}")
    print(f"Output Cost  : {row['output_cost']}")
    print(f"Total Cost   : {row['total_cost']}")
    print("===================================================")