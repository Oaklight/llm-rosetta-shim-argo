"""The standalone copy must be the same program, and must stay loadable.

Two failures are worth catching here, and neither is caught by running the
package's tests:

* the copy drifts from the module it was generated from;
* the module grows an import that a downloaded file cannot resolve, so the
  copy runs on this machine and fails on the operator's.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "argo-tunnel.py"
_SOURCE = _REPO / "src" / "llm_rosetta_shim_argonne" / "tunnel.py"


def test_the_copy_is_regenerated_when_the_module_changes():
    """A stale copy is worse than no copy: it ships an older CLI under the name
    of the current one."""
    done = subprocess.run(
        [sys.executable, str(_REPO / "scripts" / "sync_cli_script.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr or done.stdout


def test_the_copy_carries_no_package_import():
    """It is fetched onto a machine that has no llm-rosetta, so an import of
    the package — or of a sibling module — would fail there and only there."""
    text = _SCRIPT.read_text()
    for line in text.splitlines():
        stripped = line.strip()
        assert not stripped.startswith(
            ("from .", "from llm_rosetta", "import llm_rosetta")
        ), f"standalone copy imports the package: {stripped!r}"


def test_the_copy_names_where_it_came_from():
    first = _SCRIPT.read_text().splitlines()[1]
    assert "src/llm_rosetta_shim_argonne/tunnel.py" in first


@pytest.mark.parametrize("argv", [["proxy-url", "--port", "43210"]])
def test_the_copy_runs_without_site_packages(argv):
    """``-I`` isolates the interpreter: no user site, no environment, no
    current-directory import path."""
    done = subprocess.run(
        [sys.executable, "-I", str(_SCRIPT), *argv],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "socks5://127.0.0.1:43210"


def test_the_copy_reports_its_destination_the_same_way():
    """``status --json`` is what a launcher parses; the copy must answer the
    same shape, including which machine ``auth`` would reach."""
    done = subprocess.run(
        [sys.executable, "-I", str(_SCRIPT), "status", "--json", "--target", "lambda5"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
    )
    assert '"target": "lambda5"' in done.stdout
    # Nothing is authenticated in an empty HOME, so the exit code is 1 — the
    # answer, not a crash.
    assert done.returncode == 1


def test_a_package_import_fails_the_generator():
    """The guard is what keeps the copy loadable; prove it fires rather than
    assuming the check is reached."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "sync_cli_script", _REPO / "scripts" / "sync_cli_script.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    broken = _SOURCE.read_text().replace(
        "import argparse", "import argparse\nfrom . import __version__", 1
    )
    with pytest.raises(SystemExit) as caught:
        module.render(broken)
    assert "standalone" in str(caught.value)
