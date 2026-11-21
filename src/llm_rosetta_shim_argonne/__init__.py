"""Argonne provider shims for llm-rosetta: ALCF, Argo and AskSage.

One package carries the three Argonne vendors, each under
``providers/<vendor>/`` in the layout core's shim loader reads.  llm-rosetta
discovers the ``llm_rosetta.shim_providers`` entry points and calls one
function per vendor (:func:`register_alcf`, :func:`register_argo`,
:func:`register_asksage`).  Core runs each entry point in its own ``try``, so
a vendor that fails to load is logged and the other two still register.

The entry points run during ``import llm_rosetta``.  This module therefore
imports only :mod:`llm_rosetta.contracts`, and only inside the functions: the
``argo-tunnel`` console script lives in this package too, talks to ssh rather
than to core, and must work where llm-rosetta is not importable.
"""

from __future__ import annotations

from pathlib import Path

__version__ = "0.4.0"

#: The root of the shim tree: ``<vendor>[/<lane>]/<type>[/<format>]``.
PROVIDERS_DIR = Path(__file__).parent / "providers"

#: The vendor directories this package ships, one entry point each.
VENDORS = ("alcf", "argo", "asksage")


def register_vendor(vendor: str) -> list:
    """Load one vendor's shims from the bundled tree into llm-rosetta.

    The scan is restricted to ``providers/<vendor>``, so each entry point
    registers its own vendor and a broken file in another vendor's directory
    cannot stop it.

    Args:
        vendor: One of :data:`VENDORS`.

    Returns:
        The shims this call registered.

    Raises:
        ValueError: When *vendor* is not a vendor this package ships.
    """
    if vendor not in VENDORS:
        raise ValueError(f"unknown vendor {vendor!r}; expected one of {VENDORS}")
    from llm_rosetta.contracts.shim import load_providers_from_dir

    return load_providers_from_dir(PROVIDERS_DIR, only_paths=[vendor])


def register_alcf() -> list:
    """Entry point: register the ``alcf_<cluster>--chat_completions`` shims."""
    return register_vendor("alcf")


def register_argo() -> list:
    """Entry point: register the ``argo--*`` shims."""
    return register_vendor("argo")


def register_asksage() -> list:
    """Entry point: register the ``asksage--*`` shims."""
    return register_vendor("asksage")


def register_shims() -> list:
    """Register the shims of all three vendors (a convenience for tests and hosts).

    The installed package does not use this: it declares one entry point per
    vendor, so that one vendor's failure does not hide the others.

    Returns:
        The shims this call registered.
    """
    shims: list = []
    for vendor in VENDORS:
        shims.extend(register_vendor(vendor))
    return shims
