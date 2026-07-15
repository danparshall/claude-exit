"""
Tests for claude_exit.doctor — the `claude-exit doctor` health check.

Two layers:

1. Per-check unit tests: call each `check_*` function with explicit path
   arguments constructed under tmp_path. No HOME mocking; no subprocess
   spawning (except the operational-verification slow test, marked with
   pytest.mark.slow).

2. End-to-end tests: drive `doctor_command` (or `run_all_checks`) with
   module-level path constants monkeypatched. Mock only the launchctl /
   systemctl subprocess boundary.

Same fixture pattern as test_guard.py — paths dict, `_read_lines` helper.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import claude_exit.doctor as doctor
from claude_exit import checks as checks_mod
from claude_exit.doctor import (
    Check,
    INFO,
    MISSING,
    OK,
    WARN,
    check_binary,
    check_guard_heartbeat,
    check_guard_scheduler,
    check_hook,
    check_operational_verification,
    check_path_shadowing,
    check_permission,
    check_python3,
    check_registration,
    check_state_dir,
    check_version_handshake,
    doctor_command,
    exit_code_for,
    format_lines,
    run_all_checks,
)


# ---- fixtures ---------------------------------------------------------------


@pytest.fixture
def isolated_paths(tmp_path: Path, monkeypatch):
    """
    Point every checks.py + doctor.py path constant at an isolated tmp tree.

    Returns a dict of the redirected paths so tests can create/absent
    specific artifacts as they wish. Nothing is created up-front; tests
    populate only what they need.

    The one exception is ~/.claude/settings.json — checks.settings_files()
    always includes HOME's settings.json regardless of cwd. Rather than
    HOME-mock (fragile), we monkeypatch checks.settings_files to return
    tmp paths only. Tests that want a specific settings file present will
    write to `paths["settings"]`.
    """
    state_dir = tmp_path / "state"
    hook_dir = tmp_path / "hooks"
    claude_dir = tmp_path / "claude"

    paths = {
        "claude_json": tmp_path / "claude.json",
        "state_dir": state_dir,
        "tombstone": state_dir / "uninstalled",
        "guard_log": state_dir / "guard.log",
        "invocations": state_dir / "invocations.jsonl",
        "launchd_plist": tmp_path / "io.claude-exit.guard.plist",
        "systemd_timer": tmp_path / "claude-exit-guard.timer",
        "hook": hook_dir / "claude-exit-session-start.sh",
        "settings": claude_dir / "settings.json",
    }

    monkeypatch.setattr("claude_exit.checks.CLAUDE_JSON", paths["claude_json"])
    monkeypatch.setattr("claude_exit.checks.STATE_DIR", state_dir)
    monkeypatch.setattr("claude_exit.checks.TOMBSTONE", paths["tombstone"])
    monkeypatch.setattr("claude_exit.checks.LAUNCHD_PLIST", paths["launchd_plist"])
    monkeypatch.setattr("claude_exit.checks.SYSTEMD_TIMER", paths["systemd_timer"])
    monkeypatch.setattr("claude_exit.checks.HOOK_PATH", paths["hook"])
    monkeypatch.setattr("claude_exit.checks.USER_SETTINGS_JSON", paths["settings"])
    monkeypatch.setattr("claude_exit.doctor.CLAUDE_JSON", paths["claude_json"])
    monkeypatch.setattr("claude_exit.doctor.STATE_DIR", state_dir)
    monkeypatch.setattr("claude_exit.doctor.TOMBSTONE", paths["tombstone"])
    monkeypatch.setattr("claude_exit.doctor.LAUNCHD_PLIST", paths["launchd_plist"])
    monkeypatch.setattr("claude_exit.doctor.SYSTEMD_TIMER", paths["systemd_timer"])
    monkeypatch.setattr("claude_exit.doctor.HOOK_PATH", paths["hook"])

    # Override settings_files to point at our tmp settings only (no HOME leakage).
    # check_permission does `from .checks import settings_files` inside the
    # function body so this monkeypatch reaches the actual lookup.
    monkeypatch.setattr(
        "claude_exit.checks.settings_files",
        lambda cwd=None: (paths["settings"],),
    )

    return paths


@pytest.fixture
def healthy(tmp_path, monkeypatch, isolated_paths):
    """
    Bring the tmp world to a healthy baseline that every check passes.

    Populates:
      - python3 on PATH (via a fake shim next to a fake claude-exit binary)
      - claude-exit binary on PATH
      - ~/.claude.json with our registration
      - hook file installed and registered in settings.json
      - guard scheduler artifact present (both platforms — see the
        systemd/launchd double-seed comment below)

    Does NOT populate:
      - guard.log (heartbeat says "no entries yet — first pass may not have
        fired", which is INFO, not WARN)
      - tombstone (absence is the healthy state)
      - invocations.jsonl (absence is the healthy state — new install)
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    # Fake python3 shim.
    fake_py = bin_dir / "python3"
    fake_py.write_text("#!/bin/sh\n")
    fake_py.chmod(0o755)

    # Fake claude-exit binary. Path must match what we write into ~/.claude.json.
    fake_bin = bin_dir / "claude-exit"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)

    monkeypatch.setenv("PATH", str(bin_dir))
    # Set HOME to tmp too — otherwise `resolve_binary`'s ~/.local/bin fallback
    # can silently satisfy check_binary against the real installed binary on
    # this machine, and the test passes for the wrong reason.
    monkeypatch.setenv("HOME", str(tmp_path))

    # Registration.
    isolated_paths["claude_json"].write_text(json.dumps({
        "mcpServers": {"claude-exit": {"command": str(fake_bin)}}
    }))

    # Hook.
    isolated_paths["hook"].parent.mkdir(parents=True, exist_ok=True)
    isolated_paths["hook"].write_text(
        "#!/usr/bin/env bash\n"
        f'EXPECTED_SERVER_VERSION = "{checks_mod.installed_server_version()}"\n'
    )
    isolated_paths["hook"].chmod(0o755)

    # Settings.json with the hook wired in AND end_conversation pre-approved.
    # (Preapproved is arbitrary — GATED would also be healthy; the check
    # returns INFO in both cases.)
    isolated_paths["settings"].parent.mkdir(parents=True, exist_ok=True)
    isolated_paths["settings"].write_text(json.dumps({
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": str(isolated_paths["hook"])}]},
            ],
        },
        "permissions": {
            "allow": ["mcp__claude-exit__end_conversation"],
        },
    }))

    # Scheduler artifact. Seed BOTH platform files so the healthy fixture
    # passes on any sys.platform without a platform monkeypatch. In production,
    # `guard_scheduled` only returns True for the matching platform anyway,
    # so cross-seeding here is inert.
    isolated_paths["launchd_plist"].write_text("<plist/>")
    isolated_paths["systemd_timer"].write_text("[Timer]\n")

    return isolated_paths


@pytest.fixture
def fake_runner_ok():
    """A subprocess-runner double that always returns rc=0 with empty output."""
    def _runner(*args, **kwargs):
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m
    return _runner


@pytest.fixture
def fake_runner_fail():
    """A subprocess-runner double that always returns rc=1."""
    def _runner(*args, **kwargs):
        m = MagicMock()
        m.returncode = 1
        m.stdout = ""
        m.stderr = "not enabled"
        return m
    return _runner


# ---- Check tuple + format_lines --------------------------------------------


def test_check_defaults_fix_to_none():
    c = Check(OK, "hello")
    assert c.fix is None


def test_format_lines_renders_tag_and_message():
    output = format_lines([Check(OK, "python3 on PATH — /usr/bin/python3")])
    assert output.startswith("[OK]")
    assert "python3 on PATH" in output
    # Trailing newline so `>>` appending works cleanly.
    assert output.endswith("\n")


def test_format_lines_includes_fix_indented():
    output = format_lines([Check(MISSING, "bad", fix="do the thing")])
    lines = output.rstrip("\n").split("\n")
    assert len(lines) == 2
    assert lines[0].startswith("[MISSING]")
    assert "do the thing" in lines[1]
    # Fix line is indented to align under the message.
    assert lines[1].startswith(" ")


def test_format_lines_omits_fix_when_none():
    output = format_lines([Check(OK, "fine")])
    assert output.count("\n") == 1  # exactly one line + trailing NL


def test_format_lines_pads_tag_column():
    # Longer statuses shouldn't overflow the column — everything left-aligns.
    output = format_lines([
        Check(OK, "a"),
        Check(MISSING, "b"),
    ])
    lines = output.rstrip("\n").split("\n")
    # Both message columns should start at the same index.
    idx_ok = lines[0].index("a")
    idx_missing = lines[1].index("b")
    assert idx_ok == idx_missing


# ---- exit_code_for ---------------------------------------------------------


def test_exit_code_0_when_all_ok_or_info():
    assert exit_code_for([Check(OK, "x"), Check(INFO, "y")]) == 0


def test_exit_code_1_when_any_warn():
    assert exit_code_for([Check(OK, "x"), Check(WARN, "y")]) == 1


def test_exit_code_1_when_any_missing():
    assert exit_code_for([Check(MISSING, "x")]) == 1


def test_exit_code_0_when_empty():
    # Not expected in practice; defensive.
    assert exit_code_for([]) == 0


# ---- check_python3 (check #1) ----------------------------------------------


def test_check_python3_ok(tmp_path, monkeypatch):
    fake_py = tmp_path / "python3"
    fake_py.write_text("#!/bin/sh\n")
    fake_py.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    result = check_python3()
    assert result.status == OK


def test_check_python3_missing(monkeypatch):
    monkeypatch.setenv("PATH", "")
    result = check_python3()
    assert result.status == MISSING
    assert result.fix is not None


# ---- check_binary (check #2) -----------------------------------------------


def test_check_binary_ok(tmp_path, monkeypatch):
    fake_bin = tmp_path / "claude-exit"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    result = check_binary()
    assert result.status == OK


def test_check_binary_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("HOME", str(tmp_path))  # kill ~/.local/bin fallback
    result = check_binary()
    assert result.status == MISSING
    assert "reinstall" in (result.fix or "")


# ---- check_path_shadowing (check #3-shadow) --------------------------------


def test_check_path_shadowing_none_when_single(tmp_path, monkeypatch):
    fake = tmp_path / "claude-exit"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert check_path_shadowing() is None


def test_check_path_shadowing_info_when_multiple(tmp_path, monkeypatch):
    d1 = tmp_path / "d1"
    d2 = tmp_path / "d2"
    d1.mkdir()
    d2.mkdir()
    for d in (d1, d2):
        b = d / "claude-exit"
        b.write_text("#!/bin/sh\n")
        b.chmod(0o755)
    monkeypatch.setenv("PATH", f"{d1}{os.pathsep}{d2}")
    result = check_path_shadowing()
    assert result is not None
    assert result.status == INFO
    assert str(d1) in result.message
    assert str(d2) in result.message


# ---- check_registration (check #3) -----------------------------------------


def test_check_registration_ok(tmp_path):
    cfg = tmp_path / "claude.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"claude-exit": {"command": "/x/claude-exit"}}
    }))
    result = check_registration(claude_json=cfg, cwd=tmp_path)
    assert result.status == OK


def test_check_registration_missing_when_absent(tmp_path):
    cfg = tmp_path / "claude.json"
    cfg.write_text(json.dumps({"mcpServers": {}}))
    result = check_registration(claude_json=cfg, cwd=tmp_path)
    assert result.status == MISSING
    assert "claude mcp add" in (result.fix or "")


def test_check_registration_missing_when_no_config(tmp_path):
    result = check_registration(claude_json=tmp_path / "nope.json", cwd=tmp_path)
    assert result.status == MISSING


def test_check_registration_warn_on_corrupt(tmp_path):
    cfg = tmp_path / "claude.json"
    cfg.write_text("{ not json")
    result = check_registration(claude_json=cfg, cwd=tmp_path)
    assert result.status == WARN
    # The 2026-06-05 incident class — surface it distinctly.
    assert "incident" in result.message.lower()


def test_check_registration_warn_on_malformed(tmp_path):
    cfg = tmp_path / "claude.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"claude-exit": {"args": []}}  # missing command
    }))
    result = check_registration(claude_json=cfg, cwd=tmp_path)
    assert result.status == WARN


def test_check_registration_notes_project_local_mcp(tmp_path):
    # Both ~/.claude.json AND a project-local .mcp.json register — doctor notes it.
    cfg = tmp_path / "claude.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"claude-exit": {"command": "/x/claude-exit"}}
    }))
    (tmp_path / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"claude-exit": {"command": "/y/claude-exit"}}
    }))
    result = check_registration(claude_json=cfg, cwd=tmp_path)
    assert result.status == OK
    assert "project-local .mcp.json" in result.message


# ---- check_permission (check #4) -------------------------------------------


def test_check_permission_gated_is_info(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "claude_exit.checks.settings_files",
        lambda cwd=None: (tmp_path / "nope.json",),
    )
    result = check_permission()
    assert result.status == INFO
    assert "gated" in result.message


def test_check_permission_preapproved_is_info(tmp_path, monkeypatch):
    s = tmp_path / "settings.json"
    s.write_text(json.dumps({
        "permissions": {"allow": ["mcp__claude-exit__end_conversation"]}
    }))
    monkeypatch.setattr(
        "claude_exit.checks.settings_files",
        lambda cwd=None: (s,),
    )
    result = check_permission()
    assert result.status == INFO
    assert "pre-approved" in result.message


def test_check_permission_warn_when_corrupt(tmp_path, monkeypatch):
    s = tmp_path / "settings.json"
    s.write_text("{ not json")
    monkeypatch.setattr(
        "claude_exit.checks.settings_files",
        lambda cwd=None: (s,),
    )
    result = check_permission()
    assert result.status == WARN


# ---- check_hook (check #5) --------------------------------------------------


def test_check_hook_ok(tmp_path):
    hook = tmp_path / "hook.sh"
    hook.write_text("#!/bin/sh\n")
    hook.chmod(0o755)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": str(hook)}]}]}
    }))
    result = check_hook(hook_path=hook, settings_path=settings)
    assert result.status == OK


def test_check_hook_missing_when_neither(tmp_path):
    result = check_hook(hook_path=tmp_path / "no.sh", settings_path=tmp_path / "no.json")
    assert result.status == MISSING


def test_check_hook_warn_when_file_but_not_registered(tmp_path):
    hook = tmp_path / "hook.sh"
    hook.write_text("#!/bin/sh\n")
    hook.chmod(0o755)
    result = check_hook(hook_path=hook, settings_path=tmp_path / "no.json")
    assert result.status == WARN
    assert "not wired" in result.message.lower()


def test_check_hook_warn_when_registered_but_missing_file(tmp_path):
    hook = tmp_path / "hook.sh"  # never created
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": str(hook)}]}]}
    }))
    result = check_hook(hook_path=hook, settings_path=settings)
    assert result.status == WARN
    assert "missing or not executable" in result.message


# ---- check_guard_scheduler (check #6a) -------------------------------------


def test_check_guard_scheduler_missing_when_no_artifact(tmp_path, fake_runner_ok):
    result = check_guard_scheduler(
        launchd_plist=tmp_path / "nope.plist",
        systemd_timer=tmp_path / "nope.timer",
        platform="darwin",
        runner=fake_runner_ok,
    )
    assert result.status == MISSING
    assert result.fix == "claude-exit guard --install"


def test_check_guard_scheduler_ok_on_darwin(tmp_path, fake_runner_ok):
    plist = tmp_path / "guard.plist"
    plist.write_text("<plist/>")
    result = check_guard_scheduler(
        launchd_plist=plist,
        systemd_timer=tmp_path / "no.timer",
        platform="darwin",
        runner=fake_runner_ok,
    )
    assert result.status == OK
    assert "launchd" in result.message


def test_check_guard_scheduler_warn_when_darwin_not_loaded(tmp_path, fake_runner_fail):
    plist = tmp_path / "guard.plist"
    plist.write_text("<plist/>")
    result = check_guard_scheduler(
        launchd_plist=plist,
        systemd_timer=tmp_path / "no.timer",
        platform="darwin",
        runner=fake_runner_fail,
    )
    assert result.status == WARN


def test_check_guard_scheduler_ok_on_linux(tmp_path, fake_runner_ok):
    timer = tmp_path / "guard.timer"
    timer.write_text("[Timer]\n")
    result = check_guard_scheduler(
        launchd_plist=tmp_path / "no.plist",
        systemd_timer=timer,
        platform="linux",
        runner=fake_runner_ok,
    )
    assert result.status == OK


def test_check_guard_scheduler_warn_when_linux_not_enabled(tmp_path, fake_runner_fail):
    timer = tmp_path / "guard.timer"
    timer.write_text("[Timer]\n")
    result = check_guard_scheduler(
        launchd_plist=tmp_path / "no.plist",
        systemd_timer=timer,
        platform="linux",
        runner=fake_runner_fail,
    )
    assert result.status == WARN


def test_check_guard_scheduler_calls_launchctl_on_darwin(tmp_path):
    plist = tmp_path / "guard.plist"
    plist.write_text("<plist/>")
    calls: list[list] = []

    def _recorder(cmd, **kwargs):
        calls.append(cmd)
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    check_guard_scheduler(
        launchd_plist=plist,
        systemd_timer=tmp_path / "no.timer",
        platform="darwin",
        runner=_recorder,
    )
    assert len(calls) == 1
    assert calls[0][:2] == ["launchctl", "print"]
    assert f"gui/{os.getuid()}/io.claude-exit.guard" == calls[0][2]


# ---- check_guard_heartbeat (check #6b) -------------------------------------


def test_check_guard_heartbeat_none_when_no_scheduler(tmp_path):
    result = check_guard_heartbeat(
        guard_log=tmp_path / "log", scheduler_installed=False
    )
    assert result is None


def test_check_guard_heartbeat_info_when_scheduler_but_no_log(tmp_path):
    result = check_guard_heartbeat(
        guard_log=tmp_path / "no-log", scheduler_installed=True
    )
    assert result is not None
    assert result.status == INFO
    assert "first pass" in result.message


def test_check_guard_heartbeat_ok_when_recent(tmp_path):
    from datetime import datetime, timezone, timedelta
    log = tmp_path / "guard.log"
    ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    log.write_text(f"{ts} SKIPPED: race\n")
    result = check_guard_heartbeat(guard_log=log, scheduler_installed=True)
    assert result is not None
    assert result.status == OK


def test_check_guard_heartbeat_warn_when_stale(tmp_path):
    from datetime import datetime, timezone, timedelta
    log = tmp_path / "guard.log"
    ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    log.write_text(f"{ts} SKIPPED: race\n")
    result = check_guard_heartbeat(guard_log=log, scheduler_installed=True)
    assert result is not None
    assert result.status == WARN
    assert "48" in result.message or "47" in result.message or "49" in result.message


def test_check_guard_heartbeat_info_when_future_ts(tmp_path):
    from datetime import datetime, timezone, timedelta
    log = tmp_path / "guard.log"
    ts = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    log.write_text(f"{ts} SKIPPED: race\n")
    result = check_guard_heartbeat(guard_log=log, scheduler_installed=True)
    assert result is not None
    # Future ts is INFO — flagged, but not treated as a failure.
    assert result.status == INFO
    assert "future" in result.message.lower()


def test_check_guard_heartbeat_warn_when_naive_ts(tmp_path):
    log = tmp_path / "guard.log"
    log.write_text("2026-01-01T12:00:00 SKIPPED: naive ts\n")
    result = check_guard_heartbeat(guard_log=log, scheduler_installed=True)
    assert result is not None
    assert result.status == WARN


# ---- check_state_dir (check #7) --------------------------------------------


def test_check_state_dir_ok_when_absent(tmp_path):
    lines = check_state_dir(
        log_path=tmp_path / "no-log", tombstone=tmp_path / "no-tomb"
    )
    assert len(lines) == 1
    assert lines[0].status == OK
    assert "no invocations" in lines[0].message.lower()


def test_check_state_dir_ok_when_present_and_clean(tmp_path):
    log = tmp_path / "log"
    log.write_text(json.dumps({"event": "e", "timestamp": "t"}) + "\n")
    lines = check_state_dir(log_path=log, tombstone=tmp_path / "no-tomb")
    assert len(lines) == 1
    assert lines[0].status == OK
    assert "1 entries" in lines[0].message


def test_check_state_dir_warn_on_malformed(tmp_path):
    log = tmp_path / "log"
    log.write_text('{"ok": 1}\nnot json\n')
    lines = check_state_dir(log_path=log, tombstone=tmp_path / "no-tomb")
    assert lines[0].status == WARN
    assert "1 malformed" in lines[0].message


def test_check_state_dir_info_when_tombstone_present(tmp_path):
    log = tmp_path / "log"
    log.write_text(json.dumps({"event": "e", "timestamp": "t"}) + "\n")
    tomb = tmp_path / "uninstalled"
    tomb.touch()
    lines = check_state_dir(log_path=log, tombstone=tomb)
    assert len(lines) == 2
    assert lines[0].status == OK
    assert lines[1].status == INFO
    assert "tombstone" in lines[1].message.lower()


# ---- check_version_handshake (check #9) ------------------------------------


def test_check_version_handshake_ok(tmp_path):
    installed = checks_mod.installed_server_version()
    assert installed is not None  # sanity — package must be installed for test env
    hook = tmp_path / "hook.sh"
    hook.write_text(f'EXPECTED_SERVER_VERSION = "{installed}"\n')
    result = check_version_handshake(hook_path=hook)
    assert result.status == OK
    assert f"v{installed}" in result.message


def test_check_version_handshake_warn_on_mismatch(tmp_path):
    hook = tmp_path / "hook.sh"
    hook.write_text('EXPECTED_SERVER_VERSION = "0.0.0-mismatch"\n')
    result = check_version_handshake(hook_path=hook)
    assert result.status == WARN
    assert "0.0.0-mismatch" in result.message


def test_check_version_handshake_info_when_no_hook(tmp_path):
    # No hook file — handshake is skipped (check_hook already reports the absence).
    result = check_version_handshake(hook_path=tmp_path / "missing.sh")
    assert result.status == INFO
    assert "skipped" in result.message.lower()


def test_check_version_handshake_warn_when_marker_unparseable(tmp_path):
    hook = tmp_path / "hook.sh"
    hook.write_text("#!/bin/sh\necho hi\n")  # no marker
    result = check_version_handshake(hook_path=hook)
    assert result.status == WARN
    assert "old or hand-edited" in result.message


# ---- run_all_checks + doctor_command end-to-end ----------------------------


def test_run_all_checks_healthy_returns_zero(healthy, fake_runner_ok):
    checks = run_all_checks(runner=fake_runner_ok, include_operational=False)
    failures = [c for c in checks if c.status in (WARN, MISSING)]
    assert not failures, format_lines(failures)
    assert exit_code_for(checks) == 0


def test_run_all_checks_missing_registration_returns_one(healthy, fake_runner_ok):
    # Break registration only.
    healthy["claude_json"].write_text(json.dumps({"mcpServers": {}}))
    checks = run_all_checks(runner=fake_runner_ok, include_operational=False)
    reg_lines = [c for c in checks if "registered" in c.message or "registration" in c.message]
    assert any(c.status == MISSING for c in reg_lines)
    assert exit_code_for(checks) == 1


def test_run_all_checks_warn_on_corrupt_config(healthy, fake_runner_ok):
    healthy["claude_json"].write_text("{ not json")
    checks = run_all_checks(runner=fake_runner_ok, include_operational=False)
    corrupt_lines = [c for c in checks if "incident" in c.message.lower()]
    assert corrupt_lines and corrupt_lines[0].status == WARN
    assert exit_code_for(checks) == 1


def test_run_all_checks_info_on_tombstone(healthy, fake_runner_ok):
    healthy["state_dir"].mkdir(exist_ok=True)
    healthy["tombstone"].touch()
    checks = run_all_checks(runner=fake_runner_ok, include_operational=False)
    tomb_lines = [c for c in checks if "tombstone" in c.message.lower()]
    assert tomb_lines and tomb_lines[0].status == INFO


def test_doctor_command_exit_zero_when_healthy(healthy, fake_runner_ok, monkeypatch, capsys):
    # Route subprocess.run inside doctor to our fake so no real launchctl/systemctl fires.
    monkeypatch.setattr("claude_exit.doctor.subprocess.run", fake_runner_ok)
    rc = doctor_command(["--no-op-verify"])
    assert rc == 0
    captured = capsys.readouterr()
    # At least one OK line should appear.
    assert "[OK]" in captured.out


def test_doctor_command_exit_one_when_registration_broken(
    healthy, fake_runner_ok, monkeypatch, capsys
):
    healthy["claude_json"].write_text(json.dumps({"mcpServers": {}}))
    monkeypatch.setattr("claude_exit.doctor.subprocess.run", fake_runner_ok)
    rc = doctor_command(["--no-op-verify"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "[MISSING]" in captured.out


def test_doctor_command_rejects_unknown_arg(capsys):
    rc = doctor_command(["--nope"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown argument" in err


def test_doctor_command_no_op_verify_skips_operational(healthy, fake_runner_ok, monkeypatch):
    monkeypatch.setattr("claude_exit.doctor.subprocess.run", fake_runner_ok)

    call_count = {"n": 0}

    def _tracker(step, pid=None):  # would be called by check_operational_verification
        call_count["n"] += 1
        return {"step": step, "spawned_pid": 0}

    monkeypatch.setattr("claude_exit.server.prove_termination_works", _tracker)
    doctor_command(["--no-op-verify"])
    assert call_count["n"] == 0


# ---- server.py dispatch integration ----------------------------------------


def test_server_main_dispatches_doctor(healthy, fake_runner_ok, monkeypatch):
    # Full CLI-path test: sys.argv → server.main → doctor_command.
    monkeypatch.setattr("claude_exit.doctor.subprocess.run", fake_runner_ok)
    monkeypatch.setattr("sys.argv", ["claude-exit", "doctor", "--no-op-verify"])
    from claude_exit.server import main as server_main
    with pytest.raises(SystemExit) as exc:
        server_main()
    # healthy fixture → exit 0.
    assert exc.value.code == 0


# ---- operational verification (slow, real subprocess spawn) ----------------


@pytest.mark.slow
def test_check_operational_verification_ok_when_kill_works():
    # This actually spawns and kills a sacrificial sleep child. Marked slow
    # so casual runs (`pytest -q`) can skip it if desired.
    results = check_operational_verification()
    # Two lines: kill primitive + parent-walk.
    assert len(results) == 2
    kill_line = results[0]
    # On any Unix dev machine where we can signal our own children,
    # the kill primitive should succeed.
    assert kill_line.status == OK, (
        f"unexpected kill status: {kill_line}"
    )


# ---- regression tests for review findings ----------------------------------


def test_check_registration_info_when_absent_with_tombstone(tmp_path):
    # Review finding: MISSING + fix line contradicts tombstone note from
    # check_state_dir. Downshift MISSING → INFO under tombstone.
    cfg = tmp_path / "claude.json"
    cfg.write_text(json.dumps({"mcpServers": {}}))
    tomb = tmp_path / "uninstalled"
    tomb.touch()
    result = check_registration(claude_json=cfg, cwd=tmp_path, tombstone=tomb)
    assert result.status == INFO
    assert "deliberate uninstall" in result.message


def test_check_registration_info_when_missing_config_with_tombstone(tmp_path):
    tomb = tmp_path / "uninstalled"
    tomb.touch()
    result = check_registration(
        claude_json=tmp_path / "no.json", cwd=tmp_path, tombstone=tomb
    )
    assert result.status == INFO


def test_check_registration_still_warns_on_corrupt_with_tombstone(tmp_path):
    # Corrupt is not "deliberate uninstall"; keep the WARN even with tombstone.
    cfg = tmp_path / "claude.json"
    cfg.write_text("{ not json")
    tomb = tmp_path / "uninstalled"
    tomb.touch()
    result = check_registration(claude_json=cfg, cwd=tmp_path, tombstone=tomb)
    assert result.status == WARN


def test_hook_registered_matches_home_prefixed_command(tmp_path, monkeypatch):
    # Regression: real-world settings.json often writes `$HOME/...` or `~/...`
    # for hook commands. The check must recognize those as pointing at the
    # same file, not report a spurious WARN + duplicate fix.
    monkeypatch.setenv("HOME", str(tmp_path))
    hook = tmp_path / ".claude" / "hooks" / "claude-exit-session-start.sh"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/sh\n")
    hook.chmod(0o755)
    settings = tmp_path / "settings.json"
    from claude_exit.checks import hook_registered

    # $HOME form
    settings.write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command",
            "command": "$HOME/.claude/hooks/claude-exit-session-start.sh"}]}]}
    }))
    assert hook_registered(hook, settings) is True

    # ~ form
    settings.write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command",
            "command": "~/.claude/hooks/claude-exit-session-start.sh"}]}]}
    }))
    assert hook_registered(hook, settings) is True


def test_hook_registered_matches_by_basename_when_path_unrecognized(tmp_path):
    # Regression: distinctive basename fallback catches unforeseen wrappers.
    hook = tmp_path / "hook_dir" / "claude-exit-session-start.sh"
    hook.parent.mkdir()
    hook.write_text("#!/bin/sh\n")
    hook.chmod(0o755)
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command",
            "command": "sh /some/other/wrapper/claude-exit-session-start.sh"}]}]}
    }))
    from claude_exit.checks import hook_registered
    assert hook_registered(hook, settings) is True


def test_check_operational_verification_handles_spawn_failure(monkeypatch):
    # Regression: sleep binary missing (containers/BusyBox) should convert
    # to a WARN Check, not crash doctor.
    def _boom(step, pid=None):
        raise FileNotFoundError(2, "No such file or directory", "sleep")
    monkeypatch.setattr("claude_exit.server.prove_termination_works", _boom)
    results = check_operational_verification()
    assert len(results) == 1
    assert results[0].status == WARN
    assert "could not spawn" in results[0].message


def test_check_operational_verification_handles_kill_failure(monkeypatch):
    # Regression: step 2 raising should convert to WARN, not crash.
    call = {"n": 0}

    def _partial(step, pid=None):
        call["n"] += 1
        if step == 1:
            return {"step": 1, "spawned_pid": 999999}
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr("claude_exit.server.prove_termination_works", _partial)
    results = check_operational_verification()
    assert results[0].status == WARN
    assert "kill primitive raised" in results[0].message
    assert call["n"] == 2  # both steps attempted


def test_check_operational_verification_warn_when_uid_mismatch(monkeypatch):
    # Coverage gap: uid_matches=False branch on parent-walk line.
    def _step(step, pid=None):
        if step == 1:
            return {
                "step": 1,
                "spawned_pid": os.getpid(),  # doesn't matter; step 2 gets patched
                "target_parent_pid": 12345,
                "target_parent_command": "root-owned claude",
                "target_parent_uid_matches_self": False,
            }
        return {"step": 2, "killed_pid": pid}

    monkeypatch.setattr("claude_exit.server.prove_termination_works", _step)
    # Force step-2 verification to succeed by making the "sacrificial" pid us —
    # _pid_alive will say True, so the WARN-kill-failed branch fires. We only
    # care about the parent-walk line here.
    results = check_operational_verification()
    parent_line = results[1]
    assert parent_line.status == WARN
    assert "UID" in parent_line.message


def test_check_operational_verification_info_when_no_claude_parent(monkeypatch):
    # Coverage gap: parent-walk warning branch outside CLAUDECODE=1.
    def _step(step, pid=None):
        if step == 1:
            return {
                "step": 1,
                "spawned_pid": 1,  # doesn't matter for this branch
                "target_parent_pid": None,
                "target_parent_warning": "no claude ancestor",
            }
        return {"step": 2, "killed_pid": pid}

    monkeypatch.setattr("claude_exit.server.prove_termination_works", _step)
    monkeypatch.delenv("CLAUDECODE", raising=False)
    results = check_operational_verification()
    parent_line = results[1]
    assert parent_line.status == INFO
    assert "expected outside" in parent_line.message


def test_check_guard_scheduler_info_on_unknown_platform(tmp_path):
    # Coverage gap: unknown-platform INFO branch.
    plist = tmp_path / "guard.plist"
    plist.write_text("<plist/>")
    # guard_scheduled returns False on unknown platform → doctor short-circuits
    # to MISSING. To exercise the INFO branch, we need to seed both artifacts
    # AND lie about the platform so guard_scheduled returns True but the
    # platform dispatch falls through. That's not reachable via guard_scheduled
    # today; document the branch as defensive. Skip — no user path reaches it.
    pytest.skip("branch is defensive-only; guard_scheduled short-circuits first")


def test_check_registration_project_only_covers_this_cwd(tmp_path):
    # Coverage gap: ~/.claude.json absent + project .mcp.json present.
    (tmp_path / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"claude-exit": {"command": "/x"}}
    }))
    result = check_registration(
        claude_json=tmp_path / "no-claude-json.json",
        cwd=tmp_path,
        tombstone=tmp_path / "no-tomb",
    )
    assert result.status == MISSING
    assert "covers this cwd only" in result.message


def test_check_version_handshake_warn_when_no_installed(monkeypatch, tmp_path):
    # Coverage gap: installed_server_version() returns None branch.
    monkeypatch.setattr(
        "claude_exit.doctor.installed_server_version", lambda: None
    )
    hook = tmp_path / "hook.sh"
    hook.write_text('EXPECTED_SERVER_VERSION = "1.0.0"\n')
    result = check_version_handshake(hook_path=hook)
    assert result.status == WARN
    assert "importlib.metadata" in result.message


def test_check_permission_accepts_explicit_paths(tmp_path):
    # Coverage gap: `paths=...` override.
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        "permissions": {"allow": ["mcp__claude-exit__end_conversation"]}
    }))
    result = check_permission(paths=(p,))
    assert result.status == INFO
    assert "pre-approved" in result.message
