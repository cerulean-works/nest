#!/usr/bin/env python3
"""
Writes a throwaway staging CA and a server certificate for the PoC gateway.

Usage: make_certs.py OUT_DIR [--name grpc.staging.nest.invalid]

Creates `ca.crt`, `server.crt` and `server.key` (mode 0600). The certificate is
valid for 7 days and only for the given DNS name, so it can never be mistaken for
a production certificate. Live staging tests use the gateway's real certificate
(Let's Encrypt or Cloudflare) instead.
"""

import argparse
import datetime
import os
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DEFAULT_NAME = "grpc.staging.nest.invalid"


def make(out: Path, name: str = DEFAULT_NAME) -> None:
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Nest H06 PoC staging CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=7))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    server = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=7))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    (out / "ca.crt").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (out / "server.crt").write_bytes(server.public_bytes(serialization.Encoding.PEM))
    key_path = out / "server.key"
    key_path.touch(mode=0o600, exist_ok=True)
    os.chmod(key_path, 0o600)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out", type=Path)
    parser.add_argument("--name", default=DEFAULT_NAME)
    args = parser.parse_args()
    make(args.out, args.name)
    print(f"Wrote {args.out}/ca.crt, server.crt and server.key for {args.name}")


if __name__ == "__main__":
    main()
