# PRD: AI Governance Platform — Expansion Features

**Status:** Draft for review
**Owner:** TBD
**Audience:** Engineering (execution-level)
**Source documents:** `GOVERNANCE_EXPANSION_PROPOSAL.docx` (feature proposals), `CLAUDE.md` / `GOVERNANCE_WORKFLOW.md` (current architecture)

---

## 1. Background

The AI Governance Platform is a FastAPI proxy that sits in front of all AI provider traffic (Azure OpenAI, OpenAI, Anthropic, Google Gemini) for external teams. It already enforces rate limits, budgets, and PII masking, and tracks token/cost usage per org/project/user (see Part 1 capability table in the proposal doc — all of the above are **Live**).

This PRD scopes six proposed expansions from `GOVERNANCE_EXPANSION_PROPOSAL.docx` §2.1–2.6 into buildable, sequenced work. It is written for engineers who will implement against the existing codebase, so each feature section states **what already exists**, **what's missing**, and **the specific build approach chosen** (rather than re-listing all options from the proposal doc).

### 1.1 Known dependency: `governance_rule_service` is not live

`app/services/governance_rule_service.py` (model allow/block lists, token ceilings) is fully implemented and administered via its router, but **is not called anywhere in the live proxy path** (`app/routers/proxy.py`) — it currently has zero effect on traffic. This matters here because:

- **2.2 (Optimization Tips Engine)** wants to suggest cheaper/allowed model substitutions — that suggestion surface is more useful, and safer, once allow/block rules are actually enforced (otherwise a "tip" can recommend a model the org intends to block).
- Wiring this in is **not** part of this PRD's scope, but is called out as a **prerequisite/parallel-track dependency** for 2.2 Phase 2 (model-substitution tips). Track it as a separate ticket; do not block 2.1/2.3/2.4 on it.

---

## 2. Goals

- Give orgs/projects real-time (not just hourly-batch) visibility into token/cost usage.
- Surface actionable, explainable cost-optimization recommendations.
- Quantify and display cumulative savings attributable to governance decisions.
- Close out multi-provider support so Claude, Gemini, and OpenAI are first-class, symmetric citizens in live routing (not just ingestion).
- Keep the platform extensible so future providers/rules/checks are additive, not core-logic changes.
- (Separately) evaluate whether a licensed, expiring downloadable package is actually a required delivery model before investing engineering time in it.

## 3. Non-goals

- Replacing the existing hourly/daily APScheduler rollups — they remain the source of truth for historical/monthly reporting; new real-time paths are additive.
- Building a general-purpose BI tool — this PRD assumes an off-the-shelf tool (Grafana/Metabase) is pointed at new data, not that we build dashboards from scratch.
- Wiring `governance_rule_service` into the live proxy path (tracked separately, see §1.1).

---

## 4. Current-state summary (per feature area)

| Proposal feature | Current status | Existing building blocks |
|---|---|---|
| 2.1 Per-Project/Per-User Token Analysis | Partially built (batch only) | `DailyUserUsage` table, hourly `_rebuild_daily_user_summary`, `GET /costs/by-user`, `UsageAnomaly` table + daily z-score-like anomaly job (`ANOMALY_SPIKE_RATIO`, `ANOMALY_BASELINE_DAYS`, etc.) |
| 2.2 Optimization Tips Engine | Not built | `ModelPricing` DB table + `MODEL_PRICING` catalogue (cost data to compare against); no rule engine or tips storage yet |
| 2.3 Savings Counter | Not built | `RequestCost` per-request cost rows exist; no baseline-comparison or ledger logic |
| 2.4 Multi-Model Support | Partially built | `deployment_service.py` (multi-provider deployment resolution/routing), ingestion adapters for openai/anthropic/google/generic, `ModelPricing` table — gaps detailed in §5.4 |
| 2.5 Licensed downloadable package | Not built | None — greenfield, and scope itself is unconfirmed |
| 2.6 Staying expandable | Partially true by construction | Adapter registry pattern already used for ingestion; `GovernanceRule` table is DB-configurable; no event bus or feature-flag system yet |

Redis is already integrated (`app/services/redis_client.py`, used by `rate_limit_service.py` with INCR+EXPIRE counters and Postgres fallback) — this is the pattern 2.1 and 2.3's live counters should reuse rather than introducing a new caching layer.

---

## 5. Feature specs

### 5.1 Per-Project / Per-User Token Analysis (2.1)

**Problem:** Usage/cost data updates hourly (via the scheduler), not in real time. Orgs/admins can't see a spike as it happens.

**Approach:**
1. **Live Redis counters** — on each completed request (same background-task hook that currently writes `TokenUsage`/`RequestCost` in `proxy.py`), INCR per-user and per-project token/cost counters in Redis, keyed by day, reusing `app.services.redis_client` and the counter pattern already established in `rate_limit_service.py`. Falls back gracefully (read-through to `DailyUserUsage`) when Redis is unreachable, matching existing fallback behavior.
2. **New read endpoint** — `GET /costs/live` (or extend `GET /costs/by-user`) reads the Redis counters for "today," and existing summary tables for prior days, giving a stitched real-time + historical view without duplicating rollup logic.
3. **Anomaly detection stays daily-batch for now** — `_detect_daily_anomalies` (z-score-style spike ratio vs. `ANOMALY_BASELINE_DAYS`) already exists; extending it to real-time is out of scope for Phase 1 (see Phase 2 below).
4. Do **not** stand up a separate time-series DB (TimescaleDB/InfluxDB) or message queue (Kafka/Redis Streams) for Phase 1 — existing Postgres summary tables + Redis counters cover the real-time + historical need at current scale. Revisit only if query load on `DailyUserUsage` becomes a bottleneck.

**Phasing:**
- Phase 1: Redis live counters + `/costs/live` endpoint.
- Phase 2: Real-time anomaly flagging (EWMA on the Redis counters) as an extension of, not replacement for, the existing daily `UsageAnomaly` job.

**Open questions:** none blocking Phase 1.

---

### 5.2 Optimization Tips Engine (2.2)

**Problem:** No mechanism today suggests cost/efficiency improvements to orgs.

**Approach — rule engine, DB-configurable (aligned with §5.6 extensibility goal):**
1. New `optimization_tips` table (org_id, project_id, tip_type, message, evidence_json, status, created_at) — generated by a scheduled job (reuse the existing APScheduler process, new job registered in `app/scheduler.py`), not per-request, matching the proposal's "never on the request path" principle.
2. **Rule 1 — output/input ratio check:** if a project's average output tokens far exceed input tokens, suggest a response-length cap. Plain if/then against `DailyUserUsage`/`DailyOrgSummary` aggregates.
3. **Rule 2 — model-substitution check:** compare actual spend against `ModelPricing` for a cheaper equivalent model. **Depends on `governance_rule_service` being wired live (§1.1)** so a suggested substitute isn't itself a blocked model — sequence this rule after that dependency lands, or explicitly caveat "recommendation ignores block-list" if shipped earlier.
4. **Rule 3 — oversized-prompt check:** using `tiktoken` (already a proxy dependency for token counting), flag prompts whose token count is disproportionate to typical usage for that project.
5. Duplicate/cache-opportunity detection (prompt hashing) and AI-generated plain-English tips are **Phase 2** — they need a decision on where prompt text is retained/hashed (PII implications, given `pii_engine` already masks sensitive fields) before implementation.

**Open question for you:** Do we already retain raw prompt text anywhere post-PII-masking that a duplicate-detection job could hash, or does this require a new opt-in retention policy?

---

### 5.3 Savings Counter (2.3)

**Problem:** No cumulative "$ saved" metric exists.

**Approach:**
1. New `savings_ledger` table (org_id, project_id, ai_request_id, baseline_model, baseline_cost, actual_cost, savings_amount, created_at), written in the same background-task hook that computes `RequestCost` in `proxy.py` — baseline cost computed via `ModelPricing`/`MODEL_PRICING` for a configurable "baseline/reference model" per model family.
2. Running total maintained in Redis (same pattern as §5.1) for live "$ saved this month," backed by the Postgres ledger as source of truth (recomputable, auditable — avoids Redis-only ledger drift).
3. Expose via new endpoint, e.g. `GET /costs/savings`.
4. Slack/Teams milestone notifications are **Phase 2** — needs a decision on notification channel/webhook ownership (see open question below).

**Open question for you:** What should "baseline model" mean — a fixed reference model per family (e.g., always compare against GPT-4-class pricing), or the most expensive model the org is *allowed* to use per governance rules? The latter is more accurate but depends on §1.1 (governance rules being enforced) to know what's "allowed."

---

### 5.4 Multi-Model Support: Claude / Gemini / OpenAI (2.4)

**Framed as completion work**, not greenfield — most of the plumbing exists.

**What's already live:**
- `deployment_service.py` resolves and routes live traffic to Azure/OpenAI/Anthropic/Google deployments via the `ModelDeployment` table, with `.env`-based fallback deployments.
- Ingestion adapters exist for openai, anthropic, google, and a generic fallback (`app/services/ingestion/adapters/`), registered via `@adapter_registry.register` — no core-logic changes needed to add a vendor.
- `ModelPricing` DB table + `MODEL_PRICING` catalogue already support multi-provider pricing lookups.

**Gaps to close (this is the actual scope of 2.4):**
1. Confirm parity: for each of Claude/Gemini/OpenAI, is there a fully live, tested end-to-end proxy path (not just ingestion-log-upload)? `CLAUDE.md` §"Two onboarding speeds" from the proposal doc maps directly onto the existing distinction between live `ModelDeployment` routing vs. the ingestion adapters — audit which providers currently only have the ingestion path and need the live-routing path added.
2. Common usage schema — verify `TokenUsage`/`RequestCost`/`AiResponse` are already provider-agnostic (they appear to be, given `deployment_service` abstracts the provider). Document this explicitly rather than rebuilding.
3. Secrets isolation — audit whether all provider credentials (`AZURE_OPENAI_*`, `OPENAI_*`, `AZURE_*`, plus Anthropic/Google equivalents once added) are consistently sourced from `.env`/`config.py` with no cross-provider leakage; a secrets manager (Vault) is a **Phase 2** infra decision, not required to close functional gaps.

**Open question for you:** Which specific provider(s) currently lack a live `ModelDeployment`-routed path and only have ingestion adapters? (I can audit this in the codebase if you want — flagging as a question since "Claude/Gemini" being fully live vs. ingestion-only determines most of the 2.4 backlog.)

---

### 5.5 Licensed, Expiring Downloadable Package (2.5)

The proposal doc itself flags this as a distribution/licensing decision, not a governance feature, and recommends confirming the delivery model is needed before building.

**This PRD does not scope implementation.** Recommended next step: a short discovery doc (or spike) answering:
- Is this platform being distributed to external customers as a standalone deployable, or is it internal-only?
- If external: signed JWT license (offline-capable, `PyJWT`) is the proposal's own recommendation — revisit sizing once the above is answered.

Placeholder phase in the roadmap; not sequenced until the above is answered.

---

### 5.6 Staying Expandable (2.6)

Largely a set of engineering principles to apply *while building 5.1–5.4*, not a standalone deliverable:

- New optimization rules (5.2) and provider adapters (5.4) should follow the existing plugin/registry pattern already used in `app/services/ingestion/` — no new pattern needed.
- `GovernanceRule` and the new `optimization_tips` rules should be DB-configurable (already true for `GovernanceRule`; extend the same principle to tips).
- Feature flags tied to licensing: deferred until §5.5 is resolved.
- Internal event notifications (modules reacting to activity vs. being wired in directly): **not required** for 5.1–5.4 as scoped above — all new work hooks into the existing background-task pattern already used in `proxy.py`. Revisit if/when a real fan-out need appears (e.g., Slack notifications in 5.3 Phase 2).

---

## 6. Suggested phasing / roadmap

| Phase | Scope |
|---|---|
| Phase 1 | 5.1 (live counters + endpoint), 5.4 gap audit + close, 5.3 (ledger + live counter) |
| Phase 2 | 5.2 (rule-based tips, Rules 1 & 3), governance_rule_service wired live (separate ticket, unblocks 5.2 Rule 2) |
| Phase 3 | 5.2 Rule 2 (model substitution) + duplicate-detection/AI-generated tips, 5.3 Slack/Teams notifications |
| Unscheduled / pending discovery | 5.5 (licensing — pending scope confirmation), 5.6 event-bus/feature-flags (pending real need) |

## 7. Open questions requiring your input

1. **5.2:** Is raw prompt text retained anywhere post-PII-masking for duplicate-detection hashing, or does this need a new retention policy decision?
2. **5.3:** Should "baseline model" for savings be a fixed reference model per family, or the most expensive *allowed* model per governance rules (depends on §1.1)?
3. **5.4:** Which provider(s) — Claude, Gemini, or both — currently lack live `ModelDeployment` routing and only have the ingestion-adapter path? (Can audit if you'd like.)
4. **5.5:** Is the platform intended for external distribution (making licensing relevant), or internal-only?
