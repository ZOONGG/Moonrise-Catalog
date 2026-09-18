#!/usr/bin/env python3
"""Validate Moonrise package manifests and generate deterministic catalog outputs."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ROOT / "catalog" / "packages"
PACKAGE_SCHEMA = ROOT / "catalog" / "schema" / "package.schema.json"
INDEX_SCHEMA = ROOT / "catalog" / "schema" / "index.schema.json"
REPOSITORIES = ROOT / "docs" / "repositories.json"
MAX_ASSET_SIZE = 512 * 1024 * 1024
USER_AGENT = "Moonrise-Catalog-Validator/1.0"

DOWNLOAD_MODES = {
    "github-release": "githubRelease",
    "upstream-direct": "upstreamDirect",
    "upstream-raw": "upstreamRaw",
    "moonrise-reproducible-build": "moonriseRelease",
    "source-only": "sourceOnly",
    "external-page": "externalPage",
    "unavailable": "unavailable",
}
TYPE_NAMES = {"weave-mod": "weaveMod", "java-agent": "javaAgent"}
RISK_NAMES = {
    "normal": "normal",
    "experimental": "experimental",
    "server-dependent": "serverDependent",
    "unfair-advantage": "unfairAdvantage",
    "cheat-client": "cheatClient",
    "unknown": "unknown",
}
POLICY_NAMES = {
    "one-click": "oneClick",
    "confirmation-required": "confirmationRequired",
    "source-only": "sourceOnly",
    "external-download": "externalDownload",
    "unavailable": "unavailable",
}


def read_json(path: Path):
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def write_json(path: Path, value) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(value)


def load_manifests() -> list[dict]:
    return [read_json(path) for path in sorted(PACKAGES.glob("*.json"))]


def iter_urls(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"url", "repository", "licenseUrl"} and child:
                yield child
            yield from iter_urls(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_urls(child)


def validate_manifests(manifests: list[dict]) -> None:
    schema = read_json(PACKAGE_SCHEMA)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors: list[str] = []
    ids: set[str] = set()
    slugs: set[str] = set()
    for manifest in manifests:
        label = manifest.get("id", "<missing-id>")
        for error in validator.iter_errors(manifest):
            location = ".".join(str(part) for part in error.absolute_path)
            errors.append(f"{label}:{location}: {error.message}")
        if label in ids:
            errors.append(f"duplicate package id: {label}")
        ids.add(label)
        slug = manifest.get("slug", "")
        if slug in slugs:
            errors.append(f"duplicate package slug: {slug}")
        slugs.add(slug)
        if manifest.get("restricted") and (manifest.get("featured") or manifest.get("recommended")):
            errors.append(f"{label}: restricted packages cannot be featured or recommended")
        if manifest.get("riskLevel") in {"unfair-advantage", "cheat-client"} and not manifest.get("restricted"):
            errors.append(f"{label}: high-risk packages must be restricted")
        mode = manifest.get("distribution", {}).get("mode")
        policy = manifest.get("installPolicy")
        if policy == "one-click" and mode not in {"github-release", "upstream-direct", "upstream-raw", "moonrise-reproducible-build"}:
            errors.append(f"{label}: one-click policy requires an installable distribution mode")
        if mode in {"source-only", "external-page", "unavailable"} and policy in {"one-click", "confirmation-required"}:
            errors.append(f"{label}: non-downloadable distribution cannot use install policy {policy}")
        for url in iter_urls(manifest):
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.netloc:
                errors.append(f"{label}: URL must use HTTPS: {url}")
        for release in manifest.get("releases", []):
            asset = release["asset"]
            if Path(asset["fileName"]).name != asset["fileName"] or ".." in asset["fileName"]:
                errors.append(f"{label}: unsafe asset filename: {asset['fileName']}")
            if asset["fileSize"] > MAX_ASSET_SIZE:
                errors.append(f"{label}: asset exceeds {MAX_ASSET_SIZE} bytes")
    if errors:
        raise ValueError("\n".join(errors))


def generated_at(manifests: list[dict]) -> str:
    dates = [
        manifest["source"]["latestCommit"]["date"]
        for manifest in manifests
        if manifest["source"].get("latestCommit")
    ]
    if not dates:
        return "1970-01-01T00:00:00Z"
    value = max(datetime.fromisoformat(date.replace("Z", "+00:00")) for date in dates)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def app_package(manifest: dict) -> dict:
    source = manifest["source"]
    icon = (
        "restricted"
        if manifest["restricted"]
        else "archived"
        if source["archived"]
        else "source-only"
        if manifest["installPolicy"] == "source-only"
        else manifest["type"]
    )
    releases = []
    for release in manifest["releases"]:
        asset = release["asset"]
        releases.append(
            {
                "version": release["version"],
                "publishedAt": release["publishedAt"],
                "changelog": release["changelog"]["en"],
                "localizedChangelog": release["changelog"],
                "artifact": {
                    "downloadMode": DOWNLOAD_MODES[manifest["distribution"]["mode"]],
                    "assetUrl": asset["url"],
                    "upstreamReleaseUrl": manifest["distribution"].get("url"),
                    "fileName": asset["fileName"],
                    "fileSize": asset["fileSize"],
                    "sha256": asset["sha256"],
                    "javaVersion": asset.get("javaVersion"),
                    "weaveLoaderRange": asset.get("weaveLoaderRange"),
                    "minecraftVersions": manifest["supportedMinecraftVersions"],
                },
                "dependencies": [],
                "conflicts": [],
            }
        )
    return {
        "id": manifest["id"],
        "slug": manifest["slug"],
        "type": TYPE_NAMES[manifest["type"]],
        "name": manifest["localizations"]["en"]["name"],
        "summary": manifest["localizations"]["en"]["summary"],
        "description": manifest["localizations"]["en"]["description"],
        "localizations": manifest["localizations"],
        "authors": manifest["authors"],
        "homepage": manifest["distribution"].get("url"),
        "repository": source.get("repository"),
        "license": None if source.get("license") is None else {"name": source["license"], "url": source.get("licenseUrl")},
        "tags": ["restricted"] if manifest["restricted"] else [],
        "icon": f"https://zoongg.github.io/Moonrise-Catalog/assets/packages/{icon}.svg",
        "screenshots": [],
        "supportedMinecraftVersions": manifest["supportedMinecraftVersions"],
        "supportedClientVersions": manifest["supportedClientVersions"],
        "archived": source["archived"],
        "riskLevel": RISK_NAMES[manifest["riskLevel"]],
        "installPolicy": POLICY_NAMES[manifest["installPolicy"]],
        "restricted": manifest["restricted"],
        "featured": manifest["featured"],
        "recommended": manifest["recommended"],
        "releases": releases,
        "dependencies": [],
        "conflicts": [],
        "warnings": [{"code": warning["code"], "message": warning["en"], "localizedMessage": {"en": warning["en"], "ru": warning["ru"]}} for warning in manifest["warnings"]],
        "statistics": {"downloads": None, "moonriseInstalls": 0},
    }


def markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines.extend("| " + " | ".join(cell.replace("|", "\\|").replace("\n", " ") for cell in row) + " |" for row in rows)
    return "\n".join(lines) + "\n"


def package_page(manifest: dict) -> str:
    source = manifest["source"]
    warnings = "\n".join(f"- {warning['en']} / {warning['ru']}" for warning in manifest["warnings"]) or "- None recorded."
    todos = "\n".join(f"- {todo}" for todo in manifest["todos"]) or "- None."
    return (
        f"# {manifest['name']}\n\n"
        f"{manifest['summary']}\n\n"
        f"- Type: `{manifest['type']}`\n"
        f"- Risk: `{manifest['riskLevel']}`\n"
        f"- Install policy: `{manifest['installPolicy']}`\n"
        f"- Distribution: `{manifest['distribution']['mode']}`\n"
        f"- Repository: {source.get('repository') or 'Unavailable'}\n"
        f"- License: {source.get('license') or 'Not declared'}\n"
        f"- Archived: {str(source['archived']).lower()}\n"
        f"- Minecraft: {', '.join(manifest['supportedMinecraftVersions']) or 'Not declared upstream'}\n\n"
        f"## Description\n\n{manifest['description']}\n\n"
        f"## Warnings\n\n{warnings}\n\n"
        f"## Manual follow-up\n\n{todos}\n"
    )


def html_document(title: str, body: str) -> str:
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{html.escape(title)}</title>"
        "<style>body{max-width:1080px;margin:0 auto;padding:32px 20px;font:16px/1.55 system-ui,sans-serif;"
        "color:#e8e8ee;background:#101116}a{color:#8ec5ff}table{width:100%;border-collapse:collapse}"
        "th,td{padding:10px;border-bottom:1px solid #30323c;text-align:left;vertical-align:top}"
        ".meta{color:#a9abb8}.warning{color:#ffc7a8}code{background:#242630;padding:2px 5px;border-radius:4px}</style>"
        f"</head><body>{body}</body></html>\n"
    )


def package_html(manifest: dict) -> str:
    source = manifest["source"]
    localizations = manifest["localizations"]
    repository = source.get("repository")
    repository_html = (
        f'<a href="{html.escape(repository, quote=True)}">{html.escape(repository)}</a>'
        if repository
        else "Unavailable"
    )
    warnings = "".join(
        f'<li class="warning">{html.escape(warning["en"])}<br>{html.escape(warning["ru"])}</li>'
        for warning in manifest["warnings"]
    ) or "<li>None recorded.</li>"
    body = (
        '<p><a href="../../">← Moonrise catalog</a></p>'
        f'<h1>{html.escape(manifest["name"])}</h1>'
        f'<p>{html.escape(localizations["en"]["description"])}</p>'
        f'<p lang="ru">{html.escape(localizations["ru"]["description"])}</p>'
        '<table><tbody>'
        f'<tr><th>Type</th><td><code>{html.escape(manifest["type"])}</code></td></tr>'
        f'<tr><th>Risk</th><td><code>{html.escape(manifest["riskLevel"])}</code></td></tr>'
        f'<tr><th>Install policy</th><td><code>{html.escape(manifest["installPolicy"])}</code></td></tr>'
        f'<tr><th>Distribution</th><td><code>{html.escape(manifest["distribution"]["mode"])}</code></td></tr>'
        f'<tr><th>Repository</th><td>{repository_html}</td></tr>'
        f'<tr><th>License</th><td>{html.escape(source.get("license") or "Not declared")}</td></tr>'
        f'<tr><th>Archived</th><td>{str(source["archived"]).lower()}</td></tr>'
        '</tbody></table>'
        f'<h2>Warnings / Предупреждения</h2><ul>{warnings}</ul>'
        '<p class="meta">Catalog metadata does not establish that a package is safe or permitted by a server.</p>'
    )
    return html_document(manifest["name"], body)


def catalog_html(manifests: list[dict], generated_at_value: str) -> str:
    rows = "".join(
        "<tr>"
        f'<td><a href="packages/{manifest["slug"]}/">{html.escape(manifest["name"])}</a></td>'
        f'<td><code>{html.escape(manifest["type"])}</code></td>'
        f'<td><code>{html.escape(manifest["riskLevel"])}</code></td>'
        f'<td><code>{html.escape(manifest["installPolicy"])}</code></td>'
        "</tr>"
        for manifest in manifests
    )
    body = (
        "<h1>Moonrise package catalog</h1>"
        "<p>Public metadata for Lunar Client Java agents and Weave mods. "
        "Restricted, archived, source-only, and unavailable projects remain visible with explicit policy labels.</p>"
        f'<p class="meta">{len(manifests)} packages · generated {html.escape(generated_at_value)}</p>'
        '<p><a href="catalog/index.json">Application JSON</a> · '
        '<a href="schema/package.schema.json">Package schema</a></p>'
        '<table><thead><tr><th>Package</th><th>Type</th><th>Risk</th><th>Install policy</th></tr></thead>'
        f'<tbody>{rows}</tbody></table>'
    )
    return html_document("Moonrise package catalog", body)


def generate(manifests: list[dict]) -> None:
    validate_manifests(manifests)
    stamp = generated_at(manifests)
    index = {
        "schemaVersion": 1,
        "generatedAt": stamp,
        "signatureStatus": "unsignedDevelopment",
        "packages": [app_package(manifest) for manifest in manifests],
    }
    write_json(ROOT / "catalog" / "index.json", index)
    write_json(ROOT / "dist" / "catalog" / "index.json", index)
    (ROOT / "dist" / "schema").mkdir(parents=True, exist_ok=True)
    shutil.copy2(PACKAGE_SCHEMA, ROOT / "dist" / "schema" / "package.schema.json")
    shutil.copy2(INDEX_SCHEMA, ROOT / "dist" / "schema" / "index.schema.json")
    pages = ROOT / "docs" / "packages"
    pages.mkdir(parents=True, exist_ok=True)
    for stale in pages.glob("*.md"):
        stale.unlink()
    static_packages = ROOT / "dist" / "packages"
    if static_packages.exists():
        shutil.rmtree(static_packages)
    for manifest in manifests:
        text = package_page(manifest)
        write_text(pages / f"{manifest['slug']}.md", text)
        static = ROOT / "dist" / "packages" / manifest["slug"] / "index.md"
        static.parent.mkdir(parents=True, exist_ok=True)
        write_text(static, text)
        write_text(static.with_suffix(".html"), package_html(manifest))

    write_text(ROOT / "dist" / "index.html", catalog_html(manifests, stamp))

    rows = [[m["name"], m["type"], m["riskLevel"], m["installPolicy"], m["source"].get("repository") or "local/unavailable"] for m in manifests]
    repos = read_json(REPOSITORIES)
    repo_rows = [[r["repository"], r["result"], r["reason"]] for r in repos]
    all_report = "# All discovered packages and examined repositories\n\n" + markdown_table(
        ["Package", "Type", "Risk", "Install policy", "Source"], rows
    ) + "\n## Repositories examined\n\n" + markdown_table(["Repository", "Result", "Reason"], repo_rows)
    write_text(ROOT / "docs" / "ALL_DISCOVERED_PACKAGES.md", all_report)

    report_specs = [
        ("INSTALLABLE_PACKAGES.md", "Installable packages", lambda m: m["installPolicy"] in {"one-click", "confirmation-required"}),
        ("SOURCE_ONLY_PACKAGES.md", "Source-only packages", lambda m: m["installPolicy"] == "source-only"),
        ("RESTRICTED_PACKAGES.md", "Restricted packages", lambda m: m["restricted"]),
    ]
    for filename, title, predicate in report_specs:
        selected = [row for row, manifest in zip(rows, manifests) if predicate(manifest)]
        write_text(ROOT / "docs" / filename, f"# {title}\n\n" + markdown_table(["Package", "Type", "Risk", "Install policy", "Source"], selected))

    licenses = [[m["name"], m["source"].get("license") or "MISSING", str(m["source"]["automaticRedistributionPermitted"]), m["distribution"]["mode"]] for m in manifests]
    write_text(ROOT / "docs" / "LICENSE_AUDIT.md", "# License audit\n\nNo third-party JAR is stored or mirrored by this repository.\n\n" + markdown_table(["Package", "License", "Redistribution", "Mode"], licenses))
    compatibility = [[m["name"], ", ".join(m["supportedMinecraftVersions"]) or "Not declared", m["compatibilityNotes"]] for m in manifests]
    write_text(ROOT / "docs" / "COMPATIBILITY_STATUS.md", "# Compatibility status\n\n" + markdown_table(["Package", "Minecraft versions", "Evidence/status"], compatibility))

    counts = {
        "inspectedRepositories": len(repos),
        "catalogEntries": len(manifests),
        "oneClick": sum(m["installPolicy"] == "one-click" for m in manifests),
        "confirmationRequired": sum(m["installPolicy"] == "confirmation-required" for m in manifests),
        "sourceOnly": sum(m["installPolicy"] == "source-only" for m in manifests),
        "externalDownload": sum(m["installPolicy"] == "external-download" for m in manifests),
        "unavailable": sum(m["installPolicy"] == "unavailable" for m in manifests),
        "restricted": sum(m["restricted"] for m in manifests),
        "missingLicenses": sum(m["source"].get("license") is None for m in manifests),
    }
    write_json(ROOT / "docs" / "catalog-summary.json", counts)


def request(url: str, method: str = "HEAD"):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    return urllib.request.urlopen(req, timeout=30)


def network_validate(manifests: list[dict]) -> None:
    checked: set[str] = set()
    errors: list[str] = []
    for manifest in manifests:
        urls = []
        repository = manifest["source"].get("repository")
        if repository and manifest["distribution"]["mode"] != "unavailable":
            urls.append(repository)
        urls.extend(release["asset"]["url"] for release in manifest["releases"])
        for url in urls:
            if url in checked:
                continue
            checked.add(url)
            try:
                with request(url) as response:
                    if response.status >= 400:
                        errors.append(f"{url}: HTTP {response.status}")
            except urllib.error.HTTPError as error:
                if error.code in {403, 404, 405}:
                    try:
                        with request(url, "GET") as response:
                            if response.status >= 400:
                                errors.append(f"{url}: HTTP {response.status}")
                    except Exception as inner:
                        errors.append(f"{url}: {inner}")
                else:
                    errors.append(f"{url}: HTTP {error.code}")
            except Exception as error:
                errors.append(f"{url}: {error}")
    if errors:
        raise ValueError("\n".join(errors))


def inspect_artifact(path: Path, expected_type: str) -> None:
    if not zipfile.is_zipfile(path):
        raise ValueError(f"{path.name}: not a ZIP/JAR")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        for name in names:
            normalized = name.replace("\\", "/")
            if normalized.startswith("/") or ".." in normalized.split("/"):
                raise ValueError(f"{path.name}: archive traversal entry {name}")
        has_weave = "weave.mod.json" in names
        manifest = ""
        try:
            manifest = archive.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
        except KeyError:
            pass
        if expected_type == "weave-mod" and not (has_weave or "Weave-Entry:" in manifest):
            raise ValueError(f"{path.name}: declared Weave mod but no weave.mod.json or legacy Weave entry")
        if expected_type == "java-agent" and "Premain-Class:" not in manifest:
            raise ValueError(f"{path.name}: declared Java agent but Premain-Class is absent")


def artifact_validate(manifests: list[dict]) -> None:
    cache = ROOT / "download-cache"
    cache.mkdir(exist_ok=True)
    for manifest in manifests:
        if manifest["installPolicy"] not in {"one-click", "confirmation-required"}:
            continue
        if manifest["source"]["automaticRedistributionPermitted"] is not True:
            continue
        for release in manifest["releases"]:
            asset = release["asset"]
            target = cache / f"{manifest['id']}-{release['version']}.jar"
            if not target.exists():
                req = urllib.request.Request(asset["url"], headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=90) as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
            size = target.stat().st_size
            if size != asset["fileSize"]:
                raise ValueError(f"{manifest['id']}: expected {asset['fileSize']} bytes, got {size}")
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if digest != asset["sha256"]:
                raise ValueError(f"{manifest['id']}: SHA-256 mismatch")
            inspect_artifact(target, manifest["type"])


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("generate")
    validate = subparsers.add_parser("validate")
    validate.add_argument("--network", action="store_true")
    validate.add_argument("--artifacts", action="store_true")
    args = parser.parse_args()
    manifests = load_manifests()
    validate_manifests(manifests)
    if args.command == "generate":
        generate(manifests)
    else:
        Draft202012Validator(read_json(INDEX_SCHEMA), format_checker=FormatChecker()).validate(read_json(ROOT / "catalog" / "index.json"))
        if args.network:
            network_validate(manifests)
        if args.artifacts:
            artifact_validate(manifests)
    print(f"{args.command}: {len(manifests)} package manifests OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
