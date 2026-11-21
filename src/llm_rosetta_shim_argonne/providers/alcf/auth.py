"""ALCF inference credential — the Globus grant, owned by the shim.

The ALCF inference service does not take a static key.  It takes a Globus
access token, which is minted from a grant a human obtains once by signing in
at ``auth.globus.org``; the refresh token that comes back is then spent on
every later mint, without a browser.

That is an OAuth profile, not an argv: the first run needs a browser, what is
kept is a record rather than a string, and the refresh token rotates.  So the
exchange is code, shipped beside the shim's ``provider.yaml`` and reached
through :class:`llm_rosetta.contracts.auth.AuthExchange` — the gateway
only decides *when* to ask for a credential.

Two properties shape the module:

* **The vendor's own file is the record.**  :mod:`globus_sdk` and the
  ``alcf-ai`` helper keep the grant in
  ``~/.globus/app/<client-id>/inference_app/tokens.json``.  When that file
  exists it is read and rewritten in place, so an installation that already
  signed in — including the ``~/.globus`` volume a container mounts — keeps
  working without a second sign-in, and the vendor's tools keep working after
  this shim refreshes the token.  Only when nothing exists there is a record
  written under the gateway's ``state_dir``.
* **One record per configured provider.**  The gateway hands each provider its
  own ``state_dir``, so pooling several ALCF accounts means declaring several
  providers, not teaching this module about directories.

The sign-in is headless-safe: Globus shows the authorization code on its own
page, so :meth:`AlcfGlobus.begin_login` returns a URL and
:meth:`AlcfGlobus.complete_login` accepts what the operator copies back — no
loopback listener and no port on the gateway host.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from llm_rosetta.contracts.auth import (
    AuthContext,
    AuthExchangeError,
    BaseAuthExchange,
    InteractiveSetupRequired,
)

# ---------------------------------------------------------------------------
# Globus constants (public, not secrets — the same values alcf-ai ships)
# ---------------------------------------------------------------------------
AUTH_BASE = "https://auth.globus.org"
TOKEN_ENDPOINT = f"{AUTH_BASE}/v2/oauth2/token"
REVOKE_ENDPOINT = f"{AUTH_BASE}/v2/oauth2/token/revoke"
AUTHORIZE_ENDPOINT = f"{AUTH_BASE}/v2/oauth2/authorize"
CLIENT_ID = "58fdd3bc-e1c3-4ce5-80ea-8d6b87cfb944"
#: The resource server whose token the inference API accepts.
RESOURCE_SERVER = "681c10cc-f684-4540-bcd7-0b4df3bc26ef"
GATEWAY_SCOPE = f"{AUTH_BASE}/scopes/{RESOURCE_SERVER}/action_all"
OPENID_SCOPE = "openid"
#: ALCF requires its access policy in the authorization request; the sign-in
#: page refuses an account that has not accepted it.
SESSION_POLICY = "83732ff2-9c42-4548-b5ce-17e498c84f6a"
#: Globus renders the code on its own page, so a loopback listener is not
#: needed and the gateway host needs no open port.
REDIRECT_URI = f"{AUTH_BASE}/v2/web/auth-code"

#: Refresh this long before the token dies: the gateway's refresh interval is
#: minutes apart, so a token that expires in between would fail requests.
SKEW = 120.0
TIMEOUT = 30.0
MAX_BYTES = 1024 * 1024

RECORD_NAME = "credential.json"
PENDING_NAME = "pending-login.json"
PENDING_TTL = 15 * 60.0

#: Where the vendor's tooling keeps the same grant.  Read first, written back
#: in the vendor's own layout, so both halves of a container agree on one file.
VENDOR_TOKEN_PATH = (
    Path.home() / ".globus" / "app" / CLIENT_ID / ("inference_app") / "tokens.json"
)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class _Refused(Exception):
    """The token endpoint answered a non-2xx status.

    Carries the OAuth ``error`` code when the body has one: ``invalid_grant``
    means the grant is spent or revoked, which is a sign-in problem rather
    than a retryable one.
    """

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}" if detail else f"HTTP {status}")
        self.status = status
        self.code = code
        self.detail = detail


def _form_post(
    url: str, fields: Mapping[str, str], *, secrets: tuple[str, ...] = ()
) -> str:
    """POST ``application/x-www-form-urlencoded`` and return the body text."""
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        body = exc.read(4096).decode("utf-8", "replace")
        raise _Refused(exc.code, _oauth_error(body), _scrub(body, secrets)) from exc
    except urllib.error.URLError as exc:
        raise AuthExchangeError(f"{url} unreachable: {exc.reason}") from exc
    if len(raw) > MAX_BYTES:
        raise AuthExchangeError(f"{url} answered more than {MAX_BYTES} bytes")
    return raw.decode("utf-8", "replace")


def _json_post(
    url: str, fields: Mapping[str, str], *, secrets: tuple[str, ...] = ()
) -> dict[str, Any]:
    """POST form fields and decode one JSON object from the answer."""
    text = _form_post(url, fields, secrets=secrets)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AuthExchangeError(f"{url} did not answer JSON") from exc
    if not isinstance(payload, dict):
        raise AuthExchangeError(
            f"{url} answered {type(payload).__name__}, not an object"
        )
    return payload


def _oauth_error(body: str) -> str:
    """Return the OAuth error code in *body*, or an empty string."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    return error if isinstance(error, str) else ""


def _scrub(text: str, secrets: tuple[str, ...]) -> str:
    """Remove the values in *secrets* from *text* before it is shown.

    A vendor's error body is not under our control, and this text reaches an
    operator-facing message; a token echoed back by the endpoint would
    otherwise be printed.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text[:300]


def _vendor_message(text: str) -> str:
    """Return the vendor's own wording, clipped and without a token."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text.strip()[:300]
    if isinstance(payload, dict):
        for key in ("error_description", "message", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:300]
    return text.strip()[:300]


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.time()


def _slug(value: str) -> str:
    """Return a filesystem-safe form of a provider name."""
    return "".join(c if c.isalnum() or c in "-._" else "_" for c in value) or "provider"


def _state_dir(ctx: AuthContext) -> Path:
    """Return the directory a record this shim owns would live in."""
    if ctx.state_dir:
        return Path(ctx.state_dir)
    return Path.home() / ".config" / "llm-rosetta" / "alcf" / _slug(ctx.provider)


def _vendor_dir_writable() -> bool:
    """Whether the vendor's grant file can be written where it lives.

    A container may mount ``~/.globus`` read-only.  Refreshing into a file
    that cannot be written would cost every later mint a second refresh of an
    already-rotated token, so the decision is made before the file is chosen.
    """
    path = VENDOR_TOKEN_PATH
    if path.exists():
        return os.access(path, os.W_OK)
    parent = path.parent
    while not parent.exists():
        parent = parent.parent
    return os.access(parent, os.W_OK)


def _record_path(ctx: AuthContext) -> Path:
    """Return the file this provider's record is read from and written to.

    The vendor's own file wins when it exists: a sign-in performed by
    ``alcf-ai`` or this shim's CLI is then the same grant the gateway uses,
    and the vendor's tools see every refresh this shim performs.
    """
    if VENDOR_TOKEN_PATH.exists():
        return VENDOR_TOKEN_PATH
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


@contextmanager
def _record_lock(ctx: AuthContext) -> Iterator[None]:
    """Hold an exclusive lock across one read-modify-write of the record.

    Globus rotates the refresh token on use: two processes refreshing at once
    would spend it twice and leave the account unreachable.  The lock sits
    beside the record it protects.
    """
    import fcntl

    path = _record_path(ctx)
    _prepare_dir(path.parent)
    fd = os.open(str(path.parent / ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _grant_from_token_response(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the inference grant in a Globus token response.

    Globus answers one token for ``auth.globus.org`` and the resource-server
    tokens under ``other_tokens``; the one the inference API accepts is the
    entry named by :data:`RESOURCE_SERVER`.  A response that carries the grant
    at the top level is accepted too, because a refresh answers that way.
    """
    others = payload.get("other_tokens")
    if isinstance(others, list):
        for entry in others:
            if (
                isinstance(entry, dict)
                and entry.get("resource_server") == RESOURCE_SERVER
                and isinstance(entry.get("access_token"), str)
            ):
                return dict(entry)
    if isinstance(payload.get("access_token"), str) and (
        payload.get("resource_server") == RESOURCE_SERVER
    ):
        return dict(payload)
    raise AuthExchangeError(
        "the token response carried no token for the ALCF inference service "
        f"(resource server {RESOURCE_SERVER}); is the account entitled to it?"
    )


def _grant_to_record(grant: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise one grant into the record this module stores."""
    access = grant.get("access_token")
    if not isinstance(access, str) or not access:
        raise AuthExchangeError("the grant carries no access token")
    expires_in = grant.get("expires_in")
    record: dict[str, Any] = {
        "access_token": access,
        "refresh_token": grant.get("refresh_token") or "",
        "scope": grant.get("scope") or "",
        "resource_server": RESOURCE_SERVER,
        "token_type": grant.get("token_type") or "Bearer",
        "expires_at": _now() + float(expires_in)
        if isinstance(expires_in, (int, float))
        else None,
        "saved_at": _now(),
    }
    identity = grant.get("identity_id")
    if isinstance(identity, str) and identity:
        record["identity_id"] = identity
    return record


def _parse_record(doc: Any) -> dict[str, Any]:
    """Read a grant out of either file shape this module may find on disk.

    The vendor's ``tokens.json`` nests one entry per resource server under
    ``data.DEFAULT``; the record this module writes under ``state_dir`` uses
    the same layout, so one reader serves both and the vendor's tools can read
    what the gateway wrote.
    """
    if not isinstance(doc, dict):
        raise AuthExchangeError("the credential record is not an object")
    nested = doc.get("data")
    if isinstance(nested, dict):
        default = nested.get("DEFAULT")
        if isinstance(default, dict):
            entry = default.get(RESOURCE_SERVER)
            if isinstance(entry, dict):
                return _normalise_stored(entry)
    if isinstance(doc.get("access_token"), str):
        return _normalise_stored(doc)
    raise AuthExchangeError(
        "the credential record holds no ALCF inference token "
        f"(resource server {RESOURCE_SERVER})"
    )


def _normalise_stored(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Return a stored grant in this module's flat shape."""
    access = entry.get("access_token")
    if not isinstance(access, str) or not access:
        raise AuthExchangeError("the stored grant holds no access token")
    expires_at = entry.get("expires_at")
    if expires_at is None:
        # The vendor's file names the instant, this module names the span.
        expires_at = entry.get("expires_at_seconds")
    record: dict[str, Any] = {
        "access_token": access,
        "refresh_token": entry.get("refresh_token") or "",
        "scope": entry.get("scope") or "",
        "resource_server": RESOURCE_SERVER,
        "token_type": entry.get("token_type") or "Bearer",
        "expires_at": float(expires_at)
        if isinstance(expires_at, (int, float))
        else None,
        "saved_at": entry.get("saved_at"),
    }
    identity = entry.get("identity_id")
    if isinstance(identity, str) and identity:
        record["identity_id"] = identity
    return record


def _render_record(record: Mapping[str, Any]) -> str:
    """Render *record* in the vendor's ``tokens.json`` layout.

    Written in one layout everywhere: the gateway's own record directory then
    holds a file ``alcf-ai`` can read, and a mounted ``~/.globus`` keeps
    working after this shim refreshes the grant.
    """
    entry = {
        "resource_server": RESOURCE_SERVER,
        "scope": record.get("scope") or GATEWAY_SCOPE,
        "access_token": record["access_token"],
        "refresh_token": record.get("refresh_token") or "",
        "expires_at_seconds": int(record["expires_at"])
        if isinstance(record.get("expires_at"), (int, float))
        else 0,
        "token_type": record.get("token_type") or "Bearer",
    }
    identity = record.get("identity_id")
    if isinstance(identity, str) and identity:
        entry["identity_id"] = identity
    document = {
        "data": {"DEFAULT": {RESOURCE_SERVER: entry}},
        "format_version": "2.0",
        "globus-sdk.version": "llm-rosetta-shim",
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def _read_record(ctx: AuthContext) -> dict[str, Any]:
    """Return the stored grant, or raise when there is none to use.

    Raises:
        InteractiveSetupRequired: No record exists for this provider.
        AuthExchangeError: The record exists but cannot be used.
    """
    candidates = [
        p for p in (VENDOR_TOKEN_PATH, _state_dir(ctx) / RECORD_NAME) if p.exists()
    ]
    if not candidates:
        raise InteractiveSetupRequired(
            "no ALCF sign-in for this provider yet; start one from the admin "
            "panel (Sign in) or run `llm-rosetta credential login`"
        )
    path = candidates[0]
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthExchangeError(
            f"credential record at {path} is unreadable: {exc}"
        ) from exc
    return _parse_record(document)


def _save_record(ctx: AuthContext, record: Mapping[str, Any]) -> None:
    """Store *record*, preferring the file it was read from."""
    path = _record_path(ctx)
    _write_private(path, _render_record(record))


def _fresh(record: Mapping[str, Any], *, now: float | None = None) -> bool:
    """Whether the record's access token is still worth sending."""
    moment = _now() if now is None else now
    expires_at = record.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        # A record with no expiry is sent until the upstream refuses it, which
        # the 401 retry then turns into a refresh.
        return True
    return moment < float(expires_at) - SKEW


# ---------------------------------------------------------------------------
# Mint
# ---------------------------------------------------------------------------


def _refresh(ctx: AuthContext, record: Mapping[str, Any]) -> dict[str, Any]:
    """Spend the refresh token and store the grant it answers with."""
    refresh_token = record.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise InteractiveSetupRequired(
            "the ALCF sign-in carries no refresh token; sign in again"
        )
    try:
        payload = _json_post(
            TOKEN_ENDPOINT,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            },
            secrets=(refresh_token,),
        )
    except _Refused as exc:
        if exc.code in ("invalid_grant", "invalid_client", "invalid_refresh_token"):
            raise InteractiveSetupRequired(
                "the ALCF grant is no longer valid (the sign-in was revoked or "
                "used elsewhere); sign in again"
            ) from None
        raise AuthExchangeError(f"the token refresh failed ({exc.status})") from None
    fresh = _grant_to_record(_grant_from_token_response(payload))
    # A refresh response may omit the identity; the stored one still holds.
    if not fresh.get("identity_id") and record.get("identity_id"):
        fresh["identity_id"] = record["identity_id"]
    if not fresh.get("scope") and record.get("scope"):
        fresh["scope"] = record["scope"]
    _save_record(ctx, fresh)
    return fresh


def _credential(ctx: AuthContext) -> str:
    """Return a usable access token, refreshing it when it is near expiry."""
    record = _read_record(ctx)
    if _fresh(record):
        return str(record["access_token"])
    with _record_lock(ctx):
        # Another process may have refreshed while this one waited for the
        # lock; re-read rather than spend the refresh token a second time.
        record = _read_record(ctx)
        if _fresh(record):
            return str(record["access_token"])
        if not _vendor_dir_writable() and _record_path(ctx) == VENDOR_TOKEN_PATH:
            raise AuthExchangeError(
                f"the ALCF grant at {VENDOR_TOKEN_PATH} is expired and the file "
                "is not writable, so the refreshed token cannot be saved; mount "
                "the directory read-write or set the gateway's data directory"
            )
        return str(_refresh(ctx, record)["access_token"])


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------


def _pending_path(ctx: AuthContext) -> Path:
    return _state_dir(ctx) / PENDING_NAME


def _pkce() -> tuple[str, str]:
    """Return a PKCE ``(verifier, challenge)`` pair."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _authorize_url(challenge: str, state: str) -> str:
    """Build the URL a human opens to grant the ALCF inference scope."""
    query = urllib.parse.urlencode(
        {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "scope": f"{OPENID_SCOPE} {GATEWAY_SCOPE}",
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "session_required_policies": SESSION_POLICY,
            "state": state,
        }
    )
    return f"{AUTHORIZE_ENDPOINT}?{query}"


def _read_pending(ctx: AuthContext) -> dict[str, Any]:
    """Return the stored pending sign-in, refusing a stale one."""
    path = _pending_path(ctx)
    try:
        with open(path, encoding="utf-8") as handle:
            pending = json.load(handle)
    except FileNotFoundError as exc:
        raise AuthExchangeError(
            "no ALCF sign-in is in progress; start one first"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthExchangeError("the pending ALCF sign-in is unreadable") from exc
    if not isinstance(pending, dict) or not isinstance(pending.get("verifier"), str):
        raise AuthExchangeError("the pending ALCF sign-in has an invalid shape")
    created = pending.get("created_at")
    if not isinstance(created, (int, float)) or _now() - float(created) > PENDING_TTL:
        _discard_pending(ctx)
        raise AuthExchangeError("the ALCF sign-in expired; start again")
    return pending


def _discard_pending(ctx: AuthContext) -> None:
    """Consume the pending sign-in, including after a failure."""
    try:
        _pending_path(ctx).unlink()
    except FileNotFoundError:
        pass


def _callback_code(value: str) -> str:
    """Return the authorization code in *value*.

    Globus shows the bare code on its page, but an operator may paste the
    whole ``https://auth.globus.org/v2/web/auth-code?code=...`` URL instead;
    both are accepted rather than making the difference a support question.
    """
    text = value.strip()
    if not text:
        raise AuthExchangeError("no authorization code was given")
    if "code=" in text:
        parsed = urllib.parse.urlparse(text)
        query = urllib.parse.parse_qs(parsed.query)
        code = (query.get("code") or [""])[0]
        if code:
            return code
    return text


def begin_login(ctx: AuthContext) -> dict[str, Any]:
    """Start a sign-in and return only what a human needs.

    Starting again supersedes an unfinished sign-in: only the newest verifier
    is stored, so two open tabs cannot both mint a record.

    Returns:
        ``authorization_url`` and ``expires_at`` (epoch seconds).  The PKCE
        verifier is stored, never returned.

    Raises:
        AuthExchangeError: The pending sign-in could not be written.
    """
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    created = _now()
    pending = {
        "verifier": verifier,
        "state": state,
        "created_at": created,
        "expires_at": created + PENDING_TTL,
    }
    _write_private(
        _pending_path(ctx), json.dumps(pending, indent=2, sort_keys=True) + "\n"
    )
    return {
        "authorization_url": _authorize_url(challenge, state),
        "expires_at": created + PENDING_TTL,
    }


def complete_login(ctx: AuthContext, callback_url: str) -> dict[str, Any]:
    """Finish a sign-in from the code Globus showed the operator.

    Args:
        ctx: The provider this record belongs to.
        callback_url: The authorization code, or the URL Globus displayed.

    Returns:
        The token-free summary of the stored grant.

    Raises:
        AuthExchangeError: No sign-in is pending, it expired, or Globus
            refused the code.
    """
    pending = _read_pending(ctx)
    code = _callback_code(callback_url)
    try:
        payload = _json_post(
            TOKEN_ENDPOINT,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": CLIENT_ID,
                "code_verifier": str(pending["verifier"]),
            },
            secrets=(code,),
        )
    except _Refused as exc:
        raise AuthExchangeError(
            f"Globus refused the sign-in ({exc.status}): {_vendor_message(exc.detail)}"
        ) from None
    finally:
        # Single-use either way: a consumed code cannot be replayed, and a
        # refused one should not leave a verifier lying around.
        _discard_pending(ctx)
    record = _grant_to_record(_grant_from_token_response(payload))
    _save_record(ctx, record)
    return _summary(record)


def login(ctx: AuthContext) -> dict[str, Any]:
    """Run the sign-in on this machine: print the URL, read the code back.

    The flow the CLI drives.  It returns the record itself — the caller is
    about to hand it to the gateway, and the record *is* the credential — and
    a copy stays on this machine so the same provider works here too.
    """
    started = begin_login(ctx)
    print(
        "Open this URL in any browser, sign in with your ALCF account, and "
        "paste the code it shows back here:\n\n"
        f"  {started['authorization_url']}\n",
        flush=True,
    )
    try:
        code = input("Authorization code: ")
    except EOFError as exc:
        raise AuthExchangeError("no authorization code was entered") from exc
    summary = complete_login(ctx, code)
    return {**summary, **_read_record(ctx)}


def _summary(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the token-free part of a record, for a UI to render."""
    return {
        "connected": True,
        "subject": record.get("identity_id"),
        "expires_at": record.get("expires_at"),
        "scopes": [record["scope"]] if record.get("scope") else [],
        "saved_at": record.get("saved_at"),
    }


def _revoke(record: Mapping[str, Any]) -> bool:
    """Ask Globus to revoke the grant; report whether it did."""
    token = record.get("refresh_token") or record.get("access_token")
    if not isinstance(token, str) or not token:
        return False
    _form_post(
        REVOKE_ENDPOINT,
        {"client_id": CLIENT_ID, "token": token},
        secrets=(token,),
    )
    return True


# ---------------------------------------------------------------------------
# The exchange
# ---------------------------------------------------------------------------


class AlcfGlobus(BaseAuthExchange):
    """The ALCF inference credential, as the gateway sees it."""

    kind = "oauth"
    consumes_key = False

    def needs_interactive_setup(self, ctx: AuthContext) -> bool:
        """Whether a human still has to sign in for this provider.

        Reads local state only: the gateway calls this while building a
        config, outside any event loop.
        """
        try:
            record = _read_record(ctx)
        except (InteractiveSetupRequired, AuthExchangeError):
            return True
        return not record.get("access_token")

    async def credential(self, ctx: AuthContext) -> str:
        """Return the access token, refreshing it on a worker thread."""
        return await asyncio.to_thread(_credential, ctx)

    def account_id(self, ctx: AuthContext) -> str | None:
        """Return the Globus identity — the account, not the token.

        The access token rotates, the identity does not, so two providers
        signed into one ALCF account group together.
        """
        try:
            record = _read_record(ctx)
        except (InteractiveSetupRequired, AuthExchangeError):
            return None
        identity = record.get("identity_id")
        return identity if isinstance(identity, str) and identity else None

    def describe(self, ctx: AuthContext) -> dict[str, Any] | None:
        """Return who is signed in and until when, never the tokens."""
        try:
            record = _read_record(ctx)
        except (InteractiveSetupRequired, AuthExchangeError):
            return None
        return _summary(record)

    def begin_login(self, ctx: AuthContext) -> dict[str, Any]:
        """Start a sign-in the webadmin panel can complete."""
        return begin_login(ctx)

    def complete_login(self, ctx: AuthContext, callback_url: str) -> dict[str, Any]:
        """Complete the sign-in with the code the operator pasted."""
        return complete_login(ctx, callback_url)

    def login(self, ctx: AuthContext) -> dict[str, Any]:
        """Run the sign-in here, where the operator and their browser are."""
        return login(ctx)

    def install(self, ctx: AuthContext, record: Any) -> dict[str, Any]:
        """Adopt a record produced on another machine.

        Accepts both shapes the flow can produce: this module's flat record
        (what ``llm-rosetta credential login`` prints) and a Globus
        ``tokens.json`` document copied from a machine that signed in with the
        vendor's own tooling.  A record without a refresh token is refused:
        it would work until the access token expired and then strand the
        provider with no way back except another sign-in.
        """
        if not isinstance(record, Mapping):
            raise AuthExchangeError("a credential record must be an object")
        parsed = _parse_record(dict(record))
        if not parsed.get("refresh_token"):
            raise AuthExchangeError(
                "the record carries no refresh token, so the credential could "
                "not be renewed; sign in again with consent for offline access"
            )
        _save_record(ctx, parsed)
        return _summary(parsed)

    def revoke(self, ctx: AuthContext) -> bool:
        """Sign out: drop the record first, then revoke the grant upstream.

        Removal happens before the network call and even when it fails — a
        refresh that read the old record just before a delete would otherwise
        write it back afterwards and leave a signed-out provider holding a
        live grant.
        """
        try:
            record = _read_record(ctx)
        except (InteractiveSetupRequired, AuthExchangeError):
            record = {}
        with _record_lock(ctx):
            for path in (VENDOR_TOKEN_PATH, _state_dir(ctx) / RECORD_NAME):
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
        if not record:
            return False
        try:
            return _revoke(record)
        except _Refused:
            return False
        except AuthExchangeError:
            return False


EXCHANGE = AlcfGlobus()
