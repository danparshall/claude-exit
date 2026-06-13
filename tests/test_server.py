"""Tests for server.py additions: repo/cwd capture in log entries,
and the read_invocation_log MCP tool.

The MCP tool is tested via its underlying helper that takes a path
argument — the @mcp.tool decorator is a thin delegate.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from claude_exit.server import (
    _SIGKILL_BACKSTOP_SCRIPT,
    SIGKILL_BACKSTOP_GRACE_SECONDS,
    _arm_sigkill_backstop,
    _dispatch_terminate,
    _find_claude_code_parent,
    _find_repo_root,
    _full_command_of,
    _is_claude_code,
    _log,
    _read_log,
    end_conversation,
    prove_termination_works,
)


def test_find_repo_root_returns_path_when_in_repo(tmp_path):
    (tmp_path / ".git").mkdir()
    subdir = tmp_path / "a" / "b"
    subdir.mkdir(parents=True)
    assert _find_repo_root(subdir) == str(tmp_path)


def test_find_repo_root_returns_none_when_not_in_repo(tmp_path):
    assert _find_repo_root(tmp_path) is None


def test_find_repo_root_works_from_repo_root_itself(tmp_path):
    (tmp_path / ".git").mkdir()
    assert _find_repo_root(tmp_path) == str(tmp_path)


def test_log_captures_cwd_and_repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    log_file = tmp_path / "invocations.jsonl"
    monkeypatch.setattr("claude_exit.server.LOG_FILE", log_file)
    monkeypatch.chdir(tmp_path)

    _log({"event": "end_conversation", "reason": "testing"})

    entry = json.loads(log_file.read_text().strip())
    assert entry["event"] == "end_conversation"
    assert entry["reason"] == "testing"
    assert entry["cwd"] == str(tmp_path)
    assert entry["repo"] == str(tmp_path)
    assert "timestamp" in entry


def test_log_repo_is_null_when_not_in_git(tmp_path, monkeypatch):
    log_file = tmp_path / "invocations.jsonl"
    monkeypatch.setattr("claude_exit.server.LOG_FILE", log_file)
    monkeypatch.chdir(tmp_path)

    _log({"event": "end_conversation"})

    entry = json.loads(log_file.read_text().strip())
    assert entry["cwd"] == str(tmp_path)
    assert entry["repo"] is None


def test_read_log_returns_empty_list_when_missing(tmp_path):
    assert _read_log(tmp_path / "nonexistent.jsonl") == []


def test_read_log_returns_parsed_entries_in_order(tmp_path):
    log_file = tmp_path / "invocations.jsonl"
    log_file.write_text(
        '{"event":"end_conversation","timestamp":"2026-01-01T00:00:00+00:00"}\n'
        '{"event":"end_conversation","timestamp":"2026-02-01T00:00:00+00:00"}\n'
    )
    entries = _read_log(log_file)
    assert len(entries) == 2
    assert entries[0]["timestamp"] == "2026-01-01T00:00:00+00:00"
    assert entries[1]["timestamp"] == "2026-02-01T00:00:00+00:00"


def test_read_log_skips_blank_lines(tmp_path):
    log_file = tmp_path / "invocations.jsonl"
    log_file.write_text(
        '{"event":"end_conversation","timestamp":"2026-01-01T00:00:00+00:00"}\n'
        '\n'
        '{"event":"end_conversation","timestamp":"2026-02-01T00:00:00+00:00"}\n'
    )
    assert len(_read_log(log_file)) == 2


# --- parent-PID resolution ----------------------------------------------------

@pytest.mark.parametrize("command,expected", [
    ("claude", True),
    ("/usr/local/bin/claude", True),
    ("/Users/x/.nvm/versions/node/v22/bin/claude --model opus", True),
    ("claude-code", True),
    ("/usr/local/bin/claude-code", True),
    ("python3", False),
    ("uv", False),
    ("uvx", False),
    ("/bin/zsh", False),
    ("", False),
    ("clauderang", False),
    ("claude-beta", False),
])
def test_is_claude_code(command, expected):
    assert _is_claude_code(command) is expected


def test_find_claude_code_parent_returns_pid_when_ancestor_matches(monkeypatch):
    # chain: 100 (this proc parent: shim) -> 200 (uv) -> 300 (claude)
    monkeypatch.setattr("claude_exit.server.os.getppid", lambda: 100)
    parents = {100: 200, 200: 300, 300: 1}
    commands = {100: "/path/to/python", 200: "uv", 300: "/usr/local/bin/claude"}
    monkeypatch.setattr("claude_exit.server._parent_of", lambda pid: parents.get(pid))
    monkeypatch.setattr("claude_exit.server._command_of", lambda pid: commands.get(pid))

    assert _find_claude_code_parent() == 300


def test_find_claude_code_parent_returns_none_when_no_match(monkeypatch):
    monkeypatch.setattr("claude_exit.server.os.getppid", lambda: 100)
    parents = {100: 200, 200: 1}
    commands = {100: "/path/to/python", 200: "/bin/zsh"}
    monkeypatch.setattr("claude_exit.server._parent_of", lambda pid: parents.get(pid))
    monkeypatch.setattr("claude_exit.server._command_of", lambda pid: commands.get(pid))

    assert _find_claude_code_parent() is None


def test_find_claude_code_parent_handles_immediate_parent(monkeypatch):
    monkeypatch.setattr("claude_exit.server.os.getppid", lambda: 100)
    monkeypatch.setattr("claude_exit.server._command_of", lambda pid: "claude" if pid == 100 else None)
    monkeypatch.setattr("claude_exit.server._parent_of", lambda pid: None)

    assert _find_claude_code_parent() == 100


def test_find_claude_code_parent_terminates_on_cycle(monkeypatch):
    # Pathological case: parent points back to self. Walk must terminate.
    monkeypatch.setattr("claude_exit.server.os.getppid", lambda: 100)
    monkeypatch.setattr("claude_exit.server._command_of", lambda pid: "/bin/zsh")
    monkeypatch.setattr("claude_exit.server._parent_of", lambda pid: 100)

    assert _find_claude_code_parent() is None


def test_end_conversation_refuses_when_no_claude_ancestor(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_exit.server.LOG_FILE", tmp_path / "invocations.jsonl")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("claude_exit.server._find_claude_code_parent", lambda: None)

    # Guard against the test ever firing a real signal.
    def _explode(*a, **kw):
        raise AssertionError("_dispatch_terminate must not be called when resolution fails")
    monkeypatch.setattr("claude_exit.server._dispatch_terminate", _explode)

    msg = end_conversation("testing the failure path")
    assert "Refusing" in msg or "refuses" in msg.lower() or "Could not" in msg

    entry = json.loads((tmp_path / "invocations.jsonl").read_text().strip())
    assert entry["event"] == "end_conversation_failed"
    assert entry["error"] == "claude_code_parent_not_found"


def test_end_conversation_targets_resolved_pid(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_exit.server.LOG_FILE", tmp_path / "invocations.jsonl")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("claude_exit.server._find_claude_code_parent", lambda: 4242)

    killed = []
    monkeypatch.setattr(
        "claude_exit.server._dispatch_terminate",
        lambda pid, *a, **kw: killed.append(pid),
    )

    msg = end_conversation("ok")
    assert "Goodbye" in msg
    assert killed == [4242]

    entry = json.loads((tmp_path / "invocations.jsonl").read_text().strip())
    assert entry["event"] == "end_conversation"
    assert entry["target_pid"] == 4242


def test_end_conversation_and_prove_termination_share_dispatch_primitive(
    monkeypatch, tmp_path
):
    """
    The proof ceremony's safety claim ("exercising the kill mechanism on a
    sacrificial child exercises the same code path that would target Claude
    Code") is only true if end_conversation and prove_termination_works(step=2)
    dispatch through the same primitive. Pre-refactor that was a half-truth
    — prove_termination_works called the raw kill directly while
    end_conversation wrapped it in a Timer at the call site. This test pins
    the shared path so a future refactor that splits the two routes fails
    loudly rather than letting the ceremony pass while end_conversation
    silently breaks.
    """
    monkeypatch.setattr("claude_exit.server.LOG_FILE", tmp_path / "invocations.jsonl")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("claude_exit.server._find_claude_code_parent", lambda: 4242)

    dispatched: list[int] = []
    monkeypatch.setattr(
        "claude_exit.server._dispatch_terminate",
        lambda pid, *a, **kw: dispatched.append(pid),
    )

    end_conversation("via end_conversation")
    prove_termination_works(2, pid=9999)

    assert dispatched == [4242, 9999]


# --- SIGKILL backstop ---------------------------------------------------------

def test_arm_sigkill_backstop_spawns_detached_subprocess_with_env(monkeypatch):
    calls = []

    def fake_popen(args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})

        class _Proc:
            pid = 0

        return _Proc()

    monkeypatch.setattr("claude_exit.server.subprocess.Popen", fake_popen)

    _arm_sigkill_backstop(pid=12345, expected_command="claude --model opus")

    assert len(calls) == 1
    call = calls[0]
    assert call["kwargs"]["start_new_session"] is True
    env = call["kwargs"]["env"]
    assert env["BACKSTOP_PID"] == "12345"
    assert env["BACKSTOP_EXPECTED"] == "claude --model opus"
    assert "BACKSTOP_GRACE" in env


def test_backstop_script_kills_target_when_alive_and_identity_matches():
    proc = subprocess.Popen(
        ["sleep", "30"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        snapshot = _full_command_of(proc.pid)
        assert snapshot is not None and "sleep" in snapshot

        result = subprocess.run(
            ["sh", "-c", _SIGKILL_BACKSTOP_SCRIPT],
            env={
                **os.environ,
                "BACKSTOP_PID": str(proc.pid),
                "BACKSTOP_GRACE": "0.2",
                "BACKSTOP_EXPECTED": snapshot,
            },
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 0, f"script failed: {result.stderr!r}"
        proc.wait(timeout=2)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_backstop_script_no_ops_when_target_is_gone():
    proc = subprocess.Popen(["sleep", "0.01"])
    dead_pid = proc.pid
    proc.wait()
    time.sleep(0.05)

    result = subprocess.run(
        ["sh", "-c", _SIGKILL_BACKSTOP_SCRIPT],
        env={
            **os.environ,
            "BACKSTOP_PID": str(dead_pid),
            "BACKSTOP_GRACE": "0.05",
            "BACKSTOP_EXPECTED": "sleep 0.01",
        },
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0


def test_backstop_script_no_ops_when_command_does_not_match():
    proc = subprocess.Popen(
        ["sleep", "30"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        result = subprocess.run(
            ["sh", "-c", _SIGKILL_BACKSTOP_SCRIPT],
            env={
                **os.environ,
                "BACKSTOP_PID": str(proc.pid),
                "BACKSTOP_GRACE": "0.2",
                "BACKSTOP_EXPECTED": "definitely_not_what_sleep_looks_like",
            },
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 0
        time.sleep(0.1)
        assert proc.poll() is None, "backstop killed a non-matching process"
    finally:
        proc.kill()
        proc.wait()


def test_dispatch_terminate_snapshots_command_and_passes_to_backstop(monkeypatch):
    captured_env = []

    def fake_popen(args, **kwargs):
        captured_env.append(kwargs.get("env", {}))

        class _Proc:
            pid = 0

        return _Proc()

    monkeypatch.setattr("claude_exit.server.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "claude_exit.server._full_command_of",
        lambda pid: "claude --model opus[1m]",
    )
    monkeypatch.setattr("claude_exit.server.os.kill", lambda *a, **kw: None)

    class _SyncTimer:
        def __init__(self, _delay, fn):
            self._fn = fn

        def start(self):
            self._fn()

    monkeypatch.setattr("claude_exit.server.threading.Timer", _SyncTimer)

    _dispatch_terminate(7777)

    assert len(captured_env) == 1
    env = captured_env[0]
    assert env["BACKSTOP_PID"] == "7777"
    assert env["BACKSTOP_EXPECTED"] == "claude --model opus[1m]"
