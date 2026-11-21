#!/usr/bin/env python3
"""Build the per-vendor shim archives this repository publishes at a release tag.

The package ships three vendors under ``src/llm_rosetta_shim_argonne/providers``.
A release publishes one archive per vendor, so a gateway installs and upgrades
each vendor on its own (``scripts/build_manifest.py`` lists them)::

    dist/<version>/<vendor>.tar.gz          built here
    dist/<version>/<vendor>.tar.gz.sha256   the digest, beside it

An archive's root is ``providers/``.  Under it sit the vendor's directory,
byte for byte as the package ships it, and a ``shims.yaml`` cut from the
package's own to that vendor's entries::

    providers/shims.yaml
    providers/<vendor>/provider.yaml
    providers/<vendor>/auth.py
    providers/<vendor>/<lane>/<type>/provider.yaml
    ...

A gateway extracts it and loads ``providers/`` with core's
``load_providers_from_dir``, which is what the package's entry points call on
the same tree.

The layout check is the point of this script: an archive that extracts to a
different shape is worse than no archive, because it loads nothing and the
failure surfaces far from the build.  The script imports nothing outside the
standard library, so CI runs it before anything is installed.

Usage::

    python scripts/build_shim_archive.py                 # build and verify every vendor
    python scripts/build_shim_archive.py --vendor argo   # one vendor
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import re
import sys
import tarfile
from pathlib import Path

#: Everything the package does not ship: byte-code caches, editor droppings.
#: A rebuild must not depend on whether the tree was imported before.
_IGNORED_DIR_NAMES = {"__pycache__", ".git", ".pytest_cache", ".ruff_cache"}
_IGNORED_SUFFIXES = {".pyc", ".pyo"}
_IGNORED_FILE_NAMES = {".DS_Store"}

#: The directory every archive carries at its root.
ARCHIVE_ROOT = "providers"

#: The package that ships the providers tree.
PACKAGE = "llm_rosetta_shim_argonne"

#: The selection file a gateway reads from the scan root.
SHIMS_FILE = "shims.yaml"

#: Fixed metadata, so two builds of the same tree are byte-identical and the
#: published digest means "these files", not "these files and this clock".
_FIXED_MTIME = 0
_FIXED_MODE = 0o644
_FIXED_DIR_MODE = 0o755


class ArchiveError(RuntimeError):
    """The archive does not carry the layout the repository ships."""


def repo_root() -> Path:
    """Return the repository root this script lives in."""
    return Path(__file__).resolve().parent.parent


def package_dir(root: Path | None = None) -> Path:
    """Return the ``src/llm_rosetta_shim_argonne`` package directory."""
    path = (root or repo_root()) / "src" / PACKAGE
    if not path.is_dir():
        raise ArchiveError(f"{path}: the package directory is missing")
    return path


def providers_dir(root: Path | None = None) -> Path:
    """Return the shim tree the archives are cut from."""
    return package_dir(root) / "providers"


def vendors(root: Path | None = None) -> list[str]:
    """Return the vendor directories under ``providers/``, sorted.

    A directory whose name starts with ``_`` or ``.`` is not a vendor: core's
    loader skips it too.
    """
    return sorted(
        d.name
        for d in providers_dir(root).iterdir()
        if d.is_dir()
        and not d.name.startswith(("_", "."))
        and d.name not in _IGNORED_DIR_NAMES
    )


def vendor_dir(vendor: str, root: Path | None = None) -> Path:
    """Return ``providers/<vendor>``, refusing a name that is not a vendor."""
    if vendor not in vendors(root):
        raise ArchiveError(
            f"unknown vendor {vendor!r}; the package ships {vendors(root)}"
        )
    return providers_dir(root) / vendor


def shipped_files(directory: Path) -> list[str]:
    """Return the files under *directory*, relative and POSIX, sorted.

    Excludes what the package does not ship, so a stale ``__pycache__`` cannot
    change the archive from one machine to the next.
    """
    files: list[str] = []
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if any(part in _IGNORED_DIR_NAMES for part in relative.parts[:-1]):
            continue
        if path.is_dir():
            continue
        if path.suffix in _IGNORED_SUFFIXES or path.name in _IGNORED_FILE_NAMES:
            continue
        files.append(relative.as_posix())
    return files


_NAME_LINE = re.compile(r"^name:\s*['\"]?([^'\"\s#]+)", re.MULTILINE)


def declared_shims(vendor: str, root: Path | None = None) -> list[tuple[str, str]]:
    """Return ``(name, path)`` for every shim leaf under ``providers/<vendor>``.

    A leaf is a ``provider.yaml`` with a top-level ``name:``; the vendor
    manifest at the vendor root declares none.  ``path`` is relative to
    ``providers/``.  The list is what the manifest's ``shims`` field and the
    archive's ``shims.yaml`` are derived from, so a leaf added without an entry
    cannot ship unlisted.
    """
    base = providers_dir(root)
    found: list[tuple[str, str]] = []
    for path in sorted(vendor_dir(vendor, root).rglob("provider.yaml")):
        match = _NAME_LINE.search(path.read_text(encoding="utf-8"))
        if match is None:
            continue
        found.append((match.group(1), path.parent.relative_to(base).as_posix()))
    return found


def _read_shims_file(path: Path) -> tuple[str | None, list[tuple[str, str]]]:
    """Return ``(core, [(name, path), ...])`` from the package's ``shims.yaml``.

    The file has a fixed shape (``version``, ``core``, a ``shims`` list of
    ``name``/``path`` pairs), so it is read without a YAML library.
    """
    core: str | None = None
    entries: list[tuple[str, str]] = []
    pending: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line:
            continue
        if line.startswith("core:"):
            core = line.split(":", 1)[1].strip().strip("'\"")
            continue
        stripped = line.strip().removeprefix("- ").strip()
        if stripped.startswith("name:"):
            pending = stripped.split(":", 1)[1].strip().strip("'\"")
        elif stripped.startswith("path:"):
            if pending is None:
                raise ArchiveError(f"{path}: a 'path' entry without a 'name'")
            entries.append((pending, stripped.split(":", 1)[1].strip().strip("'\"")))
            pending = None
    if pending is not None:
        raise ArchiveError(f"{path}: entry {pending!r} has no 'path'")
    return core, entries


def vendor_shims_file(vendor: str, root: Path | None = None) -> bytes:
    """Return the ``shims.yaml`` an archive of *vendor* carries.

    It is the package's ``shims.yaml`` cut to the entries under the vendor's
    directory, after a check that those entries are exactly the leaves the
    vendor ships (:func:`declared_shims`).

    Raises:
        ArchiveError: When the package's list and the leaves disagree.
    """
    core, entries = _read_shims_file(providers_dir(root) / SHIMS_FILE)
    listed = [(n, p) for n, p in entries if p == vendor or p.startswith(f"{vendor}/")]
    declared = declared_shims(vendor, root)
    if sorted(listed) != sorted(declared):
        raise ArchiveError(
            f"providers/{SHIMS_FILE} does not match the {vendor} leaves:\n"
            f"  listed:   {sorted(listed)}\n"
            f"  declared: {sorted(declared)}"
        )
    lines = ["version: 1"]
    if core:
        lines.append(f'core: "{core}"')
    lines.append("shims:")
    for name, path in listed:
        lines.append(f"  - name: {name}")
        lines.append(f"    path: {path}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def archive_contents(vendor: str, root: Path | None = None) -> dict[str, bytes]:
    """Return the archive's files, relative to ``providers/``, in archive order."""
    directory = vendor_dir(vendor, root)
    files = shipped_files(directory)
    if not any(name.endswith("provider.yaml") for name in files):
        raise ArchiveError(f"{directory}: no provider.yaml to archive")
    contents = {SHIMS_FILE: vendor_shims_file(vendor, root)}
    for relative in files:
        contents[f"{vendor}/{relative}"] = (directory / relative).read_bytes()
    return contents


def _dir_info(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = _FIXED_DIR_MODE
    info.mtime = _FIXED_MTIME
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def build_archive(vendor: str, root: Path | None = None) -> bytes:
    """Return a deterministic ``.tar.gz`` of one vendor, rooted at ``providers/``."""
    contents = archive_contents(vendor, root)
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
            tar.addfile(_dir_info(ARCHIVE_ROOT))
            for relative, data in contents.items():
                info = tarfile.TarInfo(f"{ARCHIVE_ROOT}/{relative}")
                info.size = len(data)
                info.mode = _FIXED_MODE
                info.mtime = _FIXED_MTIME
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def archive_members(data: bytes) -> list[str]:
    """Return the regular-file members of *data*, in archive order."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return [m.name for m in tar.getmembers() if m.isfile()]


def verify_archive(data: bytes, vendor: str, root: Path | None = None) -> None:
    """Check that *data* extracts to exactly the files one vendor ships.

    Raises:
        ArchiveError: On a root other than ``providers/``, an absolute or
            escaping member, a duplicate, a missing file, an extra file, or a
            member whose bytes differ from the tree's.
    """
    expected = archive_contents(vendor, root)

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        got: dict[str, bytes] = {}
        names: list[str] = []
        for member in tar.getmembers():
            if not member.isfile():
                continue
            names.append(member.name)
            extracted = tar.extractfile(member)
            got[member.name] = extracted.read() if extracted is not None else b""
    if len(set(names)) != len(names):
        raise ArchiveError("archive carries duplicate members")

    prefix = f"{ARCHIVE_ROOT}/"
    for name in names:
        if not name.startswith(prefix):
            raise ArchiveError(
                f"member {name!r} is outside {ARCHIVE_ROOT}/; the archive root "
                f"must be {ARCHIVE_ROOT}/ so a source can declare "
                f'path: "{ARCHIVE_ROOT}"'
            )
        parts = name.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ArchiveError(f"member {name!r} has an unsafe path")

    relative = {name[len(prefix) :]: blob for name, blob in got.items()}
    if sorted(relative) != sorted(expected):
        missing = sorted(set(expected) - set(relative))
        extra = sorted(set(relative) - set(expected))
        raise ArchiveError(
            f"archive does not match providers/{vendor}:\n"
            f"  missing: {missing or 'none'}\n"
            f"  extra: {extra or 'none'}"
        )
    changed = sorted(name for name in expected if expected[name] != relative[name])
    if changed:
        raise ArchiveError(f"archive members differ from the tree: {changed}")


def sha256_hex(data: bytes) -> str:
    """Return the hex SHA-256 of *data*."""
    return hashlib.sha256(data).hexdigest()


def digest_file_line(data: bytes, name: str) -> str:
    """Return a ``sha256sum -c`` compatible line."""
    return f"{sha256_hex(data)}  {name}\n"


def main(argv: list[str] | None = None) -> int:
    """Build and verify the per-vendor archives (nothing is written)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--vendor",
        action="append",
        help="vendor to build (repeatable; default: every vendor)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        help="repository root (default: the one this script lives in)",
    )
    args = parser.parse_args(argv)

    root = (args.root or repo_root()).resolve()
    for vendor in args.vendor or vendors(root):
        data = build_archive(vendor, root)
        verify_archive(data, vendor, root)
        print(
            f"{vendor}.tar.gz  files={len(archive_members(data))}  sha256={sha256_hex(data)}"
        )
    print("check: ok (nothing written; scripts/build_manifest.py writes dist/)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ArchiveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
