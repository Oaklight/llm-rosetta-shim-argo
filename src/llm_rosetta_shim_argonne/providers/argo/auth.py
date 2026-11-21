"""Argo's credential: an ANL account, valid only while the SSH tunnel is up.

Argo has no token to mint.  What the upstream checks is the account name, and
what makes the name reachable is the SSH master that ``argo-tunnel`` owns —
``apps.inside.anl.gov`` resolves only inside Argonne's network, and the jump
host asks for Duo MFA.  That is not a credential exchange in the OAuth sense,
but it is exactly the shape :class:`AuthExchange` exists for: a shim-owned
form the gateway cannot do on its own, whose failure has one operator action
attached, and whose state the panel should name instead of leaving a 403 from
upstream to explain it.

So the exchange owns two facts and no secrets:

* **The account name is the credential.**  It arrives as the operator's
  ``api_key`` (``consumes_key``), which is how the panel keeps the key field:
  the ANL username belongs in that box, and the gateway never sends it
  anywhere but the auth header.
* **The tunnel is a precondition, not the credential.**  When the provider
  entry declares a local SOCKS ``proxy`` and nothing listens on it, the
  exchange refuses *before* a request would fail at the proxy with a 502 that
  says nothing about Duo.  It never starts, stops or repairs the tunnel: the
  gateway has no terminal, and ``auth`` blocks on a person.

Two rules the contract imposes and this module keeps:

* ``needs_interactive_setup`` and ``account_id`` must read local state only,
  because the gateway calls them while building a config, outside any event
  loop.  A bounded ``connect()`` to a loopback port is local state; running
  ``argo-tunnel status`` would not be, so it is not done here.
* Nothing here is stored.  There is no record to install, revoke or sign out
  of — the tunnel's own profile belongs to ``argo-tunnel``, which owns its
  lifecycle and its privacy.
"""

from __future__ import annotations

import socket
import urllib.parse
from collections.abc import Mapping
from typing import Any

from llm_rosetta.contracts.auth import (
    AuthContext,
    AuthExchangeError,
    BaseAuthExchange,
    InteractiveSetupRequired,
)

#: The console script that owns the tunnel.  Named in messages only: this
#: module never runs it.
TUNNEL_COMMAND = "argo-tunnel"

#: Schemes whose listener is a SOCKS proxy.  Anything else in ``proxy`` is a
#: proxy the operator configured for another reason, so it is not treated as
#: the Argo tunnel and is not probed.
_SOCKS_SCHEMES = frozenset({"socks5", "socks5h", "socks"})

#: Hosts whose listener is ours to probe.  A remote proxy is not: the answer
#: would be a network round trip on the config-build path.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0", ""})

#: Long enough for a loopback accept, short enough not to be felt on a config
#: build.  A refused connection returns at once, which is the common case.
PROBE_TIMEOUT = 0.5


def _listening(host: str, port: int, timeout: float = PROBE_TIMEOUT) -> bool:
    """Whether something accepts a TCP connection at *host*:*port*.

    A bound port is not proof of *ownership* — ``argo-tunnel`` makes the same
    distinction — but for the question asked here ("would a request through
    this proxy even reach a forwarder?") a listener is the practical answer.

    Args:
        host: Host to connect to.
        port: TCP port.
        timeout: Seconds to wait.

    Returns:
        ``True`` when the connection was accepted.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _proxy_parts(raw: Any) -> tuple[str, int] | None:
    """Parse a provider ``proxy`` value into a loopback SOCKS address.

    Args:
        raw: The provider entry's ``proxy``, e.g.
            ``"socks5://127.0.0.1:10801"``.

    Returns:
        ``(host, port)`` for a SOCKS proxy on this machine, else ``None``.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if "://" not in text:
        text = "socks5://" + text
    parts = urllib.parse.urlsplit(text)
    if parts.scheme.lower() not in _SOCKS_SCHEMES:
        return None
    host = parts.hostname or ""
    if host not in _LOOPBACK_HOSTS:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        return None
    return host, port


class ArgoCredential(BaseAuthExchange):
    """The ANL account name, with the tunnel it needs declared and checked."""

    #: Reported to the operator; the panel's card says "sign-in".
    kind = "tunnel"

    #: The operator's ``api_key`` is this exchange's *input*: the ANL username
    #: is the credential, so the key field stays and is not a second secret.
    consumes_key = True

    def _tunnel(self, ctx: AuthContext) -> tuple[str, int] | None:
        """The loopback SOCKS address this provider's requests go through."""
        return _proxy_parts(ctx.config.get("proxy"))

    def _tunnel_down(self, ctx: AuthContext) -> str | None:
        """The reason the tunnel is unusable, or ``None`` when it is fine.

        Returns a sentence an operator can act on, naming the command and the
        proxy the entry points at — the two facts a failure message otherwise
        leaves out.
        """
        address = self._tunnel(ctx)
        if address is None:
            return None
        host, port = address
        if _listening(host, port):
            return None
        where = f"socks5://{host}:{port}"
        return (
            f"the Argo SSH tunnel is not listening on {where}; start it with "
            f"'{TUNNEL_COMMAND} auth' (it asks for Duo MFA), then "
            f"'{TUNNEL_COMMAND} start' if the master already exists"
        )

    def _account(self, ctx: AuthContext) -> str:
        """The account name the operator stored, or a refusal saying where it goes."""
        account = (ctx.input_key or "").strip()
        if not account:
            raise AuthExchangeError(
                "Argo authenticates with your ANL account name, and none is "
                "configured: put it in this provider's api_key field (or "
                "export ARGO_API_KEY)"
            )
        return account

    def needs_interactive_setup(self, ctx: AuthContext) -> bool:
        """Whether a human must act before a request can succeed.

        True when the entry declares a local SOCKS proxy with no listener.
        That is the state a person fixes by authenticating — not something a
        retry gets past — and the panel renders it as "needs sign-in" with
        the exchange's message underneath.
        """
        return self._tunnel_down(ctx) is not None

    async def credential(self, ctx: AuthContext) -> str:
        """Return the ANL account name the upstream expects.

        Raises:
            InteractiveSetupRequired: The declared tunnel is not listening.
            AuthExchangeError: No account name is configured.
        """
        # The account is checked first: a missing name is a configuration
        # error on this host, and reporting the tunnel instead would send the
        # operator to a command that does not fix it.
        account = self._account(ctx)
        down = self._tunnel_down(ctx)
        if down is not None:
            raise InteractiveSetupRequired(down)
        return account

    def account_id(self, ctx: AuthContext) -> str | None:
        """The account name, so two providers of the same account group.

        The gateway hashes this before it stores or renders it; it is never
        sent upstream, and it is not a secret — it is the same name the
        operator typed into the key field.
        """
        account = (ctx.input_key or "").strip()
        return account or None

    def describe(self, ctx: AuthContext) -> Mapping[str, Any] | None:
        """The token-free summary the panel renders, or ``None`` when unusable.

        ``None`` while the tunnel is down, so a provider that cannot serve is
        not reported as connected.  No ``expires_at``: the account name does
        not expire, and the panel treats a missing one as "no expiry to show".
        """
        account = (ctx.input_key or "").strip()
        if not account or self._tunnel_down(ctx) is not None:
            return None
        summary: dict[str, Any] = {"subject": account}
        address = self._tunnel(ctx)
        summary["tunnel"] = (
            f"socks5://{address[0]}:{address[1]}" if address else "direct"
        )
        return summary

    # The optional half of the contract stays unimplemented on purpose: there
    # is no record to install, no browser flow, and nothing to revoke.  A
    # sign-out would be stopping the tunnel, which is `argo-tunnel`'s own
    # command and is shared by every provider that uses it.


#: The loader reads exactly this name.
EXCHANGE = ArgoCredential()
