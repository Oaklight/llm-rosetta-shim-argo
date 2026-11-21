"""The tunnel CLI: argv composition, the four status states, and the CLI exit codes.

No test opens a connection.  The ssh side is a fake :class:`Runner` that
records argv and answers with a scripted result; the SOCKS probe is replaced
the same way.  What is being held is the *decision* each command makes —
notably that only ``auth`` may clear ``BatchMode``, and that a forward is
never added to a master that does not answer.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Sequence

import pytest

from llm_rosetta_shim_argonne.tunnel import (
    SSH_FAILURE,
    Tunnel,
    TunnelConfig,
    TunnelError,
    TunnelState,
    CommandResult,
    _build_parser,
    _config_from_args,
    _write_profile,
    main,
    probe_socks,
)


@pytest.fixture(autouse=True)
def _hermetic_ssh(monkeypatch, tmp_path_factory):
    """Keep the suite off the host's ssh config, its listening sockets and its
    real state directory (``~/.config/llm-rosetta-gateway``)."""
    monkeypatch.setattr("llm_rosetta_shim_argonne.tunnel._ssh_g", lambda *a, **k: {})
    monkeypatch.setattr("llm_rosetta_shim_argonne.tunnel._SSH_G_CACHE", {})
    monkeypatch.setattr("llm_rosetta_shim_argonne.tunnel.listener_owner", lambda *a: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("config")))


class FakeRunner:
    """Answers each ssh invocation from a script, recording the argv."""

    def __init__(self, results: Sequence[CommandResult] | None = None) -> None:
        self.calls: list[tuple[list[str], bool]] = []
        self._results = list(results or [])

    def __call__(
        self, argv: Sequence[str], *, interactive: bool = False
    ) -> CommandResult:
        self.calls.append((list(argv), interactive))
        if self._results:
            return self._results.pop(0)
        return CommandResult(0)

    @property
    def argvs(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]


def _alive_check() -> CommandResult:
    return CommandResult(0, "Master running (pid=123)\n")


def _absent_check() -> CommandResult:
    return CommandResult(
        SSH_FAILURE, "", "Control socket connect(/tmp/x): No such file or directory\n"
    )


def _wedged_check() -> CommandResult:
    return CommandResult(
        SSH_FAILURE, "", "Control socket connect(/tmp/x): Connection refused\n"
    )


def _args(*positional, **overrides):
    """Build a namespace: positional words first, then options.

    A keyword of ``True`` adds a bare flag; anything else takes a value.
    """
    argv = list(positional)
    for key, value in overrides.items():
        flag = "--" + key.replace("_", "-")
        argv += [flag] if value is True else [flag, str(value)]
    return _build_parser().parse_args(argv)


def _stdout_of(argv, capsys) -> str:
    main(argv)
    return capsys.readouterr().out.strip()


def _tunnel(runner: FakeRunner, port_open: bool = False, **overrides) -> Tunnel:
    config = replace(TunnelConfig(user="someone", port=10801), **overrides)
    return Tunnel(config, runner=runner, prober=lambda bind, port: port_open)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_proxy_url_is_what_a_provider_config_takes():
    assert TunnelConfig().proxy_url == "socks5://127.0.0.1:10801"


def test_env_override_and_empty_means_unset():
    config = TunnelConfig.from_env(
        {
            "ARGO_TUNNEL_HOST": "homes.cels.anl.gov",
            "ARGO_TUNNEL_JUMP": "",
            "ARGO_TUNNEL_PORT": "10999",
            "ARGO_TUNNEL_USER": "abc",
        }
    )
    # An empty variable keeps the default rather than blanking it: only the
    # explicit word "none" decides "no jump hop".
    assert config.effective_jump == "logins.cels.anl.gov"
    assert config.port == 10999
    assert config.user == "abc"
    assert config.login == "abc@homes.cels.anl.gov"
    assert config.jump_login == "abc@logins.cels.anl.gov"


def test_env_can_disable_the_jump_hop():
    config = TunnelConfig.from_env({"ARGO_TUNNEL_JUMP": "none"})
    assert config.no_jump is True
    assert config.jump_login is None


def test_a_bad_port_is_refused_rather_than_ignored():
    with pytest.raises(TunnelError) as caught:
        TunnelConfig.from_env({"ARGO_TUNNEL_PORT": "not-a-port"})
    assert "ARGO_TUNNEL_PORT" in str(caught.value)


def test_socket_path_differs_per_target_so_two_masters_never_collide():
    a = TunnelConfig(user="u", host="h1")
    b = TunnelConfig(user="u", host="h2")
    assert a.socket != b.socket
    # The port is part of the identity too: two forwards need two masters.
    assert a.socket != replace(a, port=9999).socket


def test_a_configured_socket_path_wins():
    config = TunnelConfig(control_path="/tmp/ctl")
    assert str(config.socket) == "/tmp/ctl"


# ---------------------------------------------------------------------------
# Which machine is authenticated to
# ---------------------------------------------------------------------------


def test_a_default_run_builds_its_destination_from_the_vendor_pair():
    config = TunnelConfig(user="abc")
    assert config.login == "abc@homes.cels.anl.gov"
    assert config.jump_login == "abc@logins.cels.anl.gov"


def test_a_default_run_prefers_the_stanza_that_maps_to_the_default_host(monkeypatch):
    """An Argonne account reaches the host through its own ``Host homes``
    stanza; addressing the bare name would skip the route and the key."""
    monkeypatch.setattr(
        "llm_rosetta_shim_argonne.tunnel._ssh_g",
        lambda config, name: (
            {"hostname": ["homes.cels.anl.gov"], "user": ["pding"]}
            if name == "homes"
            else {}
        ),
    )
    # No --user: the stanza's own account is not prefixed onto the alias, the
    # way ssh resolves it, so the stanza's User wins rather than being shadowed.
    assert TunnelConfig().login == "homes"
    # Where the operator named the account it is carried, and still no -J.
    config = TunnelConfig(user="pding")
    assert config.login == "pding@homes"
    # The alias carries its own ProxyJump; adding -J would replace it.
    assert config.jump_login is None


def test_the_account_name_qualifies_the_target_without_rewriting_it():
    """``lambda5`` stays the ``Host`` alias ssh matches on; the account is the
    ssh ``user@host`` prefix, not part of the pattern."""
    config = TunnelConfig(target="lambda5", user="abc")
    assert config.login == "abc@lambda5"
    assert "lambda5" in config.hop_entries()


def test_a_raw_target_keeps_its_own_user():
    config = TunnelConfig(target="someone@lambda5.cels.anl.gov")
    assert config.login == "someone@lambda5.cels.anl.gov"


def test_a_conflicting_account_name_is_refused():
    with pytest.raises(ValueError):
        TunnelConfig(target="someone@lambda5.cels.anl.gov", user="abc").login


def test_a_target_without_a_jump_flag_leaves_ssh_config_to_decide():
    """An alias usually carries its own ``ProxyJump``; passing ``-J`` would
    silently replace it with our guess at the vendor's hop."""
    assert TunnelConfig(target="lambda5").jump_login is None


def test_a_target_with_an_explicit_jump_still_gets_one():
    config = TunnelConfig(target="lambda5", jump="logins.cels.anl.gov", user="u")
    assert config.jump_login == "u@logins.cels.anl.gov"


def test_a_jump_that_already_carries_a_user_is_not_prefixed():
    config = TunnelConfig(target="lambda5", jump="bob@logins.cels.anl.gov", user="u")
    assert config.jump_login == "bob@logins.cels.anl.gov"


def test_a_direct_connection_passes_no_jump_even_without_a_target():
    config = TunnelConfig(host="inner.example", no_jump=True)
    assert config.login == "inner.example"
    assert config.jump_login is None


def test_the_default_destination_still_uses_the_vendor_hop():
    assert TunnelConfig().effective_jump == "logins.cels.anl.gov"


def test_auth_targets_the_destination_the_operator_named():
    runner = FakeRunner([_absent_check(), CommandResult(0)])
    tunnel = Tunnel(
        TunnelConfig(target="lambda5", port=10999),
        runner=runner,
        prober=lambda *a: False,
    )
    tunnel.auth()
    assert runner.argvs[-1][-1] == "lambda5"
    assert "-J" not in runner.argvs[-1]


def test_two_targets_do_not_share_a_master():
    a = TunnelConfig(target="lambda5")
    b = TunnelConfig(target="metis")
    assert a.socket != b.socket


def test_env_names_the_target(monkeypatch):
    _with_ssh_g(monkeypatch, {})
    config = TunnelConfig.from_env({"ARGO_TUNNEL_TARGET": "me@lambda5.cels.anl.gov"})
    assert config.login == "me@lambda5.cels.anl.gov"
    assert config.jump_login is None


def test_cli_target_wins_over_host_and_user():
    """The target is addressed verbatim so its alias still resolves; ``--user``
    is carried as the login prefix, not dropped."""
    args = _args(target="lambda5", host="ignored.example", user="ignored")
    config = _config_from_args(args)
    assert config.login == "ignored@lambda5"


def test_cli_jump_none_is_a_decision_not_an_absence():
    config = _config_from_args(_args(target="mine", jump="none"))
    assert config.jump_login is None


# ---------------------------------------------------------------------------
# Socket placement and persist lifetime
# ---------------------------------------------------------------------------


def _with_ssh_g(monkeypatch, values: dict[str, str]) -> None:
    """Pin what ``ssh -G`` reports, so the placement tests never touch ssh."""
    monkeypatch.setattr(
        "llm_rosetta_shim_argonne.tunnel._resolved_ssh_config", lambda config: dict(values)
    )


def test_without_a_system_setting_the_socket_lives_under_our_config_dir(monkeypatch):
    _with_ssh_g(monkeypatch, {})
    socket = TunnelConfig(user="u").socket
    assert socket.parent.parent.name == "llm-rosetta-gateway"
    assert socket.parent.name == "tunnel"
    assert socket.name.startswith("ssh-")


def test_xdg_config_home_moves_the_default_location(monkeypatch, tmp_path):
    _with_ssh_g(monkeypatch, {})
    # A short root on purpose: a deeply nested home would cross the sun_path
    # limit and be rehomed to /tmp (see the next test).
    short = Path(tempfile.mkdtemp(prefix="xdg-", dir="/tmp"))
    try:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(short))
        socket = TunnelConfig(user="u").socket
        assert socket.is_relative_to(short)
        assert socket.parent.name == "tunnel"
    finally:
        shutil.rmtree(short, ignore_errors=True)


def test_a_home_too_deep_for_a_unix_socket_is_refused(monkeypatch, tmp_path):
    """``sockaddr_un.sun_path`` is ~100 bytes; past that ssh silently truncates
    and the socket can never be found again, so it is an error, not a fallback."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ("d" * 120)))
    with pytest.raises(TunnelError) as caught:
        _ = TunnelConfig(user="u").socket
    assert "too long" in str(caught.value)


def test_a_state_dir_the_operator_named_wins(monkeypatch, tmp_path):
    _with_ssh_g(monkeypatch, {})
    socket = TunnelConfig(user="u", state_dir=tmp_path).socket
    assert socket.is_relative_to(tmp_path)


def test_a_system_controlpath_is_used_rather_than_our_own(monkeypatch, tmp_path):
    """A ControlMaster the operator configured is a session worth reusing, not
    one to route around: a tool that invented its own path would report
    ``absent`` while an authenticated master sat right there."""
    system_socket = tmp_path / "cm-abc"
    _with_ssh_g(monkeypatch, {"controlpath": str(system_socket)})
    assert TunnelConfig(user="u").socket == system_socket


def test_a_system_controlpath_is_used_verbatim_including_ssh_tokens(monkeypatch):
    """``ssh -G`` already expanded ``~`` and ``%C`` into a concrete path, so the
    value is taken as given — re-expanding it would invent a different file."""
    resolved = str(Path.home() / "cm-d999a3ce479981f70bc3d02384a65d896e2481f8")
    _with_ssh_g(monkeypatch, {"controlpath": resolved})
    assert TunnelConfig(user="u").socket == Path(resolved)


def test_a_too_deep_system_controlpath_is_refused(monkeypatch, tmp_path):
    deep = tmp_path / ("d" * 120)
    _with_ssh_g(monkeypatch, {"controlpath": str(deep / "sock")})
    with pytest.raises(TunnelError) as caught:
        _ = TunnelConfig(user="u").socket
    assert "ControlPath" in str(caught.value)


def test_an_explicit_path_beats_everything(monkeypatch, tmp_path):
    _with_ssh_g(monkeypatch, {"controlpath": str(tmp_path / "system")})
    explicit = tmp_path / "explicit"
    assert TunnelConfig(user="u", control_path=explicit).socket == explicit


def test_two_targets_get_two_sockets(monkeypatch):
    _with_ssh_g(monkeypatch, {})
    a = TunnelConfig(user="u", host="h1")
    b = TunnelConfig(user="u", host="h2")
    assert a.socket != b.socket


def test_persist_falls_back_to_our_default_when_ssh_says_no(monkeypatch):
    """``ssh -G`` prints ``controlpersist no`` when nobody configured one: it is
    the default, not a choice, so it must not be propagated."""
    _with_ssh_g(monkeypatch, {"controlpersist": "no"})
    assert TunnelConfig(user="u").effective_control_persist() == "8h"


def test_persist_honours_the_systems_value(monkeypatch):
    _with_ssh_g(monkeypatch, {"controlpersist": "14400"})
    assert TunnelConfig(user="u").effective_control_persist() == "14400"


def test_an_explicit_persist_beats_the_systems(monkeypatch):
    _with_ssh_g(monkeypatch, {"controlpersist": "14400"})
    assert (
        TunnelConfig(user="u", control_persist="1h").effective_control_persist() == "1h"
    )


def test_the_same_persist_reaches_the_auth_command(monkeypatch):
    _with_ssh_g(monkeypatch, {"controlpersist": "14400"})
    runner = FakeRunner([_absent_check(), CommandResult(0)])
    _tunnel(runner).auth()
    assert "ControlPersist=14400" in runner.argvs[-1]


def test_env_can_name_the_state_dir(monkeypatch, tmp_path):
    _with_ssh_g(monkeypatch, {})
    config = TunnelConfig.from_env({"ARGO_TUNNEL_STATE_DIR": str(tmp_path)})
    assert config.socket.is_relative_to(tmp_path)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "check, port_open, expected",
    [
        (_absent_check(), False, TunnelState.ABSENT),
        (_wedged_check(), False, TunnelState.WEDGED),
        (_alive_check(), False, TunnelState.IDLE),
        (_alive_check(), True, TunnelState.FORWARDING),
    ],
)
def test_status_separates_the_four_states(check, port_open, expected, tmp_path):
    config = replace(TunnelConfig(user="someone", port=10801), state_dir=tmp_path)
    if expected == TunnelState.FORWARDING:
        # A listening port alone is not proof; the record is.
        _write_profile(config, owned=True, forward=True)
    tunnel = Tunnel(config, runner=FakeRunner([check]), prober=lambda *a: port_open)
    assert tunnel.status() == expected


def test_an_absent_master_is_never_blamed_on_the_port():
    """The probe must not run when nothing answers: a busy port would lie."""
    probed = []
    tunnel = Tunnel(
        TunnelConfig(user="u"),
        runner=FakeRunner([_absent_check()]),
        prober=lambda bind, port: probed.append(port) or True,
    )
    assert tunnel.status() == TunnelState.ABSENT
    assert probed == []


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def test_auth_builds_a_master_connection_and_is_the_only_interactive_call():
    runner = FakeRunner([_absent_check(), CommandResult(0)])
    _tunnel(runner).auth()
    argv, interactive = runner.calls[-1]
    assert interactive is True
    assert "-M" in argv and "-f" in argv and "-N" in argv
    assert "BatchMode=yes" not in argv
    # The persist value is resolved, not literal: ssh_config's own value wins
    # over this tool's default (see the placement tests below).
    assert any(a.startswith("ControlPersist=") for a in argv)
    assert "--" not in argv


def test_auth_names_the_forward_options_it_will_need_later():
    runner = FakeRunner([_absent_check(), CommandResult(0)])
    _tunnel(runner).auth()
    assert "ExitOnForwardFailure=yes" in runner.argvs[-1]


def test_auth_names_its_destination_last():
    """Regression: the destination used to be appended only to the commands
    that carry ``-O``, so ``auth`` invoked ssh with no host at all — ssh
    printed its usage text and the CLI reported an empty error."""
    runner = FakeRunner([_absent_check(), CommandResult(0)])
    _tunnel(runner).auth()
    argv = runner.argvs[-1]
    assert argv[-1] == "someone@homes.cels.anl.gov"
    assert "--" not in argv


def test_auth_reuses_a_live_master_without_a_second_prompt():
    runner = FakeRunner([_alive_check()])
    result = _tunnel(runner).auth()
    assert result.ok
    # Only the check ran: no second ssh, so no second Duo approval.
    assert len(runner.calls) == 1
    assert runner.calls[0][1] is False


def test_auth_never_removes_a_wedged_socket_it_did_not_create():
    with pytest.raises(TunnelError) as caught:
        _tunnel(FakeRunner([_wedged_check()])).auth()
    assert "does not answer" in str(caught.value)
    assert "inspect" in (caught.value.hint or "")


def test_auth_reports_ssh_output_on_failure():
    runner = FakeRunner(
        [_absent_check(), CommandResult(SSH_FAILURE, "", "Permission denied")]
    )
    with pytest.raises(TunnelError) as caught:
        _tunnel(runner).auth()
    assert "Permission denied" in str(caught.value)


# ---------------------------------------------------------------------------
# start / stop / logout
# ---------------------------------------------------------------------------


def test_start_adds_the_forward_with_the_full_spec():
    runner = FakeRunner([_alive_check(), CommandResult(0)])
    _tunnel(runner, port_open=False).start()
    argv = runner.argvs[-1]
    assert "-O" in argv and argv[argv.index("-O") + 1] == "forward"
    assert argv[argv.index("-D") + 1] == "127.0.0.1:10801"


def test_start_is_idempotent_when_this_tool_owns_the_forward(tmp_path):
    config = TunnelConfig(user="u", state_dir=tmp_path)
    _write_profile(config, owned=True, forward=True)
    runner = FakeRunner([_alive_check()])
    result = Tunnel(config, runner=runner, prober=lambda *a: True).start()
    assert result.ok
    assert len(runner.calls) == 1  # only the check; ssh -O forward never ran


def test_an_unowned_listener_is_never_taken_for_our_forward(tmp_path):
    """A SOCKS server on the port proves nothing about who put it there."""
    config = TunnelConfig(user="u", state_dir=tmp_path)
    tunnel = Tunnel(config, runner=FakeRunner([_alive_check()]), prober=lambda *a: True)
    assert tunnel.status() == TunnelState.WEDGED


def test_start_without_a_master_tells_the_operator_what_to_run():
    with pytest.raises(TunnelError) as caught:
        _tunnel(FakeRunner([_absent_check()])).start()
    assert "auth" in (caught.value.hint or "")


def test_start_over_a_wedged_master_also_refuses():
    with pytest.raises(TunnelError):
        _tunnel(FakeRunner([_wedged_check()])).start()


def test_stop_cancels_the_same_spec_it_forwarded(tmp_path):
    config = replace(TunnelConfig(user="someone", port=10801), state_dir=tmp_path)
    _write_profile(config, owned=True, forward=True)
    runner = FakeRunner([_alive_check(), CommandResult(0)])
    Tunnel(config, runner=runner, prober=lambda *a: True).stop()
    argv = runner.argvs[-1]
    assert argv[argv.index("-O") + 1] == "cancel"
    assert argv[argv.index("-D") + 1] == "127.0.0.1:10801"


def test_stop_without_a_master_is_a_no_op():
    runner = FakeRunner([_absent_check()])
    assert _tunnel(runner).stop().ok
    assert len(runner.calls) == 1


def test_stop_on_an_idle_master_does_not_send_a_cancel():
    """A cancel for a forward that was never added answers with ssh's generic
    failure text, which cannot be told from a real failure — so it is never
    sent."""
    runner = FakeRunner([_alive_check()])
    result = _tunnel(runner, port_open=False).stop()
    assert result.ok
    assert len(runner.calls) == 1  # the check; no '-O cancel'


def test_stop_refuses_a_socket_it_cannot_read():
    with pytest.raises(TunnelError) as caught:
        _tunnel(FakeRunner([_wedged_check()])).stop()
    assert "inspect" in (caught.value.hint or "")


def test_stop_reports_a_real_cancel_failure(tmp_path):
    config = replace(TunnelConfig(user="someone", port=10801), state_dir=tmp_path)
    _write_profile(config, owned=True, forward=True)
    runner = FakeRunner([_alive_check(), CommandResult(SSH_FAILURE, "", "boom")])
    with pytest.raises(TunnelError) as caught:
        Tunnel(config, runner=runner, prober=lambda *a: True).stop()
    assert "boom" in str(caught.value)


def test_logout_exits_the_master():
    runner = FakeRunner([_alive_check(), CommandResult(0)])
    _tunnel(runner).logout()
    assert runner.argvs[-1][runner.argvs[-1].index("-O") + 1] == "exit"


# ---------------------------------------------------------------------------
# argv shape
# ---------------------------------------------------------------------------


def test_every_non_interactive_command_runs_in_batch_mode(tmp_path):
    """Only ``auth`` may prompt; a stale master must fail fast instead of
    waiting for an MFA answer nobody is there to give."""
    config = replace(TunnelConfig(user="someone", port=10801), state_dir=tmp_path)
    _write_profile(config, owned=True, forward=True)
    # Every later call defaults to success, i.e. a live master.
    runner = FakeRunner([_alive_check()])
    tunnel = Tunnel(config, runner=runner, prober=lambda *a: True)
    tunnel.status()
    tunnel.start()
    tunnel.stop()
    tunnel.logout()
    assert len(runner.calls) >= 6
    for argv, interactive in runner.calls:
        assert interactive is False
        assert "BatchMode=yes" in argv


def test_the_jump_host_is_carried_on_the_command():
    runner = FakeRunner([_alive_check()])
    _tunnel(runner, port_open=True).status()
    argv = runner.argvs[-1]
    assert argv[argv.index("-J") + 1] == "someone@logins.cels.anl.gov"


def test_no_jump_hop_means_no_j_flag():
    runner = FakeRunner([_alive_check()])
    _tunnel(runner, port_open=True, jump=None, no_jump=True).status()
    assert "-J" not in runner.argvs[-1]


# ---------------------------------------------------------------------------
# SOCKS probe
# ---------------------------------------------------------------------------


def test_probe_reports_a_closed_port_as_false():
    # Port 1 is reserved and nothing listens on it; the probe must not raise.
    assert probe_socks("127.0.0.1", 1, timeout=0.2) is False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_proxy_url_needs_no_ssh(capsys):
    assert main(["proxy-url", "--port", "10999"]) == 0
    assert capsys.readouterr().out.strip() == "socks5://127.0.0.1:10999"


def test_cli_status_exits_non_zero_unless_forwarding(monkeypatch, capsys):
    monkeypatch.setattr(
        "llm_rosetta_shim_argonne.tunnel.run_subprocess", FakeRunner([_absent_check()])
    )
    assert main(["status"]) == 1
    assert "absent" in capsys.readouterr().out


def test_cli_status_json_shape(monkeypatch, capsys):
    monkeypatch.setattr(
        "llm_rosetta_shim_argonne.tunnel.run_subprocess", FakeRunner([_absent_check()])
    )
    assert main(["status", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "absent"
    assert payload["proxy_url"] == "socks5://127.0.0.1:10801"


def test_an_option_before_the_subcommand_is_not_overwritten_by_it(capsys):
    """Regression: the subparser shares the parent's options, and its own
    default used to land *after* the parent parsed one, silently restoring
    ``socks5://127.0.0.1:10801`` for ``--port 10999 proxy-url``."""
    expected = "socks5://127.0.0.1:10999"
    assert _stdout_of(["proxy-url", "--port", "10999"], capsys) == expected
    assert _stdout_of(["--port", "10999", "proxy-url"], capsys) == expected


def test_json_reads_the_same_state_in_either_position(monkeypatch, capsys):
    monkeypatch.setattr(
        "llm_rosetta_shim_argonne.tunnel.run_subprocess", FakeRunner([_absent_check()])
    )
    assert main(["status", "--json", "--port", "10999"]) == 1
    first = json.loads(capsys.readouterr().out)
    monkeypatch.setattr(
        "llm_rosetta_shim_argonne.tunnel.run_subprocess", FakeRunner([_absent_check()])
    )
    assert main(["--json", "--port", "10999", "status"]) == 1
    second = json.loads(capsys.readouterr().out)
    assert first == second
    assert first["proxy_url"] == "socks5://127.0.0.1:10999"


def test_cli_without_a_command_prints_help_and_fails(capsys):
    assert main([]) == 2
    assert "argo-tunnel" in capsys.readouterr().out


def test_the_account_name_is_accepted_where_ssh_puts_it():
    config = _config_from_args(_args("auth", "pding"))
    assert config.login == "pding@homes.cels.anl.gov"
    assert config.jump_login == "pding@logins.cels.anl.gov"


def test_the_flag_beats_the_positional():
    config = _config_from_args(_args("auth", "positional", user="flag"))
    assert config.user == "flag"


def test_a_key_is_offered_on_the_hop_as_well_as_the_destination(tmp_path):
    """``-i`` reaches only the destination.  The hop re-reads configuration on
    its own, so the key is written where the hop will see it — measured: without
    it the hop answers ``Permission denied (publickey)`` before any MFA."""
    key = tmp_path / "work"
    config = TunnelConfig(user="pding", key=key)
    entries = config.hop_entries()
    assert set(entries) == {"homes.cels.anl.gov", "logins.cels.anl.gov"}
    for directives in entries.values():
        joined = "\n".join(directives)
        assert f'IdentityFile "{key}"' in joined
        assert "IdentitiesOnly yes" in joined


def test_the_generated_config_includes_the_operators_file_last(tmp_path):
    """ssh takes the first value it sees, so what this tool wrote wins where it
    speaks and the alias's HostName/ProxyJump still apply everywhere else."""
    config = TunnelConfig(
        user="pding", key=tmp_path / "work", config_file=tmp_path / "config"
    )
    (tmp_path / "config").write_text("Host *\n    ServerAliveInterval 60\n")
    path = config.write_config_file()
    text = path.read_text()
    assert text.index("Host homes.cels.anl.gov") < text.index("Include ")
    assert f'Include "{tmp_path / "config"}"' in text


def test_nothing_detected_means_no_generated_file(tmp_path):
    """Nothing to add: the operator's own configuration is the whole story, and
    ``-F`` stays a single, predictable indirection."""
    config = TunnelConfig(config_file=tmp_path / "config")
    (tmp_path / "config").write_text("Host *\n")
    assert config.write_config_file() == tmp_path / "config"


def test_an_alias_stays_an_alias_in_the_generated_file(tmp_path):
    """Rewriting ``lambda5`` into ``lambda5.cels.anl.gov`` would lose the
    stanza that supplies HostName and route."""
    config = TunnelConfig(target="lambda5", user="pding", key=tmp_path / "k")
    assert "lambda5" in config.hop_entries()


def test_proxy_names_a_jump_host_not_a_socks_url(capsys):
    """``--proxy`` is ssh's vocabulary: a hop, the same thing as ``-J``."""
    assert main(["proxy-url", "--proxy", "logins.cels.anl.gov"]) == 0
    assert capsys.readouterr().out.strip() == "socks5://127.0.0.1:10801"


def test_cli_prints_the_hint_on_a_refused_action(monkeypatch, capsys):
    monkeypatch.setattr(
        "llm_rosetta_shim_argonne.tunnel.run_subprocess", FakeRunner([_absent_check()])
    )
    assert main(["start"]) == 1
    err = capsys.readouterr().err
    assert "hint:" in err and "auth" in err
