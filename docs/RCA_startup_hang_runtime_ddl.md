# RCA — Backend never completes startup (indefinite hang on boot)

- **Date:** 2026-07-27
- **Severity:** SEV-1 (backend fully unstartable; no traffic served)
- **Status:** Fixed, pending schema application to the Azure DB and a real boot verification
- **Author:** diagnosed via live process inspection on the affected host

---

## Summary

The FastAPI backend never finished starting. A `uvicorn --reload` process had been
alive for **2h10m** with nothing listening on `:8000`. It was not slow — it was
**deadlocked** on a PostgreSQL lock while running schema-mutating DDL inside its own
startup path. Because Uvicorn does not bind the port until the FastAPI `lifespan`
startup returns, the hang was silent: no error, no log line, no open port.

The trigger was lock contention on Postgres. The **root cause** was architectural: the
application mutated its own database schema on every boot (`create_all` + ~60
`ALTER TABLE` / `CREATE INDEX` statements) instead of treating the database as
pre-migrated. A single blocked lock acquisition was enough to hang the entire process
indefinitely, and the code could not detect it.

---

## Impact

- Backend could not start at all. No proxy traffic, no admin API.
- **Self-perpetuating:** each blocked startup left an `ACCESS EXCLUSIVE` lock request
  queued on `ai_requests` / `audit_logs`. That queued request blocks *every* subsequent
  query on those tables, so each restart (and every `--reload` file-save) queued behind
  the previous crashed boot. One bad startup poisoned all following ones.
- Silent failure mode: no exception surfaced, so it read as "startup is just slow"
  rather than "startup is wedged." The user had forgotten it was even running.

---

## Detection

Diagnosed by inspecting the live host, not the logs (there were none):

| Observation | Evidence |
|---|---|
| Process alive 2h10m, nothing on `:8000` | `ps` shows `uvicorn --reload`; `ss`/`curl` show port closed |
| Worker (PID 51650) parked in `poll()` | `ps -o wchan` = `poll_schedule_timeout` |
| One ESTABLISHED socket to Postgres, idle | `ss -tnp` → `…:42034 → 52.165.98.36:5432` on the worker |

A query had been sent to Postgres and had not returned in two hours — the textbook
signature of blocking on a lock, not a slow query (a slow query still makes progress;
a lock wait is unbounded and raises nothing).

---

## Root cause

`app/main.py`'s `lifespan` handler ran, on every startup:

```python
Base.metadata.create_all(bind=engine)
with engine.connect() as conn:
    for stmt in _SAFE_ALTERS:      # ~60 statements, ~50 of them ALTER TABLE
        try:
            conn.execute(text(stmt))
        except Exception:
            pass                    # <-- cannot catch a lock WAIT
    conn.commit()                   # <-- locks held until here
```

Three compounding problems:

1. **All statements ran in one transaction on one connection.** Each `ALTER TABLE` takes
   an `ACCESS EXCLUSIVE` lock and **holds it until the final `conn.commit()`**.
2. **A lock wait never raises.** If any other session held even a read lock on
   `ai_requests` — most likely an `idle in transaction` backend left behind by a
   previously killed boot — the first `ALTER` blocked forever. The `except Exception:
   pass` is powerless here: waiting on a lock is not an error, so there is nothing to
   catch. The process just parks in `poll()`.
3. **No timeouts.** No `lock_timeout` or `statement_timeout` was set, so the wait was
   unbounded. The port never opened.

**The deeper cause:** the application performed DDL at runtime at all. Schema evolution
belongs in a migration applied *before* the app starts, not in the request-serving
process's boot path. `_SAFE_ALTERS` existed precisely because the checked-in schema
files were incomplete, so the app was patching its own schema live to compensate.

### Contributing factors (slow, not the hang)

- **Unbounded backfill on the startup path.** After the DDL, `lifespan` ran up to
  **90 days** of summary/anomaly aggregation serially before yielding — more blocking
  boot time even in the happy path.
- **Stale, incomplete schema files.** `schema_clean.sql` was labelled "single source of
  truth" but defined only **18 of 39** tables and was missing 8 `ai_requests` columns.
  Its own footer claimed 21 tables were unused — 3 of those (`route_executions`,
  `usage_anomalies`, `data_security_logs`) are in fact written/read by live code. So the
  schema files could not have replaced `_SAFE_ALTERS` as-is; the runtime DDL was load-bearing.

---

## Resolution

Made the database schema fully external and the app schema-read-only.

1. **Completed `schema_clean.sql` as the single source of truth.** Added the 4
   genuinely-live missing tables (`route_executions`, `usage_anomalies`,
   `data_security_logs`, `daily_user_usage`), the 8 missing `ai_requests` columns, and
   the indexes / DB-side ID defaults that only `_SAFE_ALTERS` had been creating. Verified
   it covers every table live code touches (22/22, zero column drift). Deleted the
   redundant `schema.sql`.
2. **Removed all runtime DDL from `app/main.py`** — `create_all` and the entire
   `_SAFE_ALTERS` list are gone.
3. **Added a read-only startup guard, `_verify_schema()`.** A single `information_schema`
   query (with `lock_timeout=5s` / `statement_timeout=15s` as insurance) diffs the DB
   against `Base.metadata` and **refuses to start** if any required table/column is
   missing, naming exactly what's absent and how to fix it. It takes no locks and holds
   no transaction — it cannot reproduce the hang.
4. **Moved the 90-day backfill off the boot path** into a one-shot APScheduler job
   (`startup_backfill`) that fires 5s after boot, so the port binds immediately.
5. **The 17 genuinely-dead tables** are excluded from the guard via `_UNUSED_TABLES` in
   `app/main.py`, mirrored by a footer in `schema_clean.sql`.

---

## Verification

- Schema covers 22/22 live tables, zero missing columns (offline diff vs `Base.metadata`).
- FK targets all defined and correctly ordered; none reference an omitted table.
- Guard tested on both paths: passes on a complete catalogue; on a damaged one it reports
  exactly the injected missing table + column and the `psql` fix command.
- Modules import cleanly.

**Not yet done (blocked on environment):**
- Real `psql -f schema_clean.sql` apply against a scratch DB, and `pytest` — Docker
  daemon was down at fix time.
- **The Azure DB still needs the schema applied** (4 tables + 8 columns). Until then the
  app will now *intentionally* refuse to start with a clear message.

---

## Remediation checklist

- [ ] Kill the still-hung `uvicorn` process and terminate any leftover
      `idle in transaction` Postgres backends (else the next boot re-queues on the lock).
- [ ] Apply the schema: `psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f schema_clean.sql`.
- [ ] Boot the app; confirm the port opens within seconds and `/health` → 200.
- [ ] Run one end-to-end proxy request; confirm rows in `ai_requests` (incl. the
      previously alter-only columns), `token_usage`, `request_cost`.
- [ ] Run `pytest tests/` against the migrated DB.

---

## Lessons learned

- **The app must not perform DDL.** Schema is a migration concern, applied before the
  process serves traffic. Runtime DDL couples boot to lock availability.
- **`except Exception: pass` around DB calls hides the worst failures.** A lock wait
  isn't an exception; swallowing errors turned a diagnosable fault into a silent hang.
- **Always set `lock_timeout` / `statement_timeout`** on any startup DB work so a boot
  can fail loudly instead of hanging forever.
- **Keep the boot path free of unbounded work** (the 90-day backfill). The port should
  open fast; heavy work belongs on a background thread.
- **"Single source of truth" must be verified, not asserted.** Both schema files carried
  that label and both were wrong; a column-level diff against the ORM caught it.

## Follow-ups worth considering

- Adopt a real migration tool (e.g. Alembic) so schema changes are versioned and applied
  as a deliberate deploy step, instead of hand-edited SQL kept in sync by convention.
- Add a CI check that diffs `schema_clean.sql` against `Base.metadata` so the two can
  never drift again.
