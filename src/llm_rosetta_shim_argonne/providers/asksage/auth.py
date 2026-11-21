"""AskSage credential — the account key, exchanged for a short-lived token.

AskSage's inference endpoints do not accept the account API key.  They take a
short-lived access token, which is minted from the key the account holder
collects on the AskSage website::

    POST {host}/user/get-token-with-api-key
    {"email": "<account email>", "api_key": "<account key>"}
    -> {"response": {"access_token": "..."}, "status": 200}

So the key is an *input* to the exchange rather than a second credential:
the operator stores it in the provider's ``api_key`` (or the shim's
``ASKSAGE_API_KEY`` variable, which a ``providers:`` entry shares with the LLM
lane), and this module turns it into the token the endpoints accept.

Two facts the exchange owns that the gateway does not model:

* **The host is part of the credential.**  AskSage runs two deployments that
  serve the same model roster but refuse each other's keys.  The token is
  minted against the origin of the provider's ``base_url``, so the two can
  never disagree — the mismatch that made ``token_command`` deployments fail
  with "Key doesnt exist".
* **The account email is not a secret but it is required.**  The vendor
  manifest declares it as a form field, so it arrives in the entry's
  ``auth_fields``; a hand-written flat ``email`` key, the record a previous
  mint wrote and ``ASKSAGE_TOKEN_EMAIL`` are read after it.

The token is cached under the gateway's ``state_dir`` until shortly before its
``exp`` claim, so a one-hour refresh interval does not spend a round trip per
hour on a token that is still good.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from llm_rosetta.contracts.auth import (
    AuthContext,
    AuthExchangeError,
    BaseAuthExchange,
)

#: The deployment the shim's ``base_url`` points at when the operator sets
#: none; only used to make the "no base_url" error actionable.
DEFAULT_HOST = "https://api.asksage.anl.gov"
TOKEN_PATH = "/user/get-token-with-api-key"

TIMEOUT = 25.0
MAX_BYTES = 1024 * 1024
#: Reuse a cached token this long before its ``exp`` claim.
SKEW = 120.0

RECORD_NAME = "credential.json"
EMAIL_ENV = "ASKSAGE_TOKEN_EMAIL"


class _Refused(Exception):
    """AskSage answered a non-2xx status."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}" if detail else f"HTTP {status}")
        self.status = status
        self.detail = detail


def _now() -> float:
    return time.time()


def _slug(value: str) -> str:
    """Return a filesystem-safe form of a provider name."""
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in value) or "provider"


def _state_dir(ctx: AuthContext) -> Path:
    """Return the directory this provider's cached token lives in."""
    if ctx.state_dir:
        return Path(ctx.state_dir)
    return Path.home() / ".config" / "llm-rosetta" / "asksage" / _slug(ctx.provider)


def _record_path(ctx: AuthContext) -> Path:
    return _state_dir(ctx) / RECORD_NAME


def _prepare_dir(path: Path) -> None:
    """Create *path* with owner-only permissions, tightening an existing one."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _write_private(path: Path, text: str) -> None:
    """Write *text* to *path* atomically, readable only by its owner."""
    _prepare_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Provider facts
# ---------------------------------------------------------------------------


def _host(ctx: AuthContext) -> str:
    """Return the AskSage deployment this provider talks to.

    Taken from the provider's ``base_url``, so the token is always minted by
    the same deployment that will be asked to accept it.  The paths each lane
    appends (``/server/openai/v1``, ``/server/anthropic``, ...) are stripped.
    """
    base = ctx.config.get("base_url")
    if not isinstance(base, str) or not base.strip():
        raise AuthExchangeError(
            "this provider has no base_url, so the AskSage deployment to mint "
            f"against is unknown; set it to {DEFAULT_HOST}"
        )
    parsed = urllib.parse.urlparse(base.strip())
    if not parsed.scheme or not parsed.hostname:
        raise AuthExchangeError(f"the provider's base_url {base!r} is not a URL")
    return f"{parsed.scheme}://{parsed.netloc}"


def _email(ctx: AuthContext, record: Mapping[str, Any] | None = None) -> str:
    """Return the account email the key belongs to.

    The entry's own ``auth_fields`` comes first — that is where the provider
    form writes what the operator typed — then a hand-written flat ``email``
    key, then the record a previous mint wrote, then the environment.
    """
    declared = ctx.config.get("auth_fields")
    if isinstance(declared, Mapping):
        value = declared.get("email")
        if isinstance(value, str) and value.strip():
            return value.strip()
    configured = ctx.config.get("email")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    if record is None:
        record = _read_record(ctx)
    if record is not None:
        stored = record.get("email")
        if isinstance(stored, str) and stored.strip():
            return stored.strip()
    from_env = os.environ.get(EMAIL_ENV, "")
    if from_env.strip():
        return from_env.strip()
    raise AuthExchangeError(
        "AskSage needs the account email to exchange the API key; fill in the "
        f"Account email field on the provider, or export {EMAIL_ENV}"
    )


def _key(ctx: AuthContext, record: Mapping[str, Any] | None = None) -> str:
    """Return the account API key used as the exchange's input."""
    if ctx.input_key and ctx.input_key.strip():
        return ctx.input_key.strip()
    if record is None:
        record = _read_record(ctx)
    if record is not None:
        stored = record.get("api_key")
        if isinstance(stored, str) and stored.strip():
            return stored.strip()
    raise AuthExchangeError(
        "no AskSage account API key is configured: put it in the provider's "
        "api_key field, or export ASKSAGE_API_KEY so the shim fills it in"
    )


def _digest(value: str) -> str:
    """Return a stable, non-reversible label for a secret or an identity."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _exchange(host: str, email: str, api_key: str) -> str:
    """Exchange the account key for an access token.

    AskSage answers HTTP 200 even when the exchange is refused, putting the
    failure in the envelope, so the status code alone is not the verdict::

        {"response": {"access_token": "..."}, "status": 200}
        {"response": "Token is invalid [2]", "status": 400}

    Raises:
        _Refused: The deployment refused the key.
        AuthExchangeError: The call failed or answered something unusable.
    """
    url = f"{host}{TOKEN_PATH}"
    payload = json.dumps({"email": email, "api_key": api_key}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        body = exc.read(4096).decode("utf-8", "replace")
        raise _Refused(exc.code, _scrub(body, api_key)) from exc
    except urllib.error.URLError as exc:
        raise AuthExchangeError(f"{url} unreachable: {exc.reason}") from exc
    if len(raw) > MAX_BYTES:
        raise AuthExchangeError(f"{url} answered more than {MAX_BYTES} bytes")
    try:
        envelope = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AuthExchangeError(f"{url} did not answer JSON") from exc
    if not isinstance(envelope, dict):
        raise AuthExchangeError(
            f"{url} answered {type(envelope).__name__}, not an object"
        )

    status = envelope.get("status")
    body = envelope.get("response")
    token = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        # Never return a non-string: the ring would send the literal "None".
        detail = body if isinstance(body, str) else json.dumps(envelope)
        raise _Refused(
            int(status) if isinstance(status, int) else 400, _scrub(detail, api_key)
        )
    return token


def _scrub(text: str, secret: str) -> str:
    """Remove *secret* from *text* before it is shown, and clip it."""
    if secret:
        text = text.replace(secret, "***")
    return text[:300]


def _expiry(token: str) -> float | None:
    """Return the token's ``exp`` claim, or ``None`` when it is not a JWT.

    The signature is not verified: this only decides when to mint again, and
    nothing the claim says is trusted beyond that.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    exp = claims.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def _read_record(ctx: AuthContext) -> dict[str, Any] | None:
    """Return the cached token, or ``None`` when there is none or it is stale.

    A record that cannot be read is not an error: the token is a cache, and
    minting again is the answer to every way it can go wrong.
    """
    path = _record_path(ctx)
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return record if isinstance(record, dict) else None


def _usable(record: Mapping[str, Any], host: str, key: str, email: str) -> bool:
    """Whether the cached token may still be sent for this provider.

    The account and the deployment are checked as well as the clock: an
    operator who replaces the key or repoints ``base_url`` expects the next
    request to use the new one, not a token minted for the old.
    """
    if record.get("host") != host or record.get("email") != email:
        return False
    if record.get("key_digest") != _digest(key):
        return False
    token = record.get("access_token")
    if not isinstance(token, str) or not token:
        return False
    expires_at = record.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        return False
    return _now() < float(expires_at) - SKEW


def _save_record(ctx: AuthContext, record: Mapping[str, Any]) -> None:
    _write_private(
        _record_path(ctx), json.dumps(dict(record), indent=2, sort_keys=True) + "\n"
    )


def _mint(ctx: AuthContext) -> str:
    """Return a usable token, minting one when the cache is stale."""
    record = _read_record(ctx)
    host = _host(ctx)
    email = _email(ctx, record)
    key = _key(ctx, record)
    if record is not None and _usable(record, host, key, email):
        return str(record["access_token"])

    try:
        token = _exchange(host, email, key)
    except _Refused as exc:
        raise AuthExchangeError(
            f"AskSage refused the account key for {email} on {host} "
            f"({exc.status}): {exc.detail}"
        ) from None
    fresh = {
        "access_token": token,
        "expires_at": _expiry(token),
        "host": host,
        "email": email,
        "key_digest": _digest(key),
        "saved_at": _now(),
    }
    _save_record(ctx, fresh)
    return token


def _summary(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the token-free part of a record, for a UI to render."""
    return {
        "connected": True,
        "subject": record.get("email"),
        "expires_at": record.get("expires_at"),
        "host": record.get("host"),
        "saved_at": record.get("saved_at"),
    }


class AskSageKeyExchange(BaseAuthExchange):
    """The AskSage account key, exchanged for the token its lanes accept."""

    kind = "api_key"
    consumes_key = True

    def needs_interactive_setup(self, ctx: AuthContext) -> bool:
        """Never: the account key in the config is the whole setup."""
        return False

    def fields(self) -> tuple[Mapping[str, Any], ...]:
        """Return nothing: the account email is declared in ``provider.yaml``.

        One home for the rule.  The vendor manifest carries a ``fields:``
        block that every leaf inherits, so the panel gets the input without
        this module, and changing the form is a change to the YAML.
        """
        return ()

    async def credential(self, ctx: AuthContext) -> str:
        """Return a token, minting one on a worker thread when needed."""
        return await asyncio.to_thread(_mint, ctx)

    def account_id(self, ctx: AuthContext) -> str | None:
        """Return the account email, so two providers on one account group."""
        record = _read_record(ctx)
        try:
            return _email(ctx, record)
        except AuthExchangeError:
            return None

    def describe(self, ctx: AuthContext) -> dict[str, Any] | None:
        """Return the cached token's account, deployment and expiry."""
        record = _read_record(ctx)
        if record is None or not record.get("access_token"):
            return None
        return _summary(record)

    def revoke(self, ctx: AuthContext) -> bool:
        """Forget the cached token.  AskSage has no revocation endpoint.

        Returns:
            ``False`` — only local state was removed.
        """
        try:
            _record_path(ctx).unlink()
        except FileNotFoundError:
            return False
        return False


EXCHANGE = AskSageKeyExchange()
