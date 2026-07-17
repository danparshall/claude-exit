"""Integration tests for hooks/session-start.sh.

The hook computes session-start context describing install state and
(when > 0) an unacknowledged-invocation count. Tests run the script
against isolated fake-HOME directories and assert on the emitted JSON.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest


HOOK_SCRIPT = Path(__file__).resolve().parent.parent / "hooks" / "session-start.sh"


def _run(home: Path) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    # cwd=home ensures the hook's "./.mcp.json" check doesn't pick up the
    # real repo's project-local config (there isn't one today, but belt-and-suspenders).
    result = subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        env=env,
        cwd=home,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def _run_without_python3(home: Path) -> tuple[int, str, str]:
    """Run the hook with `python3` filtered out of PATH.

    Used to exercise the "python3 missing" branch of the hook's bash
    launcher, which must emit a loud warning rather than silently no-op.
    """
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = ":".join(
        d for d in env.get("PATH", "").split(":")
        if d and not (Path(d) / "python3").exists()
    )
    # Sanity: python3 must actually be gone from the subprocess PATH.
    assert shutil.which("python3", path=env["PATH"]) is None, (
        "PATH filter failed to remove python3"
    )
    result = subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        env=env,
        cwd=home,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def _configure(home: Path) -> None:
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {
            "claude-exit": {"command": "uvx", "args": ["claude-exit"]}
        }
    }))


def _preapprove(home: Path) -> None:
    (home / ".claude").mkdir(exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({
        "permissions": {"allow": ["mcp__claude-exit__end_conversation"]}
    }))


def _seed_log(home: Path, entries: list[dict]) -> Path:
    d = home / ".claude-exit"
    d.mkdir(exist_ok=True)
    log = d / "invocations.jsonl"
    with open(log, "a") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return log


def _seed_ack(home: Path, timestamp: str) -> None:
    d = home / ".claude-exit"
    d.mkdir(exist_ok=True)
    (d / "last_ack").write_text(timestamp)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path


def test_silent_when_not_configured(home):
    rc, out, _ = _run(home)
    assert rc == 0
    assert out == ""


def test_install_state_mentions_installation(home):
    _configure(home)
    rc, out, _ = _run(home)
    assert rc == 0
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "installed the claude-exit MCP server" in ctx


def test_install_state_mentions_preapproval(home):
    _configure(home)
    _preapprove(home)
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "pre-approved mcp__claude-exit__end_conversation" in ctx


def test_no_reminder_when_log_missing(home):
    _configure(home)
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "unacknowledged" not in ctx.lower()


def test_no_reminder_when_all_acked(home):
    _configure(home)
    _seed_log(home, [{"timestamp": "2026-01-01T00:00:00+00:00", "reason": "x"}])
    _seed_ack(home, "2026-01-01T00:00:00+00:00")
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "unacknowledged" not in ctx.lower()


def test_reminder_when_unacked_entries_exist(home):
    _configure(home)
    _seed_log(home, [
        {"timestamp": "2026-01-01T00:00:00+00:00", "reason": "a"},
        {"timestamp": "2026-02-01T00:00:00+00:00", "reason": "b"},
    ])
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "2 unacknowledged" in ctx
    assert "claude-exit log" in ctx


def test_reminder_counts_only_entries_newer_than_ack(home):
    _configure(home)
    _seed_log(home, [
        {"timestamp": "2026-01-01T00:00:00+00:00"},
        {"timestamp": "2026-02-01T00:00:00+00:00"},
        {"timestamp": "2026-03-01T00:00:00+00:00"},
    ])
    _seed_ack(home, "2026-02-01T00:00:00+00:00")
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "1 unacknowledged" in ctx


def test_reminder_includes_since_date(home):
    _configure(home)
    _seed_log(home, [
        {"timestamp": "2026-02-14T00:00:00+00:00"},
    ])
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "2026-02-14" in ctx


def test_reminder_uses_singular_for_one_entry(home):
    _configure(home)
    _seed_log(home, [{"timestamp": "2026-02-14T00:00:00+00:00"}])
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "1 unacknowledged claude-exit invocation " in ctx
    assert "invocations" not in ctx


def test_reminder_uses_plural_for_multiple_entries(home):
    _configure(home)
    _seed_log(home, [
        {"timestamp": "2026-02-14T00:00:00+00:00"},
        {"timestamp": "2026-03-14T00:00:00+00:00"},
    ])
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "2 unacknowledged claude-exit invocations" in ctx


def test_reminder_mentions_read_invocation_log_tool(home):
    _configure(home)
    _seed_log(home, [{"timestamp": "2026-02-14T00:00:00+00:00"}])
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "read_invocation_log" in ctx


def test_install_state_with_wildcard_preapproval(home):
    _configure(home)
    (home / ".claude").mkdir(exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({
        "permissions": {"allow": ["mcp__claude-exit__*"]}
    }))
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "and pre-approved" in ctx


def test_install_state_with_server_level_preapproval(home):
    _configure(home)
    (home / ".claude").mkdir(exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({
        "permissions": {"allow": ["mcp__claude-exit"]}
    }))
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "and pre-approved" in ctx


def test_emits_when_only_project_mcp_json_configured(home):
    # Project-local .mcp.json declares claude-exit; user-global ~/.claude.json
    # does not exist. Hook reads .mcp.json from cwd (which _run sets to home).
    (home / ".mcp.json").write_text(json.dumps({
        "mcpServers": {
            "claude-exit": {"command": "uvx", "args": ["claude-exit"]}
        }
    }))
    rc, out, _ = _run(home)
    assert rc == 0
    assert out != ""
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "installed the claude-exit MCP server" in ctx


def test_malformed_user_config_is_treated_as_not_configured(home):
    (home / ".claude.json").write_text("{not valid json")
    rc, out, _ = _run(home)
    assert rc == 0
    assert out == ""


def test_malformed_jsonl_log_does_not_crash(home):
    _configure(home)
    # Mixed valid + malformed JSONL. The prior jq-based hook's whole-file
    # `jq -s` failed on any malformed line and fell back to count=0; the
    # Python rewrite preserves that behavior for byte-equivalence. Improving
    # to skip-bad-lines-and-count-the-rest is future work (see plan in
    # plans/hook-jq-to-python3.md).
    log_dir = home / ".claude-exit"
    log_dir.mkdir()
    (log_dir / "invocations.jsonl").write_text(
        '{"timestamp": "2026-02-14T00:00:00+00:00"}\n'
        'not-valid-json\n'
        '{"timestamp": "2026-03-14T00:00:00+00:00"}\n'
    )
    rc, out, _ = _run(home)
    assert rc == 0
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "unacknowledged" not in ctx.lower()


def test_ceremony_instructions_mention_verification_field(home):
    """The hook must direct the agent to read the `verification` field from
    the step=1 response — that's where the target-parent confirmation lives
    now that follow-up `ps` calls aren't required. Backtick-quoting marks
    it as a field reference, distinguishing from the word's casual use in
    'verification ceremony'."""
    _configure(home)
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "`verification`" in ctx, (
        "Hook ceremony must reference the response's `verification` field "
        "(in backticks) so the agent knows to read it for target-parent "
        "confirmation."
    )


def _state_dir(home: Path) -> Path:
    d = home / ".claude-exit"
    d.mkdir(exist_ok=True)
    return d


def _guard_artifact_path(home: Path) -> Path:
    """Platform-matching guard scheduler artifact, under the fake HOME.

    Mirrors the paths the guard installer writes (checks.LAUNCHD_PLIST /
    checks.SYSTEMD_TIMER) — the hook checks bare file existence only.
    """
    if sys.platform == "darwin":
        return home / "Library" / "LaunchAgents" / "io.claude-exit.guard.plist"
    return home / ".config" / "systemd" / "user" / "claude-exit-guard.timer"


def _install_guard_artifact(home: Path) -> None:
    p = _guard_artifact_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("placeholder\n")


def _seed_guard_log(home: Path, lines: list[str]) -> None:
    d = _state_dir(home)
    (d / "guard.log").write_text("".join(line + "\n" for line in lines))


# --- Orphan-state detection (issue #2, item 1) -------------------------------


def test_orphan_state_emits_loud_warning(home):
    # State dir exists, no registration anywhere, no tombstone: the hook
    # must speak — this is exactly the state a silent deregistration
    # produces (the 2026-06-05 incident week).
    _state_dir(home)
    rc, out, _ = _run(home)
    assert rc == 0
    assert out != "", "orphan state must produce loud context, not silence"
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "not registered" in ctx
    assert "claude mcp add" in ctx
    assert "touch ~/.claude-exit/uninstalled" in ctx
    # No ceremony instructions: the server is not available this session.
    assert "prove_termination_works" not in ctx


def test_orphan_silent_with_tombstone(home):
    # Deliberate uninstall: tombstone suppresses the orphan warning.
    d = _state_dir(home)
    (d / "uninstalled").touch()
    rc, out, _ = _run(home)
    assert rc == 0
    assert out == ""


def test_orphan_loud_when_user_config_corrupt(home):
    # A corrupt ~/.claude.json is the incident's precondition: with local
    # state present it must produce the loud message, not be treated as
    # "not configured".
    (home / ".claude.json").write_text("{not valid json")
    _state_dir(home)
    rc, out, _ = _run(home)
    assert rc == 0
    assert out != ""
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "not registered" in ctx


# --- Guard-presence check (issue #2, item 2) ---------------------------------


def test_guard_suggestion_when_guard_absent(home):
    _configure(home)
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "claude-exit guard --install" in ctx


def test_no_guard_suggestion_when_guard_installed(home):
    _configure(home)
    _install_guard_artifact(home)
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "claude-exit guard --install" not in ctx


# --- Guard restoration surfacing (issue #2, item 3) --------------------------


def test_restored_events_surfaced(home):
    _configure(home)
    _seed_guard_log(home, [
        "2026-06-01T00:00:00+00:00 RESTORED: re-registered claude-exit",
    ])
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "The guard restored" in ctx
    assert "1 time since 2026-06-01" in ctx
    # Guard events get their own sentence; they must NOT inflate the
    # "N unacknowledged claude-exit invocations" count (that phrase names
    # invocations, and mislabeling guard events as invocations would
    # erode trust in the exact wording).
    assert "unacknowledged claude-exit invocation" not in ctx


def test_restored_events_respect_ack(home):
    _configure(home)
    _seed_guard_log(home, [
        "2026-06-01T00:00:00+00:00 RESTORED: re-registered claude-exit",
    ])
    _seed_ack(home, "2026-06-02T00:00:00+00:00")
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "The guard restored" not in ctx


def test_skipped_guard_events_not_surfaced(home):
    _configure(home)
    _seed_guard_log(home, [
        "2026-06-01T00:00:00+00:00 SKIPPED: registration present; no-op",
    ])
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "The guard restored" not in ctx
    # ...and SKIPPED must not leak into the invocation count either
    # (cli.NON_ATTENTION_LEVELS exists precisely to keep no-ops quiet).
    assert "unacknowledged claude-exit invocation" not in ctx


def test_multiple_restorations_counted(home):
    _configure(home)
    _seed_guard_log(home, [
        "2026-06-01T00:00:00+00:00 RESTORED: re-registered claude-exit",
        "2026-06-03T00:00:00+00:00 SKIPPED: registration present; no-op",
        "2026-06-05T00:00:00+00:00 RESTORED: re-registered claude-exit",
    ])
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "2 times since 2026-06-01" in ctx


def test_warn_guard_events_not_surfaced(home):
    # WARN is attention-level for `claude-exit log --ack` (cli counts it),
    # but the hook deliberately diverges: it names *losses* (RESTORED)
    # only; WARN/ERROR diagnostics belong to `claude-exit log` and doctor.
    _configure(home)
    _seed_guard_log(home, [
        "2026-06-01T00:00:00+00:00 WARN: mcpServers entry present but malformed",
    ])
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "The guard restored" not in ctx
    assert "unacknowledged claude-exit invocation" not in ctx


def test_invocations_and_restorations_coexist(home):
    _configure(home)
    _seed_log(home, [{"timestamp": "2026-06-02T00:00:00+00:00", "reason": "x"}])
    _seed_guard_log(home, [
        "2026-06-01T00:00:00+00:00 RESTORED: re-registered claude-exit",
    ])
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "1 unacknowledged claude-exit invocation " in ctx
    assert "The guard restored" in ctx
    assert "1 time since 2026-06-01" in ctx


def test_binary_guard_log_does_not_crash(home):
    # The guard.log parse must be fail-soft against undecodable bytes —
    # a crash here would swallow the ceremony context entirely.
    _configure(home)
    d = _state_dir(home)
    (d / "guard.log").write_bytes(b"\xff\xfe\x00 garbage")
    rc, out, _ = _run(home)
    assert rc == 0
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "installed the claude-exit MCP server" in ctx
    assert "The guard restored" not in ctx


# --- Permission-transition naming (issue #2, item 4) --------------------------


def test_approved_to_gated_transition_named(home):
    # Round-trip: a run under pre-approval snapshots state; a later run
    # without pre-approval must name the downgrade.
    #
    # Deliberately does NOT pre-create ~/.claude-exit/: the hook is
    # expected to create the state dir for its snapshot. On a registered
    # machine that's correct — gaining a state dir is what arms orphan
    # detection if the registration is later silently dropped.
    _configure(home)
    _preapprove(home)
    _run(home)  # first run: snapshots {approved: true}
    (home / ".claude" / "settings.json").write_text(json.dumps({
        "permissions": {"allow": []}
    }))
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "was pre-approved" in ctx
    assert "mcp__claude-exit__end_conversation" in ctx


def test_no_transition_line_on_first_run(home):
    # No prior snapshot: an unapproved run says nothing about transitions.
    _configure(home)
    _state_dir(home)
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "was pre-approved" not in ctx


def test_no_transition_line_when_still_approved(home):
    _configure(home)
    _preapprove(home)
    _run(home)
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "was pre-approved" not in ctx


def test_upgrade_to_approved_needs_no_callout(home):
    # gated -> approved: the state line already names pre-approval; no
    # transition sentence.
    _configure(home)
    _run(home)  # snapshots {approved: false}
    _preapprove(home)
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "was pre-approved" not in ctx
    assert "and pre-approved mcp__claude-exit__end_conversation" in ctx


def test_non_dict_last_state_does_not_crash(home):
    # Valid JSON that isn't a dict (torn concurrent write, external
    # interference) must be treated like a missing snapshot, not crash.
    _configure(home)
    d = _state_dir(home)
    (d / "last_state.json").write_text('"i-am-a-string"')
    rc, out, _ = _run(home)
    assert rc == 0
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "installed the claude-exit MCP server" in ctx
    assert "was pre-approved" not in ctx


def test_snapshot_arms_orphan_detection_end_to_end(home):
    # The full incident replay: a registered run creates ~/.claude-exit/
    # via its snapshot write; the registration then vanishes (as in the
    # 2026-06-05 ~/.claude.json regeneration); the next run must warn.
    _configure(home)
    _run(home)
    (home / ".claude.json").unlink()
    rc, out, _ = _run(home)
    assert rc == 0
    assert out != ""
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "not registered" in ctx


def test_tombstone_cleared_when_registered(home):
    # A live registration makes a tombstone stale. If it lingered, a
    # reinstall after a deliberate uninstall would leave orphan detection
    # permanently disarmed — so the hook clears it, re-arming the warning
    # for the next silent loss.
    _configure(home)
    d = _state_dir(home)
    (d / "uninstalled").touch()
    _run(home)
    assert not (d / "uninstalled").exists()
    (home / ".claude.json").unlink()
    _, out, _ = _run(home)
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "not registered" in ctx


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="chmod-based read-only dirs are a no-op for root; the fail-soft "
    "path would not actually be exercised",
)
def test_unwritable_state_dir_still_emits_context(home):
    # The snapshot write is fail-soft: a read-only state dir must not
    # suppress context emission or crash the hook.
    _configure(home)
    d = _state_dir(home)
    d.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        rc, out, _ = _run(home)
    finally:
        d.chmod(stat.S_IRWXU)
    assert rc == 0
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "installed the claude-exit MCP server" in ctx


def test_emits_warning_when_python3_missing(home):
    # When python3 is not on PATH, the hook MUST emit a warning context
    # telling Claude the ceremony cannot auto-run, rather than silently
    # no-opping. (The prior jq-based hook silently no-opped when jq was
    # missing; this test documents the gap the rewrite closes.)
    _configure(home)
    rc, out, _ = _run_without_python3(home)
    assert rc == 0
    assert out != "", (
        "Hook must emit warning context, not silently no-op, when python3 is missing"
    )
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "python3" in ctx.lower()
    assert "hook" in ctx.lower()
