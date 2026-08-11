"""Stage one client's license material for the downloadable image.

The image is identical for every client (see LICENSING_PACKAGING.md) — only
what's mounted at /app/license differs. This script copies a client's
signed license (from license_issue.py) and the shared public key (from
license_generate_keypair.py) into the local ./license/ directory that
docker-compose.yml mounts into the container, and reminds you what to do
next.

Example — staging a new client for first deploy:

    python scripts/license_deploy.py \\
        --license-file acme-2026.lic \\
        --public-key license_public_key.pem \\
        --customer "Acme Retail Co."

Renewing later works the same way: re-run with the new .lic file, then
either restart the container or use POST /license/upload — no rebuild.
"""
import argparse
import shutil
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--license-file", required=True, help="Path to the customer's .lic file")
    parser.add_argument("--public-key", required=True, help="Path to license_public_key.pem")
    parser.add_argument("--license-dir", default="license", help="Mount dir docker-compose.yml points at ./license")
    parser.add_argument("--customer", default=None, help="Display name, printed only")
    args = parser.parse_args()

    license_dir = Path(args.license_dir)
    license_dir.mkdir(parents=True, exist_ok=True)

    shutil.copyfile(args.public_key, license_dir / "license_public_key.pem")
    shutil.copyfile(args.license_file, license_dir / "license.lic")

    who = f" for {args.customer!r}" if args.customer else ""
    print(f"Staged license{who} into {license_dir}/")
    print(f"  {license_dir}/license_public_key.pem")
    print(f"  {license_dir}/license.lic")
    print()
    print("Next steps:")
    print("  1. Set LICENSE_ENFORCEMENT_ENABLED=true in this deployment's .env (only on the client's box, never the shared platform)")
    print("  2. docker compose up -d   (first deploy)")
    print("     — or, for a renewal on an already-running deployment, either:")
    print("       docker compose restart backend")
    print("       or POST the new .lic to /license/upload as an admin — no restart needed")


if __name__ == "__main__":
    main()
