"""Parity of the packaged shims with the built-ins they replace."""

from __future__ import annotations

import pytest

from llm_rosetta.api import (
    get_shim,
    restore_registry,
    snapshot_registry,
    staging_registry,
)

from llm_rosetta_shim_argonne import register_asksage as register_shims

SHIMS = (
    "asksage--chat_completions",
    "asksage--messages",
    "asksage--open_responses",
    "asksage--google_generate",
)


@pytest.fixture(autouse=True)
def _registered():
    saved = snapshot_registry()
    with staging_registry():
        register_shims()
    yield
    restore_registry(saved)


@pytest.mark.parametrize(
    ("name", "fmt"),
    [
        ("asksage--chat_completions", "chat_completions"),
        ("asksage--messages", "messages"),
        ("asksage--open_responses", "open_responses"),
        ("asksage--google_generate", "google_generate"),
    ],
)
def test_shim_uses_the_canonical_format_pair(name, fmt):
    shim = get_shim(name)
    assert shim is not None
    assert shim.model_type == "llm"
    assert shim.format == fmt


def test_messages_keeps_the_anthropic_reasoning_cap():
    """A partial ``reasoning:`` block would silently flatten ``effort_field``."""
    shim = get_shim("asksage--messages")
    assert shim is not None
    assert shim.reasoning is not None
    assert shim.reasoning.thinking_modes == {
        "auto": "adaptive",
        "enabled": "enabled",
        "disabled": "disabled",
    }
    assert shim.reasoning.effort_field == "output_config.effort"
    assert shim.reasoning.effort_range == ("low", "max")
    assert shim.reasoning.unsigned_blocks == "preserve"


def test_open_responses_keeps_the_resp_prefix_and_echo_fields():
    shim = get_shim("asksage--open_responses")
    assert shim is not None
    assert shim.response_id_prefix == "resp_"
    assert set(shim.preserve_fields) == {"billing", "content_filters", "tool_usage"}
    assert shim.store_default is None


def test_google_lane_uses_the_nonstandard_auth_header():
    shim = get_shim("asksage--google_generate")
    assert shim is not None
    assert shim.connection.auth_header == "x-access-tokens"
