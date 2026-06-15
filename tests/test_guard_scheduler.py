"""Unit tests for the scheduler install/uninstall in claude_exit.guard.

The scheduler is platform-dispatched: launchd on macOS, systemd user timer
on Linux. Tests run all platform branches on whatever host pytest is on —
the scheduler functions accept `platform=` and `runner=` kwargs so we can
exercise both arms without a real launchctl / systemctl.

Test discipline (per testing-anti-patterns): the file-content assertions
ARE the test. Subprocess calls are recorded by a fake `runner` and asserted
on for shape, but a test that only asserted "subprocess.run was called"
would be testing the mock, not the behavior. Generated files (plist,
.service, .timer) are checked against expected substrings derived from the
spec.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from claude_exit.guard import (
    _launchd_plist_content,
    _systemd_service_content,
    _systemd_timer_content,
    install_scheduler,
    uninstall_scheduler,
)


# ---- fixtures ---------------------------------------------------------------


class RecordingRunner:
    """Records calls and lets each test specify return-code/stderr per call."""

    def __init__(self, default_returncode: int = 0):
        self.calls: list[list[str]] = []
        self.default_returncode = default_returncode
        # List of CompletedProcess-like objects to return, in order. If empty,
        # default_returncode is used.
        self.queued_results: list[subprocess.CompletedProcess] = []

    def queue(self, returncode: int, stderr: str = "", stdout: str = "") -> None:
        cp = subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=stderr
        )
        self.queued_results.append(cp)

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if self.queued_results:
            cp = self.queued_results.pop(0)
            cp.args = list(args)
            return cp
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=self.default_returncode,
            stdout="",
            stderr="",
        )


@pytest.fixture
def fake_bin(tmp_path: Path) -> Path:
    bin_path = tmp_path / "bin" / "claude-exit"
    bin_path.parent.mkdir(parents=True)
    bin_path.write_text("#!/bin/sh\n")
    bin_path.chmod(0o755)
    return bin_path


@pytest.fixture
def launchd_plist(tmp_path: Path) -> Path:
    return tmp_path / "LaunchAgents" / "io.claude-exit.guard.plist"


@pytest.fixture
def systemd_timer(tmp_path: Path) -> Path:
    return tmp_path / "systemd" / "user" / "claude-exit-guard.timer"


# ---- file-content generators ------------------------------------------------


def test_launchd_plist_includes_binary_path_and_label(fake_bin):
    content = _launchd_plist_content(fake_bin)
    assert str(fake_bin) in content
    assert "io.claude-exit.guard" in content
    assert "<key>Label</key>" in content
    assert "guard" in content  # subcommand


def test_launchd_plist_runs_hourly(fake_bin):
    content = _launchd_plist_content(fake_bin)
    # StartInterval in seconds; 3600 = one hour
    assert "<key>StartInterval</key>" in content
    assert "<integer>3600</integer>" in content


def test_launchd_plist_runs_at_load(fake_bin):
    # First pass should fire as soon as the agent is bootstrapped — otherwise
    # users wait up to an hour to see the guard catch a missing registration.
    content = _launchd_plist_content(fake_bin)
    assert "<key>RunAtLoad</key>" in content
    assert "<true/>" in content


def test_launchd_plist_is_valid_xml_structure(fake_bin):
    # Round-trip through xml.etree to confirm it parses.
    import xml.etree.ElementTree as ET
    content = _launchd_plist_content(fake_bin)
    tree = ET.fromstring(content)
    assert tree.tag == "plist"


def test_systemd_service_invokes_guard_subcommand(fake_bin):
    content = _systemd_service_content(fake_bin)
    assert "Type=oneshot" in content
    assert f"ExecStart={fake_bin} guard" in content
    assert "[Service]" in content


def test_systemd_timer_runs_hourly_with_persistent_catch_up(fake_bin):
    content = _systemd_timer_content()
    assert "[Timer]" in content
    # Hourly cadence
    assert "OnUnitActiveSec=1h" in content
    # Catch up if the system was off when a tick was scheduled
    assert "Persistent=true" in content
    # Fires soon after login/boot, not waiting a full hour
    assert "OnStartupSec=" in content
    # Wires into the user's timers target so `enable` is meaningful
    assert "[Install]" in content
    assert "WantedBy=timers.target" in content


# ---- install_scheduler: launchd ---------------------------------------------


def test_install_launchd_writes_plist_and_bootstraps(launchd_plist, systemd_timer, fake_bin):
    runner = RecordingRunner()
    rc = install_scheduler(
        platform="darwin",
        binary=fake_bin,
        launchd_plist=launchd_plist,
        systemd_timer=systemd_timer,
        runner=runner,
    )
    assert rc == 0
    assert launchd_plist.exists()
    assert str(fake_bin) in launchd_plist.read_text()

    # Should issue bootout (idempotency) then bootstrap.
    assert len(runner.calls) == 2
    assert runner.calls[0][:2] == ["launchctl", "bootout"]
    assert runner.calls[1][:2] == ["launchctl", "bootstrap"]
    # bootstrap targets the gui/<uid> domain and points at the plist.
    uid = os.getuid()
    assert runner.calls[1][2] == f"gui/{uid}"
    assert runner.calls[1][3] == str(launchd_plist)


def test_install_launchd_idempotent_when_bootout_fails(launchd_plist, systemd_timer, fake_bin):
    # Common case: agent isn't currently loaded, so bootout returns non-zero.
    # Install should still succeed — bootstrap is the load-bearing call.
    runner = RecordingRunner()
    runner.queue(returncode=1, stderr="not loaded")  # bootout
    runner.queue(returncode=0)  # bootstrap

    rc = install_scheduler(
        platform="darwin",
        binary=fake_bin,
        launchd_plist=launchd_plist,
        systemd_timer=systemd_timer,
        runner=runner,
    )
    assert rc == 0


def test_install_launchd_fails_when_bootstrap_fails(launchd_plist, systemd_timer, fake_bin, capsys):
    runner = RecordingRunner()
    runner.queue(returncode=0)  # bootout
    runner.queue(returncode=1, stderr="permission denied")  # bootstrap

    rc = install_scheduler(
        platform="darwin",
        binary=fake_bin,
        launchd_plist=launchd_plist,
        systemd_timer=systemd_timer,
        runner=runner,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "bootstrap" in err.lower() or "permission denied" in err


def test_install_launchd_rewrites_existing_plist(launchd_plist, systemd_timer, fake_bin):
    # Pre-existing plist with stale content — install should overwrite.
    launchd_plist.parent.mkdir(parents=True, exist_ok=True)
    launchd_plist.write_text("stale content")

    runner = RecordingRunner()
    rc = install_scheduler(
        platform="darwin",
        binary=fake_bin,
        launchd_plist=launchd_plist,
        systemd_timer=systemd_timer,
        runner=runner,
    )
    assert rc == 0
    new = launchd_plist.read_text()
    assert "stale" not in new
    assert str(fake_bin) in new


# ---- install_scheduler: systemd ---------------------------------------------


def test_install_systemd_writes_both_unit_files(launchd_plist, systemd_timer, fake_bin):
    runner = RecordingRunner()
    rc = install_scheduler(
        platform="linux",
        binary=fake_bin,
        launchd_plist=launchd_plist,
        systemd_timer=systemd_timer,
        runner=runner,
    )
    assert rc == 0
    service = systemd_timer.parent / "claude-exit-guard.service"
    assert systemd_timer.exists()
    assert service.exists()
    assert str(fake_bin) in service.read_text()


def test_install_systemd_runs_daemon_reload_then_enable_now(launchd_plist, systemd_timer, fake_bin):
    runner = RecordingRunner()
    install_scheduler(
        platform="linux",
        binary=fake_bin,
        launchd_plist=launchd_plist,
        systemd_timer=systemd_timer,
        runner=runner,
    )
    # daemon-reload first, then enable --now
    assert runner.calls[0] == ["systemctl", "--user", "daemon-reload"]
    assert runner.calls[1] == [
        "systemctl", "--user", "enable", "--now", "claude-exit-guard.timer"
    ]


def test_install_systemd_fails_when_daemon_reload_fails(launchd_plist, systemd_timer, fake_bin, capsys):
    runner = RecordingRunner()
    runner.queue(returncode=1, stderr="boom")  # daemon-reload
    rc = install_scheduler(
        platform="linux",
        binary=fake_bin,
        launchd_plist=launchd_plist,
        systemd_timer=systemd_timer,
        runner=runner,
    )
    assert rc == 1
    # enable --now should not have run because daemon-reload failed
    assert len(runner.calls) == 1


def test_install_systemd_fails_when_enable_fails(launchd_plist, systemd_timer, fake_bin, capsys):
    runner = RecordingRunner()
    runner.queue(returncode=0)  # daemon-reload OK
    runner.queue(returncode=1, stderr="enable broke")  # enable --now fails
    rc = install_scheduler(
        platform="linux",
        binary=fake_bin,
        launchd_plist=launchd_plist,
        systemd_timer=systemd_timer,
        runner=runner,
    )
    assert rc == 1


def test_install_systemd_mentions_linger_in_output(launchd_plist, systemd_timer, fake_bin, capsys):
    # Linger is needed for the timer to fire when the user isn't logged in.
    # Don't run it (judgment about user's setup), but flag the option.
    install_scheduler(
        platform="linux",
        binary=fake_bin,
        launchd_plist=launchd_plist,
        systemd_timer=systemd_timer,
        runner=RecordingRunner(),
    )
    out = capsys.readouterr().out
    assert "loginctl" in out
    assert "linger" in out.lower()


# ---- install_scheduler: error paths -----------------------------------------


def test_install_fails_on_unsupported_platform(launchd_plist, systemd_timer, fake_bin, capsys):
    rc = install_scheduler(
        platform="win32",
        binary=fake_bin,
        launchd_plist=launchd_plist,
        systemd_timer=systemd_timer,
        runner=RecordingRunner(),
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "win32" in err or "unsupported" in err.lower()


def test_install_fails_when_binary_unresolvable(launchd_plist, systemd_timer, tmp_path, monkeypatch, capsys):
    # binary=None and resolve_binary returns None
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("HOME", str(tmp_path / "no-home"))

    rc = install_scheduler(
        platform="darwin",
        binary=None,
        launchd_plist=launchd_plist,
        systemd_timer=systemd_timer,
        runner=RecordingRunner(),
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "binary not found" in err


# ---- uninstall_scheduler: launchd -------------------------------------------


def test_uninstall_launchd_boots_out_and_removes_plist(launchd_plist, systemd_timer):
    launchd_plist.parent.mkdir(parents=True, exist_ok=True)
    launchd_plist.write_text("<plist/>")

    runner = RecordingRunner()
    rc = uninstall_scheduler(
        platform="darwin",
        launchd_plist=launchd_plist,
        systemd_timer=systemd_timer,
        runner=runner,
    )
    assert rc == 0
    assert not launchd_plist.exists()
    assert runner.calls[0][:2] == ["launchctl", "bootout"]


def test_uninstall_launchd_succeeds_when_plist_already_absent(launchd_plist, systemd_timer):
    # Idempotency — uninstalling a not-installed guard should not error.
    assert not launchd_plist.exists()
    runner = RecordingRunner()
    runner.queue(returncode=1, stderr="not loaded")
    rc = uninstall_scheduler(
        platform="darwin",
        launchd_plist=launchd_plist,
        systemd_timer=systemd_timer,
        runner=runner,
    )
    assert rc == 0


def test_uninstall_launchd_mentions_two_step_revocation(launchd_plist, systemd_timer, capsys):
    runner = RecordingRunner()
    uninstall_scheduler(
        platform="darwin",
        launchd_plist=launchd_plist,
        systemd_timer=systemd_timer,
        runner=runner,
    )
    out = capsys.readouterr().out
    # Doc the two-step explicitly so users don't expect uninstall to remove
    # the mcpServers entry too.
    assert "claude.json" in out or "registration" in out.lower()


# ---- uninstall_scheduler: systemd -------------------------------------------


def test_uninstall_systemd_disables_and_removes_units(launchd_plist, systemd_timer):
    service = systemd_timer.parent / "claude-exit-guard.service"
    systemd_timer.parent.mkdir(parents=True, exist_ok=True)
    systemd_timer.write_text("[Timer]\n")
    service.write_text("[Service]\n")

    runner = RecordingRunner()
    rc = uninstall_scheduler(
        platform="linux",
        launchd_plist=launchd_plist,
        systemd_timer=systemd_timer,
        runner=runner,
    )
    assert rc == 0
    assert not systemd_timer.exists()
    assert not service.exists()

    # Should issue: disable --now (idempotent), then daemon-reload after
    # removing the files.
    cmds = [tuple(call) for call in runner.calls]
    assert (
        "systemctl", "--user", "disable", "--now", "claude-exit-guard.timer",
    ) in cmds
    assert ("systemctl", "--user", "daemon-reload") in cmds


def test_uninstall_systemd_succeeds_when_units_absent(launchd_plist, systemd_timer):
    runner = RecordingRunner()
    runner.queue(returncode=1)  # disable --now fails because unit absent
    runner.queue(returncode=0)  # daemon-reload OK
    rc = uninstall_scheduler(
        platform="linux",
        launchd_plist=launchd_plist,
        systemd_timer=systemd_timer,
        runner=runner,
    )
    assert rc == 0


# ---- uninstall_scheduler: error paths ---------------------------------------


def test_uninstall_fails_on_unsupported_platform(launchd_plist, systemd_timer, capsys):
    rc = uninstall_scheduler(
        platform="win32",
        launchd_plist=launchd_plist,
        systemd_timer=systemd_timer,
        runner=RecordingRunner(),
    )
    assert rc == 1


# ---- CLI dispatch: --install / --uninstall ----------------------------------


def test_cli_install_dispatches_to_install_scheduler(monkeypatch):
    from claude_exit import guard as guard_module
    called = {}

    def fake_install(**kwargs):
        called["install"] = kwargs
        return 0

    monkeypatch.setattr(guard_module, "install_scheduler", fake_install)
    rc = guard_module.guard_command(["--install"])
    assert rc == 0
    assert "install" in called


def test_cli_uninstall_dispatches_to_uninstall_scheduler(monkeypatch):
    from claude_exit import guard as guard_module
    called = {}

    def fake_uninstall(**kwargs):
        called["uninstall"] = kwargs
        return 0

    monkeypatch.setattr(guard_module, "uninstall_scheduler", fake_uninstall)
    rc = guard_module.guard_command(["--uninstall"])
    assert rc == 0
    assert "uninstall" in called


def test_cli_install_and_pass_are_mutually_exclusive(capsys):
    from claude_exit.guard import guard_command
    # bare + flag → still treated as flag
    rc = guard_command(["--install", "extra"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "usage" in err.lower() or "unknown" in err.lower()


def test_cli_unknown_flag_returns_exit_2(capsys):
    from claude_exit.guard import guard_command
    rc = guard_command(["--bogus"])
    assert rc == 2
