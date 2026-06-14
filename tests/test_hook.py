"""Integration tests for hooks/session-start.sh.

The hook computes session-start context describing install state and
(when > 0) an unacknowledged-invocation count. Tests run the script
against isolated fake-HOME directories and assert on the emitted JSON.
"""

import json
import os
import shutil
import subprocess
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
