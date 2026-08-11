# Licensing & Packaging Runbook

How a per-client, signed, time-limited license gets issued and deployed.
See `Licensing_Packaging_Status.docx` for the stakeholder-facing summary
this runbook implements.

**Core rule: every client gets their own dedicated image.** No shared
image, no runtime-mounted license file, no shared database, no shared
anything. A client's license is baked into a container image built just
for them (`Dockerfile.client`); the resulting tag is only ever run against
that client's own database and their own containers
(`docker-compose.client.yml.example`). Two clients never share a running
stack, so there's no way for one client's renewal, revocation, or data to
touch another's.

## 1. One-time setup (do this once, ever)

Generate the signing keypair. Only whoever issues licenses runs this, and
only once per signing identity — the same key pair signs every client's
license:

```
python scripts/license_generate_keypair.py
```

Writes `license_private_key.pem` (keep this off every machine except the
one issuing licenses — never commit it, never ship it) and
`license_public_key.pem` (not secret; baked into every client's image so
they can each verify their own license fully offline).

Build the shared base image once (unlicensed — this is what every client's
own image is built FROM):

```
docker build -t aigovernance-backend:base .
```

(`docker compose build` also produces this tag — see `docker-compose.yml`.)

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
be unique per client/term.

## 3. Build that client's own image

```
python scripts/license_build_image.py \
    --customer "Acme Retail Co." \
    --license-file acme-2026.lic \
    --public-key license_public_key.pem \
    --image-tag aigovernance-backend:acme-2026
```

This bakes `acme-2026.lic` and the public key into a new image tagged
`aigovernance-backend:acme-2026` — a self-contained artifact for this
client alone. It cannot be started against any other client's license: the
license isn't a runtime input, it's part of the image.

## 4. Deploy it

```
cp docker-compose.client.yml.example docker-compose.yml   # on the client's infra
# edit the `image:` line to this client's tag
# fill in .env: POSTGRES_PASSWORD, GOVERNANCE_MASTER_KEY, Azure OpenAI creds
docker compose up -d
```

`LICENSE_ENFORCEMENT_ENABLED=true` is baked into the image
(`Dockerfile.client`) — nothing to set for it in compose. The license is
checked at startup and every `LICENSE_CHECK_INTERVAL_SECONDS` (default
3600s) after. A missing, invalid, expired, or revoked license only freezes
the admin/analytics dashboard (HTTP 402) — the AI proxy this client's
integrations depend on is never gated (`app/main.py`: `enforce_license` is
applied to every router except `proxy_router` and `license_router`).

## 5. Renew before it lapses

`GET /license/status` on **that client's own container** (never gated, so
it's reachable even while frozen) starts returning
`show_renewal_banner: true` 15 days out (`LICENSE_RENEWAL_WARNING_DAYS`).

Two ways to renew this one client, no rebuild required for either:

- Issue the new `.lic` (`license_issue.py`), then have an admin `POST` it
  to that client's own `/license/upload` (admin API key required):

  ```
  curl -X POST https://<this-clients-host>/license/upload \
      -H "X-API-Key: <this-clients-admin-api-key>" \
      -F "file=@acme-2026-renewal.lic"
  ```

  Applied immediately (`refresh_license_status()` re-verifies on upload,
  visible to every worker on its next request — no restart). The endpoint
  rejects the file with a 400 if it doesn't verify.

**This live renewal lives in the running container's writable layer, not
the image.** It survives `docker compose restart`. It does **not** survive
recreating the container from the original image (`docker compose up
--force-recreate`, or a fresh deploy) — that reverts to whatever was baked
in at the last build. So: the live upload keeps a client running with zero
downtime the moment you renew, and at your next convenient release you
re-run `license_build_image.py` with the renewed `.lic` so a freshly
(re)started container also starts already-renewed. Nothing about this
touches any other client's image, container, or database.

## Revocation

See the `Key Challenges` section in `Licensing_Packaging_Status.docx` — an
issued license otherwise runs to its natural expiry. `app/services/license_service.py`
supports a per-client denylist of revoked `license_id`s
(`LICENSE_DENYLIST_PATH`), checked the same way as expiry, distinguished as
a `license_revoked` 402. Because verification is fully offline by design
(no phone-home to any vendor service), revoking only works for as long as
you retain filesystem/API access to that one client's own deployment — the
same access renewal already assumes. If a client has cut off that access
too, no code-level fix restores it; that's an inherent boundary of an
offline-verified system, not something specific to this implementation.

## What's still open

- **Renewal alerting:** the 15-day warning currently only shows on
  `GET /license/status` — nothing emails or pings the team/client
  automatically yet.
- **Signing-key custody & rotation:** the private key lives wherever
  `license_generate_keypair.py` was run, indefinitely, with no rotation
  process. Who holds it long-term is a business decision, not just a code
  change.
- **Dashboard banner:** the admin UI isn't in this repo; this backend only
  returns the raw JSON `GET /license/status` needs.

These are tracked separately; this runbook covers what's needed to issue,
build, ship, install, and renew one client's license today.
