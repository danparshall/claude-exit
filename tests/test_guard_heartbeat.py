"""Heartbeat-on-every-pass + stale-scheduler-target coverage.

Motivating incident (2026-08-28, observed on the codex-exit port): a
LaunchAgent, loaded and firing hourly, pointed at a moved source-checkout
path — every fire exited 2, and every existing check stayed green
("plist exists", "job loaded"). Two lessons land here:

  1. guard_pass writes a heartbeat file on EVERY pass — healthy no-op and
     tombstoned included — so a stale heartbeat means exactly one thing:
     the scheduler is not firing the guard.
  2. doctor's scheduler check verifies the loaded job's effective target
     and last exit code, not just loadedness.

Paths are passed as parameters throughout (same pattern as test_guard.py /
test_doctor.py); nothing touches the real HOME.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from claude_exit.checks import guard_heartbeat_timestamp
from claude_exit.doctor import (
    INFO,
    OK,
    WARN,
    check_guard_heartbeat,
    check_guard_scheduler,
)
from claude_exit.guard import guard_pass


# ---- fixtures ---------------------------------------------------------------


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch) -> Path:
    """A fake claude-exit binary on PATH so resolve_binary() finds it."""
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    fake = bin_path / "claude-exit"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return fake


@pytest.fixture
def paths(tmp_path: Path) -> dict:
    return {
        "claude_json": tmp_path / ".claude.json",
        "tombstone": tmp_path / "state" / "uninstalled",
        "guard_log": tmp_path / "state" / "guard.log",
    }


def _heartbeat_for(paths: dict) -> Path:
    # guard_pass's default: guard.heartbeat.json next to the guard log.
    return paths["guard_log"].parent / "guard.heartbeat.json"


def _write_heartbeat_file(path: Path, *, age_hours: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    path.write_text(json.dumps({
        "schema_version": 1,
        "service": "claude-exit",
        "action": "present_well_formed",
        "timestamp_utc": ts.isoformat(),
        "expected_interval_seconds": 3600,
    }))


# ---- guard_pass writes a heartbeat on every branch --------------------------


def test_healthy_noop_writes_heartbeat(paths, fake_bin):
    paths["claude_json"].write_text(json.dumps({
        "mcpServers": {"claude-exit": {"command": "/usr/local/bin/claude-exit"}}
    }))

    rc = guard_pass(**paths)

    assert rc == 0
    hb = json.loads(_heartbeat_for(paths).read_text())
    assert hb["action"] == "present_well_formed"
    assert hb["schema_version"] == 1
    assert hb["expected_interval_seconds"] == 3600
    # Fresh — written moments ago.
    age = datetime.now(timezone.utc) - datetime.fromisoformat(hb["timestamp_utc"])
    assert age.total_seconds() < 60
    # The healthy pass stays silent in guard.log — the heartbeat is the
    # only artifact, which is the point.
    assert not paths["guard_log"].exists()


def test_tombstoned_pass_writes_heartbeat(paths, fake_bin):
    paths["tombstone"].parent.mkdir(parents=True)
    paths["tombstone"].write_text("")

    rc = guard_pass(**paths)

    assert rc == 0
    hb = json.loads(_heartbeat_for(paths).read_text())
    assert hb["action"] == "tombstoned"


def test_restore_pass_writes_heartbeat(paths, fake_bin):
    paths["claude_json"].write_text(json.dumps({"mcpServers": {}}))

    rc = guard_pass(**paths)

    assert rc == 0
    hb = json.loads(_heartbeat_for(paths).read_text())
    assert hb["action"] == "absent"
    # And the restore itself still happened.
    data = json.loads(paths["claude_json"].read_text())
    assert data["mcpServers"]["claude-exit"] == {"command": str(fake_bin)}


def test_heartbeat_rewritten_not_appended(paths, fake_bin):
    paths["claude_json"].write_text(json.dumps({
        "mcpServers": {"claude-exit": {"command": "/usr/local/bin/claude-exit"}}
    }))
    guard_pass(**paths)
    guard_pass(**paths)

    # Still a single JSON object, not JSONL.
    hb = json.loads(_heartbeat_for(paths).read_text())
    assert hb["action"] == "present_well_formed"


def test_unwritable_heartbeat_does_not_fail_the_pass(paths, fake_bin, tmp_path):
    paths["claude_json"].write_text(json.dumps({"mcpServers": {}}))
    # A *file* where the heartbeat's parent dir should be → mkdir raises.
    blocker = tmp_path / "blocked"
    blocker.write_text("")

    rc = guard_pass(**paths, heartbeat=blocker / "guard.heartbeat.json")

    # The pass's real work (the restore) still succeeded.
    assert rc == 0
    data = json.loads(paths["claude_json"].read_text())
    assert "claude-exit" in data["mcpServers"]


# ---- guard_heartbeat_timestamp (checks.py fact) -----------------------------


def test_heartbeat_timestamp_roundtrip(tmp_path):
    hb = tmp_path / "guard.heartbeat.json"
    _write_heartbeat_file(hb, age_hours=0)
    ts = guard_heartbeat_timestamp(hb)
    assert ts is not None
    assert datetime.fromisoformat(ts).tzinfo is not None


@pytest.mark.parametrize("content", [
    None,                       # absent file
    "not json",                 # unparseable
    json.dumps([1, 2, 3]),      # not a dict
    json.dumps({"service": "claude-exit"}),      # missing timestamp_utc
    json.dumps({"timestamp_utc": 12345}),        # wrong type
])
def test_heartbeat_timestamp_none_on_bad_input(tmp_path, content):
    hb = tmp_path / "guard.heartbeat.json"
    if content is not None:
        hb.write_text(content)
    assert guard_heartbeat_timestamp(hb) is None


# ---- check_guard_heartbeat prefers the heartbeat file -----------------------


def test_fresh_heartbeat_file_is_ok_even_with_empty_log(tmp_path):
    hb = tmp_path / "guard.heartbeat.json"
    _write_heartbeat_file(hb, age_hours=0.5)
    result = check_guard_heartbeat(
        guard_log=tmp_path / "guard.log",   # never written — healthy machine
        heartbeat=hb,
        scheduler_installed=True,
    )
    assert result.status == OK


def test_stale_heartbeat_file_warns(tmp_path):
    hb = tmp_path / "guard.heartbeat.json"
    _write_heartbeat_file(hb, age_hours=48)
    result = check_guard_heartbeat(
        guard_log=tmp_path / "guard.log",
        heartbeat=hb,
        scheduler_installed=True,
    )
    assert result.status == WARN
    assert "guard heartbeat" in result.message


def test_missing_heartbeat_falls_back_to_guard_log(tmp_path):
    log = tmp_path / "guard.log"
    ts = datetime.now(timezone.utc).isoformat()
    log.write_text(f"{ts} RESTORED: registration restored\n")
    result = check_guard_heartbeat(
        guard_log=log,
        heartbeat=tmp_path / "no.heartbeat.json",
        scheduler_installed=True,
    )
    assert result.status == OK


def test_neither_heartbeat_nor_log_is_info(tmp_path):
    result = check_guard_heartbeat(
        guard_log=tmp_path / "guard.log",
        heartbeat=tmp_path / "guard.heartbeat.json",
        scheduler_installed=True,
    )
    assert result.status == INFO


# ---- check_guard_scheduler: stale target + last exit code -------------------


def _runner_with(stdout: str, returncode: int = 0):
    def _run(cmd, **kwargs):
        m = MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        m.stderr = ""
        return m
    return _run


def _launchctl_print(binary: str, last_exit: str) -> str:
    return (
        "io.claude-exit.guard = {\n"
        f"\tprogram = {binary}\n"
        "\targuments = {\n"
        f"\t\t{binary}\n"
        "\t\tguard\n"
        "\t}\n"
        f"\tlast exit code = {last_exit}\n"
        "}\n"
    )


def test_darwin_warns_on_stale_target(tmp_path, monkeypatch):
    plist = tmp_path / "guard.plist"
    plist.write_text("<plist/>")
    installed = tmp_path / "installed" / "claude-exit"
    monkeypatch.setattr(
        "claude_exit.doctor.resolve_binary", lambda: installed
    )
    # Loaded job still runs a moved checkout script — the incident shape.
    output = _launchctl_print(
        "/usr/bin/python3 /old/checkout/scripts/guard.py", "2"
    )
    result = check_guard_scheduler(
        launchd_plist=plist,
        systemd_timer=tmp_path / "no.timer",
        platform="darwin",
        runner=_runner_with(output),
    )
    assert result.status == WARN
    assert "stale" in result.message
    assert "--install" in result.fix


def test_darwin_warns_on_nonzero_last_exit(tmp_path, monkeypatch):
    plist = tmp_path / "guard.plist"
    plist.write_text("<plist/>")
    installed = tmp_path / "installed" / "claude-exit"
    monkeypatch.setattr(
        "claude_exit.doctor.resolve_binary", lambda: installed
    )
    output = _launchctl_print(str(installed), "2")
    result = check_guard_scheduler(
        launchd_plist=plist,
        systemd_timer=tmp_path / "no.timer",
        platform="darwin",
        runner=_runner_with(output),
    )
    assert result.status == WARN
    assert "exited 2" in result.message


def test_darwin_ok_with_matching_target_and_clean_exit(tmp_path, monkeypatch):
    plist = tmp_path / "guard.plist"
    plist.write_text("<plist/>")
    installed = tmp_path / "installed" / "claude-exit"
    monkeypatch.setattr(
        "claude_exit.doctor.resolve_binary", lambda: installed
    )
    output = _launchctl_print(str(installed), "0")
    result = check_guard_scheduler(
        launchd_plist=plist,
        systemd_timer=tmp_path / "no.timer",
        platform="darwin",
        runner=_runner_with(output),
    )
    assert result.status == OK


def test_darwin_never_exited_is_not_a_failure(tmp_path, monkeypatch):
    plist = tmp_path / "guard.plist"
    plist.write_text("<plist/>")
    installed = tmp_path / "installed" / "claude-exit"
    monkeypatch.setattr(
        "claude_exit.doctor.resolve_binary", lambda: installed
    )
    output = _launchctl_print(str(installed), "(never exited)")
    result = check_guard_scheduler(
        launchd_plist=plist,
        systemd_timer=tmp_path / "no.timer",
        platform="darwin",
        runner=_runner_with(output),
    )
    assert result.status == OK


def test_darwin_empty_print_output_stays_ok(tmp_path, monkeypatch):
    # Formatting we don't recognize (or terse output) must not false-alarm.
    plist = tmp_path / "guard.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(
        "claude_exit.doctor.resolve_binary",
        lambda: tmp_path / "installed" / "claude-exit",
    )
    result = check_guard_scheduler(
        launchd_plist=plist,
        systemd_timer=tmp_path / "no.timer",
        platform="darwin",
        runner=_runner_with(""),
    )
    assert result.status == OK


def test_linux_warns_on_stale_exec_start(tmp_path, monkeypatch):
    timer = tmp_path / "guard.timer"
    timer.write_text("[Timer]\n")
    installed = tmp_path / "installed" / "claude-exit"
    monkeypatch.setattr(
        "claude_exit.doctor.resolve_binary", lambda: installed
    )
    exec_start = (
        "ExecStart={ path=/usr/bin/python3 ; "
        "argv[]=/usr/bin/python3 /old/checkout/scripts/guard.py }"
    )
    result = check_guard_scheduler(
        launchd_plist=tmp_path / "no.plist",
        systemd_timer=timer,
        platform="linux",
        runner=_runner_with(exec_start),
    )
    assert result.status == WARN
    assert "stale" in result.message


def test_linux_ok_with_matching_exec_start(tmp_path, monkeypatch):
    timer = tmp_path / "guard.timer"
    timer.write_text("[Timer]\n")
    installed = tmp_path / "installed" / "claude-exit"
    monkeypatch.setattr(
        "claude_exit.doctor.resolve_binary", lambda: installed
    )
    exec_start = f"ExecStart={{ path={installed} ; argv[]={installed} guard }}"
    result = check_guard_scheduler(
        launchd_plist=tmp_path / "no.plist",
        systemd_timer=timer,
        platform="linux",
        runner=_runner_with(exec_start),
    )
    assert result.status == OK


def test_linux_empty_exec_start_stays_ok(tmp_path, monkeypatch):
    timer = tmp_path / "guard.timer"
    timer.write_text("[Timer]\n")
    monkeypatch.setattr(
        "claude_exit.doctor.resolve_binary",
        lambda: tmp_path / "installed" / "claude-exit",
    )
    result = check_guard_scheduler(
        launchd_plist=tmp_path / "no.plist",
        systemd_timer=timer,
        platform="linux",
        runner=_runner_with("ExecStart="),
    )
    assert result.status == OK
