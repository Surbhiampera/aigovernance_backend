# Governance Tool — Workflow, Logic & Output

This document describes what actually happens inside the AI Governance Proxy for a single request, based on the current code in `app/routers/proxy.py` and the services it calls. It is meant as a ground-truth reference, not an aspirational spec — where enforcement described in a service module is not actually wired into the live request path, that is called out explicitly.

## 1. Entry points

All traffic goes through `app/routers/proxy.py`, mounted at `/proxy`:

| Route | Purpose |
|---|---|
| `POST /proxy` | Canonical non-streaming chat completion endpoint |
| `POST /proxy/chat/completions`, `POST /proxy/v1/chat/completions` | Thin aliases so the OpenAI SDK (which appends `/chat/completions` to `base_url`) works unmodified |
| `POST /proxy/stream` | SSE streaming variant |
| `GET /proxy/stats/*`, `GET /proxy/v1/requests*` | Read-only admin/stats endpoints |

There is no separate embeddings endpoint — every model type goes through the same gateway; deployment resolution is driven by the `model` field in the request body, not by the route.

## 2. Request pipeline (pre-flight, shared by both chat and stream paths)

Implemented in `_run_pre_flight()`. Each stage that blocks the request writes an `AiRequest` row with `request_status="blocked"` before raising, so blocked traffic is still auditable.

1. **Authenticate** — `verify_governance_key()` hashes the `X-Governance-Key` header (SHA-256) and looks up an active, non-expired `ApiKey` row. Failure → `401`, no request row written at all.
2. **Rate limit check** — runs *before* the body is parsed, deliberately, as the cheapest gate. See §4. Failure → `429`. Any non-HTTPException error here is swallowed and logged — **rate limiting fails open**.
3. **Parse JSON body** — malformed JSON → `400`.
4. **Resolve model / deployment** — looks up candidate provider deployments for the requested model name. No match → `404`. *(Model allow/block-list rules are not checked here — see §6.)*
5. **Budget check** — see §5. Failure → `429`. Non-HTTPException errors are swallowed — **budget enforcement fails open**.
6. **PII scan** — see §7. A block verdict → `403`. Non-HTTPException errors are swallowed and the **original, unmasked** messages are forwarded — **PII scanning fails open**.
7. **Store `AiRequest`** — writes the row (`request_status="pending"`, original + sanitized text, PII metadata) and commits, so the request is durable before it ever reaches the upstream provider.
8. **Build outbound attempts** — one per candidate deployment, primary first, enabling failover without repeating steps 1–7.

## 3. Forwarding & response handling

- A process-wide concurrency limiter caps simultaneous upstream calls; if full → `503` immediately, no cost incurred.
- The request is forwarded to Azure OpenAI (or other configured provider) with retry-with-backoff on 5xx/connection errors, and failover to the next candidate deployment on 429/5xx/connection errors.
- **Azure 429** → passed through as `429` with Azure's own error body; no cost recorded.
- **Azure other error** → passed through with Azure's status code; cost *is* still recorded if Azure's error body includes a `usage` field (e.g. content-filter rejections).
- **Azure unreachable** → `502`.
- **Circuit breaker open** (repeated upstream failures) → `503`.
- On success: token usage is read from Azure's own `usage` field when present, falling back to a tiktoken estimate otherwise (see §8). Cost calculation and the `AiResponse` / `TokenUsage` / `RequestCost` / `AuditLog` / `RouteExecution` writes happen in a **background task after the client already has the response** — the client is never blocked on bookkeeping.
- The client receives the **raw provider response body, verbatim**, plus an added `X-Request-Id` header.

### Worst-case latency budget — callers must set their HTTP timeout above this

Per-attempt and total retry/failover budgets (`app/config.py`, `get_azure_read_timeout_seconds` / `get_azure_total_deadline_seconds`):

- `AZURE_READ_TIMEOUT_SECONDS` (default **60s**) — how long a single attempt against one provider/deployment is allowed to hang before it's treated as failed.
- `AZURE_TOTAL_DEADLINE_SECONDS` (default **90s**) — hard wall-clock cap across *every* retry and every failover candidate combined, kept under Azure App Service's fixed 230s front-end timeout so this app's own error handling always returns first.
- On top of that 90s, add whatever the pre-flight pipeline itself takes (auth, rate limit, budget, PII scan, DB writes — normally milliseconds, but not zero) before the upstream call even starts.

**A calling client's own HTTP timeout must be set comfortably above this — at least 100–120s for non-streaming `/proxy*` calls.** A client timeout at or below ~60–90s can fire *while this backend is still legitimately retrying/failing over*, before its own 502/503 has a chance to return. Seen in practice: a client with a 60s `requests` timeout getting `ReadTimeout` on a slow-but-in-progress request, then silently calling the provider directly as a "fallback" — which skips every governance control (budget, PII, rate limit, audit) for that request. That's not this backend being down; it's the caller giving up before this backend's own retry budget was exhausted.

### Streaming (`/proxy/stream`)
Same pre-flight pipeline. Tokens are **always** tiktoken-estimated (Azure doesn't reliably report usage inside SSE chunks). DB writes happen synchronously at the end of the generator, not backgrounded. A stream that drops mid-way is recorded as `request_status="partial"`, `failure_code="stream_incomplete"`, and still bills whatever tokens were actually produced.

## 4. Rate limiting (`rate_limit_service.py`)

- Rows in `rate_limits` can target a key, a project, or an org; **all matching rows are evaluated**, and the first one exceeded blocks the request.
- Two independent limit types per row: `max_requests_per_min` and `max_tokens_per_day`.
- **Requests/min** uses a **fixed 60-second window counter** (bucketed by `time // 60`), stored in Redis (`INCR` + `EXPIRE`), falling back to a Postgres count of recent `AiRequest` rows if Redis is down.
- **Tokens/day** is incremented once per completed request (after the real cost/usage is known), stored in Redis per UTC calendar day, with a Postgres fallback.
- At the pre-flight call site the model isn't known yet (`model=""`), so **only rules with `tool_name` = `NULL`/`'*'` can actually match** — per-model rate limits are effectively unenforceable at this stage.
- Violation → `429` with `Retry-After` header and body `{"error":"rate_limit_exceeded","scope","limit_type","limit","current_count"/"tokens_used_today","retry_after","request_id"}`.

## 5. Budget enforcement (`budget_service.py`)

- "Spend" = `SUM(RequestCost.total_cost)` for the current calendar month — i.e. actual computed cost of completed requests, not a live estimate.
- Checked in order: **project** budget first (if one exists with a positive limit), then **org** budget.
- `ratio = spend / limit`:
  - `ratio >= 1.0` → **hard block**, `429`, audit + `critical` alert.
  - `ratio >= alert_threshold_percent` (default 80%) → **warning only**, request proceeds, `high` alert (existing active alert updated in place rather than duplicated).
- Violation body: `{"error":"budget_exceeded","scope","budget_limit","current_spend","currency":"USD","request_id"}`.

## 6. Model allow/block lists & token ceilings — ⚠️ not currently enforced

`governance_rule_service.py` defines `check_governance_rules()` (blocked-model / allowed-model / max-output-tokens) and `check_max_input_tokens()`, with a clear precedence: unknown pricing → block-list → allow-list → output-token ceiling, each writing an audit row and an alert on violation.

**However**, a repo-wide search confirms neither function is called anywhere outside its own definition:

```
$ grep -rn "check_governance_rules\|check_max_input_tokens" app/
app/services/governance_rule_service.py:37:def check_governance_rules(
app/services/governance_rule_service.py:78:def check_max_input_tokens(
```

The logic is fully implemented and the `governance_rules` table is populated/administered via its router, but it is **not wired into `proxy.py`'s live request path**. Today, model allow/block lists and token ceilings have no effect on traffic. This is worth fixing or explicitly deprecating.

A second, related gap: `_load_rules()` in the same module filters only `org_id`, never `project_id`, even though `GovernanceRule` has a `project_id` column — so even once wired in, rules would be org-wide only, not project-scoped as the schema implies.

## 7. PII detection & masking (`pii_engine.py`)

- Engine: Microsoft Presidio (`AnalyzerEngine` + spaCy `en_core_web_lg`) plus two custom regex recognizers for Indian **Aadhaar** and generic **national ID / PAN**-style numbers.
- Entities detected: email, phone, credit_card, ssn, ip_address, date_of_birth, name, location, national_id, passport, bank_account (IBAN), crypto, aadhar, organization, url.
- Per-entity-type policy (`PiiPolicy` table, org-specific rows override global defaults): `block` (403), `mask` (Presidio anonymizer replaces with `[EMAIL]`-style placeholder), `alert` (logged only), `allow` (untouched). Defaults: `location`/`organization`/`url` → allow; everything else → mask.
- **Severity** (`compute_pii_severity`) — fixed by a recent commit that changed the signature from `(pii_types, total_count)` to `(entity_types)`:
  ```python
  HIGH_SENSITIVITY = {aadhar, credit_card, passport, ssn, bank_account, national_id}
  LOW_SENSITIVITY_DEFAULT_ALLOW = {location, organization, url}

  any high-sensitivity type present        → "high"
  ≥5 "significant" (non-low) types present → "high"
  ≥3 "significant" types present           → "medium"
  otherwise                                 → "low"
  ```
  Previously, severity escalated to `"high"` once *any* 5 PII detections occurred, regardless of type — so repeated low-sensitivity hits (organization/location/url) wrongly pushed nearly every flagged request to High. Now only non-low-sensitivity entities count toward the volume thresholds, while a single occurrence of a genuinely sensitive type (SSN, credit card, Aadhaar, passport, bank account, national ID) still forces High immediately.

## 8. Token counting (`token_counter.py`)

- Uses `tiktoken`, resolving an encoding via `encoding_for_model()` with prefix-based fallback (`gpt-4`, `gpt-3.5`, `text-embedding`, `claude`, `gemini`, `mistral`, `llama` all map to `cl100k_base`).
- **Non-streaming**: prefers the provider's own reported `usage.prompt_tokens`/`completion_tokens`; tiktoken is only a fallback when the provider omits usage.
- **Streaming**: always tiktoken-estimated, both sides.
- The source of each count is recorded (`TokenUsage.input_token_source`/`output_token_source` = `"azure"` or `"tiktoken_estimate"`), and `TokenUsage.is_estimated` is set whenever either side isn't a real provider-reported count.
- Vision/image tokens are estimated separately using OpenAI's official image-tiling formula when `image_url` blocks are present.

## 9. Cost calculation

Note: there are **two independent cost-calculation code paths** in this codebase:

- **`proxy.py`'s own `_calculate_cost()`** — used by the live proxy request path. Lookup order: DB `model_pricing` row scoped to `(model_name, provider)` → DB row scoped to `model_name` only → static `PROVIDER_PRICING` dict → static `MODEL_PRICING` catalogue (~35 models). If nothing matches, cost is recorded as **$0** with `cost_model_type="unknown"` and a warning is logged.
- **`cost_engine.py`'s `CostEngine`** — used only by the separate telemetry/ingestion pipeline (`app/services/ingestion/`), not by `/proxy`. It has its own 3-tier lookup plus infra-cost (latency, data transfer) calculation.

These two paths can drift independently since they don't share pricing-resolution code; worth reconciling if pricing changes need to apply consistently to both the proxy and the ingestion pipeline.

Model name aliases (e.g. `gpt4o` → `gpt-4o`) are normalized before lookup. All costs are stored to 6 decimal places (`Decimal("0.000001")`).

## 10. Key tables written per request

| Table | Written by | Contents |
|---|---|---|
| `ai_requests` | pre-flight, step 7 | request/response status, original + masked text, PII metadata, model/deployment |
| `ai_responses` | post-forward (background for non-stream) | raw response payload, tool calls, finish reason, latency |
| `token_usage` | post-forward | input/output/total tokens, source (`azure`/`tiktoken_estimate`) |
| `request_cost` | post-forward | per-token costs, total cost, currency, pricing snapshot |
| `route_executions` | post-forward | latency breakdown by pipeline phase |
| `audit_logs` | every enforcement decision | category/action/status, compliance flags, linked request |
| `alerts` | budget/governance violations | type, severity, threshold vs actual, active/resolved/dismissed |

## 11. Background aggregation (`app/scheduler.py`)

Two in-process APScheduler jobs (`coalesce=True, max_instances=1`):

- **Hourly** — rebuilds `DailyOrgSummary` for today from `RequestCost` + `AiRequest`, then runs anomaly detection (`UsageAnomaly`) comparing today's metrics against a historical baseline.
- **Every 24h** — rebuilds `MonthlyOrgSummary` from the current month's `DailyOrgSummary` rows.

Both are idempotent (delete-then-reinsert for the current period) and can be manually re-triggered via `POST /summary/admin/rebuild-daily?days_back=N` for backfill.

## 12. Output surfaces (dashboards / admin APIs)

All dashboard data is read from the tables in §10 (live) or §11 (pre-aggregated, up to ~24h stale for monthly figures):

| Router | Key endpoints | Shape |
|---|---|---|
| `audit_logs.py` | `GET /audit-logs`, `/audit-logs/pii` (RBAC: admin/security_reviewer), `/audit-logs/summary` | `{total, offset, items[]}` of audit rows, or grouped counts |
| `costs.py` | `/costs/by-model`, `/by-project`, `/by-org`, `/trend/daily`, `/trend/monthly`, `/request/{id}`, `/summary` | Arrays/dicts of token & cost totals |
| `summary.py` | `/summary/today`, `/daily`, `/monthly`, `/trends`, `/overview` | Live or pre-aggregated dashboard totals; `/overview` includes `active_alerts`, `rules_active`, `budgets_at_risk` |
| `alerts.py` | `GET /alerts/`, `PATCH /alerts/{id}/resolve|dismiss`, `/alerts/counts` | Enriched alert list/detail, severity counts |

## 13. Failure-response summary

| Condition | Status | Body |
|---|---|---|
| Invalid/expired governance key | 401 | plain string |
| Rate limit exceeded | 429 | `rate_limit_exceeded` + scope/limit/retry_after |
| Malformed body / missing model | 400 | plain string |
| Model not configured for org/project | 404 | plain string |
| Budget exceeded | 429 | `budget_exceeded` + scope/limit/current spend |
| PII block | 403 | `pii_block` + entity types |
| Upstream 429 | 429 | provider's own error body |
| Upstream other error | same as upstream | provider's own error body |
| Upstream unreachable | 502 | plain string |
| Concurrency limiter full | 503 | plain string |
| Circuit breaker open | 503 | plain string |

## 14. Known gaps worth addressing

1. **Model allow/block lists and token ceilings are implemented but not wired in** (§6) — currently have zero effect on live traffic.
2. **`governance_rules` filtering ignores `project_id`** — even once wired in, rules would apply org-wide only.
3. **Two independent, divergent cost-calculation implementations** (§9) — proxy path vs. ingestion/telemetry path.
4. **Rate limiting, budget checks, and PII scanning all fail open** on unexpected exceptions (steps 2, 5, 6 in §2) — a bug in any of these services silently disables that control rather than blocking the request.
5. **Per-model rate limits can't match** at the pre-flight call site since the model isn't resolved yet when the rate-limit check runs (§4).
6. **No enforced minimum caller-side timeout** — this backend's own retry/failover budget can legitimately run up to ~90s (§3), but nothing stops a client from calling in with a shorter HTTP timeout. When that happens the client sees a bare connection-level `ReadTimeout`, not one of this backend's own error bodies, and (per at least one observed integration) falls back to calling the provider directly and unaudited. Worth either documenting this loudly at integration time (done, see §3) or adding a fast-fail response once elapsed time exceeds a client-realistic threshold instead of continuing to retry silently.
