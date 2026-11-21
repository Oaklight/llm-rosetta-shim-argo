"""Exercise a failed forward against an isolated local SSH server."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "src/llm_rosetta_shim_argonne/tunnel.py"


def _port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    return sock


def test_busy_port_rolls_back_new_master():
    sshd = shutil.which("sshd") or "/usr/sbin/sshd"
    if not Path(sshd).is_file() or not shutil.which("ssh-keygen"):
        pytest.skip("local SSH server unavailable")
    with tempfile.TemporaryDirectory(prefix="argo-test-", dir="/tmp") as root:
        directory = Path(root)
        for name in ("host", "client"):
            subprocess.run(
                [
                    "ssh-keygen",
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-f",
                    str(directory / name),
                ],
                check=True,
            )
        (directory / "authorized_keys").write_bytes(
            (directory / "client.pub").read_bytes()
        )
        listener = _port()
        ssh_port = listener.getsockname()[1]
        listener.close()
        occupied = _port()
        occupied.listen()
        forward_port = occupied.getsockname()[1]
        config = directory / "sshd_config"
        config.write_text(
            f"Port {ssh_port}\nListenAddress 127.0.0.1\nHostKey {directory / 'host'}\nAuthorizedKeysFile {directory / 'authorized_keys'}\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nUsePAM no\nAllowTcpForwarding yes\nStrictModes no\nPidFile {directory / 'pid'}\n"
        )
        server = subprocess.Popen(
            [sshd, "-D", "-e", "-f", str(config)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        client_config = directory / "ssh_config"
        client_config.write_text(
            f"Host test-target\n  HostName 127.0.0.1\n  Port {ssh_port}\n  User {os.environ.get('USER', 'nobody')}\n  IdentityFile {directory / 'client'}\n  IdentitiesOnly yes\n  StrictHostKeyChecking no\n  UserKnownHostsFile /dev/null\n"
        )
        try:
            import time

            for _ in range(50):
                if server.poll() is not None:
                    pytest.skip(f"sshd did not start: {server.stderr.read().decode()}")
                try:
                    with socket.create_connection(("127.0.0.1", ssh_port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.05)
            args = [
                sys.executable,
                str(_SCRIPT),
                "--state-dir",
                root,
                "auth",
                "--config",
                str(client_config),
                "--target",
                "test-target",
                "--port",
                str(forward_port),
            ]
            result = subprocess.run(args, text=True, capture_output=True, timeout=30)
            assert result.returncode != 0
            assert "port" in result.stderr.lower() or "forward" in result.stderr.lower()
            sockets = list((directory / "tunnel").glob("ssh-*"))
            assert not [p for p in sockets if p.is_socket()], result.stderr
        finally:
            occupied.close()
            server.terminate()
            server.wait(timeout=5)
