# Operations Runbook — Backups, Updates, Patching

Day-2 operations for a client's self-hosted deployment (see
`LICENSING_PACKAGING.md` for how the deployment itself gets built and
shipped). Everything here runs on **that one client's own infrastructure**
— there is nothing central for us to operate, because we never hold a
client's data (see the "client self-hosts everything" model).

## Backups

**Hand the client `scripts/db_backup.sh`, `scripts/db_restore.sh`,
`scripts/uptime_watchdog.sh`, `scripts/os_patch_check.sh`, and
`scripts/lib/` (a shared helper the last two depend on) along with
`docker-compose.yml` and `.env`.** None of these are baked into the image
(the image only contains `app/`) and there's no source checkout on the
client's side to find them in otherwise — these are the extra files beyond
what `LICENSING_PACKAGING.md` lists as the handoff. They only need Docker
and `curl` to run — no Python, no dependencies beyond what's already on any
Linux server.

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

## Uptime monitoring

```
./scripts/uptime_watchdog.sh http://localhost:8000/health 3
```

Runs **outside** the container deliberately — if the container or the host
itself is down, nothing inside it can alert about it. Checks `/health`,
alerts once after 3 consecutive failed checks (default; avoids alerting on
a single blip), stays silent through the rest of an ongoing outage (no
spam), and alerts once more on recovery. Reuses the same `SMTP_*`/
`TEAMS_WEBHOOK_URLS` already configured in `.env` — via `curl`'s built-in
SMTP support, not Python, since this has to work even when the app
container (and its Python environment) is the thing that's down.

Schedule it, e.g. every 5 minutes via cron on the client's host:

```
*/5 * * * * cd /path/to/this/deployment && ./scripts/uptime_watchdog.sh
```

Tested end-to-end against a real container: stopped it, confirmed silence
below threshold, confirmed exactly one alert at threshold, confirmed
silence through continued downtime, restarted it, confirmed exactly one
recovery alert.

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

**Two tools, both deliberately alert-only — neither applies anything to a
client's server automatically.** Unattended OS-level patching is genuinely
risky to script blindly (a bad kernel or Docker engine update can take a
production box down with no one watching), so nothing here bypasses a
human making that call.

```
./scripts/os_patch_check.sh
```

Checks for pending updates (Debian/Ubuntu `apt` only — reports
"unsupported" on anything else rather than guessing at a package manager),
and alerts (same `SMTP_*`/`TEAMS_WEBHOOK_URLS` mechanism as the uptime
watchdog) if any are from a security pocket. Applies nothing. Schedule it
weekly, e.g.:

```
0 6 * * 1 cd /path/to/this/deployment && ./scripts/os_patch_check.sh
```

If a client wants actual hands-off patching rather than just an alert,
`scripts/templates/50unattended-upgrades` and `20auto-upgrades` are a
conservative starting config for Debian/Ubuntu's own `unattended-upgrades`
package (Canonical/Debian's maintained tool, not something we built) —
security updates only, no automatic reboot. Copy both to
`/etc/apt/apt.conf.d/` on their host after reviewing them; this is
something the client opts into themselves, not something either of our
scripts enables on its own.

- Keep Docker Engine itself reasonably current (not covered by either tool
  above — Docker's own package, not a distro security pocket).
- Reboot on your own schedule after kernel updates, not automatically —
  `Automatic-Reboot "false"` in the template reflects this.

If you want us to manage this for a specific client, that's a different
deployment model (us hosting their infrastructure) than what's built here —
worth a separate conversation, not a checkbox in this file.
