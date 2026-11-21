"""The entry point registers the shims while ``llm_rosetta`` itself imports."""

from __future__ import annotations

import subprocess
import sys

_ENTRY_POINT = "argo"
_SHIMS = (
    "argo--chat_completions",
    "argo--messages",
    "argo--open_responses",
)

_PROBE = """
import sys
from llm_rosetta.api import get_shim
missing = [name for name in sys.argv[1:] if get_shim(name) is None]
print(" ".join(missing))
"""


def test_the_entry_point_loads_inside_the_core_import():
    """Core runs the plugin entry points during ``import llm_rosetta``.

    A shim module that imports a core name not yet bound at that moment fails
    there (``llm_rosetta.api`` is one such module).  The loader logs the
    error and goes on, and the in-process tests call ``register_shims()`` only
    after core has finished importing, so they stay green.  Only a fresh
    interpreter shows the failure, so this test starts one.
    """
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, *_SHIMS],
        capture_output=True,
        text=True,
        check=True,
    )

    assert f"Failed to load plugin shim entry point: {_ENTRY_POINT}" not in proc.stderr
    assert proc.stdout.split() == []
