"""Request-side transforms of the argo--chat_completions shim."""

from __future__ import annotations

import pytest

from llm_rosetta.shims import get_shim
from llm_rosetta.shims.provider_shim import _reset_registry
from llm_rosetta.transforms import apply_transforms

from llm_rosetta_shim_argo import register_shims


@pytest.fixture(autouse=True)
def _registered():
    _reset_registry()
    register_shims()
    yield
    _reset_registry()


class TestArgoOpenaiChatTransforms:
    @pytest.fixture(autouse=True)
    def _load_builtins(self):
        from llm_rosetta.shims.providers import load_providers

        load_providers()

    def test_argo_downgrades_developer_role(self):
        shim = get_shim("argo--chat_completions")
        assert shim is not None
        body = {
            "model": "gpt-4",
            "messages": [{"role": "developer", "content": "system prompt"}],
        }
        result = apply_transforms(shim.post_ir_transforms, body)
        assert result["messages"][0]["role"] == "system"

    def test_argo_normalizes_null_content(self):
        shim = get_shim("argo--chat_completions")
        assert shim is not None
        body = {
            "model": "gpt-4",
            "messages": [
                {"role": "assistant", "content": None, "tool_calls": []},
            ],
        }
        result = apply_transforms(shim.post_ir_transforms, body)
        assert result["messages"][0]["content"] == ""

    def test_argo_strips_sampling_for_matching_models(self):
        """Models that reject sampling params get them stripped."""
        shim = get_shim("argo--chat_completions")
        assert shim is not None
        for model in (
            "claudeopus47",
            "claudesonnet5",
            "gpt5",
            "gpt5nano",
            "gpt55",
            "gpt56sol",
        ):
            body = {
                "model": model,
                "temperature": 0.7,
                "top_p": 0.9,
                "messages": [{"role": "user", "content": "hi"}],
            }
            result = apply_transforms(shim.post_ir_transforms, body)
            assert "temperature" not in result, f"{model}: temperature not stripped"
            assert "top_p" not in result, f"{model}: top_p not stripped"

    def test_argo_keeps_sampling_for_non_matching_models(self):
        """Models that accept sampling params keep them."""
        shim = get_shim("argo--chat_completions")
        assert shim is not None
        for model in ("gpt-4o", "gpt41nano", "gpt51", "gpt54", "claudehaiku45"):
            body = {
                "model": model,
                "temperature": 0.7,
                "messages": [{"role": "user", "content": "hi"}],
            }
            result = apply_transforms(shim.post_ir_transforms, body)
            assert result["temperature"] == 0.7, f"{model}: temperature stripped"

    def test_argo_renames_max_tokens_for_gemini(self):
        shim = get_shim("argo--chat_completions")
        assert shim is not None
        body = {
            "model": "gemini25flash",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hi"}],
        }
        result = apply_transforms(shim.post_ir_transforms, body)
        assert "max_tokens" not in result
        assert result["max_completion_tokens"] == 100

    def test_argo_keeps_max_tokens_for_non_gemini(self):
        """Non-Gemini models keep max_tokens unchanged — Claude via Argo
        OAI rejects max_completion_tokens alone."""
        shim = get_shim("argo--chat_completions")
        assert shim is not None
        for model in ("gpt-4o", "claudeopus47"):
            body = {
                "model": model,
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}],
            }
            result = apply_transforms(shim.post_ir_transforms, body)
            assert result["max_tokens"] == 100
            assert "max_completion_tokens" not in result
