# AI Governance Backend — Full API Reference

Base URL (dev): `http://localhost:8000`  
Base URL (prod): `https://<governance-server>`

Authentication varies by endpoint:

- **Proxy requests** — `X-Governance-Key: gov-xxxx` header (issued by admin)
- **Admin endpoints** — API key via `Authorization: Bearer <token>` or session cookie

---

## Table of Contents

1. [Proxy — AI Request Routing](#1-proxy--ai-request-routing)
2. [Proxy — Stats & Request Log](#2-proxy--stats--request-log)
3. [Costs](#3-costs)
4. [Summary & Trends](#4-summary--trends)
5. [Organizations](#5-organizations)
6. [Projects](#6-projects)
7. [Governance Rules](#7-governance-rules)
8. [Budgets](#8-budgets)
9. [Governance Keys](#9-governance-keys)
10. [API Keys](#10-api-keys)
11. [Deployments](#11-deployments)
12. [Pricing](#12-pricing)
13. [Audit Logs](#13-audit-logs)
14. [Alerts](#14-alerts)
15. [Models](#15-models)
16. [Lookups](#16-lookups)
17. [Auth](#17-auth)
18. [Health](#18-health)
19. [Rate Limits](#19-rate-limits)
20. [Alerts — Security & Anomalies](#20-alerts--security--anomalies)

---

## 1. Proxy — AI Request Routing

All AI requests from external teams go through these endpoints. Include your governance key on every call.

> **Important — `entry_point` tracking**
>
> Every request through the proxy automatically records which route was used in the `entry_point` field (stored on `ai_requests`, visible in the request log).
>
> | Caller hits                  | `entry_point` stored         |
> | ---------------------------- | ---------------------------- |
> | `/proxy`                     | `/proxy`                     |
> | `/proxy/stream`              | `/proxy/stream`              |
> | `/proxy/chat/completions`    | `/proxy/chat/completions`    |
> | `/proxy/v1/chat/completions` | `/proxy/v1/chat/completions` |
>
> The value is captured automatically from `request.url.path` — nothing is hardcoded.
>
> **Rule for new endpoints:** Any new proxy endpoint (e.g. `/proxy/embeddings`, `/proxy/v2/...`) **must call `_run_pre_flight`** for `entry_point` to be captured. If a new endpoint bypasses `_run_pre_flight`, `entry_point` will be `null` for those requests.

> **Grouping multi-call pipelines with `X-Trace-Id`**
>
> Multi-step chatbot pipelines (e.g. classify → tool-select → generate-response) make several separate LLM calls per single user question, each hitting the proxy as its own request. Send the same `X-Trace-Id: <uuid>` header on every call belonging to one user turn.
>
> The **first** call with a given trace ID becomes the **parent** request; every later call with the same trace ID is stored as a **child** linked to it via `parent_request_id`. Effects:
>
> - `GET /proxy/v1/requests` (default, no extra params) returns **one row per parent** — the UI shows a single main entry per user question, with `total_requests`/`total_cost`/`total_tokens` etc. on that row already summed across all of its child calls.
> - `GET /proxy/v1/requests?parent_request_id=<id>` returns the parent **and** all its children as individual rows — use this to expand a toggled-open entry and show each step's own input/output tokens, payload, and response for audit.
> - `GET /proxy/v1/requests?include_children=true` returns the old flat view — every call as its own row, nothing collapsed.
> - Dashboard request-volume metrics (`/proxy/stats/overview`, `/summary/overview`, `/costs/summary`, etc.) count one pipeline as **one** request. Token and cost totals still sum every call in the pipeline — real spend isn't hidden.
> - Rate limiting and budget enforcement are unaffected — they still evaluate every call in real time, since there's no way to know in advance how many child calls will follow.
>
> Optional — omit `X-Trace-Id` and the request is logged standalone (it's its own parent, `parent_request_id` is `null`), exactly as before.
>
> **Integration checklist for client teams with multi-call pipelines (e.g. classify → tool-select → generate-response chatbots):**
>
> 1. Generate one UUID **per user question** (per turn), not per session/conversation. A conversation with 5 back-and-forth questions needs 5 different trace IDs, one per question.
> 2. Send that same UUID as `X-Trace-Id` on every proxy call made while answering that one question — classify, tool-selection, response-generation, and any others (title generation, tagging, etc.) that also go through this proxy.
> 3. If calling through the OpenAI SDK / LangChain (`base_url` pointed at `/proxy`), pass it as a per-call extra header (e.g. `extra_headers={"X-Trace-Id": trace_id}` on each `.invoke()`/`.create()`), since the SDK has no built-in concept of a "turn" header.
> 4. No other request changes needed — model, messages, auth header, response shape are all unchanged.
> 5. This is purely additive: a question that only needs 1 LLM call works identically whether or not `X-Trace-Id` is sent — there is no separate flag for "this is a single-call request" vs. "this is a multi-call request." Grouping only happens when 2+ calls genuinely share the same trace ID; a question resolved in one call is automatically its own one-row group (`request_count: 1`).

### `POST /proxy`

Non-streaming chat completion. Returns a standard OpenAI-compatible response.

**Headers**

```
X-Governance-Key: gov-xxxxxxxxxxxx
X-Trace-Id: <uuid>            # optional — groups multi-call pipelines, see above
Content-Type: application/json
```

**Request body**

```json
{
  "model": "gpt-4o",
  "messages": [
    { "role": "system", "content": "You are a helpful assistant." },
    { "role": "user", "content": "Summarize this document..." }
  ],
  "max_tokens": 512,
  "temperature": 0.7
}
```

> `model` can also be passed as a query param: `POST /proxy?model=gpt-4o` (takes precedence over body).

**Response**

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "model": "gpt-4o",
  "choices": [
    {
      "index": 0,
      "message": { "role": "assistant", "content": "..." },
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

### `POST /proxy/chat/completions`

OpenAI SDK / LangChain compatible alias. The OpenAI SDK appends `/chat/completions` to `base_url`, so set `base_url="http://localhost:8000/proxy"`. Same request/response as `POST /proxy`.

---

### `POST /proxy/v1/chat/completions`

Same as above with a `/v1/` prefix path for clients that expect it.

---

### `POST /proxy/stream`

Streaming chat completion. Response is a Server-Sent Events (SSE) stream.

**Request body** — same as `POST /proxy`

**Response** (SSE stream)

```
data: {"id":"chatcmpl-...","choices":[{"delta":{"role":"assistant"},"index":0}]}

data: {"id":"chatcmpl-...","choices":[{"delta":{"content":"Hello"},"index":0}]}

data: [DONE]
```

Parse each `data:` line as JSON. Stop on `data: [DONE]`.

---

### Error responses (all proxy endpoints)

| Status | Meaning                                             |
| ------ | --------------------------------------------------- |
| `400`  | Bad request — invalid JSON or missing `model`       |
| `401`  | Invalid or expired governance key                   |
| `403`  | Blocked by policy (PII detected, model not allowed) |
| `404`  | Model not configured / wrong URL                    |
| `429`  | Rate limit or monthly budget exceeded               |
| `502`  | AI provider unreachable                             |

Error body example:

```json
{
  "detail": "Request blocked: sensitive PII detected.",
  "pii_types": ["EMAIL", "PHONE"]
}
```

---

## 2. Proxy — Stats & Request Log

### `GET /proxy/v1/requests`

List proxy requests with filtering and pagination. Used for the Request-wise Breakdown dashboard.

By default this returns **one row per parent request** — if a chatbot pipeline made 4 LLM calls under the same `X-Trace-Id`, only the first (parent) call appears here, with `total_requests`/`total_cost`/`total_tokens`-equivalent fields (`total_tokens`, `total_cost`, `prompt_tokens`, `completion_tokens`, etc.) already summed across all 4 calls. Pass `parent_request_id` to drill into a specific group's individual calls when the UI toggle is expanded.

| Query param          | Type   | Description                      |
| -------------------- | ------ | -------------------------------- |
| `project_id`         | string | Filter by project                |
| `org_id`             | string | Filter by organisation           |
| `request_id`         | string | Fetch a specific request (parent or child) by its own ID |
| `request_type`       | string | e.g. `chat_completion`           |
| `status`             | string | e.g. `success`, `error`          |
| `pii_only`           | bool   | Return only PII-flagged requests |
| `trace_id`           | string | Filter to one trace              |
| `parent_request_id`  | string | Return this parent **and all of its child calls** as individual rows — use to expand a toggled-open entry |
| `include_children`   | bool   | `true` returns the old flat view — every call as its own row, nothing collapsed |
| `group_by`           | string | Set to `trace_id` for an aggregated-by-trace view instead of the default parent/child view (legacy, kept for trace-level reporting) |
| `limit`              | int    | Page size (default 50)           |
| `offset`             | int    | Pagination offset (default 0)    |

**Response — default (collapsed to parents)**

```json
{
  "total": 1,
  "offset": 0,
  "items": [
    {
      "request_id": "req_abc123",
      "parent_request_id": null,
      "request_count": 4,
      "org_id": "org_xyz",
      "project_id": "proj_xyz",
      "model_name": "gpt-4o",
      "request_type": "chat_completion",
      "request_status": "success",
      "prompt_tokens": 2400,
      "completion_tokens": 980,
      "total_tokens": 3380,
      "input_cost": 0.0021,
      "output_cost": 0.0070,
      "total_cost": 0.0091,
      "llm_cost": 0.0091,
      "pii_detected": false,
      "pii_types": [],
      "pii_action_taken": null,
      "provider": "azure_openai",
      "source_system": null,
      "client_ip": "127.0.0.1",
      "trace_id": "a1b2c3d4-...",
      "received_at": "2026-06-09T07:00:00",
      "created_at": "2026-06-09T07:00:01"
    }
  ]
}
```

`request_count` is the number of LLM calls rolled into this row (1 if the request never fanned out). All cost/token fields are summed across the parent and its children — real spend stays accurate even though it's reported as one request.

**Response — `?parent_request_id=req_abc123` (expand to see each call)**

```json
{
  "total": 4,
  "offset": 0,
  "items": [
    { "request_id": "req_abc123", "parent_request_id": null,            "model_name": "gpt-4o-mini", "prompt_tokens": 400, "completion_tokens": 20, "total_cost": 0.0004, "request_payload": { "...": "classify_query call" } },
    { "request_id": "req_def456", "parent_request_id": "req_abc123",    "model_name": "gpt-4o-mini", "prompt_tokens": 600, "completion_tokens": 80, "total_cost": 0.0010, "request_payload": { "...": "tool-selection call" } },
    { "request_id": "req_ghi789", "parent_request_id": "req_abc123",    "model_name": "gpt-4o",      "prompt_tokens": 1200, "completion_tokens": 750, "total_cost": 0.0065, "request_payload": { "...": "response-generation call" } }
  ]
}
```

Each child row retains its own input/output tokens, payload, and response for auditing — they just don't get a separate top-level row in the default list view.

---

### `GET /proxy/stats/overview`

Headline metrics for the governance dashboard.

| Query param | Type   | Description                          |
| ----------- | ------ | ------------------------------------ |
| `org_id`    | string | Filter by organisation               |
| `days`      | int    | Lookback window in days (default 30) |

---

### `GET /proxy/stats/trends`

Daily trends in request volume and costs.

| Query param | Type   | Description                          |
| ----------- | ------ | ------------------------------------ |
| `org_id`    | string | Filter by organisation               |
| `days`      | int    | Lookback window in days (default 30) |

---

### `GET /proxy/stats/by-project-model`

Request stats grouped by project and model.

| Query param | Type   | Description                          |
| ----------- | ------ | ------------------------------------ |
| `org_id`    | string | Filter by organisation               |
| `days`      | int    | Lookback window in days (default 30) |

---

### `GET /proxy/stats/pii`

PII detection breakdown by type and action taken.

| Query param | Type   | Description                          |
| ----------- | ------ | ------------------------------------ |
| `org_id`    | string | Filter by organisation               |
| `days`      | int    | Lookback window in days (default 30) |

---

### `GET /proxy/stats/tool-call-reliability`

Per-model breakdown of tool-equipped requests vs. requests where the model actually invoked a tool. A model that's given tool/function definitions but frequently returns zero tool calls is unreliable for agentic workloads — this surfaces that failure pattern (the model hallucinating an answer instead of calling a tool).

| Query param | Type   | Description                          |
| ----------- | ------ | ------------------------------------ |
| `org_id`    | string | Filter by organisation               |
| `days`      | int    | Lookback window in days (default 30) |

**Response**

```json
[
  {
    "model": "gpt-4o",
    "tool_equipped_requests": 120,
    "requests_with_tool_calls": 110,
    "requests_with_zero_tool_calls": 10,
    "zero_tool_call_rate_pct": 8.3
  }
]
```

---

### `GET /proxy/v1/requests/{request_id}/pii-detail`

Full PII entity detail for a single request, including before/after (masked) values for prompt and response text. Used by the PII drill-down view.

**Response**

```json
{
  "request_id": "req_abc123",
  "org_id": "org_xyz",
  "project_id": "proj_xyz",
  "pii_detected": true,
  "pii_types": ["EMAIL", "PHONE"],
  "pii_action_taken": "masked",
  "pii_severity": "medium",
  "pii_entities_detected": 2,
  "pii_entities_masked": 2,
  "pii_detail": [],
  "request_payload": {},
  "created_at": "2026-06-09T07:00:00",
  "original_messages": [],
  "original_prompt_text": "Contact me at john@example.com",
  "sanitized_prompt_text": "Contact me at [EMAIL]",
  "response_text": "...",
  "output_pii_types": []
}
```

---

## 3. Costs

### `GET /costs/summary`

Total cost summary for a date range and org/project.

| Query param  | Type              | Description            |
| ------------ | ----------------- | ---------------------- |
| `org_id`     | string            | Filter by organisation |
| `project_id` | string            | Filter by project      |
| `start`      | date `YYYY-MM-DD` | Start date (inclusive) |
| `end`        | date `YYYY-MM-DD` | End date (inclusive)   |

**Response**

```json
{
  "total_requests": 3,
  "input_tokens": 1800,
  "output_tokens": 3900,
  "total_tokens": 5700,
  "input_cost": 0.00054,
  "output_cost": 0.00117,
  "total_cost": 0.00166,
  "currency": "USD"
}
```

---

### `GET /costs/by-model`

Cost breakdown aggregated by model and provider.

| Query param  | Type   | Description            |
| ------------ | ------ | ---------------------- |
| `org_id`     | string | Filter by organisation |
| `project_id` | string | Filter by project      |
| `start`      | date   | Start date             |
| `end`        | date   | End date               |

---

### `GET /costs/by-project`

Cost breakdown aggregated by project.

| Query param | Type   | Description            |
| ----------- | ------ | ---------------------- |
| `org_id`    | string | Filter by organisation |
| `start`     | date   | Start date             |
| `end`       | date   | End date               |

---

### `GET /costs/by-org`

Cost breakdown aggregated by organisation.

| Query param | Type | Description |
| ----------- | ---- | ----------- |
| `start`     | date | Start date  |
| `end`       | date | End date    |

---

### `GET /costs/trend/daily`

Daily cost trend for the past N days.

| Query param  | Type   | Description                    |
| ------------ | ------ | ------------------------------ |
| `org_id`     | string | Filter by organisation         |
| `project_id` | string | Filter by project              |
| `days`       | int    | Lookback (default 30, max 365) |

---

### `GET /costs/trend/monthly`

Monthly cost trend (pre-aggregated by scheduler, up to 24h stale).

| Query param  | Type   | Description                   |
| ------------ | ------ | ----------------------------- |
| `org_id`     | string | Filter by organisation        |
| `project_id` | string | Filter by project             |
| `months`     | int    | Lookback (default 12, max 36) |

---

### `GET /costs/request/{request_id}`

Cost details for a single request.

---

## 4. Summary & Trends

### `GET /summary/today`

Live today-only cost and token summary from `RequestCost`.

---

### `GET /summary/daily`

Daily summary rollup for a date range (aggregated by scheduler).

---

### `GET /summary/monthly`

Monthly summary for a range of months (pre-aggregated).

---

### `GET /summary/monthly-by-model`

Monthly cost and token breakdown per model.

---

### `GET /summary/trends`

Daily cost and token trends for charts.

---

### `GET /summary/overview`

Headline metrics for the governance dashboard.

---

### `POST /summary/admin/rebuild-daily`

Manually rebuild `DailyOrgSummary` rows (and re-run anomaly detection) for the past N days. Useful after fixing a bug that prevented the scheduler from running. Idempotent — safe to call repeatedly.

| Query param | Type | Description                              |
| ----------- | ---- | ----------------------------------------- |
| `days_back` | int  | How many past days to rebuild (default 7, max 90) |

**Response**

```json
{
  "rebuilt": ["2026-06-23", "2026-06-22"],
  "errors": []
}
```

---

## 5. Organizations

### `GET /organizations/`

List all organisations.

### `GET /organizations/{org_id}`

Get a specific organisation.

### `POST /organizations/`

Create a new organisation.

**Request body**

```json
{
  "id": "reckit_1780983190087",
  "name": "Reckit",
  "plan_type": "enterprise"
}
```

> **Auto-provisioned deployments:** `POST /organizations/` also creates one org-wide `ModelDeployment` row (`project_id` null) per entry in the `STANDARD_MODEL_DEPLOYMENTS` env var, so a brand-new org immediately has access to every standard model without a separate `POST /deployments` call per model. See [§11 Deployments](#11-deployments) for the env var format and the backfill endpoint for orgs created before this existed.

### `PUT /organizations/{org_id}`

Update an organisation.

### `DELETE /organizations/{org_id}`

Delete an organisation and all related data, including any `ModelDeployment` rows for that org.

### `POST /organizations/{org_id}/provision-deployments`

Backfill the standard model deployments (from `STANDARD_MODEL_DEPLOYMENTS`) for an org that already exists — use this for orgs created before auto-provisioning was added, or to re-sync after updating the env var. Safe to call repeatedly; it adds new rows, it does not touch or remove existing ones.

**Response**

```json
{
  "org_id": "reckit_1780983190087",
  "deployments_created": 2
}
```

---

## 6. Projects

### `GET /projects/`

List all projects.

| Query param | Type   | Description            |
| ----------- | ------ | ---------------------- |
| `org_id`    | string | Filter by organisation |

### `GET /projects/{project_id}`

Get a specific project.

### `POST /projects/`

Create a new project.

**Request body**

```json
{
  "id": "reckit_marketplace_1780983215793",
  "org_id": "reckit_1780983190087",
  "name": "Reckit Marketplace"
}
```

### `PUT /projects/{project_id}`

Update a project.

### `DELETE /projects/{project_id}`

Delete a project and all related data.

---

## 7. Governance Rules

### `GET /governance/rules`

List all governance rules.

| Query param  | Type   | Description            |
| ------------ | ------ | ---------------------- |
| `org_id`     | string | Filter by organisation |
| `project_id` | string | Filter by project      |

### `POST /governance/rules`

Create or update a governance rule (model allow/block list, token ceiling, etc.).

**Request body example — block a model**

```json
{
  "org_id": "reckit_1780983190087",
  "project_id": "reckit_marketplace_1780983215793",
  "metric": "model_name",
  "operator": "eq",
  "value": "gpt-4",
  "action": "block",
  "scope": "project"
}
```

---

## 8. Budgets

### `GET /budgets/utilization`

Current-month spend vs. limit for every budget, with status (`ok`, `warning`, `exceeded`, `no_budget`).

| Query param | Type   | Description             |
| ----------- | ------ | ------------------------ |
| `org_id`    | string | Filter by organisation   |

**Response**

```json
[
  {
    "org_id": "reckit_1780983190087",
    "project_id": "reckit_marketplace_1780983215793",
    "budget_type": "monthly",
    "limit_amount": 100.0,
    "current_spend": 42.5,
    "alert_threshold_percent": 80,
    "utilization_percent": 42.5,
    "status": "ok"
  }
]
```

---

### `GET /budgets/`

List all budgets.

| Query param | Type   | Description            |
| ----------- | ------ | ---------------------- |
| `org_id`    | string | Filter by organisation |

### `GET /budgets/{budget_id}`

Get a specific budget.

### `POST /budgets/`

Create a new budget.

**Request body**

```json
{
  "org_id": "reckit_1780983190087",
  "project_id": "reckit_marketplace_1780983215793",
  "period": "monthly",
  "limit_usd": 100.0
}
```

### `PUT /budgets/{budget_id}`

Update a budget.

### `DELETE /budgets/{budget_id}`

Delete a budget.

---

## 9. Governance Keys

Governance keys are issued to external teams to authenticate proxy requests.

### `GET /governance-keys/`

List all governance keys for an organisation (raw key values not included).

| Query param | Type   | Description |
| ----------- | ------ | ----------- |
| `org_id`    | string | Required    |

### `POST /governance-keys/`

Create a new governance key. **Raw key is returned once — store it immediately.**

**Request body**

```json
{
  "org_id": "reckit_1780983190087",
  "project_id": "reckit_marketplace_1780983215793",
  "label": "team-alpha-key"
}
```

**Response**

```json
{
  "key_id": "gk_...",
  "raw_key": "gov-xxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "org_id": "reckit_1780983190087",
  "project_id": "reckit_marketplace_1780983215793"
}
```

### `POST /governance-keys/{key_id}/rotate`

Regenerate the secret for an existing governance key. Use this when a team has lost their key. Works on both active and revoked keys (reactivates a revoked key). **New raw key is returned once — store it immediately.**

**Response**

```json
{
  "key_id": "gk-...",
  "raw_key": "gov-xxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "raw_key_hint": "gov-...xyz4",
  "org_id": "reckit_1780983190087",
  "project_id": "reckit_marketplace_1780983215793",
  "key_name": "team-alpha-key",
  "rotated_at": "2026-06-12T10:00:00.000000"
}
```

### `DELETE /governance-keys/{key_id}`

Revoke (deactivate) a governance key.

---

## 10. API Keys

### `GET /api-keys/`

List all API keys.

| Query param  | Type   | Description            |
| ------------ | ------ | ---------------------- |
| `org_id`     | string | Filter by organisation |
| `project_id` | string | Filter by project      |

### `POST /api-keys/`

Create a new API key.

### `DELETE /api-keys/{key_id}`

Delete an API key.

---

## 11. Deployments

Model deployments map a model name to an AI provider endpoint, scoped per `org_id` (and optionally `project_id` — leave it `null` to cover every project under that org).

> **Two ways a model resolves for a request:**
>
> 1. **DB row (`ModelDeployment`)** — looked up by `org_id` + `model_name`, project-specific rows preferred over org-wide ones. This is the durable, per-org way to grant access to a model.
> 2. **Env-var fallback** — used only when no DB row matches. Two fallback sets are currently wired up, each tied to one model name and applied across *any* org with no matching DB row:
>    - `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_DEPLOYMENT_NAME` / `AZURE_OPENAI_API_VERSION`
>    - `OPENAI_API_KEY` / `OPENAI_ENDPOINT` / `AZURE_DEPLOYMENT` / `OPENAI_API_VERSION`
>
>    Both can be active at once and are matched by model name per-request, so the same org/project can use both models in parallel. Because the fallback is global (not org-scoped), prefer DB rows when you need to restrict a model to specific orgs.
>
> **Auto-provisioning new orgs:** set `STANDARD_MODEL_DEPLOYMENTS` to a JSON array of deployment templates (see `app/config.py`) and every org created via `POST /organizations/` automatically gets one org-wide `ModelDeployment` row per template. For orgs that already existed before this was set up, call `POST /organizations/{org_id}/provision-deployments` (§5) to backfill.

### `GET /deployments`

List all deployments.

### `GET /deployments/{deployment_id}`

Get a specific deployment.

### `POST /deployments`

Register a new deployment.

**Request body**

```json
{
  "org_id": "reckit_1780983190087",
  "model_name": "gpt-4o",
  "provider": "azure_openai",
  "endpoint": "https://<resource>.openai.azure.com/",
  "deployment_name": "gpt-4o-prod",
  "api_version": "2024-02-01"
}
```

### `PATCH /deployments/{deployment_id}`

Update a deployment.

### `DELETE /deployments/{deployment_id}`

Deactivate a deployment.

---

## 12. Pricing

### `GET /pricing/`

List all model pricing entries (admin-managed overrides + catalogue).

### `POST /pricing/`

Create or update a pricing entry.

**Request body**

```json
{
  "model_name": "gpt-4o",
  "provider": "azure_openai",
  "input_cost_per_1k": 0.005,
  "output_cost_per_1k": 0.015
}
```

### `DELETE /pricing/{pricing_id}`

Delete a pricing entry.

---

## 13. Audit Logs

### `GET /audit-logs`

List non-sensitive audit log rows.

| Query param  | Type   | Description            |
| ------------ | ------ | ---------------------- |
| `org_id`     | string | Filter by organisation |
| `project_id` | string | Filter by project      |
| `limit`      | int    | Page size              |
| `offset`     | int    | Pagination offset      |

### `GET /audit-logs/pii`

List PII-flagged audit log rows. Requires `admin` or `security_reviewer` role.

### `GET /audit-logs/summary`

Counts grouped by category/action for compliance dashboards. Requires `admin` or `security_reviewer` role.

---

## 14. Alerts

### `GET /alerts/`

List alerts with optional filtering.

| Query param  | Type   | Description                         |
| ------------ | ------ | ----------------------------------- |
| `status`     | string | `open`, `resolved`, `dismissed`     |
| `org_id`     | string | Filter by organisation              |
| `project_id` | string | Filter by project                   |
| `alert_type` | string | e.g. `budget_exceeded`, `anomaly`   |
| `severity`   | string | `low`, `medium`, `high`, `critical` |

### `GET /alerts/counts`

Alert counts by severity for dashboard badges.

### `PATCH /alerts/{alert_id}/resolve`

Mark an alert as resolved.

### `PATCH /alerts/{alert_id}/dismiss`

Dismiss an alert.

---

## 15. Models

### `GET /models/`

List all available models.

---

## 16. Lookups

Read-only dropdown values for building UIs or validating inputs.

| Endpoint                        | Returns                                               |
| ------------------------------- | ----------------------------------------------------- |
| `GET /lookups/providers`        | Distinct provider names                               |
| `GET /lookups/request-types`    | e.g. `chat_completion`, `embedding`                   |
| `GET /lookups/request-statuses` | e.g. `success`, `error`, `blocked`                    |
| `GET /lookups/rule-metrics`     | e.g. `model_name`, `total_tokens`                     |
| `GET /lookups/rule-scopes`      | e.g. `org`, `project`                                 |
| `GET /lookups/rule-operators`   | e.g. `eq`, `gt`, `in`                                 |
| `GET /lookups/severities`       | `low`, `medium`, `high`, `critical`                   |
| `GET /lookups/plan-types`       | e.g. `free`, `pro`, `enterprise`                      |
| `GET /lookups/environments`     | e.g. `dev`, `staging`, `prod`                         |
| `GET /lookups/budget-periods`   | e.g. `daily`, `monthly`                               |
| `GET /lookups/proxy-orgs`       | Organisations observed in proxy requests              |
| `GET /lookups/proxy-projects`   | Projects observed in proxy requests                   |
| `GET /lookups/scope-references` | Entities for a given scope (org/project/user/api_key) |

---

## 17. Auth

### `GET /auth/me`

Return current authenticated user info.

### `POST /auth/logout`

Logout the current session.

---

## 18. Health

### `GET /health`

Liveness probe. Kept minimal so a slow DB or open circuit doesn't make orchestration think the instance itself is dead.

```json
{ "status": "healthy", "version": "3.0.0" }
```

### `GET /health/detailed`

Capacity/diagnostics for alerting — DB connection pool stats, Azure OpenAI concurrency/circuit-breaker state, and scheduler heartbeat. Not used as the liveness probe.

```json
{
  "status": "healthy",
  "db_pool": { "checkedout": 1, "checkedin": 2, "overflow": 0, "size": 3 },
  "azure": {
    "concurrent_in_flight": 0,
    "concurrent_max": 10,
    "circuit_open": false
  },
  "scheduler": { "running": true, "last_run": "2026-06-24T06:00:00" }
}
```

---

## 19. Rate Limits

Per-org, per-project, or per-key request/token rate limits (distinct from budgets, which are spend-based).

### `GET /rate-limits/`

List rate limits.

| Query param  | Type   | Description            |
| ------------ | ------ | ----------------------- |
| `org_id`     | string | Filter by organisation  |
| `project_id` | string | Filter by project       |

### `POST /rate-limits/`

Create a rate limit.

**Request body**

```json
{
  "org_id": "reckit_1780983190087",
  "project_id": "reckit_marketplace_1780983215793",
  "key_id": null,
  "max_requests_per_min": 60,
  "max_tokens_per_day": 1000000
}
```

### `PUT /rate-limits/{rate_limit_id}`

Update a rate limit.

### `DELETE /rate-limits/{rate_limit_id}`

Delete a rate limit.

---

## 20. Alerts — Security & Anomalies

Dedicated endpoints for the security/compliance dashboard — PII exposure, data exfiltration, misuse patterns, and usage anomalies. Distinct from the general-purpose [Alerts](#14-alerts) endpoints.

### `GET /alerts-security/summary`

Aggregate counts: total security events, PII events (incl. those flagged on proxy requests), misuse events, data-out violations, and average/highest risk score.

| Query param  | Type   | Description                  |
| ------------ | ------ | ----------------------------- |
| `org_id`     | string | Filter by organisation        |
| `project_id` | string | Filter by project             |
| `start_date` | date   | Only events on/after this date |

### `GET /alerts-security/logs`

List `DataSecurityLog` rows, merged with PII-flagged proxy requests, sorted by recency.

| Query param       | Type   | Description                     |
| ----------------- | ------ | -------------------------------- |
| `org_id`          | string | Filter by organisation           |
| `project_id`      | string | Filter by project                |
| `pii_detected`    | bool   | Filter to PII / non-PII events   |
| `misuse_detected` | bool   | Filter to misuse-flagged events  |
| `start_date`      | date   | Only events on/after this date   |
| `limit`           | int    | Page size (default 50, max 200)  |
| `offset`          | int    | Pagination offset                |

### `GET /alerts-security/anomalies/open-count`

Count of `UsageAnomaly` rows with `status = "open"`. For a dashboard badge.

### `GET /alerts-security/anomalies`

List usage anomalies (spend/usage spikes vs. baseline).

| Query param  | Type   | Description                       |
| ------------ | ------ | ----------------------------------- |
| `org_id`     | string | Filter by organisation              |
| `project_id` | string | Filter by project                   |
| `status`     | string | e.g. `open`, `resolved`             |
| `start_date` | date   | Only anomalies on/after this date   |
| `limit`      | int    | Page size (default 50, max 200)     |
| `offset`     | int    | Pagination offset                   |

### `PATCH /alerts-security/anomalies/{anomaly_id}/resolve`

Mark an anomaly as resolved.

### `GET /alerts-security/alerts`

List `Alert` rows scoped to the security dashboard (same underlying model as [`GET /alerts/`](#14-alerts)).

| Query param  | Type   | Description                       |
| ------------ | ------ | ----------------------------------- |
| `status`     | string | `open`, `resolved`, `dismissed`     |
| `org_id`     | string | Filter by organisation              |
| `project_id` | string | Filter by project                   |
| `start_date` | date   | Only alerts on/after this date      |
| `limit`      | int    | Page size (default 50, max 200)     |
| `offset`     | int    | Pagination offset                   |

### `PATCH /alerts-security/alerts/{alert_id}/resolve`

Mark an alert as resolved.

---

## Quick Reference — All Routes

| Method   | Path                            | Description                     |
| -------- | ------------------------------- | ------------------------------- |
| `POST`   | `/proxy`                        | Chat completion (non-streaming) |
| `POST`   | `/proxy/chat/completions`       | OpenAI SDK / LangChain alias    |
| `POST`   | `/proxy/v1/chat/completions`    | OpenAI SDK v1 path alias        |
| `POST`   | `/proxy/stream`                 | Streaming chat (SSE)            |
| `GET`    | `/proxy/v1/requests`            | List proxy requests (paginated) |
| `GET`    | `/proxy/stats/overview`         | Proxy headline metrics          |
| `GET`    | `/proxy/stats/trends`           | Daily request/cost trends       |
| `GET`    | `/proxy/stats/by-project-model` | Stats by project + model        |
| `GET`    | `/proxy/stats/pii`              | PII detection breakdown         |
| `GET`    | `/proxy/stats/tool-call-reliability` | Per-model zero-tool-call rate |
| `GET`    | `/proxy/v1/requests/{id}/pii-detail` | Full PII detail for one request |
| `GET`    | `/costs/summary`                | Total cost summary              |
| `GET`    | `/costs/by-model`               | Cost breakdown by model         |
| `GET`    | `/costs/by-project`             | Cost breakdown by project       |
| `GET`    | `/costs/by-org`                 | Cost breakdown by org           |
| `GET`    | `/costs/trend/daily`            | Daily cost trend                |
| `GET`    | `/costs/trend/monthly`          | Monthly cost trend              |
| `GET`    | `/costs/request/{id}`           | Single request cost detail      |
| `GET`    | `/summary/today`                | Today's live summary            |
| `GET`    | `/summary/daily`                | Daily rollup                    |
| `GET`    | `/summary/monthly`              | Monthly rollup                  |
| `GET`    | `/summary/monthly-by-model`     | Monthly by model                |
| `GET`    | `/summary/trends`               | Summary trends for charts       |
| `GET`    | `/summary/overview`             | Dashboard headline metrics      |
| `POST`   | `/summary/admin/rebuild-daily`  | Manually rebuild daily summaries |
| `GET`    | `/organizations/`               | List orgs                       |
| `POST`   | `/organizations/`               | Create org                      |
| `GET`    | `/organizations/{id}`           | Get org                         |
| `PUT`    | `/organizations/{id}`           | Update org                      |
| `DELETE` | `/organizations/{id}`           | Delete org                      |
| `POST`   | `/organizations/{id}/provision-deployments` | Backfill standard model deployments |
| `GET`    | `/projects/`                    | List projects                   |
| `POST`   | `/projects/`                    | Create project                  |
| `GET`    | `/projects/{id}`                | Get project                     |
| `PUT`    | `/projects/{id}`                | Update project                  |
| `DELETE` | `/projects/{id}`                | Delete project                  |
| `GET`    | `/governance/rules`             | List governance rules           |
| `POST`   | `/governance/rules`             | Create / update rule            |
| `GET`    | `/budgets/utilization`          | Current-month spend vs. limit    |
| `GET`    | `/budgets/`                     | List budgets                    |
| `POST`   | `/budgets/`                     | Create budget                   |
| `GET`    | `/budgets/{id}`                 | Get budget                      |
| `PUT`    | `/budgets/{id}`                 | Update budget                   |
| `DELETE` | `/budgets/{id}`                 | Delete budget                   |
| `GET`    | `/governance-keys/`             | List governance keys            |
| `POST`   | `/governance-keys/`             | Create governance key           |
| `POST`   | `/governance-keys/{id}/rotate`  | Rotate (reset) governance key   |
| `DELETE` | `/governance-keys/{id}`         | Revoke governance key           |
| `GET`    | `/api-keys/`                    | List API keys                   |
| `POST`   | `/api-keys/`                    | Create API key                  |
| `DELETE` | `/api-keys/{id}`                | Delete API key                  |
| `GET`    | `/deployments`                  | List deployments                |
| `POST`   | `/deployments`                  | Create deployment               |
| `GET`    | `/deployments/{id}`             | Get deployment                  |
| `PATCH`  | `/deployments/{id}`             | Update deployment               |
| `DELETE` | `/deployments/{id}`             | Deactivate deployment           |
| `GET`    | `/pricing/`                     | List pricing entries            |
| `POST`   | `/pricing/`                     | Create / update pricing         |
| `DELETE` | `/pricing/{id}`                 | Delete pricing entry            |
| `GET`    | `/audit-logs`                   | List audit logs                 |
| `GET`    | `/audit-logs/pii`               | PII-flagged audit logs          |
| `GET`    | `/audit-logs/summary`           | Audit log counts by category    |
| `GET`    | `/alerts/`                      | List alerts                     |
| `GET`    | `/alerts/counts`                | Alert counts by severity        |
| `PATCH`  | `/alerts/{id}/resolve`          | Resolve alert                   |
| `PATCH`  | `/alerts/{id}/dismiss`          | Dismiss alert                   |
| `GET`    | `/models/`                      | List models                     |
| `GET`    | `/lookups/*`                    | Dropdown lookup values          |
| `GET`    | `/auth/me`                      | Current user info               |
| `POST`   | `/auth/logout`                  | Logout                          |
| `GET`    | `/health`                       | Health check (liveness)         |
| `GET`    | `/health/detailed`              | Capacity diagnostics             |
| `GET`    | `/rate-limits/`                 | List rate limits                |
| `POST`   | `/rate-limits/`                 | Create rate limit               |
| `PUT`    | `/rate-limits/{id}`             | Update rate limit                |
| `DELETE` | `/rate-limits/{id}`             | Delete rate limit                |
| `GET`    | `/alerts-security/summary`      | Security event summary           |
| `GET`    | `/alerts-security/logs`         | PII / misuse / data-out log      |
| `GET`    | `/alerts-security/anomalies/open-count` | Open anomaly count       |
| `GET`    | `/alerts-security/anomalies`   | List usage anomalies             |
| `PATCH`  | `/alerts-security/anomalies/{id}/resolve` | Resolve anomaly         |
| `GET`    | `/alerts-security/alerts`      | List security alerts             |
| `PATCH`  | `/alerts-security/alerts/{id}/resolve` | Resolve security alert   |
