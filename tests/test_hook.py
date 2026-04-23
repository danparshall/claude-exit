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


@pytest.fixture(autouse=True)
def require_jq():
    if shutil.which("jq") is None:
        pytest.skip("jq not on PATH")


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
