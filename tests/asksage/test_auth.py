"""Tests for the AskSage key exchange (``providers/asksage/auth.py``)."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from llm_rosetta.contracts.auth import AuthContext, AuthExchangeError


class _Response:
    """The part of an ``http.client.HTTPResponse`` the exchange reads."""

    def __init__(self, body: io.BytesIO) -> None:
        self._body = body

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


AUTH_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "llm_rosetta_shim_argonne"
    / "providers"
    / "asksage"
    / "auth.py"
)


@pytest.fixture
def asksage(monkeypatch):
    spec = importlib.util.spec_from_file_location("asksage_auth_test", AUTH_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    monkeypatch.delenv(module.EMAIL_ENV, raising=False)
    return module


def _ctx(tmp_path: Path, **config: Any) -> AuthContext:
    settings: dict[str, Any] = {
        "base_url": "https://api.asksage.anl.gov/server/openai/v1",
        "email": "user@example.gov",
    }
    settings.update(config)
    return AuthContext(
        provider="asksage",
        config=settings,
        state_dir=str(tmp_path / "state"),
        input_key="key-1",
    )


def _jwt(exp: float | None) -> str:
    """Return an unsigned JWT-shaped string carrying *exp*."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=")
    claims = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=")
    return f"{header.decode()}.{claims.decode()}.sig"


@pytest.fixture
def minted(asksage, monkeypatch):
    """Patch the HTTP exchange and record every call."""
    calls: list[tuple[str, str, str]] = []

    def _exchange(host, email, api_key):
        calls.append((host, email, api_key))
        return _jwt(1e12)

    monkeypatch.setattr(asksage, "_exchange", _exchange)
    return calls


def test_the_host_comes_from_the_provider_base_url(asksage):
    ctx = _ctx(Path("/tmp/ignored"), base_url="https://api.asksage.ai/server/messages")

    assert asksage._host(ctx) == "https://api.asksage.ai"


def test_a_provider_without_a_base_url_is_told_what_to_set(asksage, tmp_path):
    ctx = AuthContext(provider="asksage", config={}, state_dir=str(tmp_path))

    with pytest.raises(AuthExchangeError, match="base_url"):
        asksage._host(ctx)


def test_the_first_mint_calls_the_exchange(asksage, tmp_path, minted):
    ctx = _ctx(tmp_path)

    token = asksage._mint(ctx)

    assert token.startswith("ey")
    assert minted == [("https://api.asksage.anl.gov", "user@example.gov", "key-1")]


def test_a_cached_token_costs_no_second_call(asksage, tmp_path, minted):
    ctx = _ctx(tmp_path)

    first = asksage._mint(ctx)
    second = asksage._mint(ctx)

    assert first == second
    assert len(minted) == 1


def test_a_changed_key_mints_again(asksage, tmp_path, minted):
    ctx = _ctx(tmp_path)
    asksage._mint(ctx)
    changed = AuthContext(
        provider="asksage",
        config=dict(ctx.config),
        state_dir=ctx.state_dir,
        input_key="key-2",
    )

    asksage._mint(changed)

    assert len(minted) == 2
    assert minted[-1][2] == "key-2"


def test_a_changed_host_mints_again(asksage, tmp_path, minted):
    ctx = _ctx(tmp_path)
    asksage._mint(ctx)
    moved = AuthContext(
        provider="asksage",
        config={"base_url": "https://api.asksage.ai/v1", "email": "user@example.gov"},
        state_dir=ctx.state_dir,
        input_key="key-1",
    )

    asksage._mint(moved)

    assert [call[0] for call in minted] == [
        "https://api.asksage.anl.gov",
        "https://api.asksage.ai",
    ]


def test_a_refusal_names_the_account_and_scrubs_the_key(asksage, tmp_path, monkeypatch):
    """The layer that builds the message scrubs the key it was handed."""
    ctx = _ctx(tmp_path)
    body = json.dumps(
        {"response": "Token is invalid [2] key-1", "status": 400}
    ).encode()

    def _refuse(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://api.asksage.anl.gov/user/get-token-with-api-key",
            400,
            "Bad Request",
            {},
            io.BytesIO(body),
        )

    monkeypatch.setattr(asksage.urllib.request, "urlopen", _refuse)

    with pytest.raises(AuthExchangeError) as excinfo:
        asksage._mint(ctx)
    message = str(excinfo.value)
    assert "user@example.gov" in message
    assert "api.asksage.anl.gov" in message
    assert "key-1" not in message


def test_a_refusal_in_a_200_envelope_is_still_a_refusal(asksage, tmp_path, monkeypatch):
    """AskSage answers 200 with the failure in the body; the body decides."""
    ctx = _ctx(tmp_path)
    body = io.BytesIO(
        json.dumps({"response": "Key doesnt exist", "status": 400}).encode()
    )

    monkeypatch.setattr(
        asksage.urllib.request, "urlopen", lambda *a, **k: _Response(body)
    )

    with pytest.raises(AuthExchangeError, match="Key doesnt exist"):
        asksage._mint(ctx)


def test_a_missing_email_points_at_both_ways_to_set_it(asksage, tmp_path):
    ctx = AuthContext(
        provider="asksage",
        config={"base_url": "https://api.asksage.ai/server/openai/v1"},
        state_dir=str(tmp_path),
        input_key="key-1",
    )

    with pytest.raises(AuthExchangeError, match="Account email"):
        asksage._mint(ctx)


def test_the_email_may_come_from_the_environment(
    asksage, tmp_path, minted, monkeypatch
):
    monkeypatch.setenv(asksage.EMAIL_ENV, "env@example.gov")
    ctx = AuthContext(
        provider="asksage",
        config={"base_url": "https://api.asksage.ai/v1"},
        state_dir=str(tmp_path),
        input_key="key-1",
    )

    asksage._mint(ctx)

    assert minted[0][1] == "env@example.gov"


def test_a_missing_key_says_where_it_belongs(asksage, tmp_path):
    ctx = AuthContext(
        provider="asksage",
        config={"base_url": "https://api.asksage.ai/v1", "email": "a@b.c"},
        state_dir=str(tmp_path),
    )

    with pytest.raises(AuthExchangeError, match="api_key"):
        asksage._mint(ctx)


def test_the_endpoint_never_needs_a_sign_in(asksage, tmp_path):
    assert asksage.EXCHANGE.needs_interactive_setup(_ctx(tmp_path)) is False


def test_the_exchange_takes_the_key_as_input(asksage):
    assert asksage.EXCHANGE.consumes_key is True
    assert asksage.EXCHANGE.kind == "api_key"


def test_describe_reports_the_account_without_the_token(asksage, tmp_path, minted):
    ctx = _ctx(tmp_path)
    token = asksage._mint(ctx)

    summary = asksage.EXCHANGE.describe(ctx)

    assert summary is not None
    assert summary["subject"] == "user@example.gov"
    assert summary["host"] == "https://api.asksage.anl.gov"
    assert token not in json.dumps(summary)


def test_revoke_forgets_the_cached_token(asksage, tmp_path, minted):
    ctx = _ctx(tmp_path)
    asksage._mint(ctx)

    assert asksage.EXCHANGE.revoke(ctx) is False
    assert asksage._record_path(ctx).exists() is False


def test_the_email_comes_from_the_entry_auth_fields(asksage, tmp_path, minted):
    """The provider form stores what the operator typed in the entry."""
    ctx = AuthContext(
        provider="asksage",
        config={
            "base_url": "https://api.asksage.ai/v1",
            "auth_fields": {"email": "entered@example.gov"},
        },
        state_dir=str(tmp_path),
        input_key="key-1",
    )

    asksage._mint(ctx)

    assert minted[0][1] == "entered@example.gov"


def test_a_hand_written_email_key_still_works(asksage, tmp_path, minted):
    """An entry written before the form existed keeps working."""
    ctx = AuthContext(
        provider="asksage",
        config={"base_url": "https://api.asksage.ai/v1", "email": "old@example.gov"},
        state_dir=str(tmp_path),
        input_key="key-1",
    )

    asksage._mint(ctx)

    assert minted[0][1] == "old@example.gov"


def test_the_declared_field_is_the_account_email(asksage):
    """The vendor manifest declares the input; the exchange declares nothing.

    The form is data, so a change to it is a change to the YAML — the module
    is not the home of the wording, the order, or the required flag.
    """
    from llm_rosetta.api import load_providers_from_dir

    root = AUTH_PATH.parents[1]
    by_name = {s.name: s for s in load_providers_from_dir(root, only_paths=["asksage"])}

    for shim in by_name.values():
        (field,) = shim.fields
        assert field["key"] == "email"
        # The value has three sources with a precedence order, so the form
        # cannot demand it: the env var is a legitimate answer the panel
        # cannot see.
        assert "required" not in field
        assert field["label"] == "Account email"
    # No second declaration: the exchange returns an empty tuple.
    assert asksage.EXCHANGE.fields() == ()
