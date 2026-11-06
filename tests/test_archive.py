"""The release archive carries exactly the ``providers/`` tree the package ships.

A mirror that extracts to the wrong shape is worse than no mirror: nothing
loads and the failure surfaces far from the build.  These tests hold the build
script to its layout check, to determinism and to the digest format.
"""

from __future__ import annotations

import importlib.util
import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_shim_archive.py"
_PACKAGE = "llm_rosetta_shim_argo"


def _load_script():
    spec = importlib.util.spec_from_file_location("build_shim_archive", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


archive = _load_script()
PROVIDERS = archive.providers_dir()


def _repack(data: bytes, *, add: str | None = None, drop: str | None = None) -> bytes:
    """Return *data* with one member added or removed."""
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as src:
        members = [(m, src.extractfile(m).read()) for m in src.getmembers() if m.isfile()]
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for member, blob in members:
            if member.name == drop:
                continue
            tar.addfile(member, io.BytesIO(blob))
        if add is not None:
            info = tarfile.TarInfo(add)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    return out.getvalue()


def test_the_shim_name_matches_the_package():
    assert archive.shim_name() == "argo"
    assert archive.package_dir().name == _PACKAGE


def test_the_archive_root_is_providers():
    data = archive.build_archive(PROVIDERS)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = [m.name for m in tar.getmembers()]
    assert names[0] == "providers"
    assert all(name == "providers" or name.startswith("providers/") for name in names)


def test_the_archive_carries_every_shipped_file_and_nothing_else():
    data = archive.build_archive(PROVIDERS)
    expected = {f"providers/{path}" for path in archive.shipped_files(PROVIDERS)}
    assert set(archive.archive_members(data)) == expected


def test_the_build_is_deterministic():
    assert archive.build_archive(PROVIDERS) == archive.build_archive(PROVIDERS)


def test_the_verified_archive_passes_its_own_check():
    data = archive.build_archive(PROVIDERS)
    archive.verify_archive(data, PROVIDERS)


def test_an_extra_member_fails_the_layout_check():
    data = _repack(archive.build_archive(PROVIDERS), add="providers/stray.yaml")
    with pytest.raises(archive.ArchiveError, match="extra"):
        archive.verify_archive(data, PROVIDERS)


def test_a_missing_member_fails_the_layout_check():
    data = archive.build_archive(PROVIDERS)
    first = archive.archive_members(data)[0]
    with pytest.raises(archive.ArchiveError, match="missing"):
        archive.verify_archive(_repack(data, drop=first), PROVIDERS)


def test_a_member_outside_the_root_fails_the_layout_check():
    data = _repack(archive.build_archive(PROVIDERS), add="elsewhere/provider.yaml")
    with pytest.raises(archive.ArchiveError, match="outside providers/"):
        archive.verify_archive(data, PROVIDERS)


def test_bytecode_caches_stay_out(tmp_path: Path):
    tree = tmp_path / "providers"
    (tree / "vendor").mkdir(parents=True)
    (tree / "vendor" / "provider.yaml").write_text("name: x\n")
    (tree / "vendor" / "__pycache__").mkdir()
    (tree / "vendor" / "__pycache__" / "transforms.cpython-312.pyc").write_bytes(b"x")
    assert archive.shipped_files(tree) == ["vendor/provider.yaml"]


@pytest.mark.skipif(shutil.which("sha256sum") is None, reason="no sha256sum")
def test_the_digest_file_verifies_with_sha256sum(tmp_path: Path):
    data = archive.build_archive(PROVIDERS)
    name = f"{archive.shim_name()}.tar.gz"
    (tmp_path / name).write_bytes(data)
    (tmp_path / f"{name}.sha256").write_text(archive.digest_file_line(data, name))

    result = subprocess.run(
        ["sha256sum", "-c", f"{name}.sha256"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
