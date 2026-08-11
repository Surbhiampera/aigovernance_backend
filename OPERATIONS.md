# Operations Runbook — Backups, Updates, Patching

Day-2 operations for a client's self-hosted deployment (see
`LICENSING_PACKAGING.md` for how the deployment itself gets built and
shipped). Everything here runs on **that one client's own infrastructure**
— there is nothing central for us to operate, because we never hold a
client's data (see the "client self-hosts everything" model).

## Backups

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
