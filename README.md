# Moonrise package catalog

This repository contains the public, metadata-only package catalog consumed by Moonrise. It indexes Lunar Client Java agents and Weave mods, including archived, restricted, source-only, and unavailable projects. It does not redistribute third-party JARs.

Package manifests under `catalog/packages/` are the source of truth. Run:

```powershell
python -m pip install -r requirements-dev.txt
python scripts/catalog.py generate
python scripts/catalog.py validate --network --artifacts
```

The generated application endpoint is `catalog/index.json`. Human-readable reports are generated under `docs/`, and static website data is generated under `dist/`.

Catalog integrity, licenses, and hashes do not establish that a package is safe or allowed by a server. Restricted entries are not featured, recommended, or bundled with Moonrise.
