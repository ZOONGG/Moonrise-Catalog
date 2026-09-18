#!/usr/bin/env python3
"""Create or verify an optional Ed25519 signature for catalog/index.json."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "catalog" / "index.json"


def write_text(path: Path, value: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding=encoding, newline="\n") as stream:
        stream.write(value)


def private_key_from_environment() -> Ed25519PrivateKey | None:
    value = os.environ.get("MOONRISE_CATALOG_ED25519_PRIVATE_KEY", "").strip()
    if not value:
        return None
    if value.startswith("-----BEGIN"):
        key = serialization.load_pem_private_key(value.encode(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("MOONRISE_CATALOG_ED25519_PRIVATE_KEY is not Ed25519")
        return key
    raw = base64.b64decode(value, validate=True)
    if len(raw) != 32:
        raise ValueError("Base64 Ed25519 private key must contain exactly 32 raw bytes")
    return Ed25519PrivateKey.from_private_bytes(raw)


def sign() -> None:
    key = private_key_from_environment()
    status = ROOT / "dist" / "catalog" / "signing-status.json"
    status.parent.mkdir(parents=True, exist_ok=True)
    if key is None:
        write_text(status, json.dumps({"status": "unsigned-development"}, indent=2) + "\n")
        print("No signing secret; emitted explicitly unsigned development status.")
        return
    index = json.loads(CATALOG.read_text(encoding="utf-8"))
    index["signatureStatus"] = "signedProduction"
    payload = (json.dumps(index, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    CATALOG.write_bytes(payload)
    (ROOT / "dist" / "catalog" / "index.json").write_bytes(payload)
    signature = key.sign(payload)
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    for directory in (ROOT / "catalog", ROOT / "dist" / "catalog"):
        directory.mkdir(parents=True, exist_ok=True)
        write_text(directory / "index.json.sig", base64.b64encode(signature).decode() + "\n", "ascii")
        write_text(directory / "index.json.pub", base64.b64encode(public).decode() + "\n", "ascii")
    write_text(status, json.dumps({"status": "signed-production", "algorithm": "Ed25519"}, indent=2) + "\n")
    print("Signed catalog/index.json with Ed25519.")


def verify(public_path: Path, signature_path: Path) -> None:
    public = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_path.read_text().strip(), validate=True))
    signature = base64.b64decode(signature_path.read_text().strip(), validate=True)
    public.verify(signature, CATALOG.read_bytes())
    print("Ed25519 signature valid.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--public-key", type=Path)
    parser.add_argument("--signature", type=Path)
    args = parser.parse_args()
    if args.verify:
        if not args.public_key or not args.signature:
            parser.error("--verify requires --public-key and --signature")
        verify(args.public_key, args.signature)
    else:
        sign()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
