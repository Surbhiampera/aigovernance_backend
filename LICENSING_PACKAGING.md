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
issued license otherwise runs to its natural expiry.

```
python scripts/license_revoke.py \
    --host https://acme.clients.example.com \
    --admin-key <acme's admin API key> \
    --license-id acme-2026 \
    --reason "contract ended 2026-08-01"
```

This calls that client's own `POST /license/revoke` — the same admin API
key renewal already requires is all that's needed; no filesystem or docker
access to their box, no rebuild. It writes the `license_id` to a local
denylist (`LICENSE_DENYLIST_PATH`) and re-verifies immediately, visible to
every worker on its next request (same mechanism as renewal). The freeze
this produces is distinguished from an ordinary expiry as a `license_revoked`
402, and `/license/upload` refuses to re-install an already-revoked license.
Only ever affects the one deployment you point it at.

Because verification is fully offline by design (no phone-home to any
vendor service), this only works for as long as you retain admin API
access to that one client's own deployment. If a client has revoked or
firewalled off your admin key too, no code-level fix restores access;
that's an inherent boundary of an offline-verified system, not something
specific to this implementation.

## Renewal & DB health alerting

See `OPERATIONS.md`. Set `SMTP_*`/`TEAMS_WEBHOOK_URLS` in a deployment's
`.env` and the 15-day renewal warning (and expiry, and revocation) email or
Teams-notify that one deployment's own configured recipients once a day —
no longer just a banner on `GET /license/status`. Off by default; nothing
is sent anywhere unless explicitly configured, and nothing about a client
ever reaches you through this — it only notifies whoever *that client's*
`.env` points at.

## Key custody & rotation

**Proposed policy** (for business sign-off — this is a recommendation, not
a decision made on your behalf):

- Store `license_private_key.pem` in a secrets manager (e.g. a cloud KMS or
  vault product), not as a bare file on someone's laptop — it's the one
  artifact that, if leaked, lets anyone forge a valid license for any
  customer.
- Restrict who can invoke `license_issue.py` (i.e., who can reach the
  private key) to a small, named set of people — issuing a license is a
  business action, not something every engineer needs routine access to.
- Rotate on a fixed schedule (a reasonable starting point: yearly, or
  immediately if the key is ever suspected compromised) rather than never.

**Rotation is technically supported today**, independent of when the
policy above gets adopted. `verify_license_any_key()`
(`app/services/license_service.py`) tries every configured public key in
turn, and `LICENSE_PUBLIC_KEY_EXTRA_PATHS` (comma-separated, alongside the
primary `LICENSE_PUBLIC_KEY_PATH`) lets a deployment accept licenses signed
by more than one key at once. A rotation looks like:

1. Generate a new keypair (`license_generate_keypair.py` — use different
   `--private-key-out`/`--public-key-out` names so the old ones aren't
   overwritten).
2. For each active client: add the new public key alongside the old one
   (`LICENSE_PUBLIC_KEY_EXTRA_PATHS`) — their currently-installed,
   old-key-signed license keeps verifying, no interruption.
3. Reissue that client's license under the new key at your convenience
   (`license_issue.py --private-key <new-private-key>`), install it the
   normal way (`/license/upload` or a rebuild).
4. Once every active client has been reissued under the new key, drop the
   old key from `LICENSE_PUBLIC_KEY_EXTRA_PATHS` (and from where the old
   private key is stored) on your own schedule.

Tested end-to-end: `tests/test_license_service.py::test_verify_license_any_key_tries_each_key_in_turn`
and `test_refresh_license_status_honors_extra_public_key_paths` cover a
pre-rotation license verifying against `[new_key, old_key]` exactly as
above.

## Dashboard banner

Still open. The admin UI isn't in this repo — this backend only returns
the raw JSON `GET /license/status` needs (`analytics_frozen`,
`show_renewal_banner`, `days_until_expiry`, etc.). Deliberately left as-is
per an earlier decision not to build cross-deployment visibility that
would require phone-home reporting, which conflicts with the fully-offline
design everything else here follows.
