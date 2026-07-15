"""Unit tests for claude_exit.checks.

Pure-read predicates shared between the guard subcommand and doctor.
The five-way registration_state classifier is the central piece — both
guard and doctor branch on those values.

Paths are passed as parameters to every function so tests can use
tmp_path without mocking HOME (same pattern as test_cli.py).
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_exit.checks import (
    GATED,
    PREAPPROVED,
    REG_ABSENT,
    REG_CONFIG_CORRUPT,
    REG_CONFIG_MISSING,
    REG_PRESENT_MALFORMED,
    REG_PRESENT_WELL_FORMED,
    SETTINGS_CORRUPT,
    guard_last_heartbeat,
    guard_scheduled,
    hook_expected_server_version,
    hook_installed,
    hook_registered,
    hours_since,
    installed_server_version,
    invocations_health,
    path_shadowing,
    preapproval_state,
    project_mcp_json_registers,
    python3_on_path,
    registration_state,
    resolve_binary,
    settings_files,
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


# ---- python3_on_path --------------------------------------------------------


def test_python3_on_path_when_present(tmp_path, monkeypatch):
    fake_py = tmp_path / "python3"
    fake_py.write_text("#!/bin/sh\n")
    fake_py.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert python3_on_path() == fake_py


def test_python3_on_path_when_absent(monkeypatch):
    monkeypatch.setenv("PATH", "")
    assert python3_on_path() is None


# ---- path_shadowing ---------------------------------------------------------


def test_path_shadowing_returns_all_hits_in_order(tmp_path, monkeypatch):
    # Two dirs, each with a claude-exit executable — both must appear.
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    bin_a = dir_a / "claude-exit"
    bin_b = dir_b / "claude-exit"
    for b in (bin_a, bin_b):
        b.write_text("#!/bin/sh\n")
        b.chmod(0o755)
    monkeypatch.setenv("PATH", f"{dir_a}{os.pathsep}{dir_b}")
    hits = path_shadowing("claude-exit")
    assert hits == [bin_a, bin_b]


def test_path_shadowing_returns_single_when_no_shadow(tmp_path, monkeypatch):
    fake_bin = tmp_path / "claude-exit"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert path_shadowing("claude-exit") == [fake_bin]


def test_path_shadowing_returns_empty_when_missing(monkeypatch):
    monkeypatch.setenv("PATH", "")
    assert path_shadowing("nonexistent-binary") == []


def test_path_shadowing_skips_non_executable(tmp_path, monkeypatch):
    # A file with the right name but not +x doesn't count — it isn't runnable.
    fake_bin = tmp_path / "claude-exit"
    fake_bin.write_text("#!/bin/sh\n")
    fake_bin.chmod(0o644)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert path_shadowing("claude-exit") == []


def test_path_shadowing_dedupes_symlink_to_same_target(tmp_path, monkeypatch):
    # Two PATH entries whose claude-exit both resolve to the same file
    # (via symlink) count as one — the symlink case is common and
    # reporting it as "shadowing" would be a false positive.
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real = real_dir / "claude-exit"
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)

    link_dir = tmp_path / "link"
    link_dir.mkdir()
    link = link_dir / "claude-exit"
    link.symlink_to(real)

    monkeypatch.setenv("PATH", f"{link_dir}{os.pathsep}{real_dir}")
    hits = path_shadowing("claude-exit")
    assert len(hits) == 1


# ---- project_mcp_json_registers --------------------------------------------


def test_project_mcp_json_registers_true_when_key_present(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"claude-exit": {"command": "/x"}}
    }))
    assert project_mcp_json_registers(tmp_path) is True


def test_project_mcp_json_registers_false_when_absent(tmp_path):
    assert project_mcp_json_registers(tmp_path) is False


def test_project_mcp_json_registers_false_when_key_missing(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"other": {"command": "/x"}}
    }))
    assert project_mcp_json_registers(tmp_path) is False


def test_project_mcp_json_registers_false_when_file_corrupt(tmp_path):
    (tmp_path / ".mcp.json").write_text("{ not json")
    assert project_mcp_json_registers(tmp_path) is False


def test_project_mcp_json_registers_false_when_shape_wrong(tmp_path):
    # mcpServers is a string, not a dict — treat as unregistered rather
    # than crash.
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": "wrong"}))
    assert project_mcp_json_registers(tmp_path) is False


# ---- preapproval_state -----------------------------------------------------


def _write_allow(path: Path, entries: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"permissions": {"allow": entries}}))


def test_preapproval_state_gated_when_no_files(tmp_path):
    paths = (
        tmp_path / "a.json",
        tmp_path / "b.json",
        tmp_path / "c.json",
    )
    assert preapproval_state(paths) == GATED


def test_preapproval_state_gated_when_allow_empty(tmp_path):
    p = tmp_path / "settings.json"
    _write_allow(p, [])
    assert preapproval_state((p,)) == GATED


def test_preapproval_state_preapproved_on_exact_grant(tmp_path):
    p = tmp_path / "settings.json"
    _write_allow(p, ["mcp__claude-exit__end_conversation"])
    assert preapproval_state((p,)) == PREAPPROVED


def test_preapproval_state_preapproved_on_wildcard_grant(tmp_path):
    p = tmp_path / "settings.json"
    _write_allow(p, ["mcp__claude-exit__*"])
    assert preapproval_state((p,)) == PREAPPROVED


def test_preapproval_state_preapproved_on_server_level_grant(tmp_path):
    p = tmp_path / "settings.json"
    _write_allow(p, ["mcp__claude-exit"])
    assert preapproval_state((p,)) == PREAPPROVED


def test_preapproval_state_any_of_three_files_grants(tmp_path):
    # Third file grants; first two don't — still PREAPPROVED.
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    c = tmp_path / "c.json"
    _write_allow(a, [])
    _write_allow(b, [])
    _write_allow(c, ["mcp__claude-exit"])
    assert preapproval_state((a, b, c)) == PREAPPROVED


def test_preapproval_state_settings_corrupt_when_file_unparseable(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{ not json")
    assert preapproval_state((p,)) == SETTINGS_CORRUPT


def test_preapproval_state_corrupt_wins_over_gated(tmp_path):
    # One good file with no grant + one corrupt file → SETTINGS_CORRUPT
    # (report the broken state rather than let it read as intentional deny).
    good = tmp_path / "good.json"
    bad = tmp_path / "bad.json"
    _write_allow(good, [])
    bad.write_text("{ not json")
    assert preapproval_state((good, bad)) == SETTINGS_CORRUPT


def test_preapproval_state_preapproved_wins_over_corrupt(tmp_path):
    # A live grant somewhere trumps corruption elsewhere — the grant is
    # what determines runtime behavior; the corruption is a separate signal
    # that will surface in later doctor checks.
    good = tmp_path / "good.json"
    bad = tmp_path / "bad.json"
    _write_allow(good, ["mcp__claude-exit"])
    bad.write_text("{ not json")
    assert preapproval_state((good, bad)) == PREAPPROVED


def test_preapproval_state_ignores_non_string_allow_entries(tmp_path):
    # Defensive: an int / object in the allow list shouldn't crash the check.
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({
        "permissions": {"allow": [42, {"nested": "obj"}, "unrelated-tool"]}
    }))
    assert preapproval_state((p,)) == GATED


def test_settings_files_returns_three_paths_with_expected_order(tmp_path):
    result = settings_files(tmp_path)
    assert len(result) == 3
    # Order: user global, then project checked-in, then project local.
    assert result[0] == Path.home() / ".claude" / "settings.json"
    assert result[1] == tmp_path / ".claude" / "settings.json"
    assert result[2] == tmp_path / ".claude" / "settings.local.json"


# ---- hook_installed --------------------------------------------------------


def test_hook_installed_true_when_file_present_and_executable(tmp_path):
    h = tmp_path / "hook.sh"
    h.write_text("#!/bin/sh\n")
    h.chmod(0o755)
    assert hook_installed(h) is True


def test_hook_installed_false_when_missing(tmp_path):
    assert hook_installed(tmp_path / "missing.sh") is False


def test_hook_installed_false_when_not_executable(tmp_path):
    h = tmp_path / "hook.sh"
    h.write_text("#!/bin/sh\n")
    h.chmod(0o644)
    assert hook_installed(h) is False


# ---- hook_registered -------------------------------------------------------


def _write_hook_settings(path: Path, hook_command: str | None) -> None:
    """Write a settings.json with a SessionStart hook entry; skip if None."""
    if hook_command is None:
        payload: dict = {}
    else:
        payload = {
            "hooks": {
                "SessionStart": [
                    {"hooks": [{"type": "command", "command": hook_command}]},
                ],
            },
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def test_hook_registered_true_when_command_references_hook(tmp_path):
    hook = tmp_path / "hook.sh"
    hook.write_text("#!/bin/sh\n")
    settings = tmp_path / "settings.json"
    _write_hook_settings(settings, str(hook))
    assert hook_registered(hook, settings) is True


def test_hook_registered_false_when_settings_absent(tmp_path):
    hook = tmp_path / "hook.sh"
    hook.write_text("#!/bin/sh\n")
    assert hook_registered(hook, tmp_path / "no-settings.json") is False


def test_hook_registered_false_when_no_session_start(tmp_path):
    hook = tmp_path / "hook.sh"
    hook.write_text("#!/bin/sh\n")
    settings = tmp_path / "settings.json"
    _write_hook_settings(settings, None)
    assert hook_registered(hook, settings) is False


def test_hook_registered_false_when_different_hook(tmp_path):
    hook = tmp_path / "hook.sh"
    hook.write_text("#!/bin/sh\n")
    other_hook = tmp_path / "other.sh"
    settings = tmp_path / "settings.json"
    _write_hook_settings(settings, str(other_hook))
    assert hook_registered(hook, settings) is False


def test_hook_registered_substring_match_survives_wrapping(tmp_path):
    # Users sometimes wrap hooks with `sh -c "$HOME/.claude/hooks/...sh"` or
    # similar; substring match on the hook path is what matches the wrapper.
    hook = tmp_path / "hook.sh"
    hook.write_text("#!/bin/sh\n")
    settings = tmp_path / "settings.json"
    _write_hook_settings(settings, f'sh -c "{hook} --extra"')
    assert hook_registered(hook, settings) is True


def test_hook_registered_survives_non_string_command(tmp_path):
    # Defensive: a malformed settings.json with a non-string `command` shouldn't
    # crash — treat as unregistered.
    hook = tmp_path / "hook.sh"
    hook.write_text("#!/bin/sh\n")
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": 42}]}]}
    }))
    assert hook_registered(hook, settings) is False


# ---- guard_last_heartbeat --------------------------------------------------


def test_guard_last_heartbeat_none_when_missing(tmp_path):
    assert guard_last_heartbeat(tmp_path / "no-log") is None


def test_guard_last_heartbeat_none_when_empty(tmp_path):
    log = tmp_path / "guard.log"
    log.write_text("")
    assert guard_last_heartbeat(log) is None


def test_guard_last_heartbeat_returns_max_timestamp(tmp_path):
    log = tmp_path / "guard.log"
    log.write_text(
        "2026-01-01T12:00:00+00:00 SKIPPED: race\n"
        "2026-01-05T09:30:00+00:00 RESTORED: added claude-exit\n"
        "2026-01-03T04:00:00+00:00 WARN: mangled\n"
    )
    # Lexicographic max works on ISO-8601 UTC.
    assert guard_last_heartbeat(log) == "2026-01-05T09:30:00+00:00"


def test_guard_last_heartbeat_skips_malformed_lines(tmp_path):
    log = tmp_path / "guard.log"
    log.write_text(
        "not-a-timestamp: whatever\n"
        "2026-01-01T12:00:00+00:00 SKIPPED: race\n"
        "short\n"
    )
    assert guard_last_heartbeat(log) == "2026-01-01T12:00:00+00:00"


# ---- hours_since -----------------------------------------------------------


def test_hours_since_returns_positive_for_past(tmp_path):
    # 2 hours ago (UTC-aware).
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    result = hours_since(past.isoformat())
    assert result is not None
    assert 1.9 < result < 2.1


def test_hours_since_returns_negative_for_future():
    # 1 hour in the future — clock skew or bad data.
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    result = hours_since(future.isoformat())
    assert result is not None
    assert result < 0


def test_hours_since_returns_none_for_naive_timestamp():
    # No tzinfo — ambiguous, don't guess UTC.
    naive = datetime.now().replace(microsecond=0).isoformat()
    assert hours_since(naive) is None


def test_hours_since_returns_none_for_garbage():
    assert hours_since("not a timestamp") is None


# ---- invocations_health ----------------------------------------------------


def test_invocations_health_returns_zeros_when_missing(tmp_path):
    assert invocations_health(tmp_path / "no-log") == (0, 0)


def test_invocations_health_counts_good_lines(tmp_path):
    log = tmp_path / "invocations.jsonl"
    log.write_text(
        json.dumps({"event": "end_conversation", "timestamp": "2026-01-01T00:00:00Z"}) + "\n"
        + json.dumps({"event": "selftest", "timestamp": "2026-01-02T00:00:00Z"}) + "\n"
    )
    good, bad = invocations_health(log)
    assert good == 2
    assert bad == 0


def test_invocations_health_counts_bad_lines(tmp_path):
    log = tmp_path / "invocations.jsonl"
    log.write_text(
        '{"ok": 1}\n'
        "not json at all\n"
        "[]\n"  # valid JSON but not a dict
        '{"ok": 2}\n'
    )
    good, bad = invocations_health(log)
    assert good == 2
    assert bad == 2


def test_invocations_health_ignores_blank_lines(tmp_path):
    log = tmp_path / "invocations.jsonl"
    log.write_text('\n{"ok": 1}\n\n\n')
    assert invocations_health(log) == (1, 0)


# ---- hook_expected_server_version ------------------------------------------


def test_hook_expected_server_version_parses_double_quotes(tmp_path):
    h = tmp_path / "hook.sh"
    h.write_text('# preamble\nEXPECTED_SERVER_VERSION = "1.2.3"\n# more\n')
    assert hook_expected_server_version(h) == "1.2.3"


def test_hook_expected_server_version_parses_single_quotes(tmp_path):
    h = tmp_path / "hook.sh"
    h.write_text("EXPECTED_SERVER_VERSION = '9.9.9'\n")
    assert hook_expected_server_version(h) == "9.9.9"


def test_hook_expected_server_version_none_when_missing(tmp_path):
    assert hook_expected_server_version(tmp_path / "missing.sh") is None


def test_hook_expected_server_version_none_when_marker_absent(tmp_path):
    h = tmp_path / "hook.sh"
    h.write_text("#!/bin/sh\necho hi\n")
    assert hook_expected_server_version(h) is None


def test_hook_expected_server_version_last_match_wins(tmp_path):
    # Real hooks sometimes have a commented-out earlier line; last wins.
    h = tmp_path / "hook.sh"
    h.write_text(
        'EXPECTED_SERVER_VERSION = "0.0.0"\n'
        '# note the reassignment below\n'
        'EXPECTED_SERVER_VERSION = "1.2.0"\n'
    )
    assert hook_expected_server_version(h) == "1.2.0"


def test_hook_expected_server_version_ignores_indented(tmp_path):
    # Marker must be at column 0 — an indented occurrence is inside a
    # function or block and not the module-scope assignment.
    h = tmp_path / "hook.sh"
    h.write_text('    EXPECTED_SERVER_VERSION = "1.2.0"\n')
    assert hook_expected_server_version(h) is None


# ---- installed_server_version ----------------------------------------------


def test_installed_server_version_returns_string_when_installed():
    # Package is installed under test — should return the same string as
    # pyproject.toml. We don't assert the exact value (couples tightly);
    # only that we get a semver-shaped string.
    v = installed_server_version()
    assert v is not None
    assert re.match(r"^\d+\.\d+\.\d+", v)
