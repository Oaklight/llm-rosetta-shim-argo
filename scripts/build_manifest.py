#!/usr/bin/env python3
"""Build the per-vendor archives and the shim manifest (manifest v1).

A gateway reads ``manifest.json`` from this repository's ``dist`` branch to
choose, per vendor, the newest version its core can load, and installs the
archive that record names, pinned by its sha256 (Oaklight/llm-rosetta#1660).
This script produces both::

    <out>/manifest.json                    latest, overwritten each release
    <out>/<release>/manifest.json          immutable copy for this release tag
    <out>/<version>/<vendor>.tar.gz        one per vendor version, immutable
    <out>/<version>/<vendor>.tar.gz.sha256

``<out>`` is a checkout of the ``dist`` branch (or an empty directory).  The
records come from three places:

* ``vendors.toml`` — each vendor's version, maturity, tags and the
  ``requires`` floor; the only file a release edits by hand;
* the vendor's ``provider.yaml`` files — the ``shims`` list;
* the archive :mod:`build_shim_archive` builds — ``sha256`` and ``archive``.

A record, once published, is never edited.  When the previous manifest
already holds the vendor's version, the archive must hash to the published
digest: the same bytes keep the old record, different bytes are refused
("bump the version").  A new version is prepended, so ``versions`` stays
newest first.

Usage::

    python scripts/build_manifest.py --dry-run                 # build, validate, print
    python scripts/build_manifest.py --out dist-checkout \\
        --previous dist-checkout/manifest.json --expect-version 0.4.0
    python scripts/build_manifest.py --check path/to/manifest.json

Needs Python 3.11+ (``tomllib``); nothing outside the standard library.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_shim_archive as archives  # noqa: E402

#: The manifest format this script writes and checks.
MANIFEST_VERSION = 1

#: The catalogue id a gateway knows this repository by.
SOURCE = "argonne"

#: The repository the manifest describes.
REPO = "https://github.com/Oaklight/llm-rosetta-shim-argonne"

MATURITIES = ("experimental", "beta", "stable")
CONFORMANCE_RESULTS = ("pass", "fail", "none")

_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[.-]?(?:a|b|rc|dev|post)\d+)?$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_PROMOTED = re.compile(r"^core@\d+\.\d+\.\d+")
_SHIM_NAME = re.compile(r"^[a-z0-9_]+--[a-z0-9_]+$")


class ManifestError(RuntimeError):
    """The manifest cannot be built or does not satisfy the spec."""


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def load_vendor_config(root: Path) -> dict[str, Any]:
    """Return ``vendors.toml`` parsed, after checking it names every vendor."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        raise ManifestError("build_manifest.py needs Python 3.11+ (tomllib)") from None
    config = tomllib.loads((root / "vendors.toml").read_text(encoding="utf-8"))
    table = config.get("vendors")
    if not isinstance(table, dict) or not table:
        raise ManifestError("vendors.toml: no [vendors.<name>] tables")
    shipped = set(archives.vendors(root))
    declared = set(table)
    if shipped != declared:
        raise ManifestError(
            "vendors.toml and the providers tree disagree: "
            f"unlisted vendors {sorted(shipped - declared) or 'none'}, "
            f"listed but missing {sorted(declared - shipped) or 'none'}"
        )
    return config


def package_version(root: Path) -> str:
    """Return the package ``__version__`` (the release tag without its ``v``)."""
    init = archives.package_dir(root) / "__init__.py"
    match = re.search(
        r'^__version__\s*=\s*["\']([^"\']+)["\']', init.read_text(), re.MULTILINE
    )
    if match is None:
        raise ManifestError(f"{init}: no __version__ assignment")
    return match.group(1)


def head_commit(root: Path) -> str:
    """Return the commit the archives are built from."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def vendor_record(
    vendor: str, config: dict[str, Any], data: bytes, commit: str, root: Path
) -> dict[str, Any]:
    """Return the manifest record for one vendor's freshly built archive."""
    entry = config["vendors"][vendor]
    version = entry.get("version")
    requires = {**config.get("requires", {}), **entry.get("requires", {})}
    conformance = entry.get("conformance", {"date": None, "result": "none"})
    return {
        "version": version,
        "commit": commit,
        "archive": f"{version}/{vendor}.tar.gz",
        "sha256": archives.sha256_hex(data),
        "requires": {"surface": requires.get("surface"), "ir": requires.get("ir")},
        "shims": [name for name, _ in archives.declared_shims(vendor, root)],
        "tags": list(entry.get("tags", [])),
        "maturity": entry.get("maturity"),
        "since": entry.get("since"),
        "conformance": {
            "date": conformance.get("date"),
            "result": conformance.get("result"),
        },
        "promoted_to": entry.get("promoted_to"),
    }


def merge_versions(
    vendor: str, record: dict[str, Any], previous: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], bool]:
    """Return the vendor's version list with *record* added, and whether it is new.

    Raises:
        ManifestError: When the version is already published with other bytes.
    """
    for old in previous:
        if old.get("version") == record["version"]:
            if old.get("sha256") != record["sha256"]:
                raise ManifestError(
                    f"{vendor} {record['version']} is already published with sha256 "
                    f"{old.get('sha256')}; the archive now hashes to {record['sha256']}. "
                    f"Bump vendors.{vendor}.version in vendors.toml."
                )
            return list(previous), False
    return [record, *previous], True


def build_manifest(
    root: Path,
    *,
    previous: dict[str, Any] | None = None,
    commit: str | None = None,
    generated_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Return the manifest and the archives it adds, keyed by their ``archive`` path."""
    config = load_vendor_config(root)
    commit = commit or head_commit(root)
    generated_at = generated_at or _dt.datetime.now(_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    old_vendors = (previous or {}).get("vendors", {})
    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "source": SOURCE,
        "repo": REPO,
        "generated_at": generated_at,
        "vendors": {},
    }
    added: dict[str, bytes] = {}
    for vendor in sorted(config["vendors"]):
        data = archives.build_archive(vendor, root)
        archives.verify_archive(data, vendor, root)
        record = vendor_record(vendor, config, data, commit, root)
        versions, is_new = merge_versions(
            vendor, record, old_vendors.get(vendor, {}).get("versions", [])
        )
        if is_new:
            added[record["archive"]] = data
        manifest["vendors"][vendor] = {"versions": versions}
    # A vendor dropped from the tree keeps its published history.
    for vendor, old in old_vendors.items():
        manifest["vendors"].setdefault(vendor, old)
    problems = validate_manifest(manifest)
    if problems:
        raise ManifestError(
            "the built manifest is invalid:\n  " + "\n  ".join(problems)
        )
    return manifest, added


# ---------------------------------------------------------------------------
# Validation (the spec, without a jsonschema dependency)
# ---------------------------------------------------------------------------


def _version_key(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", value))


def _check_record(vendor: str, index: int, rec: Any) -> list[str]:
    where = f"vendors.{vendor}.versions[{index}]"
    if not isinstance(rec, dict):
        return [f"{where}: must be an object"]
    problems: list[str] = []
    expected = {
        "version", "commit", "archive", "sha256", "requires", "shims",
        "tags", "maturity", "since", "conformance", "promoted_to",
    }  # fmt: skip
    missing = expected - set(rec)
    if missing:
        problems.append(f"{where}: missing {sorted(missing)}")
    extra = set(rec) - expected
    if extra:
        problems.append(f"{where}: unknown keys {sorted(extra)}")
    version = rec.get("version")
    if not isinstance(version, str) or not _VERSION.match(version):
        problems.append(f"{where}.version: {version!r} is not a version")
    if not isinstance(rec.get("commit"), str) or not _HEX40.match(rec["commit"]):
        problems.append(f"{where}.commit: must be 40 lowercase hex digits")
    if rec.get("archive") != f"{version}/{vendor}.tar.gz":
        problems.append(f"{where}.archive: must be {version}/{vendor}.tar.gz")
    if not isinstance(rec.get("sha256"), str) or not _HEX64.match(rec["sha256"]):
        problems.append(f"{where}.sha256: must be 64 lowercase hex digits")
    requires = rec.get("requires")
    if (
        not isinstance(requires, dict)
        or set(requires) != {"surface", "ir"}
        or not all(
            isinstance(v, int) and not isinstance(v, bool) and v >= 1
            for v in requires.values()
        )
    ):
        problems.append(f"{where}.requires: must be {{surface: int>=1, ir: int>=1}}")
    shims = rec.get("shims")
    if (
        not isinstance(shims, list)
        or not shims
        or not all(isinstance(s, str) and _SHIM_NAME.match(s) for s in shims)
        or len(set(shims)) != len(shims)
    ):
        problems.append(
            f"{where}.shims: must be a non-empty list of distinct shim names"
        )
    elif not all(s.startswith(vendor) for s in shims):
        problems.append(
            f"{where}.shims: every name must start with the vendor {vendor!r}"
        )
    tags = rec.get("tags")
    if not isinstance(tags, list) or not all(isinstance(t, str) and t for t in tags):
        problems.append(f"{where}.tags: must be a list of strings")
    if rec.get("maturity") not in MATURITIES:
        problems.append(f"{where}.maturity: must be one of {MATURITIES}")
    if not isinstance(rec.get("since"), str) or not _DATE.match(rec["since"]):
        problems.append(f"{where}.since: must be YYYY-MM-DD")
    conformance = rec.get("conformance")
    if not isinstance(conformance, dict) or set(conformance) != {"date", "result"}:
        problems.append(f"{where}.conformance: must be {{date, result}}")
    else:
        if conformance["result"] not in CONFORMANCE_RESULTS:
            problems.append(
                f"{where}.conformance.result: must be one of {CONFORMANCE_RESULTS}"
            )
        date = conformance["date"]
        if date is None:
            if conformance["result"] != "none":
                problems.append(
                    f"{where}.conformance.date: a {conformance['result']} needs a date"
                )
        elif not isinstance(date, str) or not _DATE.match(date):
            problems.append(f"{where}.conformance.date: must be YYYY-MM-DD or null")
    promoted = rec.get("promoted_to")
    if promoted is not None and (
        not isinstance(promoted, str) or not _PROMOTED.match(promoted)
    ):
        problems.append(f"{where}.promoted_to: must be null or core@X.Y.Z")
    return problems


def validate_manifest(doc: Any) -> list[str]:
    """Return every way *doc* departs from manifest v1; empty when it conforms."""
    if not isinstance(doc, dict):
        return ["the manifest must be a JSON object"]
    problems: list[str] = []
    expected = {"manifest_version", "source", "repo", "generated_at", "vendors"}
    if set(doc) != expected:
        problems.append(
            f"top level: keys must be {sorted(expected)}, got {sorted(doc)}"
        )
    if doc.get("manifest_version") != MANIFEST_VERSION:
        problems.append(f"manifest_version: must be {MANIFEST_VERSION}")
    if not isinstance(doc.get("source"), str) or not re.match(
        r"^[a-z0-9_-]+$", doc["source"]
    ):
        problems.append("source: must be a short lowercase id")
    if not isinstance(doc.get("repo"), str) or not doc["repo"].startswith("https://"):
        problems.append("repo: must be an https URL")
    if not isinstance(doc.get("generated_at"), str) or not _TIMESTAMP.match(
        doc["generated_at"]
    ):
        problems.append("generated_at: must be YYYY-MM-DDTHH:MM:SSZ")
    vendors = doc.get("vendors")
    if not isinstance(vendors, dict) or not vendors:
        return problems + ["vendors: must be a non-empty object"]
    for vendor, entry in vendors.items():
        if not re.match(r"^[a-z0-9_]+$", vendor):
            problems.append(f"vendors.{vendor}: not a vendor directory name")
        if not isinstance(entry, dict) or set(entry) != {"versions"}:
            problems.append(f"vendors.{vendor}: must be {{versions: [...]}}")
            continue
        versions = entry["versions"]
        if not isinstance(versions, list) or not versions:
            problems.append(f"vendors.{vendor}.versions: must be a non-empty list")
            continue
        for index, rec in enumerate(versions):
            problems.extend(_check_record(vendor, index, rec))
        numbers = [r.get("version") for r in versions if isinstance(r, dict)]
        if all(isinstance(n, str) and _VERSION.match(n) for n in numbers):
            if len(set(numbers)) != len(numbers):
                problems.append(f"vendors.{vendor}.versions: a version is listed twice")
            elif [_version_key(n) for n in numbers] != sorted(
                (_version_key(n) for n in numbers), reverse=True
            ):
                problems.append(f"vendors.{vendor}.versions: must be newest first")
    return problems


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def dumps(manifest: dict[str, Any]) -> str:
    """Return the manifest as written to disk (stable key order, 2-space indent)."""
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def write_outputs(
    out: Path, manifest: dict[str, Any], added: dict[str, bytes], release: str
) -> list[Path]:
    """Write the new archives, their digests and both manifest copies under *out*."""
    written: list[Path] = []
    for relative, data in added.items():
        path = out / relative
        if path.exists() and path.read_bytes() != data:
            raise ManifestError(
                f"{path} exists with other bytes; published files are immutable"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        digest = path.with_name(path.name + ".sha256")
        digest.write_text(archives.digest_file_line(data, path.name))
        written += [path, digest]
    text = dumps(manifest)
    snapshot = out / release / "manifest.json"
    if snapshot.exists() and snapshot.read_text() != text:
        raise ManifestError(
            f"{snapshot} exists; release {release} was already published"
        )
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(text)
    (out / "manifest.json").write_text(text)
    written += [snapshot, out / "manifest.json"]
    return written


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run", action="store_true", help="build and validate; write nothing"
    )
    mode.add_argument("--out", type=Path, help="dist-branch checkout to write into")
    mode.add_argument(
        "--check", type=Path, help="validate an existing manifest.json and exit"
    )
    parser.add_argument(
        "--previous", type=Path, help="the published manifest.json to extend"
    )
    parser.add_argument(
        "--expect-version",
        help="fail unless the package __version__ equals this (the tag)",
    )
    parser.add_argument(
        "--commit", help="commit to record (default: git rev-parse HEAD)"
    )
    parser.add_argument(
        "--root", type=Path, help="repository root (default: this script's)"
    )
    args = parser.parse_args(argv)

    if args.check is not None:
        problems = validate_manifest(json.loads(args.check.read_text(encoding="utf-8")))
        for problem in problems:
            print(f"invalid: {problem}", file=sys.stderr)
        print("check: ok" if not problems else f"check: {len(problems)} problem(s)")
        return 1 if problems else 0

    root = (args.root or archives.repo_root()).resolve()
    release = package_version(root)
    if args.expect_version is not None and args.expect_version != release:
        raise ManifestError(
            f"tag says {args.expect_version}, package says {release}; bump the package or the tag"
        )
    previous = None
    if args.previous is not None and args.previous.is_file():
        previous = json.loads(args.previous.read_text(encoding="utf-8"))
        problems = validate_manifest(previous)
        if problems:
            raise ManifestError(
                "the previous manifest is invalid:\n  " + "\n  ".join(problems)
            )
    manifest, added = build_manifest(root, previous=previous, commit=args.commit)

    for vendor, entry in manifest["vendors"].items():
        latest = entry["versions"][0]
        state = "new" if latest["archive"] in added else "unchanged"
        print(
            f"{vendor:8} {latest['version']:10} {state:9} sha256={latest['sha256']}",
            file=sys.stderr,
        )
    if args.dry_run:
        sys.stdout.write(dumps(manifest))
        print("dry-run: ok (nothing written)", file=sys.stderr)
        return 0
    for path in write_outputs(args.out, manifest, added, release):
        print(f"wrote: {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ManifestError, archives.ArchiveError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
