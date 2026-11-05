"""The plugin registers its argo--* shims through the public shim API."""

from __future__ import annotations

import pytest

from llm_rosetta.shims import get_shim
from llm_rosetta.shims.provider_shim import _reset_registry

from llm_rosetta_shim_argo import register_shims

_SHIMS = ("argo--messages", "argo--chat_completions", "argo--open_responses")


@pytest.fixture()
def _registered():
    _reset_registry()  # clean registry, so only the plugin's shims are present
    register_shims()
    yield


@pytest.mark.parametrize("name", _SHIMS)
def test_registers_argo_shim(_registered, name):
    shim = get_shim(name)
    assert shim is not None
    assert shim.connection.base_url.startswith("https://apps.inside.anl.gov/argoapi")
    assert shim.connection.api_key_env == "ARGO_API_KEY"


@pytest.mark.parametrize("name", _SHIMS)
def test_the_connection_defaults_come_from_the_vendor_manifest(_registered, name):
    """The manifest holds the credential once; each leaf only adds its URL."""
    shim = get_shim(name)
    assert shim is not None
    assert shim.manifest is not None
    assert shim.manifest.path.name == "provider.yaml"
    assert shim.connection.api_key_env == "ARGO_API_KEY"
    assert shim.connection.model_id_field == "internal_id"
