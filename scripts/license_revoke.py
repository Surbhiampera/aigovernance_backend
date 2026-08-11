"""Revoke ONE client's license early, before its natural expiry.

Calls that client's own POST /license/revoke — the same admin API key
already required for renewal (see LICENSING_PACKAGING.md) is all that's
needed; no filesystem or docker access to their box required, and no
rebuild. Only ever affects the one deployment you point it at.

Example:

    python scripts/license_revoke.py \\
        --host https://acme.clients.example.com \\
        --admin-key <acme's admin API key> \\
        --license-id acme-2026 \\
        --reason "contract ended 2026-08-01"
"""
import argparse
import json
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", required=True, help="This client's deployment base URL, e.g. https://acme.example.com")
    parser.add_argument("--admin-key", required=True, help="This client's admin-role API key (X-API-Key)")
    parser.add_argument("--license-id", required=True, help="The license_id to revoke on this deployment")
    parser.add_argument("--reason", default="", help="Free-text note, recorded alongside the revocation")
    args = parser.parse_args()

    body = json.dumps({"license_id": args.license_id, "reason": args.reason}).encode("utf-8")
    req = urllib.request.Request(
        args.host.rstrip("/") + "/license/revoke",
        data=body,
        headers={"Content-Type": "application/json", "X-API-Key": args.admin_key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())

    print(f"Revoked '{args.license_id}' on {args.host}")
    print(f"  analytics_frozen: {result.get('analytics_frozen')}")
    print(f"  error: {result.get('error')}")


if __name__ == "__main__":
    main()
