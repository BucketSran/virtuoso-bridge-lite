from __future__ import annotations

from typing import Any

from virtuoso_bridge import cli
from virtuoso_bridge.transport.ssh import CommandResult


class _FakeRunner:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def run_command(self, command: str, timeout: int | None = None) -> CommandResult:
        self.commands.append(command)
        return CommandResult(returncode=0, stdout="12345\n", stderr="")


class _FakeSSH:
    port = 65182
    remote_port = 65181

    def __init__(self) -> None:
        self.ssh_runner = _FakeRunner()
        self.updated: dict[str, Any] = {}

    def read_state(self, profile: str | None) -> dict[str, str]:
        return {"setup_path": "/tmp/virtuoso_bridge_user/virtuoso_bridge/virtuoso_setup.il"}

    def update_state(self, profile: str | None, **updates: Any) -> None:
        self.updated = updates


def test_bootstrap_daemon_starts_headless_virtuoso(monkeypatch: Any) -> None:
    probes = iter([False, True])
    monkeypatch.setenv("VB_CADENCE_CSHRC_ci", "/home/cadence/setup.csh")
    monkeypatch.setattr(cli, "_daemon_responds", lambda *_args, **_kwargs: next(probes))

    ssh = _FakeSSH()
    cli._bootstrap_daemon_if_needed(ssh, "ci", is_local=False)

    command = ssh.ssh_runner.commands[0]
    assert "virtuoso_headless_ci.csh" in command
    assert "source /home/cadence/setup.csh" in command
    assert 'load("/tmp/virtuoso_bridge_user/virtuoso_bridge/virtuoso_setup.il")' in command
    assert "| virtuoso -nograph" in command
    assert ssh.updated["virtuoso_autostart"] is True
    assert ssh.updated["virtuoso_launcher_pid"] == 12345
    assert ssh.updated["virtuoso_remote_port"] == 65181


def test_bootstrap_daemon_skips_local_mode(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        cli,
        "_daemon_responds",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not probe")),
    )

    ssh = _FakeSSH()
    cli._bootstrap_daemon_if_needed(ssh, "ci", is_local=True)

    assert ssh.ssh_runner.commands == []
