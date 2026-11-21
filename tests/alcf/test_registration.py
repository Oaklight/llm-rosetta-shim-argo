"""The plugin registers its alcf_<cluster>--chat_completions shims via the public shim API."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from llm_rosetta.api import get_shim, staging_registry

from llm_rosetta_shim_argonne import register_alcf as register_shims

_CLUSTERS = ("metis", "minerva", "sophia")
_SHIMS = tuple(f"alcf_{cluster}--chat_completions" for cluster in _CLUSTERS)

# The three clusters share one admin top level; `display_name` is this shim's own
# row label inside it (rendered as "<display_name> · chat_completions").
_GROUP = "alcf"
_DISPLAY_NAMES = {"metis", "minerva", "sophia"}

_PROVIDERS_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "llm_rosetta_shim_argonne"
    / "providers"
    / "alcf"
)


def _provider_config(cluster: str) -> dict:
    """The raw ``provider.yaml`` of one cluster, parsed as data."""
    path = _PROVIDERS_DIR / cluster / "llm" / "provider.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture()
def _registered():
    with staging_registry():  # clean registry, so only the plugin's shims are present
        register_shims()
    yield


@pytest.mark.parametrize("name", _SHIMS)
def test_registers_alcf_shim(_registered, name):
    shim = get_shim(name)
    assert shim is not None
    assert shim.format == "chat_completions"
    assert shim.connection.base_url.startswith(
        "https://inference-api.alcf.anl.gov/resource_server/"
    )
    # The Globus grant is the credential; there is no key to fill in, and no
    # static fallback that would sit beside the exchange.
    assert shim.connection.api_key_env is None
    assert shim.connection.token_command is None


@pytest.mark.parametrize("cluster", _CLUSTERS)
def test_a_leaf_declares_only_its_own_facts(cluster):
    """The vendor's identity lives in the manifest; a leaf keeps its row label."""
    cfg = _provider_config(cluster)
    assert cfg["name"] == f"alcf_{cluster}--chat_completions"
    assert cfg["model_type"] == "llm"
    assert cfg["format"] == "chat_completions"
    assert cfg["display_name"] == cluster
    assert {"group", "group_label", "logo"}.isdisjoint(cfg)


def test_the_manifest_gives_the_three_clusters_one_group(_registered):
    assert {get_shim(name).group for name in _SHIMS} == {_GROUP}
    assert {get_shim(name).group_label for name in _SHIMS} == {"ALCF"}
    assert {get_shim(name).display_name for name in _SHIMS} == _DISPLAY_NAMES


@pytest.mark.parametrize("name", _SHIMS)
def test_the_credential_exchange_ships_with_the_shim(_registered, name):
    """Every cluster inherits the one Globus exchange beside the manifest.

    ``auth.py`` is the whole credential: the loader executes it, so a syntax
    error or a missing ``EXCHANGE`` fails here rather than at the first
    request.
    """
    shim = get_shim(name)
    assert shim is not None
    assert shim.auth is not None, "the vendor manifest ships no auth.py"
    assert shim.auth.kind == "oauth"
    assert shim.auth.consumes_key is False
    assert shim.connection.token_command is None
    assert shim.connection.token_command_env is None


@pytest.mark.parametrize("name", _SHIMS)
def test_a_provider_without_a_grant_asks_for_a_sign_in(
    _registered, name, tmp_path, monkeypatch
):
    """No record means a sign-in, reported without raising.

    ``needs_interactive_setup`` runs while the gateway builds a config, outside
    any event loop, so it must read local state and answer rather than fail.
    The vendor's own file is redirected so a developer's real Globus grant is
    neither read nor counted as this test's answer.
    """
    import sys

    from llm_rosetta.contracts.auth import AuthContext

    shim = get_shim(name)
    assert shim is not None
    assert shim.auth is not None
    module = sys.modules[type(shim.auth).__module__]
    monkeypatch.setattr(
        module, "VENDOR_TOKEN_PATH", tmp_path / "vendor" / "tokens.json"
    )
    ctx = AuthContext(provider="alcf", state_dir=str(tmp_path / "state"))

    assert shim.auth.needs_interactive_setup(ctx) is True
    assert shim.auth.account_id(ctx) is None
    assert shim.auth.describe(ctx) is None
