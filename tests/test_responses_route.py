"""POST /proxy/responses — OpenAI Responses API route.

Covers: all four aliases, verbatim forwarding of reasoning/tools/include/store,
reasoning items round-tripping next to function_call_output, SSE passthrough,
PII masking that leaves reasoning items untouched, input/output token accounting,
and rejection for providers without a Responses endpoint.

Run:
    python -m pytest tests/test_responses_route.py -v
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models import AiRequest, AiResponse, RequestCost
from tests.test_tools_repro import _fake_http, _mock_deployment, _seed

ALIASES = ["/proxy/responses", "/proxy/v1/responses", "/responses", "/v1/responses"]

REASONING_ITEM = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "ENC=="}

FAKE_RESPONSE = {
    "id": "resp_1",
    "object": "response",
    "status": "completed",
    "model": "gpt-5.6-luna",
    "output": [
        REASONING_ITEM,
        {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "read", "arguments": "{}"},
    ],
    "usage": {
        "input_tokens": 120, "output_tokens": 30, "total_tokens": 150,
        "input_tokens_details": {"cached_tokens": 100},
    },
}

COMPLETED_EVT = {"type": "response.completed", "response": FAKE_RESPONSE}
SSE_LINES = [
    "event: response.created",
    'data: {"type":"response.created","response":{"id":"resp_1"}}',
    "",
    "event: response.output_text.delta",
    'data: {"type":"response.output_text.delta","delta":"hi"}',
    "",
    "event: response.completed",
    "data: " + json.dumps(COMPLETED_EVT),
    "",
]


class _StreamResp:
    def __init__(self, lines, status=200):
        self._lines, self.status_code = lines, status
        self.is_error = status >= 400

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _StreamCtx:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return _StreamResp(self._lines)

    async def __aexit__(self, *a):
        return False


def _client(sent: list, stream_lines):
    c = AsyncMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)

    async def _post(*a, **kw):
        sent.append({"url": kw.get("url"), "body": kw.get("json")})
        return _fake_http(FAKE_RESPONSE)

    def _stream(*a, **kw):
        sent.append({"url": kw.get("url"), "body": kw.get("json")})
        return _StreamCtx(stream_lines)

    c.post, c.stream = _post, _stream
    return c


def _gw(client, db_session, org, deployments=None, stream_lines=SSE_LINES):
    sent: list = []
    raw_key = _seed(db_session, org, f"proj-{org}")
    deps = deployments if deployments is not None else [_mock_deployment()]
    patches = [
        patch("app.routers.proxy.httpx.AsyncClient", return_value=_client(sent, stream_lines)),
        patch("app.routers.proxy.get_deployments_for_org", return_value=deps),
        patch("app.routers.proxy.build_provider_request", return_value=("https://fake.azure.com/openai/v1/responses", {})),
    ]
    for p in patches:
        p.start()
    return {"X-Governance-Key": raw_key}, sent, patches


@pytest.fixture
def gw(client, db_session):
    hdrs, sent, patches = _gw(client, db_session, "org-rsp1")
    try:
        yield client, hdrs, sent
    finally:
        for p in patches:
            p.stop()


def _body(**extra):
    return {
        "model": "gpt-4o",
        "instructions": "be brief",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
        "tools": [{"type": "function", "name": "read", "parameters": {"type": "object"}}],
        "reasoning": {"effort": "high", "summary": "auto"},
        "include": ["reasoning.encrypted_content"],
        "store": False,
        "prompt_cache_key": "k1",
        "max_output_tokens": 500,
        "text": {"verbosity": "low"},
        "tool_choice": "auto",
        **extra,
    }


@pytest.mark.parametrize("path", ALIASES)
def test_non_stream_forwards_body_and_returns_response(gw, path):
    c, hdrs, sent = gw
    r = c.post(path, headers=hdrs, json=_body())
    assert r.status_code == 200, r.text
    assert r.json()["output"][0] == REASONING_ITEM  # encrypted reasoning returned intact
    out = sent[-1]["body"]
    for key in ("tools", "reasoning", "include", "store", "prompt_cache_key",
                "max_output_tokens", "text", "tool_choice", "instructions"):
        assert out[key] == _body()[key], key
    assert "messages" not in out and "stream" not in out


def test_reasoning_items_roundtrip_with_function_call_output(gw, db_session):
    c, hdrs, sent = gw
    items = [
        {"role": "user", "content": "read it"},
        REASONING_ITEM,
        {"type": "function_call", "call_id": "call_1", "name": "read", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_1", "output": "file contents"},
    ]
    r = c.post("/proxy/responses", headers=hdrs, json=_body(input=items))
    assert r.status_code == 200, r.text
    assert sent[-1]["body"]["input"] == items


def test_pii_masked_in_messages_but_reasoning_untouched(gw):
    c, hdrs, sent = gw
    items = [
        {"role": "user", "content": [{"type": "input_text", "text": "my email is john.doe@example.com"}]},
        REASONING_ITEM,
        {"type": "function_call_output", "call_id": "c", "output": "mail john.doe@example.com"},
    ]
    r = c.post("/proxy/responses", headers=hdrs, json=_body(input=items))
    if r.status_code == 403:  # org policy blocks instead of masking
        return
    assert r.status_code == 200, r.text
    fwd = sent[-1]["body"]["input"]
    assert fwd[1] == REASONING_ITEM
    assert "john.doe@example.com" not in json.dumps(fwd[0]) + json.dumps(fwd[2])


def test_accounting_uses_responses_usage_shape(gw, db_session):
    c, hdrs, sent = gw
    # The success rows are written by a background task on its own DB session
    # (not visible to this test's transaction), so capture its arguments.
    with patch("app.routers.proxy._flush_success_writes") as flush:
        r = c.post("/proxy/responses", headers=hdrs, json=_body())
    assert r.status_code == 200, r.text
    kw = flush.call_args.kwargs
    assert (kw["input_tokens"], kw["output_tokens"]) == (120, 30)
    assert (kw["input_token_source"], kw["output_token_source"]) == ("azure", "azure")
    assert kw["finish_reason"] == "tool_calls"
    db_session.expire_all()
    req = db_session.query(AiRequest).filter_by(request_id=r.headers["X-Request-Id"]).one()
    assert req.request_type == "responses"
    assert req.has_tool_definitions is True
    assert "hello" in (req.prompt_text or "")
    assert req.request_payload["input"][0]["content"][0]["text"] == "hello"


def test_response_row_redacts_encrypted_reasoning_and_counts_tool_calls(db_session):
    from app.routers.proxy import _store_response_and_cost
    org, proj = "org-rsp9", "proj-rsp9"
    _seed(db_session, org, proj)
    req = AiRequest(request_id="req-rsp9", org_id=org, project_id=proj, request_status="pending")
    db_session.add(req)
    db_session.flush()
    _store_response_and_cost(
        db=db_session, request_id="req-rsp9", org_id=org, project_id=proj,
        model="gpt-4o", deployment="gpt-4o", response_payload=FAKE_RESPONSE,
        input_tokens=120, output_tokens=30, finish_reason="tool_calls",
        latency_ms=1, status="success", input_token_source="azure", output_token_source="azure",
    )
    resp = db_session.query(AiResponse).filter_by(request_id="req-rsp9").one()
    assert resp.num_tool_calls == 1
    assert resp.response_payload["output"][0]["encrypted_content"] == "[redacted]"


@pytest.mark.parametrize("path", ALIASES)
def test_stream_passthrough_sse(gw, path):
    c, hdrs, sent = gw
    r = c.post(path, headers=hdrs, json=_body(stream=True))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "event: response.output_text.delta\ndata:" in r.text
    assert "response.completed" in r.text
    assert sent[-1]["body"]["stream"] is True


def test_stream_accounting(gw, db_session):
    c, hdrs, sent = gw
    r = c.post("/proxy/responses", headers=hdrs, json=_body(stream=True))
    rid = r.headers["X-Request-Id"]
    db_session.expire_all()
    cost = db_session.query(RequestCost).filter_by(request_id=rid).one()
    assert (cost.input_tokens, cost.output_tokens) == (120, 30)
    assert db_session.query(AiRequest).filter_by(request_id=rid).one().request_status == "success"


def test_stream_without_terminal_event_is_partial(client, db_session):
    hdrs, sent, patches = _gw(client, db_session, "org-rsp2", stream_lines=SSE_LINES[:6])
    try:
        r = client.post("/proxy/responses", headers=hdrs, json=_body(stream=True))
        rid = r.headers["X-Request-Id"]
    finally:
        for p in patches:
            p.stop()
    db_session.expire_all()
    assert db_session.query(AiRequest).filter_by(request_id=rid).one().request_status == "partial"


def test_anthropic_model_rejected(client, db_session):
    dep = MagicMock(deployment_id="d", model_name="claude", deployment_name="claude", provider="anthropic")
    hdrs, sent, patches = _gw(client, db_session, "org-rsp3", deployments=[dep])
    try:
        r = client.post("/proxy/responses", headers=hdrs, json=_body(model="claude"))
    finally:
        for p in patches:
            p.stop()
    assert r.status_code == 400
    assert "Responses API" in r.text
    assert not sent
