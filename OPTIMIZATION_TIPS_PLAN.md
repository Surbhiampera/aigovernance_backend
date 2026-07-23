# Optimization Tips Engine (PRD §5.2 / feature 2.2)

## Context

The platform records every AI request's tokens, cost, model, and prompt text, but nothing
turns that data into advice. Orgs can see _that_ they spent money; nothing tells them _how
to spend less_. PRD §5.2 asks for a tips engine; this plan implements it.

**Intended outcome:** a scheduled job that samples outlier requests/responses and emits
concrete, evidence-backed suggestions — swap to a cheaper deployed model, cap response
length, trim bloated prompts, cache repeated prompts — each linked to the actual request
IDs that triggered it so an engineer can drill in and see the offending prompt.

**Two PRD blockers turned out not to exist** (verified in code):

1. _"Rule 2 depends on `governance_rule_service` being wired live."_ It does not.
   `governance_rule_service` is still uncalled from `proxy.py` (grep confirms), but the
   tips job can read `GovernanceRule` rows itself via
   [`_load_rules()`](app/services/governance_rule_service.py#L106) and intersect with
   [`get_deployments_for_org()`](app/services/deployment_service.py#L83). Suggesting only
   models the org has actually deployed _and_ isn't block-listing is a stricter filter than
   the block-list alone. Rule 2 ships in v1.

2. _"Duplicate detection blocked on a prompt-retention decision."_ Prompt text is already
   retained. [models.py:650-653](app/models.py#L650-L653) declares `prompt_text`,
   `sanitized_prompt_text`, `messages`, `system_prompt`; [proxy.py:505-507](app/routers/proxy.py#L505-L507)
   populates them. Hash `sanitized_prompt_text` (post-PII-mask) — the safe choice is already
   available. **This answers PRD open question #1.**

**Third finding — a real gap.** `TokenUsage.cached_tokens`, `prompt_tokens`,
`completion_tokens`, `system_tokens`, `tool_definition_tokens`, `context_utilization_pct`
and `AiRequest.num_messages`, `has_system_prompt`, `prompt_char_count` are declared but
**never written** — [proxy.py:683-695](app/routers/proxy.py#L683-L695) writes only
`input_tokens`/`output_tokens`/`total_tokens`. Without these, a tip can say "your prompts
are large" but not "4k of it is unused tool definitions." Step 1 closes this.

Also confirmed available and populated: `AiResponse.finish_reason` (`"length"` ⇒ truncation,
the strongest evidence for a max-tokens tip) and `AiRequest.has_tool_definitions`.

## Scope (agreed)

- Rules 1, 2, 3 + duplicate/cache detection + truncation rule
- Backfill the missing token/prompt-shape columns in the proxy write path
- New `optimization_tips` table + router (mirrors `alerts.py`)
- Static f-string templates for tip text; `evidence_json` structured so an LLM pass can be
  layered on later with no schema change

---

## Step 1 — Populate the missing prompt-shape columns

**Files:** [app/routers/proxy.py](app/routers/proxy.py)

Only step that touches the request path. Everything derives from data already in hand — no
extra provider calls, no new dependency.

In `_store_request()` ([proxy.py:455-520](app/routers/proxy.py#L455)), alongside the existing
`prompt_text=_concat_message_text(...)` at line 506, add:

- `num_messages=len(original_messages or [])`
- `has_system_prompt=any(m.get("role") == "system" for m in original_messages or [])`
- `prompt_char_count=len(prompt_text or "")`
- `system_prompt=` the concatenated `role == "system"` message content

In the `TokenUsage(...)` writes ([proxy.py:683](app/routers/proxy.py#L683) non-stream and
[proxy.py:958](app/routers/proxy.py#L958) stream), add:

- `prompt_tokens=input_tokens`, `completion_tokens=output_tokens` — mirror the existing
  values so downstream queries can use either name
- `system_tokens` — `count_tokens(system_prompt, model)` via
  [`app/services/token_counter.py`](app/services/token_counter.py) (already a dependency)
- `tool_definition_tokens` — `count_tokens(json.dumps(body["tools"]), model)` when
  `has_tool_definitions`
- `cached_tokens` / `uncached_tokens` — read from the provider usage block when present
  (`prompt_tokens_details.cached_tokens` on OpenAI/Azure; `cache_read_input_tokens` on
  Anthropic), else 0
- `context_window_limit` + `context_utilization_pct` — from `MODEL_PRICING[...].context_window`
  in [ai_model_pricing.py](app/services/ai_model_pricing.py)

**Reuse, do not reimplement:** `_concat_message_text()` ([proxy.py:448](app/routers/proxy.py#L448)),
`count_tokens()` ([token_counter.py:47](app/services/token_counter.py#L47)),
`get_model_pricing_for_provider()` ([ai_model_pricing.py:184](app/services/ai_model_pricing.py#L184)).

All of this runs in the existing post-response background task, never before the upstream
call. Wrap the tiktoken calls in the same fail-open style used elsewhere — a token-count
failure must never fail a request.

---

## Step 2 — `optimization_tips` table

**Files:** [app/models.py](app/models.py), [schema_clean.sql](schema_clean.sql)

Both in the same commit — `_verify_schema()` in [main.py](app/main.py#L83) refuses to boot if
`models.py` declares a column the DB lacks.

Model, placed next to `UsageAnomaly` ([models.py:304](app/models.py#L304)) and following its
column conventions:

```python
class OptimizationTip(Base):
    __tablename__ = "optimization_tips"
    __table_args__ = {"extend_existing": True}

    id             = Column(BigInteger, primary_key=True)
    org_id         = Column(String(100), nullable=False)
    project_id     = Column(String(100), nullable=True)
    model_name     = Column(String(120), nullable=True)
    tip_type       = Column(String(60), nullable=False)
    severity       = Column(String(20), nullable=False, default="medium")
    title          = Column(String(255), nullable=True)
    message        = Column(Text, nullable=True)
    estimated_monthly_savings = Column(Numeric(14, 6), default=0)
    confidence     = Column(String(20), nullable=True)   # low | medium | high
    evidence_json  = Column(JSON, nullable=True)
    status         = Column(String(20), nullable=False, default="open")
    period_start   = Column(Date, nullable=True)
    period_end     = Column(Date, nullable=True)
    created_at     = Column(DateTime, server_default=func.now())
```

Matching DDL in `schema_clean.sql` next to the `usage_anomalies` block
([schema_clean.sql:555](schema_clean.sql#L555)), plus an index mirroring
`ix_usage_anomalies_org_created`:

```sql
CREATE INDEX IF NOT EXISTS ix_optimization_tips_org_created
    ON optimization_tips (org_id, created_at DESC);
```

`optimization_tips` is a live table — do **not** add it to `_UNUSED_TABLES` in
[main.py:40](app/main.py#L40).

### `evidence_json` shape (stable contract)

Every rule emits the same envelope. This is what makes tips drillable and what a future
LLM-generation pass consumes.

```json
{
  "rule": "model_substitution",
  "observed": { "metric": "blended_cost_per_1k", "value": 4.25, "unit": "USD" },
  "baseline": { "metric": "blended_cost_per_1k", "value": 0.3, "unit": "USD" },
  "sample_request_ids": ["req-...", "req-..."],
  "sample_size": 1284,
  "window_days": 7,
  "params": { "from_model": "gpt-4o", "to_model": "gpt-4o-mini" }
}
```

`sample_request_ids` holds up to 5 outlier request IDs. These feed the existing request-detail
endpoint ([proxy.py:2728](app/routers/proxy.py#L2728)), which already returns
`original_prompt_text` / `sanitized_prompt_text` — so a UI links tip → offending prompt with
no new endpoint.

---

## Step 3 — Rule registry

**New files:** `app/services/optimization/__init__.py`, `registry.py`, `rules/*.py`

Follow the existing plugin pattern in
[app/services/ingestion/registry.py](app/services/ingestion/registry.py) exactly — PRD §5.6
says no new pattern is needed. Class decorator `@tip_registry.register`, `__init__.py`
imports the rules package to trigger registration.

```python
class TipRule:
    tip_type: str          # e.g. "model_substitution"
    def evaluate(self, *, db: Session, org_id: str, project_id: str | None,
                 window_start: date, window_end: date) -> list[dict]: ...
```

Each rule returns dicts ready to become `OptimizationTip` rows. A rule that raises is logged
and skipped — one broken rule must not kill the job.

---

## Step 4 — The five rules

**New file:** `app/services/optimization/rules/` (one module per rule)

Thresholds go in [app/config.py](app/config.py) using the existing `_dec` / `_int` accessor
pattern ([config.py:192-209](app/config.py#L192)), named `TIP_*`.

### Rule 1 — output/input ratio (`response_length`)

Aggregate `DailyOrgSummary.total_completion_tokens / total_prompt_tokens` per
(org, project, tool_name) over the window. Flag above `TIP_OUTPUT_INPUT_RATIO` (default 3.0),
minimum `TIP_MIN_REQUESTS` (default 50) requests. Tip: set a `max_tokens` cap.
Savings = excess output tokens × the model's `output_per_1m`.

Note: `DailyOrgSummary.tool_name` holds the **model name** — see
[tasks.py:108](app/workers/tasks.py#L108). Confusing but correct.

### Rule 2 — model substitution (`model_substitution`)

1. Per (org, project, model), sum `RequestCost.total_cost`, `input_tokens`, `output_tokens`.
2. Candidate set = `get_deployments_for_org(db, org_id=…, project_id=…)`
   ([deployment_service.py:83](app/services/deployment_service.py#L83)) — only models the org
   can actually reach. Drop anything block-listed per `_load_rules()`
   ([governance_rule_service.py:106](app/services/governance_rule_service.py#L106)).
3. Keep candidates with the same `MODEL_PRICING[...].category` and a `context_window` ≥ the
   observed peak `TokenUsage.total_tokens` for that project (from Step 1's
   `context_window_limit` work).
4. Recost the actual token volume at the candidate's rates; emit if projected saving exceeds
   `TIP_MIN_MONTHLY_SAVINGS` (default $5).

**Cost-accuracy requirement:** the projection must use the same lookup order the proxy bills
with, or tips will quote numbers that disagree with the invoice. `_calculate_cost()` currently
lives inside [proxy.py:535](app/routers/proxy.py#L535) with a 5-tier precedence
(DB `model_pricing` provider+model → DB model-only → `PROVIDER_PRICING` → `MODEL_PRICING` →
config default). **Extract it into `app/services/cost_lookup.py` and have both `proxy.py` and
this rule import it.** Do not duplicate the logic — `cost_engine.py` already drifted from
`proxy.py` this way (noted in CLAUDE.md); don't make it three.

Confidence is `"medium"` — mark it explicitly, since equivalent-category is a heuristic, not
a quality guarantee.

### Rule 3 — oversized prompt (`oversized_prompt`)

Percentile query over `TokenUsage.input_tokens` joined to `AiRequest` per project.
Flag when p95 exceeds `TIP_PROMPT_OUTLIER_RATIO` (default 3.0) × the project median.
**No tiktoken re-tokenization** — the counts are already stored.

With Step 1 in place, split the cause using `system_tokens` and `tool_definition_tokens`, and
pick the template accordingly:

- tool defs dominate → "trim unused tool definitions"
- system prompt dominates → "the system prompt is resent every turn; shorten or cache it"
- neither → "conversation history is growing unbounded; truncate or summarize older turns"

This is the rule that produces the prompt/context-engineering coaching.

### Rule 4 — truncation (`response_truncated`)

Ratio of `AiResponse.finish_reason == "length"` per (org, project, model) over the window.
Flag above `TIP_TRUNCATION_RATE` (default 0.15) with ≥ `TIP_MIN_REQUESTS`. Cheapest rule —
the column is already populated. Tip: raise `max_tokens` or shorten the ask; truncated
responses are paid for and discarded.

### Rule 5 — duplicate / cache opportunity (`cache_opportunity`)

`SHA-256` over `AiRequest.sanitized_prompt_text` (the **masked** field — never `prompt_text`).
Group by hash per (org, project, model) over the window; flag hashes with ≥
`TIP_DUPLICATE_MIN_HITS` (default 5) repeats. Savings = (repeats − 1) × that prompt's input
cost. Tip: prompt caching or a response cache.

Two safeguards:

- Compute the hash in SQL (`encode(sha256(convert_to(sanitized_prompt_text,'UTF8')),'hex')`)
  and `GROUP BY` it — do not pull prompt bodies into Python.
- Store only the **hash prefix** in `evidence_json`, never prompt text. Drill-down goes
  through `sample_request_ids` and the existing detail endpoint, which enforces its own access
  rules.

---

## Step 5 — Scheduled job

**Files:** [app/workers/tasks.py](app/workers/tasks.py), [app/scheduler.py](app/scheduler.py)

Add `_generate_optimization_tips(*, db, window_end: date)` to `tasks.py`, following the shape
of `_detect_daily_anomalies` ([tasks.py:216](app/workers/tasks.py#L216)) — including its
**dedup-by-key** approach, which matters more here: a tip regenerated daily becomes noise.
Dedup on `(org_id, project_id, model_name, tip_type)` against open tips, and skip if an
identical tip is already `open` or was `dismissed` within `TIP_COOLDOWN_DAYS` (default 14).
A dismissed tip must not reappear next morning.

Register in `scheduler.py` as a **separate daily job** (`id="optimization_tips"`, `hours=24`),
not appended to `_job_daily_aggregation`. Reasons: Rule 5's hashing pass is the heaviest query
in the system and must not delay the hourly rollup; and the thread pool is only
`SCHEDULER_MAX_WORKERS` (default 2). Add `"optimization_tips": None` to the `_last_success`
dict ([scheduler.py:21](app/scheduler.py#L21)) so it shows in the heartbeat.

Requires `DailyOrgSummary` to be current — it is, since `daily_agg` runs hourly.

---

## Step 6 — Router

**New file:** `app/routers/optimization_tips.py`

Copy the structure of [app/routers/alerts.py](app/routers/alerts.py) verbatim — same
`_serialize` / `_enrich` org+project name-joining, same query-param filters, same
`{"total", "offset", "items"}` envelope.

| Endpoint                                | Purpose                                                                             |
| --------------------------------------- | ----------------------------------------------------------------------------------- |
| `GET /optimization-tips`                | filters: `status`, `org_id`, `project_id`, `tip_type`, `severity`; `limit`/`offset` |
| `GET /optimization-tips/summary`        | counts by `tip_type` + total `estimated_monthly_savings` — dashboard header         |
| `PATCH /optimization-tips/{id}/dismiss` | sets `status="dismissed"`, feeds the cooldown                                       |
| `PATCH /optimization-tips/{id}/apply`   | sets `status="applied"` — tracks which advice was acted on                          |
| `POST /optimization-tips/admin/rebuild` | manual re-trigger, mirrors `POST /summary/admin/rebuild-daily`                      |

Register in `_ALL_ROUTERS` ([main.py:60](app/main.py#L60)) and add the import to the
`app.routers` import block at [main.py:12](app/main.py#L12).

---

## Verification

1. **Schema first** — `psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f schema_clean.sql`, then
   `uvicorn app.main:app --reload`. Boot succeeding _is_ the schema check: `_verify_schema()`
   fails loudly and names the missing column otherwise.

2. **Step 1 columns** — send a request through `/proxy` with a system message and a `tools`
   array, then confirm the new columns are non-zero:

   ```sql
   SELECT r.num_messages, r.has_system_prompt, r.prompt_char_count,
          t.system_tokens, t.tool_definition_tokens, t.context_utilization_pct
   FROM ai_requests r JOIN token_usage t ON t.request_id = r.request_id
   ORDER BY r.created_at DESC LIMIT 1;
   ```

3. **Rules, individually** — new `tests/test_optimization_tips.py` using the `db_session`
   fixture ([tests/conftest.py:26](tests/conftest.py#L26)) and
   [tests/factories.py](tests/factories.py). Each test seeds `AiRequest`/`TokenUsage`/
   `RequestCost`/`DailyOrgSummary` rows for a synthetic org, calls one rule's `evaluate()`,
   and asserts the tip fires with the expected `tip_type` and `evidence_json` shape. Every
   test rolls back — no rows persist. Cover the negative case too: below-threshold data must
   produce **zero** tips.

4. **Dedup and cooldown** — call `_generate_optimization_tips` twice for the same window and
   assert the second run inserts 0 rows. Dismiss a tip, re-run, assert it stays dismissed.

5. **End-to-end** — `POST /optimization-tips/admin/rebuild` against real data, then
   `GET /optimization-tips`. For each returned tip, pull one ID out of
   `evidence_json.sample_request_ids` and fetch it from the request-detail endpoint; the
   prompt should visibly justify the tip.

6. **No PII leak** — grep the response of `GET /optimization-tips`: no field may contain raw
   prompt text. Rule 5 must emit a hash prefix only.

7. `pytest tests/` — full suite green, confirming Step 1's proxy changes broke nothing
   (`test_token_storage.py` and `test_pii_detection.py` are the ones at risk).
