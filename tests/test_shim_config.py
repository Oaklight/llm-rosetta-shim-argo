"""Configuration carried by the packaged shims."""

from __future__ import annotations

import pytest

from llm_rosetta.shims import get_shim
from llm_rosetta.shims.provider_shim import _reset_registry

from llm_rosetta_shim_argo import register_shims


@pytest.fixture(autouse=True)
def _registered():
    _reset_registry()
    register_shims()
    yield
    _reset_registry()


def test_model_id_field_is_internal_id():
    shim = get_shim("argo--messages")
    assert shim is not None
    assert shim.connection.model_id_field == "internal_id"


def test_messages_preserves_unsigned_blocks():
    shim = get_shim("argo--messages")
    assert shim is not None
    assert shim.reasoning is not None
    assert shim.reasoning.unsigned_blocks == "preserve"
    assert shim.model_reasoning is not None
    assert shim.model_reasoning["claudeopus47"].unsigned_blocks == "preserve"


def test_messages_model_reasoning_overrides():
    shim = get_shim("argo--messages")
    assert shim is not None and shim.model_reasoning is not None
    override = shim.model_reasoning["claudeopus47"]
    assert override.thinking_modes == {
        "auto": "adaptive",
        "enabled": "adaptive",
        "disabled": "disabled",
    }
    assert override.effort_field == "output_config.effort"
    assert override.effort_range == ("low", "max")


def test_messages_provider_thinking_modes():
    shim = get_shim("argo--messages")
    assert shim is not None and shim.reasoning is not None
    assert shim.reasoning.thinking_modes is not None
    assert set(shim.reasoning.thinking_modes) == {"auto", "enabled", "disabled"}


def test_responses_profile_traits():
    shim = get_shim("argo--open_responses")
    assert shim is not None
    assert shim.response_id_prefix == "resp_"
    assert shim.store_default is None
    assert set(shim.preserve_fields) == {"content_filters", "tool_usage"}


def test_chat_shim_declares_tool_description_limit():
    shim = get_shim("argo--chat_completions")
    assert shim is not None
    assert shim.max_tool_description_length == 1024


def test_messages_shim_declares_soft_error_patterns():
    shim = get_shim("argo--messages")
    assert shim is not None
    assert len(shim.soft_error_patterns) > 0
    pattern = shim.soft_error_patterns[0]
    assert "AUTHENTICATION NOTICE FROM ARGO" in pattern.pattern
    assert pattern.status_code == 403
