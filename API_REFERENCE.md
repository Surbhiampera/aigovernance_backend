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

### `POST /proxy`

Non-streaming chat completion. Returns a standard OpenAI-compatible response.

**Headers**

```
X-Governance-Key: gov-xxxxxxxxxxxx
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

| Query param    | Type   | Description                      |
| -------------- | ------ | -------------------------------- |
| `project_id`   | string | Filter by project                |
| `org_id`       | string | Filter by organisation           |
| `request_id`   | string | Fetch a specific request         |
| `request_type` | string | e.g. `chat_completion`           |
| `status`       | string | e.g. `success`, `error`          |
| `pii_only`     | bool   | Return only PII-flagged requests |
| `limit`        | int    | Page size (default 50)           |
| `offset`       | int    | Pagination offset (default 0)    |

**Response**uvicorn app.main:app --reload --host 0.0.0.0 --port 8001

```json
{
  "total": 3,
  "offset": 0,
  "items": [
    {
      "request_id": "req_abc123",
      "org_id": "org_xyz",
      "project_id": "proj_xyz",
      "model_name": "gpt-4o",
      "request_type": "chat_completion",
      "request_status": "success",
      "prompt_tokens": 1800,
      "completion_tokens": 3900,
      "total_tokens": 5700,
      "input_cost": 0.00054,
      "output_cost": 0.00117,
      "total_cost": 0.00166,
      "llm_cost": 0.00166,
      "pii_detected": false,
      "pii_types": [],
      "pii_action_taken": null,
      "provider": "azure_openai",
      "source_system": null,
      "client_ip": "127.0.0.1",
      "received_at": "2026-06-09T07:00:00",
      "created_at": "2026-06-09T07:00:01"
    }
  ]
}
```

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

### `PUT /organizations/{org_id}`

Update an organisation.

### `DELETE /organizations/{org_id}`

Delete an organisation and all related data.

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

Model deployments map a model name to an AI provider endpoint.

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

```json
{ "status": "healthy", "version": "3.0.0" }
```

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
| `GET`    | `/organizations/`               | List orgs                       |
| `POST`   | `/organizations/`               | Create org                      |
| `GET`    | `/organizations/{id}`           | Get org                         |
| `PUT`    | `/organizations/{id}`           | Update org                      |
| `DELETE` | `/organizations/{id}`           | Delete org                      |
| `GET`    | `/projects/`                    | List projects                   |
| `POST`   | `/projects/`                    | Create project                  |
| `GET`    | `/projects/{id}`                | Get project                     |
| `PUT`    | `/projects/{id}`                | Update project                  |
| `DELETE` | `/projects/{id}`                | Delete project                  |
| `GET`    | `/governance/rules`             | List governance rules           |
| `POST`   | `/governance/rules`             | Create / update rule            |
| `GET`    | `/budgets/`                     | List budgets                    |
| `POST`   | `/budgets/`                     | Create budget                   |
| `GET`    | `/budgets/{id}`                 | Get budget                      |
| `PUT`    | `/budgets/{id}`                 | Update budget                   |
| `DELETE` | `/budgets/{id}`                 | Delete budget                   |
| `GET`    | `/governance-keys/`             | List governance keys            |
| `POST`   | `/governance-keys/`             | Create governance key           |
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
| `GET`    | `/health`                       | Health check                    |
