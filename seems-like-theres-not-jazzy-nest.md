# Mock data generator for optimization tips

## Context

The optimization-tips feature (`app/services/optimization/rules/*`, driven by `_generate_optimization_tips` in `app/workers/tasks.py`) has strict volume gates before any tip fires: the org/project pair must have `daily_org_summary` rows in the trailing 30-day window, and each rule additionally needs ≥50 requests, specific ratios (output/input > 3x, p95 input > 3x median, ≥15% truncation rate), or ≥5 duplicate prompts — all with ≥$5/month projected savings. The current DB doesn't have enough real traffic shaped this way, so no tips are generating.

Rather than touch any rule/threshold logic, we'll write a one-off script that inserts synthetic rows into the underlying tables (`ai_requests`, `token_usage`, `request_cost`, `ai_responses`, plus rebuilding `daily_org_summary`) for a dedicated synthetic org/project, shaped to clear every rule's threshold. You'll run it against a **pg_restore'd clone** of the DB, never production. The script only produces input data — the actual tip generation (`POST /optimization-tips/admin/rebuild`) is left for you to trigger manually so the real rule engine is exercised unmodified.

## Script

New file: `scripts/generate_mock_optimization_data.py` (no `scripts/` dir exists yet — this creates one). Run as `python -m scripts.generate_mock_optimization_data` with `DATABASE_URL` pointed at the **cloned** database (same env var `tests/conftest.py` uses — read directly from env, not through `.env` split vars).

It reuses the exact ORM insert pattern already proven in `tests/test_optimization_tips.py::_seed_org` / `_seed_request` — no new insert logic invented, just generalized into a script with more rows and controlled distributions.

### Safety guardrail

Before writing anything, the script prints the resolved DB host/name from `DATABASE_URL` and requires a `--yes-this-is-a-clone` flag (or interactive `y/N` confirm) to proceed. This is a blunt but necessary check against accidentally pointing at prod.

### Synthetic identities

- `org_id = "org-mock-optztips"`, `project_id = "proj-mock-optztips"` — new `Organization` + `Project` rows, prefixed distinctly so they're trivial to spot in dashboards and to delete later (`DELETE FROM organizations WHERE id = 'org-mock-optztips' CASCADE`-style cleanup, documented in the script's docstring).
- Two `ModelDeployment` rows registered for that org: `gpt-4o` (the "expensive" deployed model, `is_default=True`) and `gpt-4o-mini` (a cheaper same-category, sufficient-context-window candidate already in `app/services/ai_model_pricing.py`'s `MODEL_PRICING` — real pricing, so `model_substitution`'s recost math is realistic, not fabricated).

### Data generation (one pass, ~70 requests over the trailing 25 days, model = `gpt-4o`)

For each synthetic request, insert `AiRequest` + `TokenUsage` + `RequestCost` + `AiResponse` together (mirrors `_seed_request`), varying `created_at` across the window. Within that set, deliberately construct:

| Rule | How it's satisfied |
|---|---|
| `response_length` | Across the ~70 rows, average `completion_tokens / prompt_tokens > 3.0`, with total request count ≥ 50 for the model. |
| `oversized_prompt` | ~8 rows get `input_tokens` 5–10x the median of the rest, so p95 > 3x median; also set `system_tokens`/`tool_definition_tokens` on those rows so cause-attribution resolves to something concrete instead of falling through to `conversation_history`. |
| `response_truncated` | ~12 rows (>15%) get `finish_reason="length"` on `AiResponse`. |
| `cache_opportunity` | ~15 rows reuse one of 2–3 fixed `sanitized_prompt_text` strings (≥5 duplicates each), rest get unique text. |
| `model_substitution` | All 70 rows costed against real `gpt-4o` pricing via the existing `calculate_cost()` helper (not hand-computed) so the rule's own recost-against-`gpt-4o-mini` comparison finds genuine savings ≥ $5/mo. |

Token/cost numbers are randomized within realistic bands (not identical rows) so p50/p95 percentile math and averages look organic rather than suspiciously uniform.

### Aggregation step

After inserting the raw rows, call the existing `_rebuild_daily_summary(db, org_id, project_id, date)` from `app/workers/tasks.py` for each date touched, instead of hand-building `daily_org_summary` — guarantees the rollup matches what the real scheduler would produce and satisfies the job-level gate (`daily_org_summary` must have rows in the window) with zero duplicated aggregation logic.

### What the script does NOT do

- Does not call `_generate_optimization_tips` itself (per your choice) — it only seeds inputs.
- Does not modify any file under `app/services/optimization/`, `app/workers/tasks.py`'s rule logic, or `app/config.py` thresholds.
- Does not touch `daily_user_usage` (no rule reads it).

## Verification

1. Run the script against the cloned DB; it prints the org_id/project_id and a row-count summary per table.
2. `curl -X POST "http://localhost:8000/optimization-tips/admin/rebuild?window_end=<today>"` (server pointed at the same clone) — should return `{"inserted": N}` with N ≥ 1, ideally 5 (one per rule).
3. `GET /optimization-tips/?org_id=org-mock-optztips` — confirm all 5 `tip_type`s appear with sane `estimated_monthly_savings` and `evidence_json`.
4. `pytest tests/test_optimization_tips.py` — confirm the existing test suite still passes untouched (proves we didn't alter rule code).

## Cleanup

Since this only ever runs against a disposable clone, cleanup is just dropping/discarding that clone DB when done — no cleanup needed in production. The script's docstring will still note the `DELETE ... WHERE org_id = 'org-mock-optztips'` cascade path in case you want to reuse the same clone for other testing afterward.
