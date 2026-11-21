"""Parity of the packaged shims with the built-ins they replace."""

from __future__ import annotations

import pytest

from llm_rosetta.api import (
    get_shim,
    restore_registry,
    snapshot_registry,
    staging_registry,
)

from llm_rosetta_shim_argonne import register_alcf as register_shims


def apply_transforms(transforms, body):
    """Run *transforms* over *body* in order, as the gateway applies a hook tuple."""
    for transform in transforms:
        body = transform(body)
    return body


SHIMS = (
    "alcf_metis--chat_completions",
    "alcf_minerva--chat_completions",
    "alcf_sophia--chat_completions",
)


@pytest.fixture(autouse=True)
def _registered():
    saved = snapshot_registry()
    with staging_registry():
        register_shims()
    yield
    restore_registry(saved)


@pytest.mark.parametrize("name", SHIMS)
def test_shim_uses_the_canonical_format_pair(name):
    shim = get_shim(name)
    assert shim is not None
    assert shim.model_type == "llm"
    assert shim.format == "chat_completions"


def test_sophia_clamps_the_effort_ladder():
    """vLLM answers 500 outside low/medium/high, so the range must reach it."""
    shim = get_shim("alcf_sophia--chat_completions")
    assert shim is not None
    assert shim.reasoning is not None
    assert shim.reasoning.effort_field == "reasoning_effort"
    assert shim.reasoning.effort_range == ("low", "high")


def test_sophia_renames_reasoning_on_message_and_delta():
    """vLLM exposes ``reasoning``; the converter reads ``reasoning_content``."""
    shim = get_shim("alcf_sophia--chat_completions")
    assert shim is not None
    body = {
        "choices": [
            {
                "message": {"reasoning": "non-streaming"},
                "delta": {"reasoning": "streaming"},
            }
        ]
    }
    out = apply_transforms(shim.pre_ir_transforms, body)
    choice = out["choices"][0]
    assert choice["message"]["reasoning_content"] == "non-streaming"
    assert choice["delta"]["reasoning_content"] == "streaming"
    assert "reasoning" not in choice["message"]
    assert "reasoning" not in choice["delta"]


@pytest.mark.parametrize("name", SHIMS)
def test_model_list_transform_is_exported(name):
    from llm_rosetta_shim_argonne.providers.alcf import model_utils

    assert callable(model_utils.make_alcf_model_list_transform)


def test_the_three_clusters_share_a_group():
    for name in SHIMS:
        shim = get_shim(name)
        assert shim is not None
        assert shim.group == "alcf"


@pytest.mark.parametrize("name", SHIMS)
def test_the_credential_comes_from_the_vendor_manifest(name):
    """One exchange, declared once, inherited by every cluster.

    The vendor ships ``auth.py`` beside its manifest, so the credential is
    minted in code for all three lanes.  A shim declares one form of its
    credential exchange, so neither an argv nor a static key may sit beside it.
    """
    shim = get_shim(name)
    assert shim is not None
    assert shim.manifest is not None
    assert shim.manifest.path.name == "provider.yaml"
    assert shim.connection.token_command is None
    assert shim.connection.api_key_env is None
    assert shim.auth is not None
    assert shim.auth is shim.manifest.auth
    assert shim.auth.kind == "oauth"
    assert shim.auth.consumes_key is False
