"""Unit tests for claude_exit.checks.

Pure-read predicates shared between the guard subcommand and doctor.
The five-way registration_state classifier is the central piece — both
guard and doctor branch on those values.

Paths are passed as parameters to every function so tests can use
tmp_path without mocking HOME (same pattern as test_cli.py).
"""

import json
import os
from pathlib import Path

import pytest

from claude_exit.checks import (
    REG_ABSENT,
    REG_CONFIG_CORRUPT,
    REG_CONFIG_MISSING,
    REG_PRESENT_MALFORMED,
    REG_PRESENT_WELL_FORMED,
    guard_scheduled,
    registration_state,
    resolve_binary,
    tombstone_present,
)


# ---- registration_state -----------------------------------------------------


def test_state_is_config_missing_when_file_absent(tmp_path):
    assert registration_state(tmp_path / "no-such.json") == REG_CONFIG_MISSING


def test_state_is_config_corrupt_when_file_unparseable(tmp_path):
    config = tmp_path / "claude.json"
    config.write_text("{ this is not json")
    assert registration_state(config) == REG_CONFIG_CORRUPT


def test_state_is_config_corrupt_when_file_empty(tmp_path):
    # Empty file: json.loads raises. Don't write to an empty file blindly —
    # might be a half-written ad-hoc edit by the user mid-save.
    config = tmp_path / "claude.json"
    config.write_text("")
    assert registration_state(config) == REG_CONFIG_CORRUPT


def test_state_is_config_corrupt_when_top_level_not_object(tmp_path):
    config = tmp_path / "claude.json"
    config.write_text("[]")
    assert registration_state(config) == REG_CONFIG_CORRUPT


def test_state_is_absent_when_no_mcp_servers_key(tmp_path):
    config = tmp_path / "claude.json"
    config.write_text(json.dumps({"otherKey": 1}))
    assert registration_state(config) == REG_ABSENT


def test_state_is_config_corrupt_when_mcp_servers_wrong_type(tmp_path):
    # Key exists but is the wrong shape — meaningful malformation, not just
    # absence. Guard should not overwrite (might be an unusual deliberate edit).
    config = tmp_path / "claude.json"
    config.write_text(json.dumps({"mcpServers": "not a dict"}))
    assert registration_state(config) == REG_CONFIG_CORRUPT


def test_state_is_absent_when_mcp_servers_empty(tmp_path):
    config = tmp_path / "claude.json"
    config.write_text(json.dumps({"mcpServers": {}}))
    assert registration_state(config) == REG_ABSENT


def test_state_is_absent_when_claude_exit_key_missing(tmp_path):
    config = tmp_path / "claude.json"
    config.write_text(json.dumps({"mcpServers": {"other-server": {"command": "x"}}}))
    assert registration_state(config) == REG_ABSENT


def test_state_is_present_well_formed_when_value_has_command(tmp_path):
    config = tmp_path / "claude.json"
    config.write_text(json.dumps({
        "mcpServers": {"claude-exit": {"command": "/usr/local/bin/claude-exit"}}
    }))
    assert registration_state(config) == REG_PRESENT_WELL_FORMED


def test_state_is_present_malformed_when_value_not_a_dict(tmp_path):
    # Existing key, but mangled value. Don't overwrite — might be deliberate.
    config = tmp_path / "claude.json"
    config.write_text(json.dumps({"mcpServers": {"claude-exit": "string-not-dict"}}))
    assert registration_state(config) == REG_PRESENT_MALFORMED


def test_state_is_present_malformed_when_command_missing(tmp_path):
    config = tmp_path / "claude.json"
    config.write_text(json.dumps({"mcpServers": {"claude-exit": {"args": []}}}))
    assert registration_state(config) == REG_PRESENT_MALFORMED


def test_state_is_present_malformed_when_command_empty(tmp_path):
    config = tmp_path / "claude.json"
    config.write_text(json.dumps({"mcpServers": {"claude-exit": {"command": ""}}}))
    assert registration_state(config) == REG_PRESENT_MALFORMED


def test_state_preserves_other_servers_unread(tmp_path):
    # registration_state should be unaffected by other entries — proves we
    # aren't accidentally validating siblings.
    config = tmp_path / "claude.json"
    config.write_text(json.dumps({
        "mcpServers": {
            "other-server": {"weird": "shape"},
            "claude-exit": {"command": "/usr/local/bin/claude-exit"},
        }
    }))
    assert registration_state(config) == REG_PRESENT_WELL_FORMED


# ---- resolve_binary ---------------------------------------------------------


def test_resolve_binary_uses_which_when_on_path(tmp_path, monkeypatch):
    fake_bin = tmp_path / "claude-exit"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert resolve_binary() == fake_bin


def test_resolve_binary_falls_back_to_local_bin(tmp_path, monkeypatch):
    # Empty PATH so shutil.which fails; ~/.local/bin/claude-exit is the fallback.
    fake_home = tmp_path
    local_bin = fake_home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    fake_bin = local_bin / "claude-exit"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)

    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("HOME", str(fake_home))
    assert resolve_binary() == fake_bin


def test_resolve_binary_returns_none_when_nothing_found(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_binary() is None


def test_resolve_binary_skips_fallback_when_not_executable(tmp_path, monkeypatch):
    # Defensive: if a non-executable file is at the fallback path, don't return it —
    # the guard would write a useless registration that fails on first invoke.
    fake_home = tmp_path
    local_bin = fake_home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    fake_bin = local_bin / "claude-exit"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o644)  # not executable

    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("HOME", str(fake_home))
    assert resolve_binary() is None


# ---- tombstone_present ------------------------------------------------------


def test_tombstone_absent_returns_false(tmp_path):
    assert tombstone_present(tmp_path / "uninstalled") is False


def test_tombstone_present_returns_true(tmp_path):
    path = tmp_path / "uninstalled"
    path.write_text("")
    assert tombstone_present(path) is True


def test_tombstone_present_does_not_require_content(tmp_path):
    # Tombstone is the existence of the file; content is irrelevant.
    path = tmp_path / "uninstalled"
    path.touch()
    assert tombstone_present(path) is True


# ---- guard_scheduled --------------------------------------------------------


def test_guard_scheduled_true_on_darwin_when_plist_exists(tmp_path):
    plist = tmp_path / "io.claude-exit.guard.plist"
    plist.write_text("<plist/>")
    assert guard_scheduled(
        launchd_plist=plist,
        systemd_timer=tmp_path / "no-timer",
        platform="darwin",
    ) is True


def test_guard_scheduled_false_on_darwin_when_plist_missing(tmp_path):
    assert guard_scheduled(
        launchd_plist=tmp_path / "missing.plist",
        systemd_timer=tmp_path / "missing.timer",
        platform="darwin",
    ) is False


def test_guard_scheduled_true_on_linux_when_timer_exists(tmp_path):
    timer = tmp_path / "claude-exit-guard.timer"
    timer.write_text("[Timer]\n")
    assert guard_scheduled(
        launchd_plist=tmp_path / "no-plist",
        systemd_timer=timer,
        platform="linux",
    ) is True


def test_guard_scheduled_false_on_linux_when_timer_missing(tmp_path):
    assert guard_scheduled(
        launchd_plist=tmp_path / "missing.plist",
        systemd_timer=tmp_path / "missing.timer",
        platform="linux",
    ) is False


def test_guard_scheduled_false_on_other_platforms(tmp_path):
    # Package is Unix-only — Windows/etc. always report False.
    plist = tmp_path / "p.plist"
    plist.write_text("x")
    timer = tmp_path / "t.timer"
    timer.write_text("x")
    assert guard_scheduled(
        launchd_plist=plist, systemd_timer=timer, platform="win32"
    ) is False


def test_guard_scheduled_does_not_cross_platform(tmp_path):
    # On darwin, a stray systemd timer file should not count.
    timer = tmp_path / "claude-exit-guard.timer"
    timer.write_text("[Timer]\n")
    assert guard_scheduled(
        launchd_plist=tmp_path / "missing.plist",
        systemd_timer=timer,
        platform="darwin",
    ) is False
