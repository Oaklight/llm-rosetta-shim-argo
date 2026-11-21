"""SSH configuration and profile regression tests."""

from __future__ import annotations

import subprocess
import pytest

import llm_rosetta_shim_argonne.tunnel as tunnel

from llm_rosetta_shim_argonne.tunnel import (
    TunnelConfig,
    TunnelError,
    _build_parser,
    _config_from_args,
    _private_dir,
)


def test_proxy_is_ssh_jump():
    args = _build_parser().parse_args(["auth", "--target", "remote", "--proxy", "hop"])
    assert _config_from_args(args).jump_login == "hop"


def test_user_applies_to_explicit_target():
    args = _build_parser().parse_args(["auth", "--target", "remote", "--user", "alice"])
    assert _config_from_args(args).login == "alice@remote"


def test_users_have_distinct_sockets(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "llm_rosetta_shim_argonne.tunnel._resolved_ssh_config", lambda _: {}
    )
    a = TunnelConfig(target="remote", user="alice", state_dir=tmp_path)
    b = TunnelConfig(target="remote", user="bob", state_dir=tmp_path)
    assert a.socket != b.socket


def test_generated_config_includes_original_for_destination_and_hop(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "llm_rosetta_shim_argonne.tunnel._resolved_ssh_config", lambda _: {}
    )
    original = tmp_path / "ssh config"
    original.write_text(
        "Host remote\n  HostName dest.example\nHost hop\n  HostName jump.example\n"
    )
    key = tmp_path / "my key"
    config = TunnelConfig(
        target="remote",
        jump="hop",
        user="alice",
        key=key,
        config_file=original,
        state_dir=tmp_path,
    )
    wrapper = config.write_config_file()
    for host, expected in [("remote", "dest.example"), ("hop", "jump.example")]:
        output = subprocess.run(
            ["ssh", "-G", "-F", str(wrapper), host],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        assert f"hostname {expected}" in output
        assert f"identityfile {key}" in output


def test_user_at_host_matches_key_in_generated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "llm_rosetta_shim_argonne.tunnel._resolved_ssh_config", lambda _: {}
    )
    key = tmp_path / "key"
    config = TunnelConfig(target="alice@remote", key=key, state_dir=tmp_path)
    wrapper = config.write_config_file()
    output = subprocess.run(
        ["ssh", "-G", "-F", str(wrapper), "alice@remote"],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    assert f"identityfile {key}" in output


def test_conflicting_username_rejected():
    args = _build_parser().parse_args(
        ["auth", "--target", "alice@remote", "--user", "bob"]
    )
    with pytest.raises(ValueError):
        _config_from_args(args)


def test_missing_state_directory_created_with_private_permissions(tmp_path):
    directory = tmp_path / "new" / "tunnel"
    _private_dir(directory)
    assert directory.is_dir()
    assert directory.stat().st_mode & 0o777 == 0o700


def test_insecure_owned_directory_has_actionable_permission_hint(tmp_path):
    directory = tmp_path / "tunnel"
    directory.mkdir(mode=0o700)
    directory.chmod(0o755)
    with pytest.raises(TunnelError) as caught:
        _private_dir(directory)
    assert str(directory) in str(caught.value)
    assert "755" in str(caught.value)
    assert caught.value.hint and "chmod 700" in caught.value.hint
    assert directory.stat().st_mode & 0o777 == 0o755


def test_symlink_directory_rejected_without_chmod_hint(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(TunnelError) as caught:
        _private_dir(link)
    assert caught.value.hint is None
    assert actual.stat().st_mode & 0o777 == 0o700


def _port_free(monkeypatch):
    """Force the SOCKS probe to report the port free, whatever this host runs."""
    original = tunnel.Tunnel.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.prober = lambda *probe_args: False

    monkeypatch.setattr(tunnel.Tunnel, "__init__", init)


def test_auth_forward_failure_rolls_back_new_master(monkeypatch, tmp_path, capsys):
    calls = []
    _port_free(monkeypatch)
    monkeypatch.setattr(
        tunnel.Tunnel, "master_state", lambda self: tunnel.TunnelState.ABSENT
    )
    monkeypatch.setattr(tunnel.Tunnel, "auth", lambda self: calls.append("auth"))

    def fail_start(self):
        calls.append("start")
        raise TunnelError("port busy")

    monkeypatch.setattr(tunnel.Tunnel, "start", fail_start)
    monkeypatch.setattr(tunnel.Tunnel, "logout", lambda self: calls.append("logout"))
    assert tunnel.main(["--state-dir", str(tmp_path), "auth"]) == 1
    assert calls == ["auth", "start", "logout"]
    assert "port busy" in capsys.readouterr().err


def test_auth_forward_failure_does_not_exit_existing_master(monkeypatch, tmp_path):
    """A master that was already there is the operator's, not this run's."""
    calls = []
    _port_free(monkeypatch)
    monkeypatch.setattr(tunnel.Tunnel, "master_state", lambda self: "alive")
    monkeypatch.setattr(tunnel.Tunnel, "auth", lambda self: calls.append("auth"))

    def fail_start(self):
        raise TunnelError("port busy")

    monkeypatch.setattr(tunnel.Tunnel, "start", fail_start)
    monkeypatch.setattr(tunnel.Tunnel, "logout", lambda self: calls.append("logout"))
    assert tunnel.main(["--state-dir", str(tmp_path), "auth"]) == 1
    # No prompt (the master answers) and no exit; the failure is reported as-is.
    assert calls == []


def test_auth_reports_failed_master_rollback(monkeypatch, tmp_path, capsys):
    _port_free(monkeypatch)
    monkeypatch.setattr(
        tunnel.Tunnel, "master_state", lambda self: tunnel.TunnelState.ABSENT
    )
    monkeypatch.setattr(tunnel.Tunnel, "auth", lambda self: None)

    def fail_start(self):
        raise TunnelError("port busy")

    def fail_logout(self):
        raise TunnelError("exit denied")

    monkeypatch.setattr(tunnel.Tunnel, "start", fail_start)
    monkeypatch.setattr(tunnel.Tunnel, "logout", fail_logout)
    assert tunnel.main(["--state-dir", str(tmp_path), "auth"]) == 1
    output = capsys.readouterr().err
    assert "port busy" in output
    assert "exit denied" in output
    assert "Inspect the master" in output


def _port_taken(monkeypatch, *, owner: str | None = None):
    """A bound SOCKS port, and nothing that could answer on it."""
    original = tunnel.Tunnel.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.prober = lambda *probe_args: True

    monkeypatch.setattr(tunnel.Tunnel, "__init__", init)
    monkeypatch.setattr(tunnel, "listener_owner", lambda *a: owner)


def test_a_bound_port_stops_auth_before_the_mfa_prompt(monkeypatch, tmp_path, capsys):
    """The reported case: after the master was killed by pid, the next socket
    the tool picked was a different one, so the record no longer matched and
    the port looked conflicting.  Nothing may reach ssh when nothing can
    answer on that socket — a Duo approval spent there is wasted."""
    _port_taken(monkeypatch, owner="ssh (pid=2654558)")
    monkeypatch.setattr(
        tunnel.Tunnel, "master_state", lambda self: tunnel.TunnelState.ABSENT
    )
    called = []
    monkeypatch.setattr(tunnel.Tunnel, "auth", lambda self: called.append("auth"))
    assert tunnel.main(["--state-dir", str(tmp_path), "auth"]) == 1
    assert called == []
    output = capsys.readouterr().err
    assert "127.0.0.1:10801" in output
    assert "pid=2654558" in output


def test_a_live_master_reuses_the_forward_under_a_new_selection(
    monkeypatch, tmp_path, capsys
):
    """Switching ``--target`` or ``--config`` moves the socket, not the
    forward.  ``ssh -O forward`` is idempotent, so the run asks the master
    instead of demanding a second Duo approval."""
    _port_taken(monkeypatch)
    monkeypatch.setattr(tunnel.Tunnel, "master_state", lambda self: "alive")
    calls = []
    monkeypatch.setattr(tunnel.Tunnel, "auth", lambda self: calls.append("auth"))
    monkeypatch.setattr(
        tunnel.Tunnel,
        "start",
        lambda self: (
            calls.append("start") or tunnel.CommandResult(0, "forward already up")
        ),
    )
    assert tunnel.main(["--state-dir", str(tmp_path), "auth"]) == 0
    assert calls == ["start"]  # asked the master; never prompted
    record = tunnel._read_profile(tmp_path)
    assert record is not None
    assert record["forward"] is True
    assert record["owned"] is False  # it was already there, so not ours to exit


def test_a_subcommand_name_in_the_account_position_is_refused():
    """The reported typo: ``argo-tunnel auth status`` reads as two commands and
    parses as one account called ``status``."""
    with pytest.raises(TunnelError) as caught:
        _config_from_args(_build_parser().parse_args(["auth", "status"]))
    assert "subcommand" in str(caught.value)
    assert "argo-tunnel status" in (caught.value.hint or "")
    # The flag still names an account, whatever it is called.
    args = _build_parser().parse_args(["auth", "--user", "status"])
    assert _config_from_args(args).user == "status"


def _fake_proc(root, pid: int, argv: list[str]) -> None:
    entry = root / str(pid)
    entry.mkdir()
    (entry / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")


def test_a_live_master_naming_our_socket_is_ours(tmp_path):
    """The profile is a record, not the only evidence.  Without this fallback a
    lost record left the operator unable to log out of a master this tool had
    started itself."""
    proc = tmp_path / "proc"
    proc.mkdir()
    ours = tmp_path / "ssh-abc"
    _fake_proc(proc, 4242, ["ssh", "-S", str(ours), "-M", "-N", "-f", "homes"])
    # Not a master: no -M.  Not ours: another socket.
    _fake_proc(proc, 4243, ["ssh", "-S", str(ours), "-O", "check", "homes"])
    _fake_proc(proc, 4244, ["ssh", "-S", str(tmp_path / "other"), "-M", "homes"])
    assert [pid for pid, _ in tunnel.master_processes(ours, proc)] == [4242]


def test_master_scan_without_proc_says_nothing(tmp_path):
    assert tunnel.master_processes(tmp_path / "ssh-abc", tmp_path / "missing") == []


def test_logout_is_allowed_for_a_master_the_proc_scan_recognises(monkeypatch, tmp_path):
    """The record is missing here, and the master still answers: the process
    naming our socket is the proof, so ``logout`` no longer refuses."""
    # Hermetic: the socket name depends on the resolved login, so a host that
    # happens to define a ``Host homes`` alias would compute a different one.
    monkeypatch.setattr("llm_rosetta_shim_argonne.tunnel._ssh_g", lambda *a, **k: {})
    monkeypatch.setattr("llm_rosetta_shim_argonne.tunnel._SSH_G_CACHE", {})
    proc = tmp_path / "proc"
    proc.mkdir()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    config = TunnelConfig(state_dir=state)
    _fake_proc(proc, 99, ["ssh", "-S", str(config.socket), "-M", "-N", "-f", "homes"])
    real = tunnel.master_processes
    monkeypatch.setattr(
        tunnel, "master_processes", lambda path, root="/proc": real(path, proc)
    )
    exited = []
    monkeypatch.setattr(tunnel.Tunnel, "master_state", lambda self: "alive")
    monkeypatch.setattr(
        tunnel.Tunnel,
        "logout",
        lambda self: exited.append(1) or tunnel.CommandResult(0, "exited"),
    )
    assert tunnel.main(["--state-dir", str(state), "logout"]) == 0
    assert exited == [1]
    record = tunnel._read_profile(state)
    assert record is not None
    assert record["owned"] is False


def test_logout_still_refuses_a_master_it_cannot_claim(monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    monkeypatch.setattr(tunnel, "master_processes", lambda *a, **k: [])
    monkeypatch.setattr(tunnel.Tunnel, "master_state", lambda self: "alive")
    called = []
    monkeypatch.setattr(tunnel.Tunnel, "logout", lambda self: called.append(1) or _ok())
    assert tunnel.main(["--state-dir", str(state), "logout"]) == 1
    assert called == []


def _ok():
    return tunnel.CommandResult(0, "exited")
