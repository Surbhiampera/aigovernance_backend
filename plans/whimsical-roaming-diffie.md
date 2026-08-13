# Realistic demo dataset for the optimization-tips engine

## Context

The only synthetic dataset we have today is [scripts/generate_mock_optimization_data.py](scripts/generate_mock_optimization_data.py): a single org/project pair both literally named `*-mock-optztips`, one flat traffic shape, and a hand-hardcoded gpt-4o rate table. It proves the rules fire, but it can't be shown to anyone — the names give it away, every tip type lands on one project, and there's no per-user, multi-provider, failure, or budget data at all.

We want a dataset that looks like a real AI-governance tenant: a portfolio of plausibly-named products, each with its own traffic shape, so that the tips list, cost breakdowns, per-user views, provider mix, and budget bars all have believable content. Only the **org** stays synthetic (`org-demo-*`), which the frontend simply won't list — every project underneath it carries a real-sounding name.

Target outcome: one new, self-contained, re-runnable script that seeds ~2,900 requests across 6 projects, where each project trips a *different* subset of optimization rules (and one project trips none), with per-user attribution, Anthropic/Google/Mistral traffic alongside OpenAI, some failed/blocked requests with audit trail, and budgets + governance rules wired in.

## What exists and gets reused (do not reimplement)

| Thing | Where | Why it matters |
|---|---|---|
| `calculate_cost(db=, model=, provider=, input_tokens=, output_tokens=)` | [app/services/cost_lookup.py](app/services/cost_lookup.py#L20) | **The** pricing source of truth, and exactly what `model_substitution` uses to recost candidates. Using it to build `RequestCost` rows makes the savings math genuine and removes the hardcoded rate constants the old script carries. |
| `get_model_pricing()` / `MODEL_PRICING` | [app/services/ai_model_pricing.py](app/services/ai_model_pricing.py#L21) | Source for context windows and `category` — both gate substitution candidacy. |
| `_rebuild_daily_summary`, `_rebuild_daily_user_summary`, `_detect_daily_anomalies` | [app/workers/tasks.py](app/workers/tasks.py#L38) | Re-run per touched date so aggregates match what the hourly scheduler would produce. `response_length` reads `daily_org_summary` **only**, and `_generate_optimization_tips` only scans org/project pairs that have a summary row — so skipping this step means zero tips. |
| `audit_service.log_event` / `log_request_blocked` | [app/services/audit_service.py](app/services/audit_service.py#L19) | Build audit rows through the real helper instead of hand-constructing `AuditLog`. |
| Existing script's structure | [scripts/generate_mock_optimization_data.py](scripts/generate_mock_optimization_data.py) | `_seed_request`, `_jitter`, `_random_timestamp`, the seeded-RNG determinism, and the safe-target print are all worth carrying over verbatim in spirit. |

Leave the old script in place and untouched — it stays as the minimal threshold-probe fixture.

## Rule thresholds this dataset is tuned against

From [app/config.py](app/config.py#L238): `TIP_WINDOW_DAYS=30`, `TIP_MIN_REQUESTS=50`, `TIP_MIN_MONTHLY_SAVINGS=$5`, `TIP_OUTPUT_INPUT_RATIO=3.0`, `TIP_PROMPT_OUTLIER_RATIO=3.0`, `TIP_TRUNCATION_RATE=0.15`, `TIP_DUPLICATE_MIN_HITS=5`.

Because the window is 30 days and traffic is spread over 30 days, `monthly_savings == raw window savings` — the $5 gate is a flat $5.

Three non-obvious behaviours the shapes below deliberately exploit:

1. **`model_substitution` candidates are org-wide, not project-scoped** — `get_deployments_for_org` ([deployment_service.py:83](app/services/deployment_service.py#L83)) ignores `project_id` for membership. So *what we register as a deployment anywhere in the org determines the candidate pool for every project*. The only clean ways to keep a project from firing it: use the cheapest deployed model in its category (savings ≤ 0), use a category with no second deployed model, or keep spend under $5.
2. **`oversized_prompt` is project-wide and model-agnostic** — one tip max per project, and it compares p95 vs median across *all* models in that project.
3. **`cache_opportunity` savings = `(hits-1) × avg(request_cost.input_token_cost)`** — a cheap model with a small prompt can never clear $5 no matter how many duplicates. Duplicates only become tips on large-context workloads, which is why they land on contract-review and knowledge-search rather than the chat bot.

## Org and deployment registry

```
org_id   = "org-demo-enterprise"
org_name = "Northwind Digital (Demo)"
```

Deployments registered org-wide (this *is* the substitution candidate pool — adding a cheaper model here changes every project's tips):

| model | provider | note |
|---|---|---|
| `gpt-4o` | openai | default |
| `gpt-4o-mini` | openai | cheapest chat, 128k ctx |
| `gpt-4` | openai | legacy, deliberately expensive |
| `claude-sonnet-4-5` | anthropic | |
| `claude-haiku-4-5` | anthropic | |
| `gemini-2.5-pro` | google | |
| `gemini-2.5-flash` | google | ties gpt-4o-mini on price — this tie is what keeps "already cheap" projects quiet |
| `codestral-2501` | mistral | only `code`-category model → no substitution candidates exist for it |
| `text-embedding-3-small` | openai | only `embedding`-category model → same |

Do **not** register `gpt-5-nano`, `gemini-1.5-flash`, or anything below $0.15/1M input — that would break the intended silence on the cheap projects.

## Project portfolio and intended tips

Each row's numbers are chosen to clear (or miss) each gate with margin. Verify against the printout in the verification section, not by eye.

### 1. Customer Support Copilot — `proj-support-copilot`
- 820 req on `gpt-4o-mini`: input band 600–1300 (median ~950), output ~380, **18% with `finish_reason="length"`**.
- 45 escalations on `gpt-4o`, same shape (deliberately under the 50-request gate).
- **Fires: `response_truncated`** (0.18 ≥ 0.15, severity medium).
- Silent elsewhere: ratio 0.42 < 3.0; p95/median ≈ 1.3 < 3.0; gpt-4o-mini spend ≈ $0.30 with a price-tied cheapest candidate → savings 0; every prompt carries a unique ticket id so no duplicate hashes.

### 2. Contract Review Assistant — `proj-contract-review`
- 190 reviews on `claude-sonnet-4-5`: input 3500–4500, output ~900.
- 60 **byte-identical** "standard NDA clause check" prompts: input ~62,000 with `system_tokens ≈ 36,000` (58%), output ~700.
- **Fires: `oversized_prompt`** (p95 ≈62k vs median ≈4k → ratio 15.5; the 0.9×p95 outlier slice is exactly the 60 big rows, avg system share 0.58 ≥ 0.3 → `cause="system_prompt"`), **`cache_opportunity`** (59 × $0.186 = **$10.97**), **`model_substitution`** ($16.64 → $0.80, savings **$15.8**; peak 62.7k tokens still fits gpt-4o-mini's 128k window).
- Silent: output/input ratio 0.05; no truncation.

### 3. Sales Email Generator — `proj-sales-outreach`
- 420 req on `gpt-4o`: input ~350, output ~4600.
- **Fires: `response_length`** (ratio 13.1, ≥ 2× threshold → **severity high**; excess 1.49M tokens → **$14.91**) and **`model_substitution`** ($19.69 → $1.18, savings **$18.5**).
- Silent: tight prompt band; no truncation; unique prompts.

### 4. Internal Knowledge Search — `proj-knowledge-search`
- 420 RAG queries on `gemini-2.5-flash`: input 1500–2500, output ~350.
- 70 **identical** nightly "index digest" runs on `gemini-2.5-pro`: input 120,000 (system 8k, tool-def 2k — both under the 0.3 share so the cause lands on history, not system), output ~1,200.
- 260 `text-embedding-3-small` calls: input ~800, output 0.
- **Fires: `cache_opportunity`** (69 × $0.15 = **$10.35**), **`oversized_prompt`** (p95 120k vs median ~1.9k → **`cause="conversation_history"`**, contrasting with project 2), **`model_substitution` on `gemini-2.5-pro`** ($11.34 → $1.31, savings **$10**).
- Silent: flash is price-tied with the cheapest candidate; the embedding model is the only `embedding`-category deployment so it has no candidates at all.

### 5. Code Review Bot — `proj-code-review-bot` — **deliberately clean, zero tips**
- 330 req on `codestral-2501`: input 3000–4200, output ~1100.
- Nothing fires: ratio 0.3; narrow prompt band; no duplicates; no truncation; `code` category has no second deployed model. This is the control case that shows the engine isn't just noise.

### 6. Marketing Content Studio — `proj-marketing-studio`
- 300 req on `gpt-4` (legacy): input ~2000, output ~800.
- **Fires: `model_substitution` only** — $32.40 → $0.23, savings **$32.20**. The classic "you're still on gpt-4" tip, and the largest single saving in the dataset.
- Silent: ratio 0.4; narrow band; no duplicates; no truncation.

Coverage: `model_substitution` ×4, `cache_opportunity` ×2, `oversized_prompt` ×2 (two different causes), `response_length` ×1, `response_truncated` ×1, clean ×1.

## The four extras

**Per-user attribution.** 5–9 named users per project (`user_id`, `user_email`, `user_role` ∈ {engineer, analyst, support_agent, legal_counsel, marketer, admin}), weighted so 2 users carry ~45% of a project's traffic — flat distributions look fake and make `/costs/by-user` boring. `_rebuild_daily_user_summary` must run per touched date or `daily_user_usage` stays empty (it filters `AiRequest.user_id IS NOT NULL`).

**Multi-provider.** Set `AiRequest.provider` / `AiResponse.provider` to match the deployment (`openai`/`anthropic`/`google`/`mistral`) and pass the same string into `calculate_cost` so provider-specific catalogue rates apply.

**Failures and blocked requests.** ~3% of each project's volume as `request_status` ∈ {`failed`, `blocked`}, following [`_mark_request_failed`](app/routers/proxy.py#L812) semantics: **no `TokenUsage` / `RequestCost` rows** (no tokens were consumed), `failure_code` + `failure_reason` set, and an `audit_logs` row via `audit_service`. Mix of causes: `pii_blocked` (403), `budget_exceeded` / `rate_limited` (429), `upstream_error` (502).
> Detail that matters: `_rebuild_daily_summary` builds its rows from `RequestCost`, so a `(org, project, model)` combination with *only* failures produces no summary row and disappears. Always give failed rows a `model_name` that also has successful traffic in the same project.

**Budgets and governance rules.**
- Org budget row (`project_id=NULL`) plus one per project. Size the limits against **current-calendar-month** spend, not the full 30-day window — `budget_service` checks the calendar month, and a 30-day lookback from today (2026-08-13) starts back in July, so only ~40% of the seeded spend counts. Aim for a spread: one project at ~85% of limit, one over 100%, the rest comfortable.
- Two `GovernanceRule` rows: a `blocked_model` for `gpt-3.5-turbo` and a `max_input_tokens` ceiling. `rule_name` is **globally unique** across the whole table, so prefix it with the org id. Never block a model that is in the deployment list — that silently removes it from the substitution candidate pool.

## Implementation

New file: `scripts/generate_demo_org_data.py`. Structure mirrors the existing script (module docstring with the "disposable clone only" warning, seeded RNG, safe-target print, single `SessionLocal()` with rollback-on-exception).

Suggested shape — a declarative spec table rather than one function per project, so tuning a shape is a data edit:

```python
@dataclass
class Batch:
    model: str; provider: str; count: int
    input_tokens: tuple[int, int]      # (low, high) band
    output_tokens: tuple[int, int]
    system_share: float = 0.0
    tool_def_share: float = 0.0
    truncation_rate: float = 0.0
    fixed_prompt: str | None = None    # set => every row byte-identical (cache rule)
    prompt_template: str = "..."       # else unique per row

@dataclass
class ProjectSpec:
    project_id: str; project_name: str
    batches: list[Batch]
    users: list[tuple[str, str, str]]
    failure_rate: float = 0.03
    budget_limit: Decimal | None = None
```

Then a single `_seed_batch()` that, per row, computes costs through `calculate_cost(...)` and writes `AiRequest` + `TokenUsage` + `RequestCost` + `AiResponse` — one code path for all six projects.

CLI flags:
- `--purge` — delete this org's `ai_requests`/`token_usage`/`request_cost`/`ai_responses`/`audit_logs`/`optimization_tips` rows before seeding. **Needed**: the script is not idempotent (re-running doubles duplicate-prompt hit counts and cost totals), unlike the old additive script.
- `--seed N` — RNG seed, default fixed for reproducibility.
- `--days N` — window span, default 30.

Order of operations in `main()`: purge (if asked) → org/projects/deployments/budgets/governance rules → per-project batches → failure rows + audit logs → `db.flush()` → per touched date, `_rebuild_daily_summary` → `_rebuild_daily_user_summary` → `_detect_daily_anomalies` → `db.commit()`.

> Both rebuild functions **delete every org's rows for the dates they touch** and recompute from `ai_requests`/`request_cost`. That is idempotent and correct, but it is a global write — another reason this must only run against a disposable database clone.

No changes to `app/` are required. No schema changes — every table used is already in `schema_clean.sql`.

## Verification

```bash
# 1. Confirm the target DB, then seed
python3 -m scripts.generate_demo_org_data --purge

# 2. Generate tips through the real code path (not from inside the script)
curl -X POST "http://localhost:8000/optimization-tips/admin/rebuild"

# 3. Inspect what fired
curl "http://localhost:8000/optimization-tips/?org_id=org-demo-enterprise&limit=100" | jq \
  '.items[] | {project_name, tip_type, model_name, severity, estimated_monthly_savings}'
curl "http://localhost:8000/optimization-tips/summary?org_id=org-demo-enterprise" | jq
```

The script should end by printing an **expected-vs-actual table** — the intended tip set per project (from the table above) next to what the rules would produce given the rows just written. This is the real acceptance test; the numbers above are calculated, not measured, and prompt-token jitter can move a borderline case.

Acceptance criteria:
1. Exactly the 10 tips listed above, on the right projects, with `proj-code-review-bot` producing **none**.
2. `oversized_prompt` reports `cause="system_prompt"` for contract-review and `"conversation_history"` for knowledge-search.
3. `GET /costs/by-user?org_id=org-demo-enterprise` returns a skewed per-user distribution.
4. Provider breakdown shows all four providers; `daily_org_summary.failure_count` is non-zero for projects with failure rows.
5. At least one budget over 100% utilization and one in the 80–95% band.
6. Re-running with `--purge` reproduces byte-identical results (fixed RNG seed).
7. No project, org, or model name contains "mock" or "test".
