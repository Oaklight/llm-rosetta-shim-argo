"""Write ``scripts/argo-tunnel.py``: a runnable copy of the tunnel module.

The CLI has one shape for two audiences.  The package installs it as a console
script (``argo-tunnel``); an operator without a checkout, or on a
host where installing is a ticket, fetches one file and runs it.  Both must be
the same program, so the second is generated from the first rather than
maintained beside it.

The generated file is the module itself, as a copy, not a re-export: anything
that can only load by importing the package would defeat the purpose.  The one
change is the banner prepended, so a reader of the fetched file knows where it
came from and that editing it is pointless.

Usage::

    python scripts/sync_cli_script.py           # write the file
    python scripts/sync_cli_script.py --check   # exit 1 if it is stale
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "src" / "llm_rosetta_shim_argonne" / "tunnel.py"
TARGET = REPO_ROOT / "scripts" / "argo-tunnel.py"

_BANNER = """#!/usr/bin/env python3
# Generated from src/llm_rosetta_shim_argonne/tunnel.py -- do not edit here.
# Regenerate with: python scripts/sync_cli_script.py
#
# Standalone by design: standard library only, no llm-rosetta import, so this
# file runs from a download with no install step:
#
#   curl -fsSLO https://raw.githubusercontent.com/Oaklight/llm-rosetta-shim-argonne/main/scripts/argo-tunnel.py
#   python argo-tunnel.py auth pding --target lambda5.cels.anl.gov
"""


def render(source: str | None = None) -> str:
    """The generated script's full text.

    Args:
        source: The module text to embed; read from :data:`SOURCE` when
            omitted.  Taking it as an argument is what lets a test feed a
            deliberately broken module without writing to the real one.

    Returns:
        The banner, then the module source unchanged.  The source already ends
        with an ``if __name__ == "__main__"`` guard, so the file is runnable as
        written.

    Raises:
        SystemExit: The source imports something a standalone copy cannot
            have, which would make the fetched file fail on a machine that has
            no llm-rosetta.
    """
    if source is None:
        source = SOURCE.read_text()
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("from .", "import llm_rosetta", "from llm_rosetta")):
            raise SystemExit(
                f"{SOURCE.name}: {stripped!r} makes the standalone copy "
                "unloadable; keep the module stdlib-only"
            )
    return _BANNER + source


def main(argv: list[str] | None = None) -> int:
    """Write or check the generated script.

    Args:
        argv: Arguments without the program name.

    Returns:
        ``0`` on success, ``1`` when ``--check`` found a stale file.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero instead of writing when the file is stale",
    )
    args = parser.parse_args(argv)

    expected = render()
    current = TARGET.read_text() if TARGET.is_file() else None

    if args.check:
        if current == expected:
            print(f"{TARGET.relative_to(REPO_ROOT)} is up to date")
            return 0
        print(
            f"{TARGET.relative_to(REPO_ROOT)} is stale; "
            "run python scripts/sync_cli_script.py",
            file=sys.stderr,
        )
        return 1

    if current == expected:
        print(f"{TARGET.relative_to(REPO_ROOT)} already up to date")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(expected)
    TARGET.chmod(0o755)
    print(f"wrote {TARGET.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
