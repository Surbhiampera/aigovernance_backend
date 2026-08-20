# Optimization Tips — Frontend Integration Spec

For the agent building the Optimization Tips page. Backend implementation:
[`app/routers/optimization_tips.py`](app/routers/optimization_tips.py),
[`app/models.py`](app/models.py) (`OptimizationTip`), rules in
[`app/services/optimization/rules/`](app/services/optimization/rules/). Design
rationale lives in [`OPTIMIZATION_TIPS_PLAN.md`](OPTIMIZATION_TIPS_PLAN.md) —
read that first if anything below is ambiguous.

## What this feature is

A daily scheduled job scans usage data and emits **tips**: concrete,
evidence-backed suggestions to cut AI spend or fix bad prompt patterns (swap
to a cheaper deployed model, cap response length, trim bloated prompts, cache
repeated prompts, stop paying for truncated responses). Each tip links to up
to 5 real request IDs that triggered it, so a user can drill into the actual
offending prompt.

Tips are **not real-time** — they're regenerated once a day by
`app/workers/tasks.py::_generate_optimization_tips`, deduplicated so an
already-open tip doesn't reappear, and a dismissed tip stays suppressed for
`TIP_COOLDOWN_DAYS` (default 14) days. Design the UI around "here's today's
advice," not a live feed.

## Base URL & auth

Same conventions as the rest of this API — see
[`How_to_use_proxyserver_REFERENCE.md`](How_to_use_proxyserver_REFERENCE.md).
Dev: `http://localhost:8000`. All endpoints below are prefixed
`/optimization-tips`. No auth dependency is currently enforced on this router
(matches `app/routers/alerts.py`, which this router mirrors) — if the app
adds a global admin-auth gate later, it applies here too, nothing
tips-specific to wire.

---

## Data model

Every tip returned by the API has this shape:

```jsonc
{
  "id": 42,
  "org_id": "org-acme",
  "org_name": "Acme Corp",
  "project_id": "proj-search",       // nullable — some tips are org-wide (e.g. oversized_prompt is project-scoped but model_name-agnostic)
  "project_name": "Search Team",     // nullable
  "model_name": "gpt-4",             // nullable — null for oversized_prompt (project-level, not model-specific)
  "tip_type": "model_substitution",  // see enum below
  "severity": "medium",              // "high" | "medium" (no "low"/"critical" currently emitted)
  "title": "Switch from gpt-4 to gpt-4o-mini for similar quality at lower cost",
  "message": "Over the last 7 days, gpt-4 cost $42.00 for 1,200,000 tokens. gpt-4o-mini is already deployed for this org, is in the same pricing category, and would have cost $0.27 for the same volume — an estimated $178.70/month saving.",
  "estimated_monthly_savings": 178.7,  // USD, float; 0 for rules that don't quantify savings (oversized_prompt, response_truncated)
  "confidence": "medium",              // "high" | "medium" | "low" — see per-rule notes below
  "evidence_json": { /* see shape below — structured, drives the "why" panel */ },
  "status": "open",                    // "open" | "dismissed" | "applied"
  "period_start": "2026-07-16",        // date the evaluation window started
  "period_end": "2026-07-23",          // date the evaluation window ended
  "created_at": "2026-07-23T02:00:11.483920"
}
```

### `tip_type` enum — what each means and how to render it

| `tip_type`            | Fires when…                                                                 | `model_name` | Typical `estimated_monthly_savings` | Suggested icon/action                          |
| ---------------------- | ---------------------------------------------------------------------------- | ------------ | ------------------------------------ | ------------------------------------------------ |
| `response_length`       | A model's completion tokens run far longer than its prompt tokens (ratio > 3x default) | set          | > 0                                   | "Cap max_tokens" — show the ratio                 |
| `model_substitution`    | A cheaper, already-deployed model exists in the same pricing category with enough context window | set (the *current* model) | > 0 | "Switch model" — show from→to in `evidence_json.params` |
| `oversized_prompt`      | p95 prompt size is far above the project median (ratio > 3x default)         | **null**     | 0 (not quantified)                    | "Trim prompt" — cause is in `evidence_json.params.cause` |
| `response_truncated`    | A meaningful share of responses hit `finish_reason == "length"`              | set          | 0 (not quantified)                    | "Raise max_tokens" — show the truncation %        |
| `cache_opportunity`     | The same (masked) prompt was sent to a model repeatedly                      | set          | > 0                                   | "Add caching" — show repeat count                 |

For `oversized_prompt`, `evidence_json.params.cause` is one of
`"tool_definitions"`, `"system_prompt"`, or `"conversation_history"` — use it
to pick a more specific icon/copy than the generic message if you want
(the `message` field already contains rule-written coaching text, so
rendering `message` verbatim is enough for a first pass).

### `evidence_json` shape (stable contract, same across all 5 rules)

```jsonc
{
  "rule": "model_substitution",                 // == tip_type
  "observed": { "metric": "blended_cost", "value": 42.0, "unit": "USD" },
  "baseline": { "metric": "blended_cost", "value": 0.27, "unit": "USD" },
  "sample_request_ids": ["req-a1b2c3", "req-d4e5f6"],  // up to 5 — use these to drill in, see below
  "sample_size": 1200000,                        // meaning varies by rule (token count, request count, hit count — see message text)
  "window_days": 7,
  "params": { "from_model": "gpt-4", "to_model": "gpt-4o-mini" }  // rule-specific extra context, see table below
}
```

`params` per rule:

- `response_length`: `{"model": "...", "excess_completion_tokens": 12345}`
- `model_substitution`: `{"from_model": "...", "to_model": "..."}`
- `oversized_prompt`: `{"cause": "tool_definitions"|"system_prompt"|"conversation_history", "avg_system_tokens": 812.0, "avg_tool_definition_tokens": 1600.0}`
- `response_truncated`: `{"model": "...", "truncated_count": 3}`
- `cache_opportunity`: `{"model": "...", "prompt_hash_prefix": "a1b2c3d4e5f6a1b2"}` — a SHA-256 prefix, **never** raw prompt text. Nothing in `evidence_json` ever contains prompt content, for any rule.

`observed`/`baseline` metric names also vary by rule (`output_input_ratio`,
`blended_cost`, `p95_input_tokens` / `median_input_tokens`,
`truncation_rate`, `duplicate_hits`) — treat them as display-only labels, not
something to branch UI logic on beyond picking a unit suffix.

---

## Endpoints

### `GET /optimization-tips/`

List tips with filters. This is the main feed for the page.

| Query param  | Type   | Default  | Notes                                                   |
| ------------ | ------ | -------- | -------------------------------------------------------- |
| `status`     | string | `"open"` | `open` \| `dismissed` \| `applied`. Pass empty/omit for all — actually omitting still defaults to `"open"` server-side, so pass `status=` explicitly if you want all statuses (any falsy value skips the filter). |
| `org_id`     | string | none     | Filter by org                                             |
| `project_id` | string | none     | Filter by project                                         |
| `tip_type`   | string | none     | One of the 5 values above                                  |
| `severity`   | string | none     | `high` \| `medium`                                        |
| `limit`      | int    | 100      | 1–500                                                      |
| `offset`     | int    | 0        | Pagination offset                                          |

Response:

```jsonc
{
  "total": 7,
  "offset": 0,
  "items": [ /* array of tip objects, see Data model above, newest first */ ]
}
```

### `GET /optimization-tips/summary`

Dashboard header — counts and total savings grouped by `tip_type`, **open
tips only** (status filter is not exposed here; it's always `open`).

| Query param  | Type   | Notes            |
| ------------ | ------ | ---------------- |
| `org_id`     | string | optional filter  |
| `project_id` | string | optional filter  |

Response:

```jsonc
{
  "by_tip_type": {
    "model_substitution": { "count": 3, "estimated_monthly_savings": 412.5 },
    "cache_opportunity":  { "count": 2, "estimated_monthly_savings": 88.0 },
    "response_truncated": { "count": 2, "estimated_monthly_savings": 0.0 }
  },
  "total_open": 7,
  "total_estimated_monthly_savings": 500.5
}
```

Use this to render the top-of-page stat tiles (e.g. "$500.50/mo potential
savings across 7 open tips") before the list loads.

### `PATCH /optimization-tips/{tip_id}/dismiss`

User says "not useful" / "won't do this." Sets `status = "dismissed"`. The
tip is suppressed from reappearing for `TIP_COOLDOWN_DAYS` (14 days) even if
the same condition still holds tomorrow.

Response: the updated tip object (same shape as a list item).
404 if `tip_id` doesn't exist.

### `PATCH /optimization-tips/{tip_id}/apply`

User says "I did this." Sets `status = "applied"` — purely a tracking marker
for "which advice got acted on," no side effects on the underlying system
(it does not, for example, actually change a model deployment).

Response: the updated tip object. 404 if `tip_id` doesn't exist.

### `POST /optimization-tips/admin/rebuild`

Manual re-trigger of the daily job (mirrors `POST
/summary/admin/rebuild-daily`). Useful for an admin "refresh tips now" button,
or for a "run once for testing" action — but note this evaluates real data
and inserts real rows (subject to the same dedup/cooldown rules as the
scheduled run), it's not a dry-run/preview.

| Query param  | Type | Default        | Notes                                    |
| ------------ | ---- | -------------- | ------------------------------------------ |
| `window_end` | date | today (`YYYY-MM-DD`) | End of the lookback window to evaluate |

Response:

```jsonc
{ "inserted": 3 }
```

`0` is a normal, expected result — it means either nothing crossed a
threshold, or every qualifying tip is already open/in cooldown. Don't treat
`inserted: 0` as an error state in the UI.

---

## Drilling into a tip's evidence

`evidence_json.sample_request_ids` holds up to 5 real request IDs. To show
"here's the actual prompt/request that triggered this," call one of the
existing request endpoints (already built, no new backend work needed):

- **`GET /proxy/v1/requests?request_id={id}`** — general request detail:
  tokens, cost, model, status, latency. Good for a compact "request card."
- **`GET /proxy/v1/requests/{id}/pii-detail`** — despite the name, this is
  the endpoint that returns prompt text: `original_prompt_text`,
  `sanitized_prompt_text`, `response_text`, plus PII fields. Use this when
  the user wants to actually read the prompt that caused the tip (e.g. for
  `oversized_prompt` or `cache_opportunity`).

Neither the tip list nor summary endpoint ever returns raw prompt text
directly — that's intentional (see "No PII leak" in the plan's verification
section). Drill-down is the only path to prompt content, and it goes through
the existing endpoint's own access rules.

---

## Suggested page composition

1. **Header stat row** — from `GET /optimization-tips/summary`: total open
   tips, total estimated monthly savings, maybe a per-`tip_type` breakdown as
   small tiles.
2. **Filter bar** — `tip_type`, `severity`, `org_id`/`project_id` (if the app
   has an org/project switcher already, wire into that instead of a
   standalone filter), and a status toggle (Open / Dismissed / Applied).
3. **Tip list/cards** — one per tip: severity badge, title, message
   (pre-written, human-readable — safe to render directly), estimated
   savings badge (hide if `0`), confidence badge, and Dismiss/Apply buttons
   (disable both once `status != "open"`, or show which action was taken).
4. **Drill-down** — clicking a tip expands/opens `evidence_json` in a
   readable form (observed vs. baseline, sample size, window) plus buttons
   to open each `sample_request_ids` entry via the endpoints above.
5. **Empty state** — "No optimization tips right now" is a *good* outcome,
   not a broken page; don't design it as an error/loading state.

No websocket/polling needed — tips regenerate once a day. A simple
refetch-on-mount (plus the manual rebuild admin action, if you're building an
admin surface) is sufficient.
