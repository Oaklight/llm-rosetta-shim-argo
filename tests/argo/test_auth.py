"""The Argo credential exchange: an account name behind an SSH tunnel.

The exchange is loaded by the shim loader from ``providers/argo/auth.py`` and
attached to every ``argo--*`` lane.  These tests hold what the admin panel and
the proxy depend on: which account the credential is, when a human has to act,
and that a dead tunnel is reported as needing a person rather than as an
upstream failure.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from llm_rosetta.api import (
    get_shim,
    restore_registry,
    snapshot_registry,
    staging_registry,
)
from llm_rosetta.contracts.auth import (
    AuthContext,
    AuthExchangeError,
    InteractiveSetupRequired,
)

from llm_rosetta_shim_argonne import register_argo as register_shims

_SHIMS = ("argo--messages", "argo--chat_completions", "argo--open_responses")


@pytest.fixture(autouse=True)
def _registered():
    saved = snapshot_registry()
    with staging_registry():
        register_shims()
    yield
    restore_registry(saved)


def _module():
    """The ``auth.py`` module as the loader executed it.

    The loader runs the file under a generated name rather than importing the
    package's module object, so a patch has to land on that module — patching
    ``llm_rosetta_shim_argonne.providers.argo.auth`` would change a second copy
    nothing reads.
    """
    shim = get_shim("argo--messages")
    assert shim is not None and shim.auth is not None
    return sys.modules[type(shim.auth).__module__]


def _exchange():
    shim = get_shim("argo--messages")
    assert shim is not None and shim.auth is not None
    return shim.auth


@pytest.fixture()
def tunnel_up(monkeypatch):
    """Every loopback connect succeeds."""
    monkeypatch.setattr(_module(), "_listening", lambda *a, **k: True)


@pytest.fixture()
def tunnel_down(monkeypatch):
    """Nothing answers on any loopback port."""
    monkeypatch.setattr(_module(), "_listening", lambda *a, **k: False)


def _ctx(**config):
    return AuthContext(provider="argo", config=config, input_key="pding")


def _credential(ctx):
    return asyncio.run(_exchange().credential(ctx))


# ---------------------------------------------------------------------------
# Loading and declaration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _SHIMS)
def test_every_lane_inherits_the_vendor_exchange(name):
    shim = get_shim(name)
    assert shim is not None
    assert shim.auth is not None
    assert type(shim.auth) is type(_exchange())
    assert shim.auth is _exchange()


def test_the_exchange_takes_the_operator_key_as_its_input():
    """The ANL account name *is* the credential, so the key field stays and the
    loader must not read it as a second credential beside the exchange."""
    assert _exchange().consumes_key is True
    assert _exchange().kind == "tunnel"
    for name in _SHIMS:
        shim = get_shim(name)
        assert shim is not None
        assert shim.connection.api_key_env == "ARGO_API_KEY"


def test_the_refresh_interval_comes_from_the_manifest():
    shim = get_shim("argo--messages")
    assert shim is not None
    assert shim.connection.token_refresh_interval == 300


def test_the_exchange_offers_no_record_flow():
    """There is no record to install or sign out of: a paste box that saves
    nothing would be worse than no box."""
    exchange = _exchange()
    assert exchange.can_install() is False
    assert exchange.can_login() is False
    assert exchange.can_begin_login() is False
    assert exchange.can_complete_login() is False
    assert exchange.can_revoke() is False


# ---------------------------------------------------------------------------
# The credential
# ---------------------------------------------------------------------------


def test_the_credential_is_the_account_name(tunnel_up):
    ctx = _ctx(proxy="socks5://127.0.0.1:10801")
    assert _credential(ctx) == "pding"


def test_a_direct_deployment_needs_no_tunnel(tunnel_down):
    """Inside the Argonne network there is no proxy and no tunnel, and the
    exchange must not invent a requirement the config did not state."""
    assert _credential(_ctx()) == "pding"
    assert _exchange().needs_interactive_setup(_ctx()) is False
    assert _exchange().describe(_ctx()) == {
        "subject": "pding",
        "tunnel": "direct",
    }


def test_a_closed_tunnel_needs_a_person_and_says_what_to_run(tunnel_down):
    ctx = _ctx(proxy="socks5://127.0.0.1:10801")
    assert _exchange().needs_interactive_setup(ctx) is True
    with pytest.raises(InteractiveSetupRequired) as caught:
        _credential(ctx)
    message = str(caught.value)
    assert "socks5://127.0.0.1:10801" in message
    assert "argo-tunnel auth" in message


def test_a_missing_account_name_is_a_configuration_error_not_a_tunnel_one(
    tunnel_down,
):
    """Mixing the two would send the operator to a command that does not fix
    the problem they have."""
    ctx = AuthContext(provider="argo", config={"proxy": "socks5://127.0.0.1:1"})
    with pytest.raises(AuthExchangeError) as caught:
        _credential(ctx)
    assert not isinstance(caught.value, InteractiveSetupRequired)
    assert "api_key" in str(caught.value)


def test_a_remote_proxy_is_not_probed(tunnel_down, monkeypatch):
    """A proxy on another host is not the tunnel this tool owns, and probing
    it would be a network round trip on the config-build path."""
    calls = []
    monkeypatch.setattr(
        _module(), "_listening", lambda *a, **k: calls.append(a) or False
    )
    assert _credential(_ctx(proxy="socks5://proxy.example:1080")) == "pding"
    assert calls == []


def test_a_non_socks_proxy_is_not_the_tunnel(tunnel_down):
    assert _credential(_ctx(proxy="http://127.0.0.1:3128")) == "pding"


# ---------------------------------------------------------------------------
# What the panel renders
# ---------------------------------------------------------------------------


def test_describe_is_absent_while_the_tunnel_is_down(tunnel_down):
    """A summary here means "connected"; a provider that cannot serve is not."""
    assert _exchange().describe(_ctx(proxy="socks5://127.0.0.1:10801")) is None


def test_describe_names_the_account_and_the_proxy(tunnel_up):
    summary = _exchange().describe(_ctx(proxy="socks5://127.0.0.1:10801"))
    assert summary == {"subject": "pding", "tunnel": "socks5://127.0.0.1:10801"}


def test_describe_has_no_expiry():
    """The account name does not expire; an ``expires_at`` would invent a
    deadline the operator would try to meet."""
    summary = _exchange().describe(_ctx())
    assert summary is not None
    assert "expires_at" not in summary


def test_account_identity_is_the_account_name():
    assert _exchange().account_id(_ctx()) == "pding"
    assert _exchange().account_id(AuthContext(provider="argo")) is None


# ---------------------------------------------------------------------------
# Proxy parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("socks5://127.0.0.1:10801", ("127.0.0.1", 10801)),
        ("socks5h://localhost:10801", ("localhost", 10801)),
        ("socks5://[::1]:10801", ("::1", 10801)),
        ("127.0.0.1:10801", ("127.0.0.1", 10801)),
        ("socks5://127.0.0.1", None),
        ("socks5://proxy.example:1080", None),
        ("http://127.0.0.1:3128", None),
        ("", None),
        (None, None),
    ],
)
def test_proxy_parsing(raw, expected):
    assert _module()._proxy_parts(raw) == expected
