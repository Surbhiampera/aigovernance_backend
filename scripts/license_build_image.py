"""Build ONE client's own dedicated image, license baked in.

Each client gets a self-contained image — never a shared image with a
mounted license file — so there's no way to accidentally start one client's
container against another client's license, and no shared state between
clients at all. See LICENSING_PACKAGING.md.

Requires the base image already built once:

    docker build -t aigovernance-backend:base .

Example — a new client:

    python scripts/license_build_image.py \\
        --customer "Acme Retail Co." \\
        --license-file acme-2026.lic \\
        --public-key license_public_key.pem \\
        --image-tag aigovernance-backend:acme-2026

Renewing later without a rebuild: POST the new .lic to that client's own
running container at /license/upload (admin API key) — takes effect
immediately, no downtime. Re-run this script with the renewed .lic at your
next release so a freshly (re)started container also starts already-renewed
instead of reverting to the license baked in at the last build.
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--license-file", required=True, help="This client's .lic from license_issue.py")
    parser.add_argument("--public-key", required=True, help="license_public_key.pem (same for every client)")
    parser.add_argument("--image-tag", required=True, help="e.g. aigovernance-backend:acme-2026")
    parser.add_argument("--base-image", default="aigovernance-backend:base")
    parser.add_argument("--denylist", default=None, help="Optional revoked-license-ids file for this client (see license_revoke.py)")
    parser.add_argument("--customer", default=None, help="Display name, printed only")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        shutil.copyfile(args.public_key, tmp_path / "license_public_key.pem")
        shutil.copyfile(args.license_file, tmp_path / "license.lic")

        dockerfile = REPO_ROOT / "Dockerfile.client"
        if args.denylist:
            shutil.copyfile(args.denylist, tmp_path / "license_denylist.txt")
            dockerfile_text = dockerfile.read_text()
            dockerfile_text += (
                "\nCOPY --chown=appuser:appuser license_denylist.txt /app/license/license_denylist.txt\n"
            )
            dockerfile = tmp_path / "Dockerfile.client.with_denylist"
            dockerfile.write_text(dockerfile_text)

        cmd = [
            "docker", "build",
            "-f", str(dockerfile),
            "--build-arg", f"BASE_IMAGE={args.base_image}",
            "-t", args.image_tag,
            str(tmp_path),
        ]
        who = f" for {args.customer!r}" if args.customer else ""
        print(f"Building {args.image_tag}{who} from base image {args.base_image}...")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            sys.exit(result.returncode)

    print(f"\nBuilt {args.image_tag} — self-contained, ready to ship to this client alone.")
    print("Point that client's own docker-compose (see docker-compose.client.yml.example) at this tag.")


if __name__ == "__main__":
    main()
