#!/usr/bin/env python3
# Generated from src/llm_rosetta_shim_argonne/tunnel.py -- do not edit here.
# Regenerate with: python scripts/sync_cli_script.py
#
# Standalone by design: standard library only, no llm-rosetta import, so this
# file runs from a download with no install step:
#
#   curl -fsSLO https://raw.githubusercontent.com/Oaklight/llm-rosetta-shim-argonne/main/scripts/argo-tunnel.py
#   python argo-tunnel.py auth pding --target lambda5.cels.anl.gov
"""The SSH tunnel that makes ``apps.inside.anl.gov`` reachable, as a CLI.

The Argo endpoint resolves only inside the Argonne network, and the path in
reaches a jump host that asks for Duo MFA on each new connection.  This module
owns both facts: it keeps one authenticated master connection open, and it
publishes a dynamic (SOCKS5) forward on top of it.

Single file on purpose
----------------------

This module imports only the standard library and nothing else from this
package, so it can be fetched and run without installing anything::

    curl -fsSLO https://raw.githubusercontent.com/Oaklight/llm-rosetta-shim-argonne/main/scripts/argo-tunnel.py
    python argo-tunnel.py auth pding --target lambda5.cels.anl.gov

That copy is generated from this file by ``scripts/sync_cli_script.py`` and a
test fails when the two drift.  The rule that keeps it possible: no relative
import, no ``llm_rosetta`` import, nothing but stdlib at module scope.

Which machine gets authenticated to
-----------------------------------

An ssh destination, not a hostname this tool assembles: ``--target lambda5``
uses the operator's own ``ssh_config`` stanza (its ``HostName``, ``Port``,
``IdentityFile`` and ``ProxyJump``), while ``--target me@host`` is used as
given.  Only when no target is named does the tool build one, from the vendor's
jump host and a host that is documented as a default rather than a fact.

Why SOCKS and not a local port forward
--------------------------------------

``ssh -L 127.0.0.1:PORT:apps.inside.anl.gov:443`` would force every client to
address ``https://127.0.0.1:PORT``, while the upstream certificate names
``apps.inside.anl.gov`` — host-name verification fails, and the gateway exposes
no TLS-verify knob.  A dynamic forward sends the *hostname* to the far side
(RFC 1928, ``ATYPE_DOMAIN``); the client still connects to
``https://apps.inside.anl.gov``, so SNI and certificate verification are the
ordinary ones and the shim's ``base_url`` needs no edit.

Why the prompt lives here
-------------------------

A forward can be added to an already-authenticated session
(``ssh -O forward``), so exactly one command — :meth:`Tunnel.auth` — can block
on a prompt.  Everything else runs with ``BatchMode=yes`` and fails rather than
wait, which is what makes the CLI safe to call from a launcher or a unit file.

State model
-----------

``status`` reports four states, because two of them look alike from a process
list:

===============  =========================================================
``forwarding``   master answers and the SOCKS port completes a greeting
``idle``         master answers, the SOCKS port does not — no forward yet
``wedged``       the control socket exists but does not answer
``absent``       there is no control socket
===============  =========================================================
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import socket
import stat
import struct
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol, Sequence

__all__ = [
    "Tunnel",
    "TunnelConfig",
    "TunnelState",
    "main",
]

#: The hop that reaches the Argo network from outside, and the machine the
#: forward is opened from.  Both are Argonne facts, and they are the pair
#: ``n-getty/argo-shim`` uses.
#:
#: ``logins.cels.anl.gov`` **cannot be logged into**: it exists to be a jump
#: host, and the account authenticates at the far end.  That is why the hop is
#: passed as ``-J`` and the key is made available on it — see
#: :meth:`TunnelConfig.hop_entries`.
DEFAULT_JUMP_HOST = "logins.cels.anl.gov"
DEFAULT_HOST = "homes.cels.anl.gov"

#: The SOCKS port.  ``1080`` is the conventional one and is often already
#: bound; a nearby fixed number keeps the printed ``proxy-url`` stable across
#: restarts, so a provider config written once keeps working.
DEFAULT_PORT = 10801
DEFAULT_BIND = "127.0.0.1"

#: How long the master stays usable after the last multiplexed session ends.
#: This is the interval the operator does not have to authenticate again.
#: Only used when the operator's own ``ssh_config`` says nothing: a
#: ``ControlPersist`` the system set is theirs, and is left alone.
DEFAULT_CONTROL_PERSIST = "8h"

#: Where this tool puts its control socket when ``ssh_config`` puts none.
#: ``%s`` is the data directory: ``$XDG_CONFIG_HOME`` or ``~/.config``.
CONTROL_SOCKET_SUBDIR = "tunnel"

#: ``sockaddr_un.sun_path`` is 108 bytes on Linux and 104 on macOS, so a
#: socket path has a hard ceiling that a deep ``$HOME`` can cross.
_MAX_SOCKET_PATH = 100

#: Watch for a link that went away without closing: a forward over a dead
#: connection still looks bound on this side.
DEFAULT_SERVER_ALIVE_INTERVAL = 15
DEFAULT_SERVER_ALIVE_COUNT_MAX = 4

#: ``ssh -O`` returns this when the command itself fails (no socket, refused).
SSH_FAILURE = 255

#: The subcommand names.  Used to refuse ``auth status``, which reads as two
#: commands and is parsed as one account named ``status``.
_COMMANDS = frozenset({"auth", "start", "stop", "status", "logout", "proxy-url"})

#: A wedged master is one that neither answers nor refuses.  Waiting longer
#: only delays the message the operator needs.
CONTROL_PROBE_TIMEOUT = 15.0


def _ssh_quote(value: str) -> str:
    """Quote one ssh_config argument and reject control characters."""
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise TunnelError("SSH configuration values cannot contain control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _private_dir(path: Path) -> None:
    """Create and validate a directory that only the current user controls."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise TunnelError(
            f"unsafe state directory: {path}; it must be owned by the current user and not be a symlink"
        )
    mode = stat.S_IMODE(info.st_mode)
    if mode != 0o700:
        raise TunnelError(
            f"unsafe state directory: {path} has permissions {mode:03o}; required: 700",
            hint=f"If this directory is dedicated to argo-tunnel, run: chmod 700 -- {shlex.quote(str(path))}",
        )


def _atomic_write(path: Path, content: str) -> None:
    """Atomically replace a private file without following its final symlink."""
    fd, tmp = tempfile.mkstemp(prefix=".argo-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def _state_lock(directory: Path):
    """Serialize profile and forwarding updates across CLI processes."""
    _private_dir(directory)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(directory / "lock", flags, 0o600)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise TunnelError("unsafe state lock")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _read_profile(directory: Path) -> dict[str, Any] | None:
    path = directory / "tunnel" / "profile.json"
    if not path.exists():
        return None
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise TunnelError(f"unsafe profile: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TunnelError(f"cannot read tunnel profile: {exc}") from exc
    if not isinstance(value, dict) or value.get("version") != 1:
        raise TunnelError("unsupported tunnel profile")
    return value


def _write_profile(config: TunnelConfig, *, owned: bool, forward: bool) -> None:
    directory = config.data_dir / "tunnel"
    _private_dir(directory)
    values = asdict(config)
    for key, value in values.items():
        if isinstance(value, Path):
            values[key] = str(value)
    record = {
        "version": 1,
        "config": values,
        "socket": str(config.socket),
        "owned": owned,
        "forward": forward,
    }
    _atomic_write(directory / "profile.json", json.dumps(record) + "\n")


def _owns_master(config: TunnelConfig) -> bool:
    """Whether the answering master is this tool's to close.

    The profile is the record, but it is keyed by socket and a lost or
    rewritten record used to leave the operator unable to log out of a master
    the tool itself had started.  A live ``ssh -M -S <our socket>`` is the
    same fact from the outside, so it counts too.
    """
    profile = _read_profile(config.data_dir)
    if profile and profile.get("socket") == str(config.socket) and profile.get("owned"):
        return True
    return bool(master_processes(config.socket))


def _saved_config(args: argparse.Namespace, config: TunnelConfig) -> TunnelConfig:
    """Use the last successful profile unless a connection identity was supplied."""
    identity = (
        "target",
        "host",
        "user",
        "username",
        "jump",
        "proxy",
        "config",
        "ssh_config",
        "key",
    )
    if args.command == "auth" or any(
        getattr(args, key, None) is not None for key in identity
    ):
        return config
    profile = _read_profile(config.data_dir)
    if not profile:
        return config
    raw = profile.get("config")
    if not isinstance(raw, dict):
        raise TunnelError("invalid tunnel profile")
    path_keys = {
        "key",
        "config_file",
        "control_path",
        "state_dir",
        "ssh_config",
        "socket_override",
    }
    values = {
        key: Path(value) if key in path_keys and value is not None else value
        for key, value in raw.items()
    }
    values["socket_override"] = Path(profile["socket"])
    try:
        saved = TunnelConfig(**values)
    except (TypeError, KeyError, ValueError) as exc:
        raise TunnelError("invalid tunnel profile") from exc
    overrides = {}
    for field in ("bind", "port", "control_persist"):
        if hasattr(args, field):
            overrides[field] = getattr(args, field)
    return replace(saved, **overrides)


class TunnelState:
    """The four states :meth:`Tunnel.status` reports."""

    FORWARDING = "forwarding"
    IDLE = "idle"
    WEDGED = "wedged"
    ABSENT = "absent"


class TunnelError(Exception):
    """An operation could not complete, with the advice the operator needs.

    Attributes:
        hint: One command or fix to print after the message, or ``None``.
    """

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


@dataclass(frozen=True)
class TunnelConfig:
    """Everything the tunnel needs to know about *where* it connects.

    Attributes:
        target: The ssh destination to authenticate to, used verbatim — either
            a ``Host`` alias from ``ssh_config`` (``lambda5``) or a raw
            destination (``someone@lambda5.cels.anl.gov``).  ``None`` builds
            one from :attr:`host` and :attr:`user`.  When set, it replaces both
            and the jump hop is left to ``ssh_config`` unless one is named
            here: an alias usually carries its own ``ProxyJump``, and passing
            ``-J`` would override that.
        host: The machine the forward is made from — the one that can reach
            the API host.  Ignored when :attr:`target` is set.
        jump: The externally reachable hop when one is named outright.  ``None``
            means "undecided": :attr:`effective_jump` then falls back to
            :data:`DEFAULT_JUMP_HOST` for a built destination, and to
            ``ssh_config`` for an alias.
        no_jump: Set when the operator asked for a direct connection
            (``--jump none``).  Distinct from :attr:`jump` being ``None``,
            which is silent rather than a decision.
        user: The account name used on both hops.  Not applied to
            :attr:`target`'s destination, which carries its own if it needs one.
        key: Private key offered on **both** hops.  This is the field that
            makes the default route work: ``-i``/``IdentitiesOnly`` apply to
            the destination only, so ``ssh -J logins.cels.anl.gov …`` never
            offers the work key on the hop, and the hop answers
            ``Permission denied (publickey)`` before any MFA prompt.  The key
            therefore goes into a generated config that names the hop as well.
        config_file: The operator's ssh config, when it is not the default
            ``~/.ssh/config``.  Read *through* by the generated file, so a
            stanza it defines still applies wherever this tool has not
            overridden a field.
        bind: Address the SOCKS port listens on.  ``127.0.0.1`` unless a
            container on the same host has to reach it.
        port: TCP port of the SOCKS listener.
        control_path: The multiplexing socket, when one is named outright.
            ``None`` means "whatever the operator's ``ssh_config`` says, else
            this tool's own location" — see :attr:`socket`.
        control_persist: How long the master outlives its last session, when
            one is named outright.  ``None`` means "``ssh_config``'s value,
            else :data:`DEFAULT_CONTROL_PERSIST`" — see
            :meth:`effective_control_persist`.
        state_dir: Where this tool keeps its socket and its small state file.
            ``None`` means ``$XDG_CONFIG_HOME/llm-rosetta-gateway``, else
            ``~/.config/llm-rosetta-gateway``.
        ssh: The ``ssh`` executable.
        ssh_config: An extra ssh config file to read.  ``ssh`` reads
            ``~/.ssh/config`` on its own; this is for a deployment that keeps
            its settings elsewhere.
    """

    target: str | None = None
    host: str = DEFAULT_HOST
    jump: str | None = None
    no_jump: bool = False
    user: str | None = None
    key: Path | None = None
    config_file: Path | None = None
    socket_override: Path | None = None
    bind: str = DEFAULT_BIND
    port: int = DEFAULT_PORT
    control_path: Path | None = None
    control_persist: str | None = None
    state_dir: Path | None = None
    ssh: str = "ssh"
    ssh_config: Path | None = None

    # -- identity -----------------------------------------------------------

    @property
    def login(self) -> str:
        """The ssh destination: the target verbatim, else ``user@host``.

        A target is passed through unchanged so that an ``ssh_config`` alias
        still resolves — rewriting ``lambda5`` into ``lambda5@lambda5`` would
        address a host that does not exist.
        """
        target = self.effective_target
        if "@" in target:
            existing, _ = target.rsplit("@", 1)
            if self.user and existing != self.user:
                raise ValueError("target user conflicts with --user")
            return target
        return f"{self.user}@{target}" if self.user else target

    @property
    def effective_target(self) -> str:
        """The destination to address: the one named, or the stanza that maps to the default host.

        Argonne accounts reach the default host through an ``ssh_config``
        stanza of their own — ``Host homes`` with a ``ProxyJump`` and a key —
        and addressing the bare host name skips all of it.  So when no target
        is named, a one-label alias (``homes``) is tried first and used only
        if it resolves to the default host; otherwise the host name stands.
        """
        if self.target:
            return self.target
        alias = self.host.split(".", 1)[0]
        if alias != self.host:
            resolved = _ssh_g(self, alias)
            if (resolved.get("hostname") or [""])[0].lower() == self.host.lower():
                return alias
        return self.host

    @property
    def effective_jump(self) -> str | None:
        """The hop to pass as ``-J``, or ``None`` to say nothing and let ssh decide.

        Three cases, and the third is the one that matters:

        1. the operator named a hop — use it;
        2. the operator said ``--jump none`` — pass no ``-J`` at all;
        3. nothing was said.  A built destination (:attr:`target` unset) gets
           the vendor default hop, because building one is this tool's job.  An
           alias gets **nothing**: its ``ssh_config`` stanza usually carries
           its own ``ProxyJump``, and passing ``-J`` would silently replace
           that with our guess.
        """
        if self.jump:
            return self.jump
        if self.no_jump or self.target:
            return None
        if self.effective_target != self.host:
            # An alias normally carries its own ProxyJump; adding -J would
            # replace the operator's route with ours.
            return None
        return DEFAULT_JUMP_HOST

    @property
    def jump_login(self) -> str | None:
        """The jump hop in ``-J`` form.

        The user is prefixed only for a bare host name; a value that already
        carries one (or a nickname) is passed through unchanged.
        """
        hop = self.effective_jump
        if hop is None:
            return None
        if "@" in hop:
            return hop
        return f"{self.user}@{hop}" if self.user else hop

    # -- hop configuration ---------------------------------------------------

    def hop_entries(self) -> dict[str, list[str]]:
        """The ``ssh_config`` blocks the generated file must carry.

        ssh applies ``-i`` and ``IdentitiesOnly`` to the destination only; a
        ``-J`` hop re-reads the configuration on its own and knows nothing
        about the command line.  On Argonne's route that is fatal: the hop
        offers ``~/.ssh/id_ed25519``, the login host refuses it, and the
        connection dies *before* the MFA prompt — which is exactly the
        ``Permission denied (publickey)`` seen when no account name was given.

        So every name that participates in the connection gets the same
        ``User`` and ``IdentityFile`` written down, in a file the hop also
        reads.

        Returns:
            ``Host`` pattern → directives, one entry per participating name.
            Empty when there is nothing to say: no key and no user means the
            operator's own configuration is already the whole story.
        """
        # The destination is named as the operator named it: an alias must stay
        # an alias, because its stanza is what supplies HostName and route.
        names = [self.effective_target.rsplit("@", 1)[-1]]
        hop = self.effective_jump
        if hop:
            for item in hop.split(","):
                name = item.rsplit("@", 1)[-1].split(":", 1)[0]
                if name not in names:
                    names.append(name)

        entries: dict[str, list[str]] = {}
        for name in names:
            # Anything the operator's own stanza already says is reused rather
            # than demanded again: the hop re-reads configuration on its own,
            # so whatever is detected for it has to be written down for it.
            detected_user, detected_keys = (
                (None, []) if self.user and self.key else _detected_identity(self, name)
            )
            user = self.user or detected_user
            keys = [self.key] if self.key else detected_keys
            directives: list[str] = []
            if user:
                cleaned = user.replace("-", "").replace("_", "").replace(".", "")
                if not cleaned.isalnum():
                    raise TunnelError("invalid SSH username")
                directives.append(f"    User {user}")
            if keys:
                for key in keys:
                    directives.append(f"    IdentityFile {_ssh_quote(str(key))}")
                directives.append("    IdentitiesOnly yes")
            if directives:
                entries[name] = directives
        return entries

    @property
    def generated_config(self) -> Path:
        """Path of the generated ``ssh_config`` this tool owns.

        Derived like the socket but **without** asking ssh anything: the
        socket may come from a system ``ControlPath``, which is read with an
        ``ssh -G`` probe, and that probe must not in turn depend on this file.
        """
        return self.data_dir / CONTROL_SOCKET_SUBDIR / (self._socket_name() + ".config")

    def write_config_file(self) -> Path | None:
        """Write the generated config, or return ``None`` when it is empty.

        The operator's own file is pulled in with ``Include`` *after* this
        tool's blocks: ssh takes the first value it sees for a directive, so
        what is written here wins where it speaks, and everything else — the
        alias's ``HostName``, a ``ProxyJump``, ciphers — still applies.

        Returns:
            The path written, ready for ``-F``.
        """
        entries = self.hop_entries()
        source = (
            self.config_file or self.ssh_config or (Path.home() / ".ssh" / "config")
        )
        if not entries:
            # Nothing to add: read the operator's file directly, so ``-F``
            # stays a single, predictable indirection.
            return source if self.config_file or self.ssh_config else None

        lines = [
            "# Generated by argo-tunnel -- do not edit.",
            "# The hop re-reads configuration on its own, so it gets the same",
            "# User and IdentityFile as the destination.",
            "",
        ]
        for name, directives in entries.items():
            if not name or any(c in name for c in "*?![] \t\r\n"):
                raise TunnelError("invalid SSH host alias")
            lines.append(f"Host {name}")
            lines.extend(directives)
            lines.append("")
        lines.append("Host *")
        if source.is_file():
            lines.append(f"Include {_ssh_quote(str(source))}")
        elif self.config_file:
            raise TunnelError(f"SSH config not found: {source}")
        if (
            not self.config_file
            and not self.ssh_config
            and Path("/etc/ssh/ssh_config").is_file()
        ):
            lines.append('Include "/etc/ssh/ssh_config"')
        lines.append("")

        path = self.generated_config
        _private_dir(path.parent)
        _atomic_write(path, "\n".join(lines))
        return path

    # -- placement ----------------------------------------------------------

    @property
    def data_dir(self) -> Path:
        """This tool's own state directory.

        The gateway's data directory is the natural home for a socket that
        exists to serve it, and it is the one location an operator already
        knows how to back up, move or remove.  ``$XDG_CONFIG_HOME`` is
        honoured because that is where the gateway puts its config.
        """
        if self.state_dir is not None:
            return Path(self.state_dir).expanduser()
        base = os.environ.get("XDG_CONFIG_HOME", "").strip()
        root = Path(base).expanduser() if base else Path.home() / ".config"
        return root / "llm-rosetta-gateway"

    def _socket_name(self) -> str:
        """A file name that is unique to this target.

        Short on purpose: the full path must fit in ``sun_path``.  The digest
        covers everything that makes two tunnels different — the target, the
        jump hop, the login and the port — so two configurations never share
        a name.
        """
        identity = f"{self.login}|{self.effective_jump or ''}|{self.port}|{self.bind}"
        return "ssh-" + hashlib.sha256(identity.encode()).hexdigest()[:12]

    @property
    def socket(self) -> Path:
        """The control socket path this tool uses.

        Resolution order, widest first — the operator's own setup wins:

        1. an explicitly configured path (:attr:`control_path`);
        2. ``ControlPath`` from ``ssh_config``, with ssh's tokens expanded by
           ssh itself, when it names one;
        3. this tool's own location under :attr:`data_dir`.

        The second case is why the path is read from ``ssh -G`` rather than
        computed: a system ``ControlMaster auto`` with
        ``ControlPath ~/.ssh/cm-%C`` is a deliberate choice, and a tool that
        invented its own path would leave that master unmanaged — status would
        report ``absent`` while an authenticated session sat right there.

        Returns:
            The socket path.  A path that would exceed the ``sun_path`` limit
            is replaced by a hashed one under the same directory.
        """
        if self.socket_override is not None:
            return Path(self.socket_override)
        if self.control_path is not None:
            return Path(self.control_path).expanduser()

        system = self.ssh_config_path("controlpath")
        if system and system.lower() != "none":
            path = Path(os.path.expanduser(system))
            if len(os.fsencode(path)) > _MAX_SOCKET_PATH:
                raise TunnelError(
                    "SSH ControlPath is too long; configure a shorter path"
                )
            return path

        directory = self.data_dir / CONTROL_SOCKET_SUBDIR
        candidate = directory / self._socket_name()
        if len(os.fsencode(candidate)) > _MAX_SOCKET_PATH:
            raise TunnelError(
                "control socket path is too long; set --state-dir to a shorter private directory"
            )
        return candidate

    def ssh_config_path(self, keyword: str) -> str | None:
        """Ask ssh what *keyword* resolves to for this target.

        ``ssh -G`` prints the effective configuration after every file and
        every ``-o`` has been applied, which is the only reliable way to see a
        system-wide choice.  The probe runs at most once per process per
        configuration.

        Args:
            keyword: A lowercase directive name, e.g. ``"controlpath"``.

        Returns:
            The value, or ``None`` when ssh says nothing useful — the binary
            is missing, the target does not resolve, or the value is ssh's own
            "unset" spelling.
        """
        resolved = _resolved_ssh_config(self)
        return resolved.get(keyword) or None

    def effective_control_persist(self) -> str:
        """How long the master survives, as this run should set it.

        Order: an explicitly configured value, then ``ssh_config``'s, then
        :data:`DEFAULT_CONTROL_PERSIST`.  ``ssh_config``'s ``ControlPersist``
        is honoured for the same reason ``ControlPath`` is: it is the
        operator's statement about how long their own session stays good for,
        and this tool's job is to reuse that session, not to overrule it.

        Returns:
            An ssh ``ControlPersist`` value: a time with a unit, or ``"yes"``.
        """
        if self.control_persist is not None:
            return self.control_persist
        system = self.ssh_config_path("controlpersist")
        # ``ssh -G`` prints ``controlpersist no`` when nobody configured one —
        # it is the default, not a choice — so it means "unset" here.  A
        # configured value comes back normalised to seconds (``4h`` → 14400),
        # which ``-o`` accepts unchanged.
        if system and system.lower() != "no":
            return system
        return DEFAULT_CONTROL_PERSIST

    @property
    def proxy_url(self) -> str:
        """The value an operator pastes into a provider's ``proxy`` field."""
        return f"socks5://{self.bind}:{self.port}"

    @property
    def forward_spec(self) -> str:
        """The ``-D`` argument, in the form ssh expects."""
        return f"{self.bind}:{self.port}"

    # -- construction -------------------------------------------------------

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> TunnelConfig:
        """Build a config from ``ARGO_TUNNEL_*`` variables.

        Args:
            env: The environment to read; defaults to :data:`os.environ`.

        Returns:
            The merged config.  An empty variable counts as unset, so a
            deployment can blank a default rather than unset it.

        Raises:
            TunnelError: ``ARGO_TUNNEL_PORT`` is not a port number.
        """
        source = os.environ if env is None else env

        def get(name: str) -> str | None:
            value = source.get(name, "").strip()
            return value or None

        config = cls()
        if target := get("ARGO_TUNNEL_TARGET"):
            config = replace(config, target=target)
        if host := get("ARGO_TUNNEL_HOST"):
            config = replace(config, host=host)
        if (jump := get("ARGO_TUNNEL_JUMP")) is not None:
            direct = jump.lower() == "none"
            config = replace(config, jump=None if direct else jump, no_jump=direct)
        if user := get("ARGO_TUNNEL_USER"):
            config = replace(config, user=user)
        if bind := get("ARGO_TUNNEL_BIND"):
            config = replace(config, bind=bind)
        if port := get("ARGO_TUNNEL_PORT"):
            try:
                config = replace(config, port=int(port))
            except ValueError as exc:
                raise TunnelError(
                    f"ARGO_TUNNEL_PORT is not a port number: {port!r}"
                ) from exc
        if socket_path := get("ARGO_TUNNEL_SOCKET"):
            config = replace(config, control_path=Path(socket_path).expanduser())
        if persist := get("ARGO_TUNNEL_CONTROL_PERSIST"):
            config = replace(config, control_persist=persist)
        if state_dir := get("ARGO_TUNNEL_STATE_DIR"):
            config = replace(config, state_dir=Path(state_dir).expanduser())
        return config


@dataclass(frozen=True)
class CommandResult:
    """What a subprocess said, in the three pieces callers branch on."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        """Whether the command exited successfully."""
        return self.returncode == 0

    @property
    def output(self) -> str:
        """stdout and stderr together, for messages."""
        return (self.stdout + self.stderr).strip()


class Runner(Protocol):
    """Runs an argv and reports its result.  Injected so tests never SSH."""

    def __call__(
        self, argv: Sequence[str], *, interactive: bool = False
    ) -> CommandResult:
        """Execute *argv*.

        Args:
            argv: The command, already split.
            interactive: When true, the child inherits the terminal so a
                prompt can be answered.  When false, stdin is closed and ssh
                runs in batch mode, so it fails instead of waiting.
        """


def run_subprocess(argv: Sequence[str], *, interactive: bool = False) -> CommandResult:
    """Execute *argv* as the real :class:`Runner`.

    A timeout is reported as :data:`SSH_FAILURE` with a message on stderr, so
    a hung control socket becomes a state rather than a hung CLI.
    """
    try:
        completed = subprocess.run(
            list(argv),
            check=False,
            text=True,
            capture_output=not interactive,
            stdin=None if interactive else subprocess.DEVNULL,
            timeout=None if interactive else CONTROL_PROBE_TIMEOUT,
        )
    except FileNotFoundError as exc:
        return CommandResult(SSH_FAILURE, "", f"cannot run {argv[0]}: {exc}")
    except subprocess.TimeoutExpired:
        return CommandResult(
            SSH_FAILURE,
            "",
            f"timed out after {CONTROL_PROBE_TIMEOUT:.0f}s waiting for {argv[0]}",
        )
    return CommandResult(
        completed.returncode,
        completed.stdout or "",
        completed.stderr or "",
    )


#: ``ssh -G`` answers are cached per target: the resolution is a property of
#: the configuration, not of the call, and ``status`` asks for two keys.
_SSH_G_CACHE: dict[tuple[str, ...], Any] = {}


def _ssh_g(config: TunnelConfig, target: str) -> dict[str, list[str]]:
    """Run ``ssh -G`` for *target* and return every directive, in order.

    A directive such as ``IdentityFile`` may appear several times and the
    order is the order ssh would try them, so the values are kept as a list.
    The operator's config file is the only ``-F``; see
    :func:`_resolved_ssh_config` for why the generated one is not used here.
    """
    key = (config.ssh, str(config.config_file or config.ssh_config or ""), target)
    if key in _SSH_G_CACHE:
        return _SSH_G_CACHE[key]
    argv = [config.ssh]
    if config.config_file or config.ssh_config:
        argv += ["-F", str(config.config_file or config.ssh_config)]
    argv += ["-G", target]
    values: dict[str, list[str]] = {}
    try:
        done = subprocess.run(
            argv, check=False, text=True, capture_output=True, stdin=subprocess.DEVNULL
        )
    except OSError:
        _SSH_G_CACHE[key] = values
        return values
    if done.returncode != 0:
        _SSH_G_CACHE[key] = values
        return values
    for line in done.stdout.splitlines():
        name, _, value = line.partition(" ")
        if name and value:
            values.setdefault(name.lower(), []).append(value.strip())
    _SSH_G_CACHE[key] = values
    return values


def _detected_identity(
    config: TunnelConfig, name: str
) -> tuple[str | None, list[Path]]:
    """What the operator's own configuration already says about *name*.

    This is what makes ``argo-tunnel auth`` work with no flags: the key and the
    account are read from the stanza that already exists, instead of being
    demanded again on the command line.
    """
    resolved = _ssh_g(config, name)
    user = (resolved.get("user") or [None])[0]
    keys = [
        Path(os.path.expanduser(value))
        for value in resolved.get("identityfile", [])
        if value.lower() != "none"
    ]
    return user, [key for key in keys if key.is_file()]


def _resolved_ssh_config(config: TunnelConfig) -> dict[str, str]:
    """The effective ssh configuration for this target, as ``ssh -G`` sees it.

    ``ssh -G`` prints the configuration ssh would use without connecting, so it
    is the only way to read a system-wide ``ControlMaster``/``ControlPath``/
    ``ControlPersist`` — the values the operator set in ``~/.ssh/config`` or
    ``/etc/ssh/ssh_config``.

    Args:
        config: Supples the target, the jump hop and any extra config file.

    Returns:
        Lowercase directive → value.  An empty mapping when ssh cannot be
        asked; every caller then falls back to this tool's own default.
    """
    key = (
        config.ssh,
        config.login,
        config.jump_login or "",
        str(config.config_file or config.ssh_config or ""),
        str(config.key or ""),
        config.user or "",
    )
    cached = _SSH_G_CACHE.get(key)
    if cached is not None:
        return cached

    argv = [config.ssh]
    # The operator's own file, never the generated one.  What is asked here —
    # ``ControlPath`` and ``ControlPersist`` — is the operator's choice, and
    # the generated file deliberately sets neither.  Reading it here would
    # also close a cycle: this probe is what :attr:`TunnelConfig.socket`
    # needs, and the generated file lives beside that socket.
    if config.config_file or config.ssh_config:
        argv += ["-F", str(config.config_file or config.ssh_config)]
    argv += ["-G"]
    if hop := config.jump_login:
        argv += ["-J", hop]
    elif config.no_jump:
        argv += ["-o", "ProxyJump=none"]
    argv.append(config.login)

    resolved: dict[str, str] = {}
    try:
        done = subprocess.run(
            argv, check=False, text=True, capture_output=True, stdin=subprocess.DEVNULL
        )
    except OSError:
        _SSH_G_CACHE[key] = resolved
        return resolved
    if done.returncode != 0:
        _SSH_G_CACHE[key] = resolved
        return resolved
    for line in done.stdout.splitlines():
        name, _, value = line.partition(" ")
        if name and value:
            resolved[name.lower()] = value.strip()
    _SSH_G_CACHE[key] = resolved
    return resolved


def probe_socks(bind: str, port: int, *, timeout: float = 3.0) -> bool:
    """Whether a SOCKS5 server answers on *bind*:*port*.

    Only the method-negotiation greeting is sent: enough to prove the port is
    a live SOCKS listener rather than any process that happens to hold it, and
    cheap enough to run on every ``status``.

    Args:
        bind: Address to connect to.
        port: Port to connect to.
        timeout: Seconds to wait for the greeting.

    Returns:
        ``True`` when the peer answers the greeting with a supported method.
    """
    try:
        with socket.create_connection((bind, port), timeout=timeout) as sock:
            sock.sendall(struct.pack("BBB", 0x05, 0x01, 0x00))
            reply = sock.recv(2)
    except OSError:
        return False
    return len(reply) == 2 and reply[0] == 0x05 and reply[1] != 0xFF


def master_processes(socket_path: Path, proc_root: Path | str = "/proc"):
    """Find live ssh masters that a given control socket path belongs to.

    The profile records which masters this tool created, and ``logout``
    refuses to close one it did not.  That rule needs a fallback: the socket
    path is a name in ``/proc/<pid>/cmdline`` on Linux, so a master still
    running with ``-M -S <socket>`` is one the tool started, even when the
    profile was lost or written under a different selection.

    Args:
        socket_path: The control socket to look for.
        proc_root: Where to look; a parameter so tests need no real process.

    Returns:
        ``(pid, command)`` for each match, oldest pid first.  Empty when the
        platform does not expose process arguments, or nobody matches.
    """
    wanted = str(socket_path)
    matches = []
    root = Path(proc_root)
    try:
        entries = list(root.iterdir())
    except OSError:
        return matches
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [part for part in raw.decode("utf-8", "replace").split("\0") if part]
        if "-M" not in argv:
            continue
        for index, token in enumerate(argv[:-1]):
            if token == "-S" and argv[index + 1] == wanted:
                matches.append((int(entry.name), " ".join(argv)))
                break
    return sorted(matches)


def listener_owner(bind: str, port: int) -> str | None:
    """Describe the process holding *bind*:*port*, if the OS will say.

    A bound port is the one failure a caller cannot fix by retrying, and the
    operator needs the pid to act on it.  ``ss`` is present on any modern
    Linux; when it is not, or it cannot see the owner, the answer is ``None``
    and the caller prints the port alone.

    Args:
        bind: Address the port is expected on.
        port: TCP port.

    Returns:
        ``"<command> (pid=<pid>)"``, or ``None``.
    """
    try:
        done = subprocess.run(
            ["ss", "-lntpH", f"sport = :{port}"],
            check=False,
            text=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in done.stdout.splitlines():
        if (
            f"127.0.0.1:{port}" not in line
            and f"{bind}:{port}" not in line
            and f"0.0.0.0:{port}" not in line
        ):
            continue
        users = re.search(r'\(\("([^"]+)",pid=(\d+)', line)
        if users:
            return f"{users.group(1)} (pid={users.group(2)})"
    return None


class Tunnel:
    """The operations the CLI exposes, over an injected runner.

    Attributes:
        config: Where the tunnel connects.
        runner: How a command is executed.
        prober: How the SOCKS port is checked; injectable for tests.
    """

    def __init__(
        self,
        config: TunnelConfig,
        runner: Runner = run_subprocess,
        prober: Any = probe_socks,
    ) -> None:
        self.config = config
        self.runner = runner
        self.prober = prober

    # -- argv builders ------------------------------------------------------

    def _config_file_for_commands(self) -> Path | None:
        """The ``-F`` path every command uses, generated or the operator's.

        Regenerated per call rather than cached: the file is small, and letting
        it go stale after an option changes would send an old key to the hop.
        """
        return self.config.write_config_file() or self.config.ssh_config

    def _transport_args(self, *, batch: bool = True) -> list[str]:
        """Options every command shares: the socket and the hop.

        ``BatchMode=yes`` is the difference between "fail" and "sit at a
        prompt" for the non-interactive commands, so it is set here once and
        cleared only by :meth:`auth`.
        """
        argv = [self.config.ssh]
        # ``-F`` here and in the ``ssh -G`` probe: the socket and the persist
        # value are read from one configuration file, and the connection must
        # be made under the same one, or the master would land somewhere the
        # probe never looked.  The generated file is what carries the key to
        # the hop — see :meth:`TunnelConfig.hop_entries`.
        if config_file := self._config_file_for_commands():
            argv += ["-F", str(config_file)]
        if batch:
            argv += ["-o", "BatchMode=yes"]
        argv += ["-S", str(self.config.socket)]
        if hop := self.config.jump_login:
            argv += ["-J", hop]
        elif self.config.no_jump:
            argv += ["-o", "ProxyJump=none"]
        return argv

    def _forward_args(self) -> list[str]:
        """The forwarding options and the link watchdog."""
        return [
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            f"ServerAliveInterval={DEFAULT_SERVER_ALIVE_INTERVAL}",
            "-o",
            f"ServerAliveCountMax={DEFAULT_SERVER_ALIVE_COUNT_MAX}",
        ]

    # -- state --------------------------------------------------------------

    def port_conflict_message(self) -> str:
        """Say which port is taken, and by whom when the OS knows."""
        owner = listener_owner(self.config.bind, self.config.port)
        where = f"{self.config.bind}:{self.config.port}"
        if owner:
            return f"SOCKS port {where} is already in use by {owner}"
        return f"SOCKS port {where} is already in use"

    def master_state(self) -> str:
        """Ask the control socket whether a master is there.

        Returns:
            :attr:`TunnelState.IDLE`-family values are not used here: the
            result is ``"alive"``, :attr:`TunnelState.ABSENT` or
            :attr:`TunnelState.WEDGED`.
        """
        argv = self._transport_args() + ["-O", "check", self.config.login]
        result = self.runner(argv)
        if result.returncode == 0:
            return "alive"
        # ssh says "No such file or directory" for a socket that was never
        # there.  Anything else — refused, timeout, garbage — means the file
        # exists but the master behind it does not answer.
        if "no such file" in result.output.lower():
            return TunnelState.ABSENT
        return TunnelState.WEDGED

    def status(self) -> str:
        """The single state a caller acts on.

        Returns:
            One of :class:`TunnelState`.  The forward is probed only when the
            master answers, so an absent master is never reported as a port
            problem.
        """
        master = self.master_state()
        if master != "alive":
            return master
        record = _read_profile(self.config.data_dir)
        if (
            record
            and record.get("socket") == str(self.config.socket)
            and record.get("forward")
        ):
            if self.prober(self.config.bind, self.config.port):
                return TunnelState.FORWARDING
            return TunnelState.IDLE
        if self.prober(self.config.bind, self.config.port):
            return TunnelState.WEDGED  # An unowned listener is not our forward.
        return TunnelState.IDLE

    # -- commands -----------------------------------------------------------

    def auth(self) -> CommandResult:
        """Open the master connection.  The only step that may prompt.

        Returns:
            The result of the foreground ssh call.

        Raises:
            TunnelError: A master already answers, or ssh failed.  Re-running
                auth over a live master would ask for a second Duo approval
                for no benefit.
        """
        state = self.master_state()
        if state == "alive":
            return CommandResult(0, "reusing existing SSH master")
        if state == TunnelState.WEDGED:
            raise TunnelError(
                f"the control socket {self.config.socket} exists but does not answer",
                hint="inspect the socket and master; it will not be removed automatically",
            )
        self.config.socket.parent.mkdir(parents=True, exist_ok=True)
        # ``-M`` only marks this connection as a master.  When the operator's
        # ssh_config already says ``ControlMaster auto`` that is redundant but
        # harmless; what must not be forced is ``ControlPath`` — it is passed
        # through ``-S`` on every command, so the master and the clients can
        # never disagree about which socket is in play.
        argv = self._transport_args(batch=False) + [
            "-M",
            "-N",
            "-f",
            "-o",
            f"ControlPersist={self.config.effective_control_persist()}",
        ]
        argv += self._forward_args()
        argv.append(self.config.login)
        result = self.runner(argv, interactive=True)
        if not result.ok:
            # Interactive: ssh wrapped its diagnostics onto the terminal, so
            # ``output`` is usually empty here.  Reporting an empty string —
            # which is what an earlier version did — tells the operator
            # nothing; name the likely cause and the flag that fixes it.
            detail = result.output.strip()
            if detail:
                message = f"ssh could not open the master connection: {detail}"
            else:
                message = (
                    "ssh could not open the master connection (see its message "
                    "above); if it was refused before any MFA prompt, the hop "
                    "did not accept the key — pass --key"
                )
            raise TunnelError(message)
        return result

    def start(self) -> CommandResult:
        """Publish the SOCKS forward on the existing master.

        Returns:
            The result of the ``ssh -O forward`` call; a forward that was
            already up returns a synthetic success without touching ssh.

        Raises:
            TunnelError: No master answers, so there is nothing to forward
                over and the caller must authenticate first.
        """
        state = self.status()
        if state == TunnelState.FORWARDING:
            return CommandResult(0, "forward already up")
        if state == TunnelState.WEDGED:
            raise TunnelError(
                self.port_conflict_message(),
                hint=(
                    "Stop that listener. If it is an SSH master this tool no longer "
                    "tracks, kill its pid; otherwise pick another port with --port."
                ),
            )
        if state == TunnelState.ABSENT:
            raise TunnelError(
                f"no master connection on {self.config.socket}",
                hint="run 'argo-tunnel auth' first",
            )
        argv = (
            self._transport_args()
            + self._forward_args()
            + [
                "-O",
                "forward",
                "-D",
                self.config.forward_spec,
                self.config.login,
            ]
        )
        result = self.runner(argv)
        if not result.ok:
            # ``-O forward`` is idempotent: a master that already forwards this
            # port answers success (measured).  So a bound port here is another
            # process's, and naming it is more use than ssh's generic text.
            if self.prober(self.config.bind, self.config.port):
                raise TunnelError(self.port_conflict_message())
            raise TunnelError(f"ssh could not add the forward: {result.output}")
        return result

    def stop(self) -> CommandResult:
        """Cancel the SOCKS forward and leave the master authenticated.

        The state is read first rather than asking ssh to cancel blind: ssh
        answers a cancel for a forward that was never added with a message
        that is also its generic failure text, so the only reliable way to
        tell "already stopped" from "the cancel failed" is to not send it.

        Returns:
            The result of the ``ssh -O cancel`` call; a tunnel that is not
            forwarding is a no-op.
        """
        state = self.status()
        if state == TunnelState.ABSENT:
            return CommandResult(0, "no master")
        if state == TunnelState.IDLE:
            return CommandResult(0, "forward was not up")
        if state == TunnelState.WEDGED:
            raise TunnelError(
                f"the control socket {self.config.socket} does not answer",
                hint="inspect the socket and master; it will not be removed automatically",
            )
        argv = self._transport_args() + [
            "-O",
            "cancel",
            "-D",
            self.config.forward_spec,
            self.config.login,
        ]
        result = self.runner(argv)
        if not result.ok:
            raise TunnelError(f"ssh could not cancel the forward: {result.output}")
        return result

    def logout(self) -> CommandResult:
        """Drop the master, and with it any forward on it.

        Returns:
            The result of ``ssh -O exit``; no master is a no-op.
        """
        if self.master_state() != "alive":
            return CommandResult(0, "no master")
        argv = self._transport_args() + ["-O", "exit", self.config.login]
        result = self.runner(argv)
        if not result.ok:
            raise TunnelError(f"ssh could not close the master: {result.output}")
        return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    epilog = (
        "Environment: ARGO_TUNNEL_TARGET, ARGO_TUNNEL_HOST, "
        "ARGO_TUNNEL_JUMP (or 'none'), ARGO_TUNNEL_USER, ARGO_TUNNEL_BIND, "
        "ARGO_TUNNEL_PORT, ARGO_TUNNEL_SOCKET, ARGO_TUNNEL_CONTROL_PERSIST, "
        "ARGO_TUNNEL_STATE_DIR."
    )
    # The connection options are accepted before *and* after the subcommand.
    # A launcher writes them once at the end of the line ("start --port N"),
    # while a script may prefer them up front; one parser serving both beats
    # a rule the operator has to remember.
    # ``SUPPRESS``, not ``None``: a subparser built from the same parent would
    # otherwise write its own default over a value the operator gave *before*
    # the subcommand, so ``--port 9 status`` would report port 10801.  With no
    # default, an option that was never given leaves no attribute at all, and
    # :func:`_config_from_args` reads each one through ``getattr``.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--target",
        "-T",
        default=argparse.SUPPRESS,
        metavar="DEST",
        help=(
            "ssh destination, used verbatim: an ssh_config alias ('lambda5') "
            "or a raw one ('lambda5.cels.anl.gov'). The route that reaches "
            f"the Argo network is {DEFAULT_HOST} via {DEFAULT_JUMP_HOST}"
        ),
    )
    common.add_argument(
        "--key",
        "-i",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help=(
            "private key, offered on the jump hop as well as the destination "
            "(-i alone would reach only the destination)"
        ),
    )
    common.add_argument(
        "--config",
        "-F",
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="ssh config file to read (default: ~/.ssh/config)",
    )
    common.add_argument(
        "--proxy",
        default=argparse.SUPPRESS,
        metavar="HOP",
        help="SSH jump host, equivalent to -J (or 'none' for direct)",
    )
    common.add_argument(
        "--host",
        default=argparse.SUPPRESS,
        help=f"machine to build a destination from, when no --target is given (default: {DEFAULT_HOST})",
    )
    common.add_argument(
        "--jump",
        default=argparse.SUPPRESS,
        metavar="HOP",
        help=(
            "externally reachable hop, or 'none' for a direct connection; "
            "with --target, omitting it leaves ssh_config's ProxyJump alone"
        ),
    )
    common.add_argument(
        "--user",
        default=argparse.SUPPRESS,
        help=(
            "account name: the login prefix on --target, and both hops' user "
            "on a built destination"
        ),
    )
    common.add_argument(
        "--bind",
        default=argparse.SUPPRESS,
        help=f"SOCKS bind address (default: {DEFAULT_BIND})",
    )
    common.add_argument(
        "--port",
        type=int,
        default=argparse.SUPPRESS,
        help=f"SOCKS port (default: {DEFAULT_PORT})",
    )
    common.add_argument(
        "--control-persist",
        default=argparse.SUPPRESS,
        help=(
            "how long the master survives idle; defaults to the operator's "
            f"ssh_config ControlPersist, else {DEFAULT_CONTROL_PERSIST}"
        ),
    )
    common.add_argument(
        "--state-dir",
        default=argparse.SUPPRESS,
        help=(
            "where this tool keeps its socket when ssh_config names none "
            "(default: $XDG_CONFIG_HOME/llm-rosetta-gateway)"
        ),
    )
    common.add_argument(
        "--ssh-config",
        default=argparse.SUPPRESS,
        help="extra ssh config file to read (ssh reads ~/.ssh/config itself)",
    )
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="report status as JSON instead of one line",
    )

    parser = argparse.ArgumentParser(
        prog="argo-tunnel",
        description=(
            "Own the SSH master connection and the SOCKS5 forward that make "
            "the Argo API host reachable."
        ),
        epilog=epilog,
        parents=[common],
    )

    def _with_username(sub: argparse.ArgumentParser) -> argparse.ArgumentParser:
        """Give a subcommand ssh's positional account name.

        It cannot live on the shared parent: there, ``argo-tunnel auth`` would
        parse ``auth`` as the username and complain that ``auth`` is not a
        subcommand.  On the subparser the subcommand is already consumed.
        """
        sub.add_argument(
            "username",
            nargs="?",
            default=argparse.SUPPRESS,
            metavar="USERNAME",
            help="account name on both hops (same as --user)",
        )
        return sub

    sub = parser.add_subparsers(dest="command")
    _with_username(
        sub.add_parser(
            "auth",
            parents=[common],
            help=(
                "open the master connection and add the forward "
                "(this is the MFA prompt)"
            ),
        )
    )
    sub.add_parser(
        "start", parents=[common], help="publish the SOCKS forward on the master"
    )
    sub.add_parser("stop", parents=[common], help="cancel the forward, keep the master")
    sub.add_parser(
        "status", parents=[common], help="report forwarding / idle / wedged / absent"
    )
    sub.add_parser("logout", parents=[common], help="drop the master")
    sub.add_parser(
        "proxy-url",
        parents=[common],
        help="print the socks5:// URL for a provider config",
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> TunnelConfig:
    """Merge the command line over the environment.

    Every option is read through ``getattr`` because the parser suppresses
    unset defaults: an absent option and an option given before the
    subcommand must both end up as "no override", and the subparser must not
    be able to blank the one that came first.

    Args:
        args: The parsed namespace.

    Returns:
        The effective config.

    Raises:
        TunnelError: An environment value cannot be used.
        ValueError: ``--target`` names an account and ``--user`` names another.
    """
    config = TunnelConfig.from_env()
    overrides: dict[str, Any] = {}
    if target := getattr(args, "target", None):
        overrides["target"] = target
    if host := getattr(args, "host", None):
        overrides["host"] = host
    proxy = getattr(args, "proxy", None)
    jump = getattr(args, "jump", None)
    if proxy and jump and proxy != jump:
        raise ValueError("--proxy conflicts with --jump")
    if hop := proxy or jump:
        direct = hop.lower() == "none"
        overrides["jump"] = None if direct else hop
        overrides["no_jump"] = direct
    # The account name is accepted in ssh's own position ("auth pding") and as
    # a flag; the positional form is the one an operator reaches for, and the
    # flag wins only because it is explicit.
    username = getattr(args, "username", None)
    if username in _COMMANDS:
        # "argo-tunnel auth status" is two commands written as one line; ssh
        # would read it as an account, and the failure would be a login error
        # that says nothing about the typo.
        raise TunnelError(
            f"{username!r} is a subcommand, not an account name",
            hint=(
                f"run 'argo-tunnel {username}'; "
                f"to name an account, pass --user {username}"
            ),
        )
    if user := getattr(args, "user", None) or username:
        overrides["user"] = user
    if key := getattr(args, "key", None):
        overrides["key"] = Path(key).expanduser()
    if config_file := getattr(args, "config", None):
        overrides["config_file"] = Path(config_file).expanduser()
    if bind := getattr(args, "bind", None):
        overrides["bind"] = bind
    if (port := getattr(args, "port", None)) is not None:
        overrides["port"] = port
    if persist := getattr(args, "control_persist", None):
        overrides["control_persist"] = persist
    if state_dir := getattr(args, "state_dir", None):
        overrides["state_dir"] = Path(state_dir).expanduser()
    if ssh_config := getattr(args, "ssh_config", None):
        overrides["ssh_config"] = Path(ssh_config).expanduser()
    result = replace(config, **overrides) if overrides else config
    _ = result.login  # Validate a username explicitly supplied twice.
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Arguments without the program name; defaults to ``sys.argv``.

    Returns:
        A process exit code: ``0`` success, ``1`` the operation did not
        complete, ``2`` a usage error.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2

    try:
        config = _config_from_args(args)
    except (TunnelError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if isinstance(exc, TunnelError) and exc.hint:
            print(f"hint: {exc.hint}", file=sys.stderr)
        return 1

    try:
        config = _saved_config(args, config)
    except TunnelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    tunnel = Tunnel(config)

    if args.command == "proxy-url":
        print(config.proxy_url)
        return 0

    if args.command == "status":
        state = tunnel.status()
        if getattr(args, "json", False):
            import json

            print(
                json.dumps(
                    {
                        "state": state,
                        "proxy_url": config.proxy_url,
                        "socket": str(config.socket),
                        # ``target`` is the resolved ssh destination, which
                        # is what an operator reads back to confirm what the
                        # next ``auth`` will actually connect to.
                        "target": config.login,
                        "jump": config.jump_login,
                    }
                )
            )
        else:
            print(f"{state}\t{config.proxy_url}\t{config.socket}")
        # A forwarding tunnel is the only state a caller can act on.
        return 0 if state == TunnelState.FORWARDING else 1

    actions = {
        "auth": tunnel.auth,
        "start": tunnel.start,
        "stop": tunnel.stop,
        "logout": tunnel.logout,
    }
    try:
        with _state_lock(config.data_dir / "tunnel"):
            if args.command == "auth":
                previous = tunnel.master_state()
                if previous == TunnelState.WEDGED:
                    raise TunnelError(
                        "SSH control socket does not answer; inspect it manually"
                    )
                # Publishing a forward needs the master, and only a missing
                # one needs the MFA prompt — so the port is checked first, and
                # only for the case that would authenticate: an MFA spent on a
                # connection that cannot publish is wasted.
                #
                # A master that does answer takes the same path whether or not
                # this selection recorded the forward.  The record is keyed by
                # socket, and changing ``--target`` or ``--config`` moves the
                # socket while the forward stays where it is; ``ssh -O forward``
                # is idempotent (measured), so asking it costs one round trip
                # and never a second Duo approval.
                if previous != "alive":
                    if tunnel.prober(config.bind, config.port):
                        raise TunnelError(tunnel.port_conflict_message())
                    tunnel.auth()
                owned = previous != "alive" or _owns_master(config)
                try:
                    result = tunnel.start()
                    if not tunnel.prober(config.bind, config.port):
                        raise TunnelError(
                            f"forward reported success but nothing listens on "
                            f"{config.bind}:{config.port}"
                        )
                    _write_profile(config, owned=owned, forward=True)
                except (TunnelError, OSError) as exc:
                    if previous == TunnelState.ABSENT:
                        try:
                            tunnel.logout()
                        except (TunnelError, OSError) as cleanup:
                            raise TunnelError(
                                f"{exc}; could not close the newly created SSH master: {cleanup}",
                                hint=f"Inspect the master at {config.socket} manually.",
                            ) from exc
                    raise
            elif args.command == "logout":
                if not _owns_master(config):
                    raise TunnelError(
                        "refusing to exit an SSH master not created by argo-tunnel"
                    )
                result = tunnel.logout()
                _write_profile(config, owned=False, forward=False)
            else:
                result = actions[args.command]()
                if args.command in ("start", "stop"):
                    _write_profile(
                        config,
                        owned=_owns_master(config),
                        forward=args.command == "start",
                    )
    except (TunnelError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if isinstance(exc, TunnelError) and exc.hint:
            print(f"hint: {exc.hint}", file=sys.stderr)
        return 1
    if result.stdout.strip():
        print(result.stdout.strip())
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
