from __future__ import annotations

from typing import Any

from virtuoso_bridge.transport import ssh
from virtuoso_bridge.transport.ssh import SSHRunner


class _FakeTunnelProcess:
    pid = 4242
    stderr = None

    def poll(self) -> None:
        return None


def test_managed_tunnel_forces_standalone_ssh_process(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeTunnelProcess:
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeTunnelProcess()

    monkeypatch.setattr(ssh.subprocess, "Popen", fake_popen)

    runner = SSHRunner(host="remote.example", user="eda", ssh_cmd="ssh")
    proc = runner.start_port_forward(65082, settle=0, remote_port=65181)

    assert proc is not None
    assert runner.tunnel_pid == 4242
    assert captured["cmd"] == [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=30",
        "-o", "GSSAPIAuthentication=no",
        "-o", "HostbasedAuthentication=no",
        "-o", "ControlMaster=no",
        "-o", "ControlPath=none",
        "-o", "ExitOnForwardFailure=yes",
        "-N",
        "-L", "65082:127.0.0.1:65181",
        "eda@remote.example",
    ]
