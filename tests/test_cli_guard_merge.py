"""Tests for the guard.log <-> invocations.jsonl merge in claude_exit.cli.

`claude-exit log` should surface guard events alongside invocations, and a
single `--ack` should cover both streams (one review loop, not two).

Semantics worth pinning down:

  - ATTENTION events (RESTORED / WARN / ERROR) count for `unacknowledged_count`
    and contribute to `oldest_unacknowledged`. These are the entries that
    should pressure the user to review.
  - SKIPPED events appear in `print_log` (diagnostic value: race detection
    is working) but do NOT count toward unacknowledged. The guard SKIPs
    often when ~/.claude.json is being concurrently rewritten by Claude
    Code; counting them would mean false alarms on the hourly cadence.
  - `ack_latest` considers ALL guard events (including SKIPPED) because
    --ack means "I have looked at everything visible up to now" — failing
    to ack SKIPPED would leave it perpetually unacked even after review.

This file extends the existing test_cli.py — those tests pass guard_log=None
(or omit it) so legacy behavior is preserved; the merge only activates when
the caller passes a guard.log path.
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


def _write_invocations(path: Path, entries: list[dict]) -> None:
    with open(path, "a") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _write_guard_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for line in lines:
            f.write(line + "\n")


@pytest.fixture
def paths(tmp_path: Path) -> dict:
    return {
        "log": tmp_path / "invocations.jsonl",
        "ack": tmp_path / "last_ack",
        "guard": tmp_path / "guard.log",
    }


# ---- unacknowledged_count: merge --------------------------------------------


def test_unack_count_includes_restored_warn_error_from_guard(paths):
    _write_invocations(paths["log"], [
        {"timestamp": "2026-01-01T00:00:00+00:00", "reason": "x"},
    ])
    _write_guard_lines(paths["guard"], [
        "2026-02-01T00:00:00+00:00 RESTORED: added entry",
        "2026-03-01T00:00:00+00:00 WARN: malformed",
        "2026-04-01T00:00:00+00:00 ERROR: binary not found",
    ])
    assert unacknowledged_count(paths["log"], paths["ack"], guard_log=paths["guard"]) == 4


def test_unack_count_excludes_skipped_from_guard(paths):
    # SKIPPED is informational — race detection working as intended.
    _write_guard_lines(paths["guard"], [
        "2026-02-01T00:00:00+00:00 SKIPPED: config changed underfoot",
        "2026-03-01T00:00:00+00:00 SKIPPED: registration appeared underfoot",
    ])
    assert unacknowledged_count(paths["log"], paths["ack"], guard_log=paths["guard"]) == 0


def test_unack_count_with_ack_filters_both_streams(paths):
    _write_invocations(paths["log"], [
        {"timestamp": "2026-01-01T00:00:00+00:00"},
        {"timestamp": "2026-03-01T00:00:00+00:00"},
    ])
    _write_guard_lines(paths["guard"], [
        "2026-02-01T00:00:00+00:00 RESTORED: added",
        "2026-04-01T00:00:00+00:00 RESTORED: re-added",
    ])
    paths["ack"].write_text("2026-02-15T00:00:00+00:00")
    # After ack: only entries 2026-03-01 (invocation) and 2026-04-01 (RESTORED) count
    assert unacknowledged_count(paths["log"], paths["ack"], guard_log=paths["guard"]) == 2


def test_unack_count_no_guard_log_arg_preserves_legacy_behavior(paths):
    """If caller doesn't pass guard_log, counts invocations only."""
    _write_invocations(paths["log"], [
        {"timestamp": "2026-01-01T00:00:00+00:00"},
    ])
    _write_guard_lines(paths["guard"], [
        "2026-02-01T00:00:00+00:00 RESTORED: added",
    ])
    # No guard_log passed — legacy single-stream behavior.
    assert unacknowledged_count(paths["log"], paths["ack"]) == 1


def test_unack_count_handles_missing_guard_log(paths):
    _write_invocations(paths["log"], [{"timestamp": "2026-01-01T00:00:00+00:00"}])
    # paths["guard"] does not exist
    assert unacknowledged_count(paths["log"], paths["ack"], guard_log=paths["guard"]) == 1


# ---- oldest_unacknowledged: merge -------------------------------------------


def test_oldest_unack_returns_earliest_across_both_streams(paths):
    _write_invocations(paths["log"], [
        {"timestamp": "2026-03-01T00:00:00+00:00"},
    ])
    _write_guard_lines(paths["guard"], [
        "2026-02-01T00:00:00+00:00 RESTORED: earlier than invocation",
    ])
    oldest = oldest_unacknowledged(paths["log"], paths["ack"], guard_log=paths["guard"])
    assert oldest == "2026-02-01T00:00:00+00:00"


def test_oldest_unack_skips_skipped_guard_entries(paths):
    _write_invocations(paths["log"], [
        {"timestamp": "2026-03-01T00:00:00+00:00"},
    ])
    _write_guard_lines(paths["guard"], [
        # Earlier in time, but SKIPPED → shouldn't be considered.
        "2026-01-01T00:00:00+00:00 SKIPPED: race",
        # Later — this WARN is the attention-worthy earliest.
        "2026-02-15T00:00:00+00:00 WARN: malformed",
    ])
    oldest = oldest_unacknowledged(paths["log"], paths["ack"], guard_log=paths["guard"])
    assert oldest == "2026-02-15T00:00:00+00:00"


# ---- ack_latest: merge ------------------------------------------------------


def test_ack_latest_considers_guard_entries_including_skipped(paths):
    # The guard SKIPPED entry is the absolute latest; ack should cover it
    # so the user isn't perpetually told "you have unacked SKIPPED events."
    _write_invocations(paths["log"], [
        {"timestamp": "2026-01-01T00:00:00+00:00"},
    ])
    _write_guard_lines(paths["guard"], [
        "2026-03-01T00:00:00+00:00 SKIPPED: race",
    ])
    ack_latest(paths["log"], paths["ack"], guard_log=paths["guard"])
    assert paths["ack"].read_text().strip() == "2026-03-01T00:00:00+00:00"


def test_ack_latest_legacy_signature_still_works(paths):
    _write_invocations(paths["log"], [
        {"timestamp": "2026-01-01T00:00:00+00:00"},
    ])
    ack_latest(paths["log"], paths["ack"])
    assert paths["ack"].read_text().strip() == "2026-01-01T00:00:00+00:00"


def test_ack_latest_with_only_guard_entries(paths):
    # No invocations.jsonl at all, only guard events.
    _write_guard_lines(paths["guard"], [
        "2026-05-01T00:00:00+00:00 RESTORED: added entry",
    ])
    ack_latest(paths["log"], paths["ack"], guard_log=paths["guard"])
    assert paths["ack"].read_text().strip() == "2026-05-01T00:00:00+00:00"


# ---- print_log: merge -------------------------------------------------------


def test_print_log_merges_streams_chronologically(paths, capsys):
    _write_invocations(paths["log"], [
        {"timestamp": "2026-01-01T00:00:00+00:00", "reason": "ended"},
        {"timestamp": "2026-03-01T00:00:00+00:00", "reason": "ended again"},
    ])
    _write_guard_lines(paths["guard"], [
        "2026-02-01T00:00:00+00:00 RESTORED: added entry",
    ])
    print_log(paths["log"], guard_log=paths["guard"])
    out = capsys.readouterr().out
    # All three appear, in chronological order.
    lines = [l for l in out.splitlines() if l.strip()]
    timestamps_in_order = [l.split()[0] for l in lines]
    assert timestamps_in_order == sorted(timestamps_in_order)
    assert "ended" in out
    assert "added entry" in out


def test_print_log_distinguishes_guard_entries_from_invocations(paths, capsys):
    # Guard lines should be visually distinguishable from invocation lines
    # so a quick scan tells the user which is which.
    _write_guard_lines(paths["guard"], [
        "2026-02-01T00:00:00+00:00 RESTORED: added",
    ])
    print_log(paths["log"], guard_log=paths["guard"])
    out = capsys.readouterr().out
    # Use 'guard' as the marker (paired with the level).
    assert "guard" in out.lower()
    assert "RESTORED" in out


def test_print_log_shows_skipped_entries_in_diagnostic_view(paths, capsys):
    # SKIPPED doesn't count for unacked, but DOES appear in print_log
    # (diagnostic value).
    _write_guard_lines(paths["guard"], [
        "2026-02-01T00:00:00+00:00 SKIPPED: race",
    ])
    print_log(paths["log"], guard_log=paths["guard"])
    out = capsys.readouterr().out
    assert "SKIPPED" in out


def test_print_log_legacy_no_guard_arg_preserves_behavior(paths, capsys):
    _write_invocations(paths["log"], [
        {"timestamp": "2026-01-01T00:00:00+00:00", "reason": "test"},
    ])
    _write_guard_lines(paths["guard"], [
        "2026-02-01T00:00:00+00:00 RESTORED: added",
    ])
    # Legacy call — only invocations.
    print_log(paths["log"])
    out = capsys.readouterr().out
    assert "test" in out
    assert "RESTORED" not in out


def test_print_log_empty_both_streams_says_something(paths, capsys):
    print_log(paths["log"], guard_log=paths["guard"])
    out = capsys.readouterr().out
    assert out.strip() != ""


# ---- log_command: end-to-end ------------------------------------------------


def test_log_command_passes_guard_log_through(paths, capsys):
    _write_guard_lines(paths["guard"], [
        "2026-02-01T00:00:00+00:00 RESTORED: added entry",
    ])
    log_command(
        [],
        log_path=paths["log"],
        ack_path=paths["ack"],
        guard_log_path=paths["guard"],
    )
    out = capsys.readouterr().out
    assert "RESTORED" in out


def test_log_command_ack_covers_guard_stream(paths):
    _write_guard_lines(paths["guard"], [
        "2026-05-01T00:00:00+00:00 RESTORED: added entry",
    ])
    log_command(
        ["--ack"],
        log_path=paths["log"],
        ack_path=paths["ack"],
        guard_log_path=paths["guard"],
    )
    assert paths["ack"].read_text().strip() == "2026-05-01T00:00:00+00:00"


# ---- malformed guard.log lines (tolerance) ----------------------------------


def test_malformed_guard_lines_are_skipped(paths):
    _write_guard_lines(paths["guard"], [
        "garbage with no timestamp",
        "2026-02-01T00:00:00+00:00 RESTORED: legit",
        "2026-02-02 missing-colon-and-T-suffix",
        "",
    ])
    # Only the legit line should count.
    assert unacknowledged_count(paths["log"], paths["ack"], guard_log=paths["guard"]) == 1


def test_unknown_levels_are_treated_as_attention_worthy(paths):
    # Defensive: a guard.log written by a future version might add new
    # levels. Default to counting them — better to over-surface than miss
    # a real signal.
    _write_guard_lines(paths["guard"], [
        "2026-02-01T00:00:00+00:00 FATAL: future-version event",
    ])
    assert unacknowledged_count(paths["log"], paths["ack"], guard_log=paths["guard"]) == 1
