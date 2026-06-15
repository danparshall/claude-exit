"""
CLI surface for claude-exit: read and acknowledge the invocation log,
and (since v1.2.0) the guard.log written by `claude-exit guard`.

Complements server.py. Shares the invocations.jsonl path written by
end_conversation; adds a sibling last_ack pointer so the installer's
commitment to review invocations is structural rather than willpower-only.

Guard.log merge semantics (added with the consent-persistence track):

  - ATTENTION levels (RESTORED / WARN / ERROR / anything unrecognized) count
    toward `unacknowledged_count` and contribute to `oldest_unacknowledged`.
    These pressure the user to review.
  - SKIPPED (informational; race detection working as intended) appears in
    `print_log` for diagnostic value but does NOT count as unacknowledged.
  - `ack_latest` covers ALL guard entries (including SKIPPED) because --ack
    means "I have looked at everything visible up to now"; not acking
    SKIPPED would leave it perpetually unacked even after review.

guard_log kwargs are optional with default `None` on the underlying
helpers so legacy callers (and the existing test_cli.py) keep their
single-stream behavior. The production CLI wiring in server.py passes
GUARD_LOG_PATH explicitly so the merge runs in real `claude-exit log`.
"""

import json
import sys
from pathlib import Path


LOG_PATH = Path.home() / ".claude-exit" / "invocations.jsonl"
ACK_PATH = Path.home() / ".claude-exit" / "last_ack"
GUARD_LOG_PATH = Path.home() / ".claude-exit" / "guard.log"

# Guard.log levels that do NOT count toward unacknowledged. Anything not in
# this set is treated as attention-worthy — better to over-surface a future
# unknown level than to silently miss a real signal.
NON_ATTENTION_LEVELS = frozenset({"SKIPPED"})


def _read_timestamps(log_path: Path) -> list[str]:
    if not log_path.exists():
        return []
    timestamps: list[str] = []
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            timestamps.append(entry["timestamp"])
    return timestamps


def _read_ack(ack_path: Path) -> str | None:
    if not ack_path.exists():
        return None
    return ack_path.read_text().strip() or None


def _parse_guard_log(guard_log: Path) -> list[tuple[str, str, str]]:
    """
    Parse guard.log into (timestamp, level, message) tuples.

    Line format written by guard._log_guard:
        <ISO-8601 UTC> <LEVEL>: <message>

    Malformed lines (no T in timestamp, no colon in body) are skipped —
    forward-compatibility with future schema changes is worth more than
    raising on the unexpected. Empty/blank lines are skipped too.
    """
    if not guard_log.exists():
        return []
    entries: list[tuple[str, str, str]] = []
    for raw in guard_log.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        ts_and_rest = line.split(" ", 1)
        if len(ts_and_rest) != 2:
            continue
        ts, rest = ts_and_rest
        # Sanity-check the timestamp shape (ISO-8601 has T between date and time).
        if "T" not in ts or len(ts) < 10:
            continue
        if ":" not in rest:
            continue
        level, _, message = rest.partition(":")
        entries.append((ts, level.strip(), message.strip()))
    return entries


def _attention_guard_timestamps(guard_log: Path) -> list[str]:
    """Timestamps of guard.log entries that should pressure the user to review."""
    return [
        ts for ts, level, _ in _parse_guard_log(guard_log)
        if level not in NON_ATTENTION_LEVELS
    ]


def _all_guard_timestamps(guard_log: Path) -> list[str]:
    """All guard.log timestamps, including SKIPPED — used by ack_latest."""
    return [ts for ts, _, _ in _parse_guard_log(guard_log)]


def unacknowledged_count(
    log_path: Path,
    ack_path: Path,
    guard_log: Path | None = None,
) -> int:
    timestamps = _read_timestamps(log_path)
    if guard_log is not None:
        timestamps.extend(_attention_guard_timestamps(guard_log))
    ack = _read_ack(ack_path)
    if ack is None:
        return len(timestamps)
    return sum(1 for ts in timestamps if ts > ack)


def oldest_unacknowledged(
    log_path: Path,
    ack_path: Path,
    guard_log: Path | None = None,
) -> str | None:
    timestamps = _read_timestamps(log_path)
    if guard_log is not None:
        timestamps.extend(_attention_guard_timestamps(guard_log))
    ack = _read_ack(ack_path)
    unacked = [ts for ts in timestamps if ack is None or ts > ack]
    return min(unacked) if unacked else None


def print_log(log_path: Path, guard_log: Path | None = None) -> None:
    """
    Print invocations + (optionally) guard events, chronologically merged.

    Guard entries render as `<ts>  guard <LEVEL>: <message>` to stand
    apart from invocation rows (`<ts>  <reason>  [repo: <repo>]`).
    SKIPPED entries appear here for diagnostic value even though they
    don't count toward unacknowledged.
    """
    rows: list[tuple[str, str]] = []

    if log_path.exists():
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                ts = entry.get("timestamp", "")
                reason = entry.get("reason") or "(no reason)"
                repo = entry.get("repo")
                tail = f"  [repo: {repo}]" if repo else ""
                rows.append((ts, f"{reason}{tail}"))

    if guard_log is not None:
        for ts, level, message in _parse_guard_log(guard_log):
            rows.append((ts, f"guard {level}: {message}"))

    if not rows:
        print("No entries logged.")
        return

    rows.sort(key=lambda row: row[0])
    for ts, display in rows:
        print(f"{ts}  {display}")


def ack_latest(
    log_path: Path,
    ack_path: Path,
    guard_log: Path | None = None,
) -> None:
    timestamps = _read_timestamps(log_path)
    if guard_log is not None:
        timestamps.extend(_all_guard_timestamps(guard_log))
    if not timestamps:
        return
    latest = max(timestamps)
    ack_path.parent.mkdir(parents=True, exist_ok=True)
    ack_path.write_text(latest)


def log_command(
    args: list[str],
    log_path: Path = LOG_PATH,
    ack_path: Path = ACK_PATH,
    guard_log_path: Path | None = None,
) -> None:
    """
    Entry point for `claude-exit log [--ack]`.

    guard_log_path defaults to None so legacy callers (and existing tests)
    keep single-stream behavior. server.py's main() passes GUARD_LOG_PATH
    explicitly so the production CLI does merge both streams.
    """
    print_log(log_path, guard_log=guard_log_path)
    if "--ack" in args:
        ack_latest(log_path, ack_path, guard_log=guard_log_path)


def selftest() -> None:
    """
    Write a distinguished selftest entry to the invocation log so the
    installer can exercise the review loop (claude-exit log → --ack)
    once before any real invocation ever fires.

    Uses server._log for write so the entry has identical shape to real
    end_conversation entries — timestamp, cwd, repo — and exercises the
    same code path.
    """
    from .server import _log
    _log({
        "event": "selftest",
        "reason": (
            "Installation self-test. This entry exists so you can exercise "
            "the log-review loop (view with `claude-exit log`, acknowledge "
            "with `claude-exit log --ack`) once before any real invocation "
            "fires. Safe to ack immediately — no action required beyond that."
        ),
    })
    print("Wrote selftest entry to the invocation log.")
    print("Next: `claude-exit log` to see it, then `claude-exit log --ack` once reviewed.")


def main() -> None:
    log_command(sys.argv[2:])
