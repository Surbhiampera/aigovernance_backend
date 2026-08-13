# Inbound load balancing for the governance proxy

## Context

We want incoming proxy traffic spread across multiple app instances, with the
balancer checking instance health *before* routing — not just round-robining into
a dead process. Today the app runs as a single container (`Dockerfile:45`,
`uvicorn --workers 2`) with one `backend` replica in `docker-compose.yml` and no
healthcheck on it.

**Short answer on packages: there is no Python package worth using for this, and
you should not write one.** Inbound load balancing is a reverse-proxy job.
Specifically:

- `uvicorn --workers N` / gunicorn do **not** give you what you're asking for.
  They fork N processes sharing one listening socket; the *kernel* hands each new
  connection to whichever worker calls `accept()` first. There is no health
  signal and no routing decision — you cannot health-check-route between workers
  inside one container. Health-aware routing requires *separate* instances behind
  a proxy.
- The real tools are Traefik, Caddy, nginx, HAProxy, Envoy. Note nginx OSS only
  does **passive** health checks (`max_fails`/`fail_timeout`); active probing
  needs nginx Plus. Traefik and Caddy both do active checks in open source.
- On Azure App Service (the production target per `Dockerfile:38`) you get this
  for free: set **Health check path** + **Scale out**, and Azure's front end
  probes each instance and pulls unhealthy ones from rotation. Zero code.

So the work is not "build a load balancer." It is: **give the balancer something
real to probe, and fix the three things that are currently unsafe when more than
one instance runs.**

### The three blockers (all verified, all pre-existing)

1. **Every process starts its own scheduler.** `start_scheduler()` is called
   unconditionally in the lifespan handler ([app/main.py:154](app/main.py#L154)), and
   APScheduler's `max_instances=1` ([app/scheduler.py](app/scheduler.py)) is
   *per-process* with an in-memory jobstore. With `--workers 2` there are
   **already 2 schedulers racing today**; 3 replicas would make it 6. The
   aggregation jobs are delete-then-reinsert over the same rows
   (`_rebuild_daily_summary` et al.), so concurrent runs race into duplicated or
   lost summary rows. This must be fixed *before* scaling out.

2. **DB connections will blow the Postgres limit.** `get_db_pool_size()=14` +
   `get_db_max_overflow()=6` = 20 per process, and the docstring at
   [app/config.py:113-120](app/config.py#L113-L120) explicitly sizes this for
   *2 workers against a 50-connection plan* (= 40 used). Three replicas × 2
   workers × 20 = **120 connections** against a 50-cap → instant connection
   exhaustion. (Aside: `CLAUDE.md` says `pool_size=3, max_overflow=0` — that is
   stale and should be corrected to match `config.py`.)

3. **`/health/detailed` is broken and returns 500.** [app/main.py:234](app/main.py#L234)
   imports `_azure_circuit` from `app.routers.proxy`, which no longer exists —
   `proxy.py` was refactored to a per-deployment `_circuit_breakers` dict. A
   repo-wide grep finds `_azure_circuit` only in `main.py`. Any probe hitting it
   errors.

Also worth knowing (not blockers, just behavior changes): `_azure_limiter` and
`_circuit_breakers` in `proxy.py` are per-process, so N instances multiply the
effective upstream concurrency cap by N, and a deployment must fail
`AZURE_CIRCUIT_FAILURE_THRESHOLD` times *per process* to trip.

---

## Plan

### 1. Fix the scheduler so only one instance runs jobs

Guard each job with a **Postgres advisory lock**. Chosen over an
`SCHEDULER_ENABLED` env flag (needs ops discipline and a designated instance) and
over APScheduler's `SQLAlchemyJobStore` (would require APScheduler to create its
own table, violating the repo's no-DDL rule and `schema_clean.sql` as the single
source of truth). Advisory locks need no DDL, no new infra, and self-heal if the
holder dies.

In [app/scheduler.py](app/scheduler.py), add a helper and wrap each `_job_*` body:

```python
@contextmanager
def _job_lock(db, job_id: str):
    """Only one process across the whole fleet runs a given job per tick."""
    key = zlib.crc32(job_id.encode()) & 0x7FFFFFFF
    got = db.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
    try:
        yield bool(got)
    finally:
        if got:
            db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
```

Each of `_job_daily_aggregation`, `_job_monthly_aggregation`,
`_job_optimization_tips`, `_job_startup_backfill` acquires it on its existing
`SessionLocal()` and returns early (debug log, no `_last_success` update) if it
loses the race. The lock is session-scoped, so the job holds one pooled
connection for its duration — already true today.

Side benefit: this also fixes the existing `--workers 2` double-run bug.

### 2. Add a real readiness endpoint for the balancer to probe

In [app/main.py](app/main.py), keep the three probes distinct:

- `GET /health` — **unchanged.** Stays static/dumb. This is the *liveness* probe;
  if it checked the DB, a database blip would make the platform kill every
  instance at once.
- `GET /health/ready` — **new.** The load balancer probe. Runs `SELECT 1` against
  the DB with a short timeout; returns `200 {"status":"ready"}` or
  `503 {"status":"not_ready","reason":...}`. Does **not** check Redis (rate
  limiting already fails open, so a Redis outage must not pull instances from
  rotation).
- `GET /health/detailed` — **fix the import.** Replace `_azure_circuit` with an
  aggregate over `proxy._circuit_breakers` (e.g. list of open circuit keys and a
  total count) and keep `_azure_limiter` for in-flight/max. Diagnostics only.

**Deliberately not doing:** failing readiness on pool saturation or a full
`_azure_limiter`. Under real load all instances saturate at once, every instance
reports unready, the balancer finds zero healthy targets, and a slowdown becomes
a full outage. Saturation belongs in `/health/detailed` for alerting; the
existing 503 from the limiter is the correct shedding mechanism.

### 3. Size the connection pool for N instances

Make the process count and pool derive from an explicit budget rather than the
hardcoded 2-worker assumption:

- Change [Dockerfile:45](Dockerfile#L45) to `--workers ${UVICORN_WORKERS:-2}`.
- Set `UVICORN_WORKERS=1` when scaling horizontally and scale *containers*
  instead — one asyncio process per container is easier to reason about and makes
  the connection math linear.
- Set `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` per environment so
  `instances × workers × (pool + overflow) ≤ ~45`. For 4 single-worker replicas:
  `DB_POOL_SIZE=8`, `DB_MAX_OVERFLOW=2`.
- Update the docstring at [app/config.py:113-120](app/config.py#L113-L120), which
  currently hardcodes the 2-worker reasoning.

If instance count needs to grow past that, PgBouncer in transaction mode is the
next step — out of scope here, worth a note in the docstring.

### 4. Local / self-hosted: Traefik in `docker-compose.yml`

Traefik over Caddy/nginx because it auto-discovers scaled replicas through Docker
labels, so `docker compose up --scale backend=3` needs no config edit, and it
does active health checks in the open-source build.

Add a `traefik` service (v3, `--providers.docker`, entrypoint on `:80`, dashboard
on `:8080`), and on `backend`:

- **Remove the `ports:` mapping** — with `--scale` > 1 a fixed host port collides.
  Traefik becomes the only published entrypoint.
- Add labels for the router, plus the active health check pointed at the new
  endpoint and a least-connections policy:

```yaml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.backend.rule=PathPrefix(`/`)"
  - "traefik.http.services.backend.loadbalancer.server.port=8000"
  - "traefik.http.services.backend.loadbalancer.healthcheck.path=/health/ready"
  - "traefik.http.services.backend.loadbalancer.healthcheck.interval=10s"
  - "traefik.http.services.backend.loadbalancer.healthcheck.timeout=3s"
```

- Add a container-level `healthcheck:` block on `backend` hitting `/health` so
  Compose itself knows when a replica is up.
- Add `UVICORN_WORKERS: ${UVICORN_WORKERS:-1}` and the `DB_POOL_SIZE` /
  `DB_MAX_OVERFLOW` env vars to the `backend` service.

Note Traefik's `PathPrefix(/)` will also front the admin routers; that's fine
since auth is unchanged, but if you want the proxy and admin APIs split later,
that's a second router rule rather than a redesign.

### 5. Production (Azure App Service): configuration, not code

No proxy container needed — document in `CLAUDE.md`:

- App Service → **Health check** → path `/health/ready`. Azure probes every
  instance and removes failing ones from the rotation. This *is* the check-first
  balancer.
- App Service → **Scale out** → set instance count (or autoscale rules).
- Set `UVICORN_WORKERS`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` as App Settings to
  match the instance count per §3.

### Files touched

| File | Change |
|---|---|
| [app/scheduler.py](app/scheduler.py) | `_job_lock` advisory-lock helper; wrap all four `_job_*` bodies |
| [app/main.py](app/main.py) | New `/health/ready`; fix `_azure_circuit` import in `/health/detailed` |
| [app/config.py](app/config.py) | Re-document pool sizing for N instances |
| [Dockerfile](Dockerfile) | `--workers ${UVICORN_WORKERS:-2}` |
| [docker-compose.yml](docker-compose.yml) | Traefik service; backend labels, healthcheck, env; drop `ports:` |
| [CLAUDE.md](CLAUDE.md) | Correct the stale pool numbers; document scale-out + Azure health check path |

---

## Verification

1. **Scheduler lock — the important one.** Start two processes against the same
   DB (`uvicorn app.main:app --port 8000` and `--port 8001`), then trigger the
   job on both concurrently. Confirm the logs show one process running and the
   other logging "lock not acquired", and that `DailyOrgSummary` row counts for
   the target date are identical before and after. Cross-check with
   `POST /summary/admin/rebuild-daily?days_back=1`.

2. **Readiness reflects reality.** `curl -i localhost:8000/health/ready` → 200.
   Stop Postgres (`docker compose stop db`), re-curl → **503**. Restart → back to
   200. Confirm `/health` stays 200 throughout (liveness must not follow the DB).

3. **`/health/detailed` no longer 500s.** `curl -s localhost:8000/health/detailed | jq`
   — expect `db_pool`, `azure.concurrent_in_flight`, the circuit summary, and
   `scheduler.last_success`.

4. **Balancing actually spreads.** `docker compose up --scale backend=3`, then
   fire ~30 requests at Traefik (`:80`) and confirm from
   `docker compose logs backend` that all three replicas served traffic.

5. **Check-first behavior.** With 3 replicas up, `docker compose pause` one of
   them. Within ~2 health-check intervals, confirm continued requests return 200
   and none are routed to the paused replica (Traefik dashboard on `:8080` should
   show it as down). Unpause and confirm it re-enters rotation.

6. **Connection budget.** With the scaled stack running under load, check
   `SELECT count(*) FROM pg_stat_activity WHERE datname='aigovernance';` stays
   comfortably under the plan limit.

7. **No regressions.** `pytest tests/` — `tests/test_health.py` asserts on
   `/health`, which is deliberately unchanged.
