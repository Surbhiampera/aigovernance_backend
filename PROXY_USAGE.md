# AI Governance Proxy — Quick Start

All AI requests from your team must go through this proxy. You get a **Governance Key** from your admin. The proxy handles routing to the AI provider — you never need provider credentials.

---

## Server Root

```
http://localhost:8000          ← development
https://<governance-server>    ← production
```

---

## Authentication

Every request must include your governance key as a header:

```
X-Governance-Key: gov-xxxxxxxxxxxx
```

Your admin issues this key. It is scoped to your organization and project.

---

## Endpoints

| Method | Full URL (dev) | Description |
|--------|---------------|-------------|
| `POST` | `http://localhost:8000/proxy` | Chat completion (non-streaming) |
| `POST` | `http://localhost:8000/proxy/chat/completions` | Same — for OpenAI SDK / LangChain |
| `POST` | `http://localhost:8000/proxy/stream` | Streaming chat (SSE) |
| `GET`  | `http://localhost:8000/health` | Health check |

---

## Which URL to use — pick ONE pattern

### Pattern A — Raw HTTP (curl / requests / httpx)

Call `/proxy` directly:

```
POST http://localhost:8000/proxy
```

---

### Pattern B — OpenAI Python SDK

The OpenAI SDK **automatically appends** `/chat/completions` to whatever `base_url` you set.

Set `base_url` to the server root **plus `/proxy`**:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/proxy",   # ← ends at /proxy, no trailing slash
    api_key="gov-xxxxxxxxxxxx",
    default_headers={"X-Governance-Key": "gov-xxxxxxxxxxxx"},
)
```

The SDK will call: `http://localhost:8000/proxy/chat/completions` ✅

> **Common mistake:** setting `base_url="http://localhost:8000/proxy/chat/completions"` — the SDK will append `/chat/completions` again and get a 404.

---

### Pattern C — LangChain / LangGraph (ChatOpenAI)

LangChain's `ChatOpenAI` passes `base_url` straight to the OpenAI SDK internally — same rule as Pattern B.

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    base_url="http://localhost:8000/proxy",   # ← ends at /proxy
    api_key="gov-xxxxxxxxxxxx",
    model="gpt-4o",
    default_headers={"X-Governance-Key": "gov-xxxxxxxxxxxx"},
)
```

The SDK will call: `http://localhost:8000/proxy/chat/completions` ✅

> **Common mistake — double `/proxy`:** if you set `base_url="http://localhost:8000/proxy"` AND also pass a path like `/proxy/chat/completions` somewhere, you will hit `http://localhost:8000/proxy/proxy/chat/completions` and get a 404. Use only the `base_url` shown above; do not add any extra path.

---

### Pattern D — LangGraph with custom httpx calls

If your LangGraph node builds the URL manually, use the full path:

```python
import httpx

GOVERNANCE_URL = "http://localhost:8000"   # server root

response = httpx.post(
    f"{GOVERNANCE_URL}/proxy/chat/completions",
    headers={"X-Governance-Key": "gov-xxxxxxxxxxxx"},
    json={
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "..."}],
    },
)
```

The request goes to: `http://localhost:8000/proxy/chat/completions` ✅

> **Common mistake:** setting `GOVERNANCE_URL = "http://localhost:8000/proxy"` and then constructing `f"{GOVERNANCE_URL}/proxy/chat/completions"` → double `/proxy` → 404.

---

## Request Body

```json
{
  "model": "gpt-4o",
  "messages": [
    { "role": "system", "content": "You are a helpful assistant." },
    { "role": "user",   "content": "Summarize the following document: ..." }
  ],
  "max_tokens": 512,
  "temperature": 0.7
}
```

> **Tip:** You can also pass the model as a query param instead of in the body:
> `POST /proxy?model=gpt-4o` — the query param takes precedence.

---

## Non-Streaming Response

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "model": "gpt-4o",
  "choices": [
    {
      "index": 0,
      "message": { "role": "assistant", "content": "Here is the summary..." },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 120,
    "completion_tokens": 85,
    "total_tokens": 205
  }
}
```

---

## Streaming Response (SSE)

`POST /proxy/stream` — response is a Server-Sent Events stream:

```
data: {"id":"chatcmpl-...","choices":[{"delta":{"role":"assistant"},"index":0}]}

data: {"id":"chatcmpl-...","choices":[{"delta":{"content":"The ocean"},"index":0}]}

data: {"id":"chatcmpl-...","choices":[{"delta":{"content":" roars..."},"index":0}]}

data: [DONE]
```

Parse each `data:` line as JSON. Stop when you receive `data: [DONE]`.

---

## Supported Models

Ask your admin for the list of models enabled for your project.

| Value to pass in `"model"` | Description |
|---------------------------|-------------|
| `gpt-4o` | GPT-4o |
| `gpt-4` | GPT-4 |
| `gpt-35-turbo` | GPT-3.5 Turbo |

If the model is not configured for your project you will receive a `404`.

---

## Error Responses

| Status | Meaning |
|--------|---------|
| `400` | Bad request — invalid JSON or missing `model` field |
| `401` | Invalid or expired governance key |
| `403` | Request blocked by policy (e.g. PII detected in prompt) |
| `404` | Model not configured / wrong URL path |
| `429` | Rate limit or monthly budget exceeded |
| `502` | AI provider unreachable |

Error body example:

```json
{
  "detail": "Request blocked: sensitive PII detected.",
  "pii_types": ["EMAIL", "PHONE"]
}
```

A blocked request also returns an `X-Request-Id` response header. Share it with your admin to look up the audit log entry.

---

## Full Code Examples

### Python — plain requests

```python
import requests

resp = requests.post(
    "http://localhost:8000/proxy",
    headers={"X-Governance-Key": "gov-xxxxxxxxxxxx"},
    json={
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "Hello!"}],
    },
)
print(resp.json()["choices"][0]["message"]["content"])
```

### Python — streaming with requests

```python
import requests, json

with requests.post(
    "http://localhost:8000/proxy/stream",
    headers={"X-Governance-Key": "gov-xxxxxxxxxxxx"},
    json={"model": "gpt-4o", "messages": [{"role": "user", "content": "Tell me a joke."}]},
    stream=True,
) as resp:
    for line in resp.iter_lines():
        if line and line != b"data: [DONE]":
            chunk = json.loads(line.removeprefix(b"data: "))
            print(chunk["choices"][0]["delta"].get("content", ""), end="", flush=True)
```

### Python — OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/proxy",        # ← /proxy, not the full path
    api_key="gov-xxxxxxxxxxxx",
    default_headers={"X-Governance-Key": "gov-xxxxxxxxxxxx"},
)

completion = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "What is 2+2?"}],
)
print(completion.choices[0].message.content)
# SDK calls: POST http://localhost:8000/proxy/chat/completions ✅
```

### Python — LangChain ChatOpenAI

```python
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

llm = ChatOpenAI(
    base_url="http://localhost:8000/proxy",        # ← /proxy, not the full path
    api_key="gov-xxxxxxxxxxxx",
    model="gpt-4o",
    default_headers={"X-Governance-Key": "gov-xxxxxxxxxxxx"},
)

response = llm.invoke([HumanMessage(content="Summarize this text: ...")])
print(response.content)
# SDK calls: POST http://localhost:8000/proxy/chat/completions ✅
```

### Python — LangGraph node with httpx

```python
import httpx

GOVERNANCE_ROOT = "http://localhost:8000"    # ← server root, no /proxy here

def call_llm(state):
    resp = httpx.post(
        f"{GOVERNANCE_ROOT}/proxy/chat/completions",    # ← full path from root
        headers={"X-Governance-Key": "gov-xxxxxxxxxxxx"},
        json={
            "model": "gpt-4o",
            "messages": state["messages"],
        },
        timeout=60,
    )
    resp.raise_for_status()
    return {"result": resp.json()["choices"][0]["message"]["content"]}
# Calls: POST http://localhost:8000/proxy/chat/completions ✅
```

### JavaScript / fetch

```js
const res = await fetch("http://localhost:8000/proxy", {
  method: "POST",
  headers: {
    "X-Governance-Key": "gov-xxxxxxxxxxxx",
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    model: "gpt-4o",
    messages: [{ role: "user", content: "Explain quantum computing simply." }],
  }),
});
const data = await res.json();
console.log(data.choices[0].message.content);
```

---

## What the Proxy Does Automatically

You do not need to handle any of the following:

- **Authentication** — validates your key and resolves your org/project
- **Rate limiting** — per-key and per-project request limits
- **Budget enforcement** — blocks requests when monthly spend limit is reached
- **Governance rules** — model allow/block lists, max token limits per request
- **PII scanning** — masks or blocks sensitive data before sending to the AI provider
- **Cost tracking** — records token usage and cost per request for your dashboard
- **Audit logging** — every request and enforcement decision is logged

---

## Health Check

```
GET http://localhost:8000/health
→ { "status": "healthy", "version": "3.0.0" }
```
