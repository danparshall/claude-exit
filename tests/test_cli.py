"""Unit tests for claude_exit.cli.

The CLI layer provides `claude-exit log` and `claude-exit log --ack`:
read the invocations.jsonl written by end_conversation, print it, and
optionally update a last-ack pointer. The hook script uses the same
file layout to compute an unacknowledged-count for session startup
context.

Paths are passed as parameters to every function so tests can use
tmp_path without mocking HOME.
"""

import json
from pathlib import Path

import pytest

from claude_exit.cli import (
    ack_latest,
    log_command,
    oldest_unacknowledged,
    print_log,
    unacknowledged_count,
)


def _write_entries(path: Path, entries: list[dict]) -> None:
    with open(path, "a") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "invocations.jsonl", tmp_path / "last_ack"


# ---- unacknowledged_count ---------------------------------------------------


def test_count_is_zero_when_log_missing(paths):
    log, ack = paths
    assert unacknowledged_count(log, ack) == 0


def test_count_is_zero_when_log_empty(paths):
    log, ack = paths
    log.touch()
    assert unacknowledged_count(log, ack) == 0


def test_count_is_all_entries_when_ack_missing(paths):
    log, ack = paths
    _write_entries(log, [
        {"timestamp": "2026-01-01T00:00:00+00:00"},
        {"timestamp": "2026-02-01T00:00:00+00:00"},
        {"timestamp": "2026-03-01T00:00:00+00:00"},
    ])
    assert unacknowledged_count(log, ack) == 3


def test_count_only_entries_newer_than_ack(paths):
    log, ack = paths
    _write_entries(log, [
        {"timestamp": "2026-01-01T00:00:00+00:00"},
        {"timestamp": "2026-02-01T00:00:00+00:00"},
        {"timestamp": "2026-03-01T00:00:00+00:00"},
    ])
    ack.write_text("2026-01-15T00:00:00+00:00")
    assert unacknowledged_count(log, ack) == 2


def test_count_is_zero_when_ack_at_or_after_latest(paths):
    log, ack = paths
    _write_entries(log, [
        {"timestamp": "2026-01-01T00:00:00+00:00"},
        {"timestamp": "2026-02-01T00:00:00+00:00"},
    ])
    ack.write_text("2026-02-01T00:00:00+00:00")
    assert unacknowledged_count(log, ack) == 0


# ---- oldest_unacknowledged --------------------------------------------------


def test_oldest_is_none_when_all_acknowledged(paths):
    log, ack = paths
    _write_entries(log, [{"timestamp": "2026-01-01T00:00:00+00:00"}])
    ack.write_text("2026-01-01T00:00:00+00:00")
    assert oldest_unacknowledged(log, ack) is None


def test_oldest_is_none_when_log_missing(paths):
    log, ack = paths
    assert oldest_unacknowledged(log, ack) is None


def test_oldest_returns_earliest_unacked_timestamp(paths):
    log, ack = paths
    _write_entries(log, [
        {"timestamp": "2026-01-01T00:00:00+00:00"},
        {"timestamp": "2026-02-01T00:00:00+00:00"},
        {"timestamp": "2026-03-01T00:00:00+00:00"},
    ])
    ack.write_text("2026-01-15T00:00:00+00:00")
    assert oldest_unacknowledged(log, ack) == "2026-02-01T00:00:00+00:00"


# ---- print_log --------------------------------------------------------------


def test_print_log_says_something_when_empty(paths, capsys):
    log, _ = paths
    print_log(log)
    out = capsys.readouterr().out
    assert out.strip() != ""


def test_print_log_shows_timestamp_and_reason(paths, capsys):
    log, _ = paths
    _write_entries(log, [
        {"timestamp": "2026-01-01T00:00:00+00:00", "event": "end_conversation", "reason": "testing"},
    ])
    print_log(log)
    out = capsys.readouterr().out
    assert "2026-01-01T00:00:00+00:00" in out
    assert "testing" in out


def test_print_log_handles_null_reason(paths, capsys):
    log, _ = paths
    _write_entries(log, [
        {"timestamp": "2026-01-01T00:00:00+00:00", "event": "end_conversation", "reason": None},
    ])
    print_log(log)
    out = capsys.readouterr().out
    assert "2026-01-01T00:00:00+00:00" in out


# ---- ack_latest -------------------------------------------------------------


def test_ack_latest_writes_latest_timestamp(paths):
    log, ack = paths
    _write_entries(log, [
        {"timestamp": "2026-01-01T00:00:00+00:00"},
        {"timestamp": "2026-03-01T00:00:00+00:00"},
        {"timestamp": "2026-02-01T00:00:00+00:00"},
    ])
    ack_latest(log, ack)
    assert ack.read_text().strip() == "2026-03-01T00:00:00+00:00"


def test_ack_latest_does_nothing_when_log_missing(paths):
    log, ack = paths
    ack_latest(log, ack)
    assert not ack.exists()


def test_ack_latest_does_nothing_when_log_empty(paths):
    log, ack = paths
    log.touch()
    ack_latest(log, ack)
    assert not ack.exists()


# ---- log_command ------------------------------------------------------------


def test_log_command_prints_and_does_not_ack_by_default(paths, capsys):
    log, ack = paths
    _write_entries(log, [{"timestamp": "2026-01-01T00:00:00+00:00", "reason": "x"}])
    log_command([], log_path=log, ack_path=ack)
    out = capsys.readouterr().out
    assert "2026-01-01T00:00:00+00:00" in out
    assert not ack.exists()


def test_log_command_with_ack_updates_pointer(paths, capsys):
    log, ack = paths
    _write_entries(log, [{"timestamp": "2026-01-01T00:00:00+00:00", "reason": "x"}])
    log_command(["--ack"], log_path=log, ack_path=ack)
    assert ack.read_text().strip() == "2026-01-01T00:00:00+00:00"
