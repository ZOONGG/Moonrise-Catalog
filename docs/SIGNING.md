# Catalog signing

Production catalogs use an Ed25519 signature over the exact UTF-8 bytes of `catalog/index.json`. Unsigned output is explicitly marked `unsigned-development` and must not be treated as production.

1. Generate an Ed25519 key using a trusted offline tool.
2. Store the Base64-encoded 32-byte raw private key or an unencrypted Ed25519 PEM key in the repository secret `MOONRISE_CATALOG_ED25519_PRIVATE_KEY`.
3. Run `python scripts/sign_catalog.py`; CI performs the same operation.
4. Commit the resulting public key only after verifying its fingerprint through a separate trusted channel. Never commit the private key.
5. Configure Moonrise with that trusted public key before requiring production signatures.

No key is generated automatically because doing so in an unattended build would not establish a trusted production root.
