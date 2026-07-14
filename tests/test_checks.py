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
    SETTINGS_ABSENT,
    SETTINGS_CORRUPT,
    SETTINGS_PRESENT,
    all_binaries_on_path,
    guard_last_heartbeat,
    guard_scheduled,
    guard_scheduler_loaded,
    hook_registered_in_settings,
    invocations_bad_lines,
    preapproval_file,
    registration_state,
    resolve_binary,
    settings_state,
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


# ---- preapproval_file -------------------------------------------------------


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def test_preapproval_file_finds_exact_key(tmp_path):
    user = tmp_path / "user.json"
    _write_json(user, {"permissions": {"allow": ["mcp__claude-exit__end_conversation"]}})
    assert preapproval_file([user]) == user


def test_preapproval_file_finds_wildcard_key(tmp_path):
    user = tmp_path / "user.json"
    _write_json(user, {"permissions": {"allow": ["mcp__claude-exit__*"]}})
    assert preapproval_file([user]) == user


def test_preapproval_file_finds_server_level_key(tmp_path):
    user = tmp_path / "user.json"
    _write_json(user, {"permissions": {"allow": ["mcp__claude-exit"]}})
    assert preapproval_file([user]) == user


def test_preapproval_file_returns_first_match_in_order(tmp_path):
    user = tmp_path / "user.json"
    project = tmp_path / "project.json"
    local = tmp_path / "local.json"
    _write_json(user, {"permissions": {"allow": ["other"]}})
    _write_json(project, {"permissions": {"allow": ["mcp__claude-exit__end_conversation"]}})
    _write_json(local, {"permissions": {"allow": ["mcp__claude-exit__end_conversation"]}})
    # Should skip user (no match) and stop at project (first match).
    assert preapproval_file([user, project, local]) == project


def test_preapproval_file_returns_none_when_nothing_matches(tmp_path):
    user = tmp_path / "user.json"
    _write_json(user, {"permissions": {"allow": ["something-else"]}})
    assert preapproval_file([user]) is None


def test_preapproval_file_tolerates_missing_and_malformed(tmp_path):
    # A missing file, an unparseable file, and a valid file with the key —
    # scan should skip the first two and return the third.
    missing = tmp_path / "missing.json"
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{ not json")
    valid = tmp_path / "valid.json"
    _write_json(valid, {"permissions": {"allow": ["mcp__claude-exit__end_conversation"]}})
    assert preapproval_file([missing, corrupt, valid]) == valid


def test_preapproval_file_tolerates_non_dict_top_level(tmp_path):
    weird = tmp_path / "weird.json"
    weird.write_text("[]")
    valid = tmp_path / "valid.json"
    _write_json(valid, {"permissions": {"allow": ["mcp__claude-exit__end_conversation"]}})
    assert preapproval_file([weird, valid]) == valid


def test_preapproval_file_tolerates_permissions_not_dict(tmp_path):
    # A permissions field that isn't a dict shouldn't crash; skip and move on.
    weird = tmp_path / "weird.json"
    _write_json(weird, {"permissions": "nope"})
    valid = tmp_path / "valid.json"
    _write_json(valid, {"permissions": {"allow": ["mcp__claude-exit__end_conversation"]}})
    assert preapproval_file([weird, valid]) == valid


def test_preapproval_file_tolerates_allow_not_list(tmp_path):
    weird = tmp_path / "weird.json"
    _write_json(weird, {"permissions": {"allow": "not-a-list"}})
    assert preapproval_file([weird]) is None


# ---- hook_registered_in_settings --------------------------------------------


def test_hook_registered_true_when_command_contains_basename(tmp_path):
    hook = tmp_path / "claude-exit-session-start.sh"
    settings = tmp_path / "settings.json"
    _write_json(settings, {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup",
                    "hooks": [
                        {"type": "command", "command": "$HOME/.claude/hooks/claude-exit-session-start.sh"},
                    ],
                }
            ]
        }
    })
    assert hook_registered_in_settings(hook, settings) is True


def test_hook_registered_true_with_literal_path(tmp_path):
    hook = tmp_path / "claude-exit-session-start.sh"
    settings = tmp_path / "settings.json"
    _write_json(settings, {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"command": "/home/dan/.claude/hooks/claude-exit-session-start.sh"}]}
            ]
        }
    })
    assert hook_registered_in_settings(hook, settings) is True


def test_hook_registered_false_when_no_matching_command(tmp_path):
    hook = tmp_path / "claude-exit-session-start.sh"
    settings = tmp_path / "settings.json"
    _write_json(settings, {
        "hooks": {
            "SessionStart": [{"hooks": [{"command": "/some/other/hook.sh"}]}]
        }
    })
    assert hook_registered_in_settings(hook, settings) is False


def test_hook_registered_false_when_no_hooks_key(tmp_path):
    hook = tmp_path / "claude-exit-session-start.sh"
    settings = tmp_path / "settings.json"
    _write_json(settings, {"permissions": {"allow": []}})
    assert hook_registered_in_settings(hook, settings) is False


def test_hook_registered_false_when_settings_missing(tmp_path):
    hook = tmp_path / "claude-exit-session-start.sh"
    settings = tmp_path / "does-not-exist.json"
    assert hook_registered_in_settings(hook, settings) is False


def test_hook_registered_false_when_settings_corrupt(tmp_path):
    hook = tmp_path / "claude-exit-session-start.sh"
    settings = tmp_path / "corrupt.json"
    settings.write_text("{ not json")
    assert hook_registered_in_settings(hook, settings) is False


def test_hook_registered_tolerates_hooks_not_list(tmp_path):
    # Defensive: hooks.SessionStart being a string shouldn't crash the check.
    hook = tmp_path / "claude-exit-session-start.sh"
    settings = tmp_path / "settings.json"
    _write_json(settings, {"hooks": {"SessionStart": "not-a-list"}})
    assert hook_registered_in_settings(hook, settings) is False


# ---- guard_scheduler_loaded -------------------------------------------------


class _FakeRunner:
    """Records subprocess.run-shape calls; returns a canned result."""
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        class _R:
            pass
        r = _R()
        r.returncode = self.returncode
        r.stdout = self.stdout
        r.stderr = self.stderr
        return r


def test_guard_scheduler_loaded_true_on_darwin_when_launchctl_rc0():
    runner = _FakeRunner(returncode=0, stdout="agent info...")
    assert guard_scheduler_loaded(platform="darwin", runner=runner, uid=501) is True
    assert runner.calls == [["launchctl", "print", "gui/501/io.claude-exit.guard"]]


def test_guard_scheduler_loaded_false_on_darwin_when_launchctl_rc_nonzero():
    runner = _FakeRunner(returncode=113, stderr="Could not find service")
    assert guard_scheduler_loaded(platform="darwin", runner=runner, uid=501) is False


def test_guard_scheduler_loaded_none_on_darwin_when_runner_raises():
    def raising(*a, **kw):
        raise FileNotFoundError("launchctl not on PATH")
    assert guard_scheduler_loaded(platform="darwin", runner=raising, uid=501) is None


def test_guard_scheduler_loaded_true_on_linux_when_enabled():
    runner = _FakeRunner(returncode=0, stdout="enabled\n")
    assert guard_scheduler_loaded(platform="linux", runner=runner) is True
    assert runner.calls == [
        ["systemctl", "--user", "is-enabled", "claude-exit-guard.timer"]
    ]


def test_guard_scheduler_loaded_false_on_linux_when_disabled():
    runner = _FakeRunner(returncode=1, stdout="disabled\n")
    assert guard_scheduler_loaded(platform="linux", runner=runner) is False


def test_guard_scheduler_loaded_false_on_linux_when_static():
    # rc 0 but stdout != "enabled" — is-enabled reports "static" for units
    # without an [Install] section. Our unit does have one, so static is a
    # broken-config signal, not a healthy state.
    runner = _FakeRunner(returncode=0, stdout="static\n")
    assert guard_scheduler_loaded(platform="linux", runner=runner) is False


def test_guard_scheduler_loaded_none_on_unsupported_platform():
    runner = _FakeRunner(returncode=0)
    assert guard_scheduler_loaded(platform="win32", runner=runner) is None
    assert runner.calls == []  # never shelled out on unsupported platform


# ---- guard_last_heartbeat ---------------------------------------------------


def test_guard_last_heartbeat_none_when_file_missing(tmp_path):
    assert guard_last_heartbeat(tmp_path / "no-such.log") is None


def test_guard_last_heartbeat_none_when_file_empty(tmp_path):
    log = tmp_path / "guard.log"
    log.write_text("")
    assert guard_last_heartbeat(log) is None


def test_guard_last_heartbeat_returns_last_timestamp(tmp_path):
    log = tmp_path / "guard.log"
    log.write_text(
        "2026-06-05T10:00:00+00:00 RESTORED: created config\n"
        "2026-06-05T11:00:00+00:00 SKIPPED: no action\n"
        "2026-06-05T12:00:00+00:00 SKIPPED: no action\n"
    )
    assert guard_last_heartbeat(log) == "2026-06-05T12:00:00+00:00"


def test_guard_last_heartbeat_skips_blank_lines(tmp_path):
    log = tmp_path / "guard.log"
    log.write_text(
        "2026-06-05T10:00:00+00:00 SKIPPED: x\n"
        "\n"
        "  \n"
    )
    assert guard_last_heartbeat(log) == "2026-06-05T10:00:00+00:00"


def test_guard_last_heartbeat_skips_malformed_lines(tmp_path):
    # A garbage line between two valid ones should be skipped without
    # crashing; the last VALID line wins.
    log = tmp_path / "guard.log"
    log.write_text(
        "2026-06-05T10:00:00+00:00 SKIPPED: x\n"
        "garbage no space\n"
        "not-a-timestamp WARN: y\n"
        "2026-06-05T12:00:00+00:00 SKIPPED: z\n"
    )
    assert guard_last_heartbeat(log) == "2026-06-05T12:00:00+00:00"


# ---- invocations_bad_lines --------------------------------------------------


def test_invocations_bad_lines_zero_when_file_missing(tmp_path):
    assert invocations_bad_lines(tmp_path / "no-such.jsonl") == (0, 0)


def test_invocations_bad_lines_zero_when_all_valid(tmp_path):
    log = tmp_path / "invocations.jsonl"
    log.write_text(
        json.dumps({"event": "selftest", "timestamp": "2026-06-05T10:00:00Z"}) + "\n"
        + json.dumps({"event": "end_conversation", "timestamp": "2026-06-05T11:00:00Z"}) + "\n"
    )
    assert invocations_bad_lines(log) == (2, 0)


def test_invocations_bad_lines_counts_malformed(tmp_path):
    log = tmp_path / "invocations.jsonl"
    log.write_text(
        json.dumps({"event": "selftest"}) + "\n"
        + "{ not json\n"
        + json.dumps({"event": "end_conversation"}) + "\n"
        + "another bad line\n"
    )
    assert invocations_bad_lines(log) == (2, 2)


def test_invocations_bad_lines_ignores_blank_lines(tmp_path):
    log = tmp_path / "invocations.jsonl"
    log.write_text(
        json.dumps({"event": "selftest"}) + "\n"
        + "\n"
        + "   \n"
        + json.dumps({"event": "end_conversation"}) + "\n"
    )
    assert invocations_bad_lines(log) == (2, 0)


# ---- all_binaries_on_path ---------------------------------------------------


def test_all_binaries_on_path_empty_when_nothing_matches(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert all_binaries_on_path("claude-exit") == []


def test_all_binaries_on_path_finds_single(tmp_path, monkeypatch):
    d = tmp_path / "bin"
    d.mkdir()
    f = d / "claude-exit"
    f.write_text("#!/bin/sh\n")
    f.chmod(0o755)
    monkeypatch.setenv("PATH", str(d))
    assert all_binaries_on_path("claude-exit") == [f]


def test_all_binaries_on_path_reports_shadowing(tmp_path, monkeypatch):
    # Two directories, both containing a claude-exit. Order must be
    # earlier-in-PATH-first, matching what `which -a` reports.
    early = tmp_path / "early"
    early.mkdir()
    early_bin = early / "claude-exit"
    early_bin.write_text("#!/bin/sh\n")
    early_bin.chmod(0o755)

    late = tmp_path / "late"
    late.mkdir()
    late_bin = late / "claude-exit"
    late_bin.write_text("#!/bin/sh\n")
    late_bin.chmod(0o755)

    monkeypatch.setenv("PATH", f"{early}{os.pathsep}{late}")
    result = all_binaries_on_path("claude-exit")
    assert result == [early_bin, late_bin]


def test_all_binaries_on_path_skips_non_executable(tmp_path, monkeypatch):
    # A non-executable file with the right name doesn't count — a shell
    # wouldn't run it either.
    d = tmp_path / "bin"
    d.mkdir()
    f = d / "claude-exit"
    f.write_text("#!/bin/sh\n")
    f.chmod(0o644)
    monkeypatch.setenv("PATH", str(d))
    assert all_binaries_on_path("claude-exit") == []


def test_all_binaries_on_path_deduplicates_symlinks(tmp_path, monkeypatch):
    # Two PATH entries pointing at the same physical binary (via symlink
    # or duplicate dir) should appear once — the deduplication is on the
    # resolved path.
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real_bin = real_dir / "claude-exit"
    real_bin.write_text("#!/bin/sh\n")
    real_bin.chmod(0o755)
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir)
    monkeypatch.setenv("PATH", f"{real_dir}{os.pathsep}{link_dir}")
    result = all_binaries_on_path("claude-exit")
    assert len(result) == 1


# ---- regression tests for review findings -----------------------------------
# One test per confirmed correctness finding in the doctor code review.
# Named `test_regression_<slug>` so a future contributor scanning failures
# can trace back to what specifically was broken and why the guard exists.


def test_regression_invocations_bad_lines_survives_non_utf8_bytes(tmp_path):
    """
    Finding: invocations_bad_lines opened the file in text mode with no
    encoding hint and no UnicodeDecodeError handler on the iteration, so
    a single non-UTF-8 byte crashed doctor with an uncaught traceback.
    Fix: open with `errors="replace"` so bad bytes become the U+FFFD
    replacement character; json.loads then fails on that line and it's
    counted as `bad` — the exact diagnostic we want.
    """
    log = tmp_path / "invocations.jsonl"
    # Mix valid lines with an outright non-UTF-8 byte sequence.
    log.write_bytes(
        json.dumps({"event": "selftest"}).encode() + b"\n"
        + b"\x80\x81not-utf8\n"
        + json.dumps({"event": "end_conversation"}).encode() + b"\n"
    )
    good, bad = invocations_bad_lines(log)
    # Should not raise; bad line should be counted.
    assert good == 2
    assert bad == 1


def test_regression_preapproval_file_tolerates_unhashable_allow_entries(tmp_path):
    """
    Finding: preapproval_file iterated allow_list with `entry in
    PREAPPROVAL_KEYS` (a frozenset). A dict/list entry — plausible from
    a hand-mangled or partially-migrated settings.json — raised TypeError:
    unhashable type. Fix: filter to strings before the membership test.
    """
    settings = tmp_path / "settings.json"
    _write_json(settings, {
        "permissions": {
            "allow": [
                {"nested": "tool"},          # unhashable dict
                ["also", "unhashable"],       # unhashable list
                "mcp__claude-exit__end_conversation",  # the real match
            ]
        }
    })
    # Should not raise; should find the string match.
    assert preapproval_file([settings]) == settings


def test_regression_preapproval_file_ignores_unhashable_when_no_string_match(tmp_path):
    """A settings file whose only entries are non-strings should read as
    'no preapproval', not raise."""
    settings = tmp_path / "settings.json"
    _write_json(settings, {"permissions": {"allow": [{"x": 1}, [1, 2]]}})
    assert preapproval_file([settings]) is None


def test_regression_preapproval_file_default_arg_reads_module_paths_at_call_time(
    tmp_path, monkeypatch
):
    """
    Finding: preapproval_file's default arg tuple `(USER_SETTINGS, ...)`
    was bound at function-definition time. Monkeypatching the module
    constants at test setup didn't reach the default. Fix: default to
    None, resolve inside the body.
    """
    from claude_exit import checks as _checks
    settings = tmp_path / "settings.json"
    _write_json(settings, {"permissions": {"allow": ["mcp__claude-exit__end_conversation"]}})
    monkeypatch.setattr(_checks, "USER_SETTINGS", settings)
    monkeypatch.setattr(_checks, "PROJECT_SETTINGS", tmp_path / "no-project.json")
    monkeypatch.setattr(_checks, "PROJECT_LOCAL_SETTINGS", tmp_path / "no-local.json")
    # No explicit settings_files arg — must resolve module constants NOW.
    assert preapproval_file() == settings


def test_regression_hook_registered_tolerates_non_string_command(tmp_path):
    """
    Finding: hook_registered_in_settings did `hook_name in command` even
    when `command` was an int/list/dict — raised TypeError. Fix: skip
    entries whose command isn't a string.
    """
    hook = tmp_path / "claude-exit-session-start.sh"
    settings = tmp_path / "settings.json"
    _write_json(settings, {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"command": 42}]},                    # int
                {"hooks": [{"command": ["some", "list"]}]},      # list
                {"hooks": [{"command": {"nested": "dict"}}]},    # dict
                {"hooks": [{"command": str(hook)}]},             # the real match
            ]
        }
    })
    # Should not raise; should find the real string match.
    assert hook_registered_in_settings(hook, settings) is True


def test_regression_hook_registered_tolerates_hooks_list_not_a_list(tmp_path):
    """A malformed entry.hooks that isn't itself a list shouldn't crash."""
    hook = tmp_path / "claude-exit-session-start.sh"
    settings = tmp_path / "settings.json"
    _write_json(settings, {
        "hooks": {"SessionStart": [{"hooks": "not-a-list"}]}
    })
    assert hook_registered_in_settings(hook, settings) is False


# ---- settings_state --------------------------------------------------------


def test_settings_state_absent_when_file_missing(tmp_path):
    assert settings_state(tmp_path / "no-such.json") == SETTINGS_ABSENT


def test_settings_state_corrupt_when_unparseable(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("{ not json")
    assert settings_state(p) == SETTINGS_CORRUPT


def test_settings_state_corrupt_when_top_level_not_object(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("[]")
    assert settings_state(p) == SETTINGS_CORRUPT


def test_settings_state_corrupt_when_empty_file(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("")
    assert settings_state(p) == SETTINGS_CORRUPT


def test_settings_state_present_when_valid_dict(tmp_path):
    p = tmp_path / "settings.json"
    _write_json(p, {"permissions": {"allow": []}})
    assert settings_state(p) == SETTINGS_PRESENT
