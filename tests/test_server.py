"""Tests for server.py additions: repo/cwd capture in log entries,
and the read_invocation_log MCP tool.

The MCP tool is tested via its underlying helper that takes a path
argument — the @mcp.tool decorator is a thin delegate.
"""

import json
from pathlib import Path

import pytest

from claude_exit.server import _find_repo_root, _log, _read_log


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
