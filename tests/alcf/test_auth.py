"""Tests for the ALCF Globus exchange (``providers/alcf/auth.py``)."""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from llm_rosetta.contracts.auth import (
    AuthContext,
    AuthExchangeError,
    InteractiveSetupRequired,
)


AUTH_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "llm_rosetta_shim_argonne"
    / "providers"
    / "alcf"
    / "auth.py"
)


@pytest.fixture
def alcf(monkeypatch, tmp_path):
    """Load ``auth.py`` the way the shim loader does, off the record path."""
    spec = importlib.util.spec_from_file_location("alcf_auth_test", AUTH_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    # The vendor's own file must not be read or written by a test run.
    monkeypatch.setattr(
        module, "VENDOR_TOKEN_PATH", tmp_path / "vendor" / "tokens.json"
    )
    return module


def _ctx(tmp_path: Path, provider: str = "alcf") -> AuthContext:
    return AuthContext(provider=provider, state_dir=str(tmp_path / "state"))


def _grant(module, **overrides: Any) -> dict[str, Any]:
    """A token response as Globus answers it: the grant under other_tokens."""
    entry = {
        "resource_server": module.RESOURCE_SERVER,
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "expires_in": 3600,
        "scope": module.GATEWAY_SCOPE,
        "identity_id": "identity-1",
    }
    entry.update(overrides)
    return {"access_token": "auth-token", "other_tokens": [entry]}


def test_the_grant_is_taken_from_the_inference_resource_server(alcf):
    record = alcf._grant_to_record(alcf._grant_from_token_response(_grant(alcf)))

    assert record["access_token"] == "access-1"
    assert record["refresh_token"] == "refresh-1"
    assert record["identity_id"] == "identity-1"


def test_a_response_without_the_inference_grant_is_refused(alcf):
    payload = {"access_token": "x", "other_tokens": [{"resource_server": "other"}]}

    with pytest.raises(AuthExchangeError, match="no token for the ALCF"):
        alcf._grant_from_token_response(payload)


def test_needs_a_sign_in_before_any_record_exists(alcf, tmp_path):
    ctx = _ctx(tmp_path)

    assert alcf.EXCHANGE.needs_interactive_setup(ctx) is True


def test_a_stored_grant_needs_no_sign_in(alcf, tmp_path):
    ctx = _ctx(tmp_path)
    alcf._save_record(
        ctx, alcf._grant_to_record(alcf._grant_from_token_response(_grant(alcf)))
    )

    assert alcf.EXCHANGE.needs_interactive_setup(ctx) is False


def test_the_record_is_written_in_the_vendor_layout(alcf, tmp_path):
    """A file the gateway writes must stay readable by ``alcf-ai``."""
    ctx = _ctx(tmp_path)
    alcf._save_record(
        ctx, alcf._grant_to_record(alcf._grant_from_token_response(_grant(alcf)))
    )

    path = alcf._record_path(ctx)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    document = json.loads(path.read_text())
    entry = document["data"]["DEFAULT"][alcf.RESOURCE_SERVER]
    assert entry["access_token"] == "access-1"
    assert entry["expires_at_seconds"] > 0


def test_a_fresh_token_costs_no_network_call(alcf, tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    alcf._save_record(
        ctx, alcf._grant_to_record(alcf._grant_from_token_response(_grant(alcf)))
    )
    monkeypatch.setattr(alcf, "_json_post", pytest.fail)

    assert alcf._credential(ctx) == "access-1"


def test_a_stale_token_is_refreshed_and_the_rotation_kept(alcf, tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    record = alcf._grant_to_record(alcf._grant_from_token_response(_grant(alcf)))
    record["expires_at"] = 0
    alcf._save_record(ctx, record)
    seen: dict[str, Any] = {}

    def _post(url, fields, *, secrets=()):
        seen.update(fields)
        return {
            "resource_server": alcf.RESOURCE_SERVER,
            "access_token": "access-2",
            "refresh_token": "refresh-2",
            "expires_in": 3600,
        }

    monkeypatch.setattr(alcf, "_json_post", _post)

    assert alcf._credential(ctx) == "access-2"
    assert seen["grant_type"] == "refresh_token"
    assert seen["refresh_token"] == "refresh-1"
    stored = alcf._read_record(ctx)
    assert stored["refresh_token"] == "refresh-2"


def test_a_refresh_keeps_the_identity_it_does_not_repeat(alcf, tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    record = alcf._grant_to_record(alcf._grant_from_token_response(_grant(alcf)))
    record["expires_at"] = 0
    alcf._save_record(ctx, record)
    monkeypatch.setattr(
        alcf,
        "_json_post",
        lambda *a, **k: {
            "resource_server": alcf.RESOURCE_SERVER,
            "access_token": "access-2",
            "refresh_token": "refresh-2",
            "expires_in": 3600,
        },
    )

    alcf._credential(ctx)

    assert alcf._read_record(ctx)["identity_id"] == "identity-1"


def test_a_refused_refresh_asks_for_a_sign_in(alcf, tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    record = alcf._grant_to_record(alcf._grant_from_token_response(_grant(alcf)))
    record["expires_at"] = 0
    alcf._save_record(ctx, record)

    def _post(url, fields, *, secrets=()):
        raise alcf._Refused(400, "invalid_grant", "Grant is invalid")

    monkeypatch.setattr(alcf, "_json_post", _post)

    with pytest.raises(InteractiveSetupRequired):
        alcf._credential(ctx)


def test_a_transient_refresh_failure_is_not_a_sign_in(alcf, tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    record = alcf._grant_to_record(alcf._grant_from_token_response(_grant(alcf)))
    record["expires_at"] = 0
    alcf._save_record(ctx, record)

    def _post(url, fields, *, secrets=()):
        raise alcf._Refused(503, "", "service unavailable")

    monkeypatch.setattr(alcf, "_json_post", _post)

    with pytest.raises(AuthExchangeError) as excinfo:
        alcf._credential(ctx)
    assert not isinstance(excinfo.value, InteractiveSetupRequired)


def test_a_record_without_a_refresh_token_asks_for_a_sign_in(alcf, tmp_path):
    ctx = _ctx(tmp_path)
    record = alcf._grant_to_record(alcf._grant_from_token_response(_grant(alcf)))
    record["refresh_token"] = ""
    record["expires_at"] = 0
    alcf._save_record(ctx, record)

    with pytest.raises(InteractiveSetupRequired, match="refresh token"):
        alcf._credential(ctx)


def test_begin_login_returns_a_url_and_hides_the_verifier(alcf, tmp_path):
    ctx = _ctx(tmp_path)

    started = alcf.EXCHANGE.begin_login(ctx)

    assert started["authorization_url"].startswith(alcf.AUTHORIZE_ENDPOINT)
    assert "code_challenge=" in started["authorization_url"]
    assert "session_required_policies=" in started["authorization_url"]
    assert set(started) == {"authorization_url", "expires_at"}
    pending = json.loads(alcf._pending_path(ctx).read_text())
    assert "verifier" in pending


def test_complete_login_exchanges_the_code_with_the_stored_verifier(
    alcf, tmp_path, monkeypatch
):
    ctx = _ctx(tmp_path)
    started = alcf.EXCHANGE.begin_login(ctx)
    pending = json.loads(alcf._pending_path(ctx).read_text())
    seen: dict[str, Any] = {}

    def _post(url, fields, *, secrets=()):
        seen.update(fields)
        return _grant(alcf)

    monkeypatch.setattr(alcf, "_json_post", _post)

    summary = alcf.EXCHANGE.complete_login(ctx, "the-code")

    assert started
    assert seen["code"] == "the-code"
    assert seen["code_verifier"] == pending["verifier"]
    assert summary["subject"] == "identity-1"
    assert alcf.EXCHANGE.needs_interactive_setup(ctx) is False
    assert not alcf._pending_path(ctx).exists()


def test_the_pasted_url_is_read_for_its_code(alcf, tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    alcf.EXCHANGE.begin_login(ctx)
    seen: dict[str, Any] = {}

    def _post(url, fields, *, secrets=()):
        seen.update(fields)
        return _grant(alcf)

    monkeypatch.setattr(alcf, "_json_post", _post)

    alcf.EXCHANGE.complete_login(
        ctx, "https://auth.globus.org/v2/web/auth-code?code=abc123&state=x"
    )

    assert seen["code"] == "abc123"


def test_a_stale_pending_sign_in_is_refused(alcf, tmp_path):
    ctx = _ctx(tmp_path)
    alcf.EXCHANGE.begin_login(ctx)
    pending = json.loads(alcf._pending_path(ctx).read_text())
    pending["created_at"] -= alcf.PENDING_TTL + 1
    alcf._write_private(alcf._pending_path(ctx), json.dumps(pending))

    with pytest.raises(AuthExchangeError, match="expired"):
        alcf.EXCHANGE.complete_login(ctx, "code")


def test_install_accepts_a_flat_record(alcf, tmp_path):
    ctx = _ctx(tmp_path)
    record = alcf._grant_to_record(alcf._grant_from_token_response(_grant(alcf)))

    summary = alcf.EXCHANGE.install(ctx, record)

    assert summary["subject"] == "identity-1"
    assert alcf._credential(ctx) == "access-1"


def test_install_accepts_a_vendor_tokens_file(alcf, tmp_path):
    ctx = _ctx(tmp_path)
    document = json.loads(
        alcf._render_record(
            alcf._grant_to_record(alcf._grant_from_token_response(_grant(alcf)))
        )
    )

    summary = alcf.EXCHANGE.install(ctx, document)

    assert summary["subject"] == "identity-1"


def test_install_refuses_a_record_it_could_not_renew(alcf, tmp_path):
    ctx = _ctx(tmp_path)
    record = alcf._grant_to_record(alcf._grant_from_token_response(_grant(alcf)))
    record["refresh_token"] = ""

    with pytest.raises(AuthExchangeError, match="refresh token"):
        alcf.EXCHANGE.install(ctx, record)


def test_revoke_removes_the_record_even_when_globus_refuses(
    alcf, tmp_path, monkeypatch
):
    ctx = _ctx(tmp_path)
    alcf._save_record(
        ctx, alcf._grant_to_record(alcf._grant_from_token_response(_grant(alcf)))
    )

    def _post(url, fields, *, secrets=()):
        raise alcf._Refused(400, "", "no")

    monkeypatch.setattr(alcf, "_form_post", _post)

    assert alcf.EXCHANGE.revoke(ctx) is False
    assert not alcf._record_path(ctx).exists()


def test_describe_never_carries_a_token(alcf, tmp_path):
    ctx = _ctx(tmp_path)
    alcf._save_record(
        ctx, alcf._grant_to_record(alcf._grant_from_token_response(_grant(alcf)))
    )

    summary = alcf.EXCHANGE.describe(ctx)

    assert summary is not None
    assert summary["subject"] == "identity-1"
    assert "access-1" not in json.dumps(summary)
    assert "refresh-1" not in json.dumps(summary)
