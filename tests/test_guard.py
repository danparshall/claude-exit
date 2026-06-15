"""Unit tests for claude_exit.guard (core pass; no scheduler yet).

The guard runs once per hour (out-of-band, via launchd/systemd timer) and
restores the claude-exit entry in ~/.claude.json if it's gone. Scope here:
the single-pass logic. Scheduler install/uninstall lands in a follow-up.

Paths are passed as parameters to every function so tests can use tmp_path
without mocking HOME (same pattern as test_cli.py / test_checks.py).
"""

import json
import os
from pathlib import Path

import pytest

from claude_exit.guard import (
    _atomic_replace_if_unchanged,
    _log_guard,
    _stat_snapshot,
    guard_pass,
)


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
    # Empty HOME so ~/.local/bin fallback doesn't catch anything stray.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return fake


@pytest.fixture
def paths(tmp_path: Path) -> dict:
    return {
        "claude_json": tmp_path / ".claude.json",
        "tombstone": tmp_path / ".claude-exit" / "uninstalled",
        "guard_log": tmp_path / ".claude-exit" / "guard.log",
    }


def _read_log_lines(log: Path) -> list[str]:
    if not log.exists():
        return []
    return [line for line in log.read_text().splitlines() if line.strip()]


# ---- guard_pass: tombstone --------------------------------------------------


def test_tombstone_makes_guard_silent_and_no_op(paths, fake_bin):
    paths["tombstone"].parent.mkdir(parents=True, exist_ok=True)
    paths["tombstone"].touch()
    # Registration is missing — guard would normally restore. Tombstone wins.
    rc = guard_pass(**paths)
    assert rc == 0
    assert not paths["claude_json"].exists()
    assert _read_log_lines(paths["guard_log"]) == []


# ---- guard_pass: PRESENT_WELL_FORMED ----------------------------------------


def test_present_well_formed_is_silent_no_op(paths, fake_bin):
    paths["claude_json"].write_text(json.dumps({
        "mcpServers": {"claude-exit": {"command": "/usr/local/bin/claude-exit"}}
    }))
    before = paths["claude_json"].read_text()
    before_stat = paths["claude_json"].stat()

    rc = guard_pass(**paths)

    assert rc == 0
    assert paths["claude_json"].read_text() == before
    assert paths["claude_json"].stat().st_mtime_ns == before_stat.st_mtime_ns
    assert _read_log_lines(paths["guard_log"]) == []


# ---- guard_pass: PRESENT_MALFORMED ------------------------------------------


def test_present_malformed_logs_warn_and_does_not_overwrite(paths, fake_bin):
    # Key exists but value is the wrong shape. Don't clobber — might be intent.
    paths["claude_json"].write_text(json.dumps({
        "mcpServers": {"claude-exit": "not-a-dict"}
    }))
    before = paths["claude_json"].read_text()

    rc = guard_pass(**paths)

    assert rc == 0
    assert paths["claude_json"].read_text() == before
    lines = _read_log_lines(paths["guard_log"])
    assert len(lines) == 1
    assert "WARN" in lines[0]
    assert "malformed" in lines[0].lower()


# ---- guard_pass: CONFIG_CORRUPT ---------------------------------------------


def test_config_corrupt_logs_warn_and_does_not_overwrite(paths, fake_bin):
    paths["claude_json"].write_text("{ this is not json")
    before = paths["claude_json"].read_text()

    rc = guard_pass(**paths)

    assert rc == 0
    assert paths["claude_json"].read_text() == before
    lines = _read_log_lines(paths["guard_log"])
    assert len(lines) == 1
    assert "WARN" in lines[0]
    assert "corrupt" in lines[0].lower()


# ---- guard_pass: ABSENT (the incident case) ---------------------------------


def test_absent_restores_entry_with_resolved_binary_path(paths, fake_bin):
    paths["claude_json"].write_text(json.dumps({
        "mcpServers": {"other-server": {"command": "/elsewhere/x"}}
    }))

    rc = guard_pass(**paths)

    assert rc == 0
    data = json.loads(paths["claude_json"].read_text())
    assert data["mcpServers"]["claude-exit"] == {"command": str(fake_bin)}
    # Other servers preserved.
    assert data["mcpServers"]["other-server"] == {"command": "/elsewhere/x"}

    lines = _read_log_lines(paths["guard_log"])
    assert len(lines) == 1
    assert "RESTORED" in lines[0]


def test_absent_preserves_unrelated_top_level_keys(paths, fake_bin):
    paths["claude_json"].write_text(json.dumps({
        "mcpServers": {},
        "someOtherTopLevelKey": {"nested": "value"},
        "numericField": 42,
    }))

    guard_pass(**paths)

    data = json.loads(paths["claude_json"].read_text())
    assert data["someOtherTopLevelKey"] == {"nested": "value"}
    assert data["numericField"] == 42
    assert data["mcpServers"]["claude-exit"] == {"command": str(fake_bin)}


def test_absent_with_no_mcp_servers_key_adds_one(paths, fake_bin):
    # File exists, no mcpServers key at all.
    paths["claude_json"].write_text(json.dumps({"otherKey": 1}))

    rc = guard_pass(**paths)

    assert rc == 0
    data = json.loads(paths["claude_json"].read_text())
    assert data["mcpServers"]["claude-exit"] == {"command": str(fake_bin)}
    assert data["otherKey"] == 1


# ---- guard_pass: CONFIG_MISSING ---------------------------------------------


def test_config_missing_creates_file_with_only_our_entry(paths, fake_bin):
    assert not paths["claude_json"].exists()

    rc = guard_pass(**paths)

    assert rc == 0
    assert paths["claude_json"].exists()
    data = json.loads(paths["claude_json"].read_text())
    assert data == {"mcpServers": {"claude-exit": {"command": str(fake_bin)}}}

    lines = _read_log_lines(paths["guard_log"])
    assert len(lines) == 1
    assert "RESTORED" in lines[0]


def test_config_missing_writes_with_mode_0600(paths, fake_bin):
    # Sensitive-config convention; matches Appendix A's recommendation.
    guard_pass(**paths)
    mode = paths["claude_json"].stat().st_mode & 0o777
    assert mode == 0o600


# ---- guard_pass: binary not resolvable --------------------------------------


def test_binary_not_resolvable_logs_error_and_does_not_write(paths, tmp_path, monkeypatch):
    # Empty PATH and empty HOME → resolve_binary returns None.
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))

    rc = guard_pass(**paths)

    assert rc == 1
    assert not paths["claude_json"].exists()
    lines = _read_log_lines(paths["guard_log"])
    assert len(lines) == 1
    assert "ERROR" in lines[0]
    assert "claude-exit binary not found" in lines[0]


def test_binary_not_resolvable_does_not_overwrite_existing_config(paths, tmp_path, monkeypatch):
    # Defensive: ABSENT case (file exists, no entry) + no binary should not
    # clobber the user's file with an empty/malformed write.
    paths["claude_json"].write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    before = paths["claude_json"].read_text()
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))

    rc = guard_pass(**paths)

    assert rc == 1
    assert paths["claude_json"].read_text() == before


# ---- _atomic_replace_if_unchanged: race detection ---------------------------


def test_atomic_replace_succeeds_when_snapshot_matches(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("original")
    snapshot = _stat_snapshot(target)

    replaced = _atomic_replace_if_unchanged(target, "new content", snapshot)

    assert replaced is True
    assert target.read_text() == "new content"


def test_atomic_replace_skips_when_file_changed_underfoot(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("original")
    stale_snapshot = _stat_snapshot(target)

    # Someone else writes to the file after our snapshot.
    target.write_text("written by someone else")

    replaced = _atomic_replace_if_unchanged(target, "our new content", stale_snapshot)

    assert replaced is False
    assert target.read_text() == "written by someone else"


def test_atomic_replace_creates_file_when_snapshot_is_none(tmp_path):
    target = tmp_path / "new.json"
    assert not target.exists()

    replaced = _atomic_replace_if_unchanged(target, "fresh content", snapshot=None)

    assert replaced is True
    assert target.read_text() == "fresh content"


def test_atomic_replace_skips_create_when_file_appeared_underfoot(tmp_path):
    target = tmp_path / "raced.json"
    # Our snapshot says "file doesn't exist."
    snapshot = None
    # But someone else creates it before we replace.
    target.write_text("they got here first")

    replaced = _atomic_replace_if_unchanged(target, "we'd have written this", snapshot)

    assert replaced is False
    assert target.read_text() == "they got here first"


def test_atomic_replace_does_not_leave_tmp_files_on_skip(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("original")
    stale_snapshot = _stat_snapshot(target)
    target.write_text("changed")

    _atomic_replace_if_unchanged(target, "new", stale_snapshot)

    # No stray tmp files left behind.
    stragglers = [p for p in tmp_path.iterdir() if p.name != "config.json"]
    assert stragglers == []


# ---- _log_guard: format -----------------------------------------------------


def test_log_guard_appends_iso_timestamp_and_message(tmp_path):
    log = tmp_path / "guard.log"
    _log_guard(log, "RESTORED", "added entry")
    line = log.read_text().strip()
    # ISO 8601 UTC: 2026-01-01T12:34:56.789+00:00 (or similar). Just check
    # the message and level are present, and there's something at the start
    # that looks like a year.
    assert "RESTORED: added entry" in line
    assert line[:4].startswith("20")  # year prefix


def test_log_guard_creates_parent_dir(tmp_path):
    log = tmp_path / "deep" / "nested" / "guard.log"
    _log_guard(log, "WARN", "test")
    assert log.exists()


def test_log_guard_appends_does_not_clobber(tmp_path):
    log = tmp_path / "guard.log"
    _log_guard(log, "RESTORED", "first")
    _log_guard(log, "WARN", "second")
    lines = _read_log_lines(log)
    assert len(lines) == 2
    assert "first" in lines[0]
    assert "second" in lines[1]


# ---- CLI: claude-exit guard dispatch ----------------------------------------


def test_cli_guard_dispatches_to_guard_pass(tmp_path, monkeypatch, capsys):
    """`claude-exit guard` (no args) runs one pass and exits 0 on success."""
    from claude_exit.server import main as server_main

    # Make resolve_binary find something.
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    fake = bin_path / "claude-exit"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    # Patch the module-level constants so CLI invocation hits tmp_path.
    claude_json = tmp_path / ".claude.json"
    state_dir = tmp_path / ".claude-exit"
    monkeypatch.setattr("claude_exit.guard.CLAUDE_JSON", claude_json)
    monkeypatch.setattr("claude_exit.guard.TOMBSTONE", state_dir / "uninstalled")
    monkeypatch.setattr("claude_exit.guard.GUARD_LOG", state_dir / "guard.log")

    monkeypatch.setattr("sys.argv", ["claude-exit", "guard"])
    with pytest.raises(SystemExit) as exc_info:
        server_main()
    assert exc_info.value.code == 0
    # CONFIG_MISSING → file was created.
    assert claude_json.exists()
