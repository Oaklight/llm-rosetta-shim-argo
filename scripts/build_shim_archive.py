#!/usr/bin/env python3
"""Build the shim archive this repository publishes at a release tag.

The gateway can load a shim package from a GitHub repository tarball, which
means no shim loads where GitHub is unreachable.  A committed archive on an
otherwise-empty branch is the one shape a file CDN can serve, so every release
publishes one:

    dist/v<version>/<shim>.tar.gz          built here
    dist/v<version>/<shim>.tar.gz.sha256   the digest, beside it

The archive's root is ``providers/``, carrying the same files the package
ships under ``src/<package>/providers``.  A gateway source then declares
``path: "providers"`` and extracts the directory it expects.

The layout check is the point of this script: an archive that extracts to a
different shape is worse than no mirror, because it loads nothing and the
failure surfaces far from the build.  ``--check`` therefore builds, verifies
and exits without writing; the release workflow runs ``--write`` and copies
the result onto the ``dist`` branch.

Usage::

    python scripts/build_shim_archive.py            # build and verify only
    python scripts/build_shim_archive.py --write    # also write dist/v<version>/
    python scripts/build_shim_archive.py --expect-version 0.2.0
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
    """Return the single ``src/llm_rosetta_shim_*`` package directory."""
    src = (root or repo_root()) / "src"
    candidates = sorted(
        p
        for p in src.iterdir()
        if p.is_dir()
        and p.name.startswith("llm_rosetta_shim_")
        # A stale editable-install metadata directory shares the prefix but is
        # not the package.
        and not p.name.endswith(".egg-info")
    )
    if len(candidates) != 1:
        names = ", ".join(p.name for p in candidates) or "none"
        raise ArchiveError(
            f"expected exactly one llm_rosetta_shim_* package under {src}, "
            f"found {names}"
        )
    return candidates[0]


def shim_name(root: Path | None = None) -> str:
    """Return the archive's shim name, e.g. ``asksage``.

    It is the package's suffix, which is also the preset key the gateway
    catalog uses, so the published file name and the mirror name agree.
    """
    return package_dir(root).name.removeprefix("llm_rosetta_shim_")


def package_version(root: Path | None = None) -> str:
    """Return ``__version__`` from the package ``__init__.py``."""
    init = package_dir(root) / "__init__.py"
    match = re.search(
        r'^__version__\s*=\s*["\']([^"\']+)["\']', init.read_text(), re.MULTILINE
    )
    if match is None:
        raise ArchiveError(f"{init}: no __version__ assignment")
    return match.group(1)


def providers_dir(root: Path | None = None) -> Path:
    """Return the package directory the archive is built from."""
    return package_dir(root) / "providers"


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


def build_archive(directory: Path) -> bytes:
    """Return a deterministic ``.tar.gz`` of *directory* rooted at ``providers/``."""
    files = shipped_files(directory)
    if not files:
        raise ArchiveError(f"{directory}: no files to archive")

    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar:
            root = tarfile.TarInfo(ARCHIVE_ROOT)
            root.type = tarfile.DIRTYPE
            root.mode = _FIXED_DIR_MODE
            root.mtime = _FIXED_MTIME
            root.uid = root.gid = 0
            root.uname = root.gname = ""
            tar.addfile(root)

            for relative in files:
                data = (directory / relative).read_bytes()
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


def verify_archive(data: bytes, directory: Path) -> None:
    """Check that *data* extracts to exactly the files *directory* ships.

    Raises:
        ArchiveError: On a root other than ``providers/``, an absolute or
            escaping member, a duplicate, a missing file, an extra file, or a
            tree with no ``provider.yaml``.
    """
    expected = shipped_files(directory)
    if not any(name.endswith("provider.yaml") for name in expected):
        raise ArchiveError(f"{directory}: no provider.yaml to archive")

    members = archive_members(data)
    if len(set(members)) != len(members):
        raise ArchiveError("archive carries duplicate members")

    prefix = f"{ARCHIVE_ROOT}/"
    for name in members:
        if not name.startswith(prefix):
            raise ArchiveError(
                f"member {name!r} is outside {ARCHIVE_ROOT}/; the archive root "
                f"must be {ARCHIVE_ROOT}/ so a source can declare "
                f'path: "{ARCHIVE_ROOT}"'
            )
        parts = name.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ArchiveError(f"member {name!r} has an unsafe path")

    got = sorted(name[len(prefix) :] for name in members)
    if got != expected:
        missing = sorted(set(expected) - set(got))
        extra = sorted(set(got) - set(expected))
        raise ArchiveError(
            f"archive does not match {directory}:\n"
            f"  missing: {missing or 'none'}\n"
            f"  extra: {extra or 'none'}"
        )


def digest_file_line(data: bytes, name: str) -> str:
    """Return a ``sha256sum -c`` compatible line."""
    return f"{hashlib.sha256(data).hexdigest()}  {name}\n"


def main(argv: list[str] | None = None) -> int:
    """Build, verify, and optionally write the release archive."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write",
        action="store_true",
        help="write dist/v<version>/<shim>.tar.gz and its .sha256",
    )
    parser.add_argument(
        "--expect-version",
        help="fail unless the package __version__ equals this value (the tag)",
    )
    parser.add_argument(
        "--root",
        type=Path,
        help="repository root (default: the one this script lives in)",
    )
    args = parser.parse_args(argv)

    root = (args.root or repo_root()).resolve()
    directory = providers_dir(root)
    name = shim_name(root)
    version = package_version(root)
    if args.expect_version is not None and args.expect_version != version:
        raise ArchiveError(
            f"tag says {args.expect_version}, package says {version}; "
            f"bump the package or the tag, not the archive"
        )

    data = build_archive(directory)
    verify_archive(data, directory)
    digest = hashlib.sha256(data).hexdigest()
    archive_name = f"{name}.tar.gz"

    print(f"archive: {archive_name}")
    print(f"files:   {len(archive_members(data))}")
    print(f"sha256:  {digest}")

    if not args.write:
        print("check:   ok (nothing written)")
        return 0

    out_dir = root / "dist" / f"v{version}"
    out_dir.mkdir(parents=True, exist_ok=True)
    archive_path = out_dir / archive_name
    digest_path = out_dir / f"{archive_name}.sha256"
    archive_path.write_bytes(data)
    digest_path.write_text(digest_file_line(data, archive_name))

    print(f"wrote:   {archive_path.relative_to(root)}")
    print(f"wrote:   {digest_path.relative_to(root)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ArchiveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
