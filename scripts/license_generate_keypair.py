"""One-time setup: generate the RSA keypair used to sign/verify licenses.

Run this ONCE, ever (per signing identity) — not per customer, not per
license. Keep the private key on the machine that issues licenses; it is
never shipped to a customer. The public key is mounted alongside each
deployment's license.lic (see LICENSING_PACKAGING.md / scripts/license_deploy.py)
— it is never baked into the image, so rotating the signing key later never
requires rebuilding or re-shipping the image itself.

    python scripts/license_generate_keypair.py

Writes:
    license_private_key.pem  — KEEP SECRET. Never commit, never ship.
    license_public_key.pem   — ships to every client's ./license mount dir.
"""
import argparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-key-out", default="license_private_key.pem")
    parser.add_argument("--public-key-out", default="license_public_key.pem")
    parser.add_argument("--key-size", type=int, default=4096)
    args = parser.parse_args()

    key = rsa.generate_private_key(public_exponent=65537, key_size=args.key_size)

    with open(args.private_key_out, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ))

    with open(args.public_key_out, "wb") as f:
        f.write(key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ))

    print(f"Wrote private key: {args.private_key_out}  (KEEP SECRET — never ship)")
    print(f"Wrote public key:  {args.public_key_out}  (bake into the package image)")


if __name__ == "__main__":
    main()
