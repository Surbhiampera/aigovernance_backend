"""Issue (or renew) a signed license for one packaged/licensed deployment.

Requires license_private_key.pem from license_generate_keypair.py — never
run this on a customer's machine, only on the machine that owns the private
key.

Example — a new 12-month license for a consulting-arm client:

    python scripts/license_issue.py \\
        --customer "Acme Retail Co." \\
        --license-id acme-2026 \\
        --days 365 \\
        --features analytics,budgets,pii_masking \\
        --out license.lic

Ship the resulting license.lic file to the customer; they drop it in place
at LICENSE_FILE_PATH and either restart or wait for the next periodic check.
"""
import argparse
import datetime

import jwt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--customer", required=True, help="Customer/org display name")
    parser.add_argument("--license-id", required=True, help="Unique id, e.g. 'acme-2026'")
    parser.add_argument("--days", type=int, default=365, help="Validity period in days")
    parser.add_argument("--features", default="", help="Comma-separated enabled feature flags")
    parser.add_argument("--private-key", default="license_private_key.pem")
    parser.add_argument("--out", default="license.lic")
    args = parser.parse_args()

    with open(args.private_key, "r") as f:
        private_key_pem = f.read()

    now = datetime.datetime.now(datetime.timezone.utc)
    claims = {
        "license_id": args.license_id,
        "customer": args.customer,
        "features": [f.strip() for f in args.features.split(",") if f.strip()],
        "iat": int(now.timestamp()),
        "exp": int((now + datetime.timedelta(days=args.days)).timestamp()),
    }

    token = jwt.encode(claims, private_key_pem, algorithm="RS256")

    with open(args.out, "w") as f:
        f.write(token)

    print(f"Issued license '{args.license_id}' for {args.customer!r}, expires {claims['exp']} ({args.days} days)")
    print(f"Wrote {args.out} — ship this file to the customer.")


if __name__ == "__main__":
    main()
