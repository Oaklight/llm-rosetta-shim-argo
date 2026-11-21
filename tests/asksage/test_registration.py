"""The plugin registers its asksage--* shims through the public shim API."""

from __future__ import annotations

from pathlib import Path

import pytest

from llm_rosetta.api import get_shim, load_providers_from_dir, staging_registry

from llm_rosetta_shim_argonne import register_asksage as register_shims

#: The directory the package ships and the entry point loads.
_PROVIDERS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "llm_rosetta_shim_argonne"
    / "providers"
)

_SHIMS = {
    "asksage--chat_completions": "chat_completions",
    "asksage--open_responses": "open_responses",
    "asksage--messages": "messages",
    "asksage--google_generate": "google_generate",
    "asksage--embeddings": "openai",
}

#: Every leaf forwards to the vendor's host; only the lane root below it
#: distinguishes them.
_EXPECTED_HOST = "https://api.asksage.ai"


@pytest.fixture()
def _registered():
    with staging_registry():  # clean registry, so only the plugin's shims are present
        register_shims()
    yield


@pytest.mark.parametrize("name,base", sorted(_SHIMS.items()))
def test_registers_asksage_shim(_registered, name, base):
    shim = get_shim(name)
    assert shim is not None
    assert shim.format == base
    assert shim.connection.base_url.startswith("https://api.asksage.ai/")
    assert shim.connection.api_key_env == "ASKSAGE_API_KEY"


def test_google_generate_overrides_auth_header(_registered):
    shim = get_shim("asksage--google_generate")
    assert shim is not None
    assert shim.connection.auth_header == "x-access-tokens"


@pytest.mark.parametrize("name", sorted(_SHIMS))
def test_every_leaf_stays_on_the_asksage_host(_registered, name):
    """A leaf's base URL is the vendor host plus its own lane root."""
    shim = get_shim(name)
    assert shim is not None
    base_url = shim.connection.base_url
    assert base_url is not None
    assert base_url.startswith(f"{_EXPECTED_HOST}/server/")


def test_the_embeddings_leaf_is_a_typed_endpoint(_registered):
    """The lane is an embeddings one, served by the OpenAI wire dialect."""
    shim = get_shim("asksage--embeddings")
    assert shim is not None
    assert shim.model_type == "embeddings"
    assert shim.format == "openai"
    # `base_url` already carries `/openai/v1` and the gateway concatenates
    # `{base_url}{default_path}`, so the path is the bare tail; `/v1/embeddings`
    # here would append a second `/v1`.
    assert shim.connection.base_url == f"{_EXPECTED_HOST}/server/openai/v1"
    assert shim.connection.default_path == "/embeddings"


@pytest.mark.parametrize("name", sorted(_SHIMS))
def test_every_lane_ships_the_key_exchange(_registered, name):
    """The credential is exchanged in code, not by an argv.

    One form per shim: the loader refuses a vendor that declares both a
    ``token_command`` and an ``auth.py``, so the absence of the command is part
    of the contract rather than a cosmetic detail.
    """
    shim = get_shim(name)
    assert shim is not None
    assert shim.auth is not None, "the vendor manifest ships no auth.py"
    assert shim.auth.kind == "api_key"
    # The account key is the exchange's *input*, which is what lets the
    # operator keep it in the provider's api_key field.
    assert shim.auth.consumes_key is True
    assert shim.connection.token_command is None
    assert shim.connection.token_command_env is None


@pytest.mark.parametrize("name", sorted(_SHIMS))
def test_the_credential_comes_from_the_vendor_manifest(_registered, name):
    """The manifest holds the credential once; each leaf only adds its URL."""
    shim = get_shim(name)
    assert shim is not None
    assert shim.manifest is not None
    assert shim.manifest.path.name == "provider.yaml"
    assert shim.connection.api_key_env == "ASKSAGE_API_KEY"
    assert shim.auth is shim.manifest.auth


def test_the_embeddings_endpoint_shares_the_lane_credential(_registered):
    """A ``providers:`` entry serves the typed lane from one key ring.

    The mint is owned by the provider entry, and the typed endpoint reads
    through to it (#1401).  A typed endpoint that is inherited but unserved
    would send an empty Authorization header and fail as a 401 far from the
    cause, which is why this is pinned.
    """
    from llm_rosetta_gateway.config import GatewayConfig

    raw: dict = {
        "providers": {
            "asksage": {
                "shim": "asksage--chat_completions",
                "api_key": "x",
                "embeddings_shim": "asksage--embeddings",
            }
        },
        "models": {},
    }

    cfg = GatewayConfig(raw)

    assert "asksage" in cfg.embeddings_providers
    info = cfg.embeddings_provider_infos["asksage"]
    assert info.credential_owner is cfg.providers["asksage"]


def test_the_legacy_section_refuses_a_minting_entry(_registered):
    """``embeddings_providers`` has no lane to share the mint from.

    The refusal is the gateway's, and it names the fix; this pins that the
    exchange counts as a minting credential like a command did.
    """
    from llm_rosetta_gateway.config import GatewayConfig

    raw: dict = {
        "embeddings_providers": {
            "asksage": {"embeddings_shim": "asksage--embeddings", "api_key": "x"},
        }
    }

    cfg = GatewayConfig(raw)

    assert "asksage" not in cfg.embeddings_providers
    marker = raw["embeddings_providers"]["asksage"]["_auto_disabled_endpoints"]
    assert "mints its credential" in marker["embeddings"]["reason"]
    assert "`providers:`" in marker["embeddings"]["reason"]


def test_the_shipped_directory_registers_every_leaf():
    """Load the files, not the entry point: this is what ships and what loads.

    ``register_shims`` needs an installed package and reaches the directory
    through the entry point.  This walks ``providers/`` the way the gateway's
    loader does, so a leaf the loader skips — an unknown ``model_type``, a
    manifest read as a shim — fails here rather than glowing green on a
    registry that some other call filled.
    """
    with staging_registry():
        loaded = load_providers_from_dir(_PROVIDERS, only_paths=["asksage"])

    assert sorted(shim.name for shim in loaded) == sorted(_SHIMS)
    assert get_shim("asksage--embeddings") is not None
