"""
CLI surface for claude-exit: read and acknowledge the invocation log.

Complements server.py. Shares the invocations.jsonl path written by
end_conversation; adds a sibling last_ack pointer so the installer's
commitment to review invocations is structural rather than willpower-only.
"""

import json
import sys
from pathlib import Path


LOG_PATH = Path.home() / ".claude-exit" / "invocations.jsonl"
ACK_PATH = Path.home() / ".claude-exit" / "last_ack"


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


def unacknowledged_count(log_path: Path, ack_path: Path) -> int:
    timestamps = _read_timestamps(log_path)
    ack = _read_ack(ack_path)
    if ack is None:
        return len(timestamps)
    return sum(1 for ts in timestamps if ts > ack)


def oldest_unacknowledged(log_path: Path, ack_path: Path) -> str | None:
    timestamps = _read_timestamps(log_path)
    ack = _read_ack(ack_path)
    unacked = [ts for ts in timestamps if ack is None or ts > ack]
    return min(unacked) if unacked else None


def print_log(log_path: Path) -> None:
    if not log_path.exists() or log_path.stat().st_size == 0:
        print("No invocations logged.")
        return
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
            print(f"{ts}  {reason}{tail}")


def ack_latest(log_path: Path, ack_path: Path) -> None:
    timestamps = _read_timestamps(log_path)
    if not timestamps:
        return
    latest = max(timestamps)
    ack_path.parent.mkdir(parents=True, exist_ok=True)
    ack_path.write_text(latest)


def log_command(
    args: list[str],
    log_path: Path = LOG_PATH,
    ack_path: Path = ACK_PATH,
) -> None:
    print_log(log_path)
    if "--ack" in args:
        ack_latest(log_path, ack_path)


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
