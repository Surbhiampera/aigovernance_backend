# Operations Runbook — Backups, Updates, Patching

Day-2 operations for a client's self-hosted deployment (see
`LICENSING_PACKAGING.md` for how the deployment itself gets built and
shipped). Everything here runs on **that one client's own infrastructure**
— there is nothing central for us to operate, because we never hold a
client's data (see the "client self-hosts everything" model).

## Backups

**Hand the client `scripts/db_backup.sh` and `scripts/db_restore.sh` along
with `docker-compose.yml` and `.env`.** They aren't baked into the image
(the image only contains `app/`) and there's no source checkout on the
client's side to find them in otherwise — these two plain shell scripts are
the one extra thing beyond what `LICENSING_PACKAGING.md` lists as the
handoff. They only need Docker to run, nothing else.

```
./scripts/db_backup.sh ./backups
```

Run from the same directory as that deployment's `docker-compose.yml`.
Dumps the live database with `pg_dump` and gzips it — safe to run while the
stack is up (no downtime, no need to stop the backend).

Schedule it, e.g. daily at 2am via cron on the client's host:

```
0 2 * * * cd /path/to/this/deployment && ./scripts/db_backup.sh ./backups
```

Restoring — **destructive**, drops and recreates the database entirely:

```
./scripts/db_restore.sh ./backups/aigovernance-<timestamp>.sql.gz
```

Prompts for confirmation before doing anything. Tested end-to-end
(backup → simulated total database loss → restore → data intact) as part
of building this.

This is entirely the client's responsibility to actually run and monitor —
nothing in this repo schedules it automatically today (see
`Licensing_Packaging_Status.docx` / the open-items list — automatic backup
scheduling and off-box copies aren't built).

## Alerting (license renewal + database health)

Off by default — every environment variable below is opt-in, and nothing
about a client's data ever leaves their own deployment. Set these in that
deployment's `.env` (`docker-compose.client.yml.example` already has the
env var lines, just fill in real values) to get email or Microsoft Teams
alerts sent to *that client's own* configured recipients:

```
SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM_EMAIL
NOTIFICATION_EMAIL         # comma-separated recipients
TEAMS_WEBHOOK_URLS         # comma-separated Incoming Webhook URLs
```

With those set:

- **License renewal / expiry** — the existing hourly license check
  (`app/scheduler.py: _job_license_check`) now also emails/Teams-notifies
  once a day while the license is within its renewal window (`high`), and
  once a day while it's expired or revoked (`critical`). Previously this
  only showed up as a banner on `GET /license/status` — easy to miss if
  nobody's actively looking at the dashboard.
- **Database health** — opt-in separately via `DB_HEALTH_CHECK_ENABLED=true`
  (`_job_db_health_check`, default hourly via `DB_HEALTH_CHECK_INTERVAL_SECONDS`).
  Checks the database is reachable and alerts `critical` if not; checks its
  logical size against `DB_SIZE_WARNING_GB` (default 20) and alerts `high`
  if over. **Does not check host disk space or backup freshness** — those
  aren't visible from inside the app container. A database can still fill
  the underlying disk without this catching it; that's still on the
  client to monitor at the host level.

Both dedup to at most once per day per condition so an ongoing outage or a
15-day renewal window doesn't spam — verified with a dedicated test suite
(`tests/test_scheduler_notifications.py`) covering every alert path and the
dedup behavior itself.

## Updating the application

When the core product changes, rebuild that one client's image and
redeploy it:

```
docker build -t aigovernance-backend:base .          # rebuild the base
python scripts/license_build_image.py --license-file <their .lic> \
    --public-key license_public_key.pem --image-tag aigovernance-backend:<their-tag>
# on their infra:
docker compose pull   # if pulling from a registry, or re-transfer the image
docker compose up -d
```

Their database is untouched by this — only the application container is
replaced. Their license (baked in at the last build) comes along with it,
so no separate renewal step is needed just because you're shipping an
update, unless the license also happens to be due for renewal.

## Updating Postgres

The database image is pinned (`postgres:16-alpine` in
`docker-compose.client.yml.example`) rather than left floating, so it never
changes underneath a client without a deliberate decision. To move to a
newer Postgres:

```
docker compose pull db
docker compose up -d db
```

Take a backup first (above) — a major-version Postgres bump can require a
dump/reload rather than an in-place upgrade; check the target version's
upgrade notes before doing this on a production client.

## Host OS patching

**Deliberately not automated by us.** The client's server, its OS, and its
security patches are the client's own infrastructure — we have no access to
it by default (see `LICENSING_PACKAGING.md`), and unattended OS-level
patching is genuinely risky to script blindly (a bad kernel or Docker
engine update can take a production box down with no one watching). This
is a recommendation to pass along to whoever operates the client's server,
not something shipped in this repo:

- Keep Docker Engine itself reasonably current.
- Enable your distro's unattended security updates (e.g. `unattended-upgrades`
  on Debian/Ubuntu) for OS-level patches specifically, not full-system
  auto-upgrades.
- Reboot on your own schedule after kernel updates, not automatically.

If you want us to manage this for a specific client, that's a different
deployment model (us hosting their infrastructure) than what's built here —
worth a separate conversation, not a checkbox in this file.
