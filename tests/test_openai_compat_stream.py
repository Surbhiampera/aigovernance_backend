"""OpenAI SDKs pick streaming with a body flag (`stream: true`), not a URL.
The OpenAI-compatible aliases must therefore return SSE when asked, and must
never forward `stream_options` upstream on a non-streaming call.

Run:
    python -m pytest tests/test_openai_compat_stream.py -v
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from tests.test_tools_repro import (
    FAKE_AZURE_RESPONSE, _fake_http, _mock_deployment, _seed,
)

SSE_LINES = [
    'data: {"choices":[{"delta":{"role":"assistant"}}]}',
    'data: {"choices":[{"delta":{"content":"one"}}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
    "data: [DONE]",
]

ALIASES = [
    "/proxy/chat/completions",
    "/proxy/v1/chat/completions",
    "/chat/completions",
    "/v1/chat/completions",
]


class _FakeStreamResp:
    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for line in SSE_LINES:
            yield line


class _FakeStreamCtx:
    async def __aenter__(self):
        return _FakeStreamResp()

    async def __aexit__(self, *a):
        return False


def _client(sent: list[dict]):
    c = AsyncMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)

    async def _post(*a, **kw):
        sent.append(kw.get("json"))
        return _fake_http(FAKE_AZURE_RESPONSE)

    def _stream(*a, **kw):
        sent.append(kw.get("json"))
        return _FakeStreamCtx()

    c.post = _post
    c.stream = _stream
    return c


@pytest.fixture
def gw(client, db_session):
    sent: list[dict] = []
    raw_key = _seed(db_session, "org-cs1", "proj-cs1")
    patches = [
        patch("app.routers.proxy.httpx.AsyncClient", return_value=_client(sent)),
        patch("app.routers.proxy.get_deployments_for_org", return_value=[_mock_deployment()]),
        patch("app.routers.proxy.build_provider_request", return_value=("https://fake.azure.com", {})),
    ]
    for p in patches:
        p.start()
    try:
        yield client, {"X-Governance-Key": raw_key}, sent
    finally:
        for p in patches:
            p.stop()


def _body(**extra):
    return {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], **extra}


@pytest.mark.parametrize("path", ALIASES)
def test_stream_true_returns_sse(gw, path):
    c, hdrs, sent = gw
    r = c.post(path, headers=hdrs, json=_body(stream=True, stream_options={"include_usage": True}))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    assert 'data: {"choices":[{"delta":{"content":"one"}}]}' in r.text
    assert r.text.rstrip().endswith("data: [DONE]")
    assert sent[-1]["stream"] is True


@pytest.mark.parametrize("path", ALIASES)
def test_non_stream_buffers_and_drops_stream_options(gw, path):
    c, hdrs, sent = gw
    r = c.post(path, headers=hdrs, json=_body(stream=False, stream_options={"include_usage": True}))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert "stream_options" not in sent[-1]
    assert "stream" not in sent[-1]
