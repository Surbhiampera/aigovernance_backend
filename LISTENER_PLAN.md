# Plan: Passive LLM Listener → Unified Governance Logging

> Status: **DRAFT — awaiting user review.** Capture method and data model are decided;
> the deliverable scope (receiver-only vs receiver + reference listener) is still open.

## Context — why we're doing this

Today all governed AI traffic must flow *through* the FastAPI proxy (`/proxy`). That inline
design is what lets it **enforce** (mask PII, block on budget, rate-limit) — but it also makes
the proxy a mandatory hop and a single point of failure ("the funnel"). The goal is a second,
**out-of-band** mode: a listener that observes LLM calls the client makes *directly* to the
provider and ships a copy to this backend for **logging/attribution only** — no enforcement, no
funnel. Run the proxy where hard enforcement is required; run the listener everywhere else for
visibility without being in the critical path.

### The one hard constraint (already reconciled)
LLM traffic is TLS-encrypted, so raw packet sniffing sees only ciphertext — it cannot read
prompts, completions, model, or token counts. To log content the way the proxy does, the listener
must capture plaintext at the application boundary. **Decision: SDK/library instrumentation** — a
thin wrapper the client apps import that hooks the OpenAI/Anthropic SDK and async-POSTs the
request/response to this backend. Clients still call providers directly (no funnel); the tradeoff
accepted is that each client app must install the lib, and the listener can log but **cannot block**.

## Key finding — the backend receiver is 70% scaffolded but DEAD

`app/services/ingestion/` already contains a working adapter registry, four vendor adapters
(openai/anthropic/google/generic), the `TelemetryEventCreate`/`BatchTelemetryIngest` schemas,
the `TelemetryEvent`/`ToolConnector` models, and `CostEngine` (which honors a `precomputed_llm_cost`).
BUT it is unwired and cannot persist anything:

- `normalizer.py:32` imports `_ingest_event` from `app.routers.telemetry` — **that module does not exist**.
- **No HTTP route** anywhere ingests telemetry; no such router is registered in `app/main.py`.
- `IngestionNormalizer` and `CostEngine.calculate` have **zero live callers**.
- That pipeline's intended sink is `telemetry_events` — a table family **separate** from the proxy's.

## Decision — land listener data in the proxy's OWN tables (not `telemetry_events`)

Rationale: every existing dashboard (`costs.py`, `summary.py`, `audit_logs.py`) reads
`ai_requests` / `ai_responses` / `token_usage` / `request_cost`. Writing there makes listener
traffic show up in all current dashboards immediately and reuses the proxy's `_calculate_cost()`.
The scaffolded `telemetry_events` adapters are therefore **not** on the critical path for this work
(they can be revisited later or deprecated).

Provenance: `AiRequest.source_system` (String, nullable — already exists, `models.py:648`, added via
`_SAFE_ALTERS` in `main.py`) will be stamped `"listener"` to distinguish these rows from proxy rows
(`entry_point` can further tag the specific SDK/source).

## Design

### Backend receiver (in scope regardless of final decision)
1. **New router** `app/routers/ingest.py`, mounted (registered in `app/main.py`) — e.g.
   `POST /ingest/llm` accepting a batch of captured exchanges. Auth via the existing
   `X-Governance-Key` → `verify_governance_key()` so ingested rows are correctly scoped to org/project.
2. **Request schema** (new, in `app/schemas.py`): a captured-exchange record — request body
   (messages/model), response body, provider, optional client-measured latency, optional
   client-side token counts, optional `X-User-Id` attribution. Keep it close to what the OpenAI SDK
   already has so the wrapper does near-zero transformation.
3. **Writer**: reuse the proxy's existing storage helpers rather than the dead ingestion pipeline —
   `_store_request()` (`proxy.py:487-523`, writes `AiRequest`) and the post-forward storage helper
   (`proxy.py:~638`, writes `AiResponse` + `TokenUsage` + `RequestCost` and calls `_calculate_cost()`).
   Refactor the shared bits of that helper so both the live proxy path and the ingest path call one
   function; stamp `source_system="listener"`, `request_status="logged"`.
4. **Tokens/cost**: prefer provider-reported `usage` from the captured response (same precedence the
   proxy uses); fall back to `token_counter.py` tiktoken estimate. Cost via existing `_calculate_cost()`.
5. **Non-goals for ingest path**: no PII *blocking*, no budget *blocking*, no rate-limit *rejection*
   (out-of-band = can't block). PII *detection/masking-for-storage* and anomaly/audit logging can still
   run so the logged copy is compliant — confirm with user whether to mask stored prompts.

### Reference listener (only if user picks "receiver + reference listener")
A minimal Python wrapper package (separate dir, e.g. `listener/`): wraps the OpenAI client, lets the
real call go straight to the provider unchanged, then fire-and-forget POSTs the captured
request+response to `/ingest/llm` on a background thread/queue so it never adds latency or a failure
mode to the client's call. Ships one runnable example that makes a real call and produces a DB row.

## Critical files
- `app/routers/ingest.py` — **new** router (the receiver).
- `app/main.py` — register the new router (follow the existing `from app.routers import (...)` block).
- `app/schemas.py` — **new** captured-exchange request schema.
- `app/routers/proxy.py` — refactor `_store_request` (487-523) and the post-forward storage helper
  (~638) into a reusable writer shared by proxy + ingest; reuse `_calculate_cost()`.
- `app/services/token_counter.py` — reuse for fallback token estimation.
- `app/models.py` — `AiRequest.source_system` already exists; confirm no new columns needed.
- `listener/` — **new**, only if reference listener is in scope.

## Open questions for the user
- **Scope**: backend receiver only (documented contract, agent built later) vs receiver + a working
  reference SDK-wrapper with an end-to-end demo.
- Should stored listener prompts be PII-masked on the way in (compliance) or stored verbatim?

## Verification
- Unit test the ingest router with SQLite (`tests/conftest.py` pattern): POST a synthetic OpenAI-shaped
  exchange with a valid governance key → assert one `AiRequest (source_system='listener')` +
  `AiResponse` + `TokenUsage` + `RequestCost` row, cost matching `_calculate_cost()`.
- Assert the row appears in `GET /costs/by-model` and `GET /summary/today` (dashboard unification).
- Auth: missing/invalid key → 401; malformed body → 400.
- If reference listener in scope: run its example against a real provider key, confirm the client
  gets a normal response AND a `source_system='listener'` row lands, with the client call unaffected
  when the backend is down (fire-and-forget must not raise into the caller).
