"""Package-wide rules: the layout, the entry points and the import boundary.

The three vendors share one package, so the rules that keep one vendor from
breaking another live here rather than under a vendor's directory.
"""

from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

import llm_rosetta_shim_argonne as package

_REPO = Path(__file__).resolve().parents[1]
_SRC = _REPO / "src" / "llm_rosetta_shim_argonne"
_SCRIPTS = _REPO / "scripts"


def _load_archives():
    if "build_shim_archive" in sys.modules:
        return sys.modules["build_shim_archive"]
    spec = importlib.util.spec_from_file_location(
        "build_shim_archive", _SCRIPTS / "build_shim_archive.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_shim_archive"] = module
    spec.loader.exec_module(module)
    return module


archives = _load_archives()


def _all_shims() -> list[str]:
    return sorted(n for v in package.VENDORS for n, _ in archives.declared_shims(v))


def test_the_package_vendors_are_the_vendor_directories():
    assert list(package.VENDORS) == archives.vendors()


def test_one_entry_point_per_vendor():
    """One per vendor, so core's per-entry-point try isolates a failing vendor."""
    eps = importlib.metadata.entry_points(group="llm_rosetta.shim_providers")
    ours = {
        ep.name: ep.value
        for ep in eps
        if ep.value.startswith("llm_rosetta_shim_argonne")
    }
    assert ours == {
        v: f"llm_rosetta_shim_argonne:register_{v}" for v in package.VENDORS
    }


def test_the_argo_tunnel_console_script_is_declared():
    scripts = importlib.metadata.entry_points(group="console_scripts")
    (tunnel,) = [ep for ep in scripts if ep.name == "argo-tunnel"]
    assert tunnel.value == "llm_rosetta_shim_argonne.tunnel:main"


def test_the_package_shims_file_lists_every_leaf():
    """``providers/shims.yaml`` is what a gateway reads for the whole tree."""
    for vendor in package.VENDORS:
        archives.vendor_shims_file(vendor)  # raises on a mismatch
    _, entries = archives._read_shims_file(_SRC / "providers" / "shims.yaml")
    assert sorted(n for n, _ in entries) == _all_shims()


def test_an_unknown_vendor_is_refused():
    with pytest.raises(ValueError, match="unknown vendor"):
        package.register_vendor("nope")


def _llm_rosetta_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append(node.module)
        elif isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
    return [m for m in found if m == "llm_rosetta" or m.startswith("llm_rosetta.")]


@pytest.mark.parametrize(
    "path",
    sorted(_SRC.rglob("*.py")),
    ids=lambda p: p.relative_to(_SRC).as_posix(),
)
def test_source_imports_core_only_through_contracts(path: Path):
    """Sidecars and entry points run during ``import llm_rosetta``.

    Only :mod:`llm_rosetta.contracts` is importable at that moment, and it is
    the surface the manifest's ``requires.surface`` versions.
    """
    bad = [
        m
        for m in _llm_rosetta_imports(path)
        if not m.startswith("llm_rosetta.contracts")
    ]
    assert bad == []


def test_a_fresh_interpreter_registers_every_shim_of_the_three_vendors():
    """``import llm_rosetta`` alone runs the three entry points."""
    probe = (
        "import sys\n"
        "from llm_rosetta.api import get_shim\n"
        "print(' '.join(n for n in sys.argv[1:] if get_shim(n) is None))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe, *_all_shims()],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Failed to load plugin shim entry point" not in proc.stderr
    assert proc.stdout.split() == []


@pytest.mark.parametrize("vendor", ["alcf", "argo", "asksage"])
def test_a_built_archive_loads_with_the_core_loader(vendor: str, tmp_path: Path):
    """Extract the archive the release publishes and load it as a gateway would."""
    data = archives.build_archive(vendor)
    blob = tmp_path / f"{vendor}.tar.gz"
    blob.write_bytes(data)
    out = tmp_path / "x"
    with tarfile.open(blob) as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(out, filter="data")
        else:  # pragma: no cover - Python without PEP 706
            tar.extractall(out)
    probe = (
        "import sys\n"
        "from pathlib import Path\n"
        "from llm_rosetta.contracts.shim import load_providers_from_dir\n"
        "import llm_rosetta\n"
        "shims = load_providers_from_dir(Path(sys.argv[1]))\n"
        "print(' '.join(sorted(s.name for s in shims)))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe, str(out / "providers")],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.split() == sorted(n for n, _ in archives.declared_shims(vendor))
