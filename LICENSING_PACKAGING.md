# Licensing & Packaging Runbook

How a per-client, signed, time-limited license gets issued and deployed
against the one downloadable image this repo builds. See
`Licensing_Packaging_Status.docx` for the stakeholder-facing summary this
runbook implements.

Core idea: **the image is the same for every client.** Only the contents of
a mounted `./license/` directory differ per deployment. Nothing here is
baked per-client into the image — that keeps a single build shippable to
everyone and makes renewals and key rotation possible without a rebuild.

## 1. One-time setup (do this once, ever)

Generate the signing keypair. Only whoever issues licenses runs this, and
only once per signing identity:

```
python scripts/license_generate_keypair.py
```

Writes `license_private_key.pem` (keep this off every machine except the
one issuing licenses — never commit it, never ship it) and
`license_public_key.pem` (not secret; it ships to every client alongside
their license).

## 2. Issue a license for a client

```
python scripts/license_issue.py \
    --customer "Acme Retail Co." \
    --license-id acme-2026 \
    --days 365 \
    --features analytics,budgets,pii_masking \
    --out acme-2026.lic
```

Produces a single signed `.lic` file for that client. `--license-id` should
be unique per client/term (used later if you need to reference this
specific license).

## 3. Stage it for deployment

```
python scripts/license_deploy.py \
    --license-file acme-2026.lic \
    --public-key license_public_key.pem \
    --customer "Acme Retail Co."
```

This copies both files into `./license/` — the directory `docker-compose.yml`
mounts into the container at `/app/license`. Do this on whatever machine
will run `docker compose up` for that client (their own box, or a
client-specific VM/host you control).

## 4. Turn on enforcement and deploy

In that deployment's `.env` (**never** the shared platform deployment):

```
LICENSE_ENFORCEMENT_ENABLED=true
```

Then:

```
docker compose up -d
```

The same image everyone else runs. `LICENSE_FILE_PATH` and
`LICENSE_PUBLIC_KEY_PATH` already default to `/app/license/license.lic` and
`/app/license/license_public_key.pem` inside the image (see `Dockerfile`),
matching where `license_deploy.py` staged the files — no need to set those
explicitly unless you're deviating from the default mount.

The license is checked at startup and every `LICENSE_CHECK_INTERVAL_SECONDS`
(default 3600s) after. A missing or invalid license only freezes the
admin/analytics dashboard (HTTP 402) — the AI proxy this client's
integrations depend on is never gated (`app/main.py`, `enforce_license` is
applied to every router except `proxy_router` and `license_router`).

## 5. Renew before it lapses

`GET /license/status` (never gated, so it's reachable even while frozen)
starts returning `show_renewal_banner: true` 15 days out
(`LICENSE_RENEWAL_WARNING_DAYS`).

Two ways to renew, no rebuild either way:

- **Filesystem access to the deployment:** re-run `license_issue.py` for a
  new term, then `license_deploy.py` again to overwrite `./license/license.lic`,
  then `docker compose restart backend` (or just wait for the next hourly
  check — it re-reads the file on its own).
- **No server access (remote client):** issue the new `.lic` the same way,
  then have an admin `POST` it to `/license/upload` (requires an admin-role
  API key):

  ```
  curl -X POST https://<client-host>/license/upload \
      -H "X-API-Key: <admin-api-key>" \
      -F "file=@acme-2026-renewal.lic"
  ```

  Applied immediately — `refresh_license_status()` re-verifies on upload and
  the endpoint rejects the file with a 400 if it doesn't verify.

## Permissions note (bind mount)

`docker-compose.yml` bind-mounts `./license:/app/license`. The container
runs as a non-root `appuser`. For **reading** the mounted files this needs
no special setup — normal file permissions (world-readable) are enough. If
you plan to use the `/license/upload` renewal path (which **writes** to
`license.lic` inside the container), make sure the host `./license`
directory is writable by the container's user, e.g.:

```
chmod 777 ./license   # simplest; fine for a local per-client scratch dir
```

or match the container's UID on the host if you'd rather not loosen
permissions that far.

## What's still open

- **Revocation:** none yet — an issued license runs to its expiry date;
  there's no way to cut a client off early today.
- **Key custody & rotation:** the private key currently lives wherever
  `license_generate_keypair.py` was run, indefinitely, with no rotation
  process. Who holds it long-term is a business decision, not just a code
  change.
- **Renewal alerting:** the 15-day warning only shows on `GET /license/status`
  today — nothing emails or pings the team/client automatically.

These are tracked separately; this runbook covers what's needed to issue,
ship, install, and renew a license today.
