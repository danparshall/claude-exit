"""
Registration watchdog — the `claude-exit guard` subcommand.

One check-and-restore pass over the `claude-exit` entry in `~/.claude.json`.
Run hourly out-of-band (launchd on macOS / systemd user timer on Linux —
scheduler install/uninstall lands in a follow-up commit), the guard bounds
silent registration loss at one hour. Without it, a Claude-Code-side
corrupt-and-regenerate of ~/.claude.json can drop our entry without
notification — the 2026-06-05 incident that motivated this design.

Design principle (see plans/consent-persistence-overview.md):
**Restore what entropy owns; alert on what intent owns.**

  - ~/.claude.json is entropy-owned (Claude Code rewrites it constantly,
    regenerates on corruption). The guard restores our entry there.
  - ~/.claude/settings.json is intent-owned (user / dotfiles). The guard
    never writes to it — that's the hook's job to detect-and-name, not
    silently repair.

State branches (via checks.registration_state):

    PRESENT_WELL_FORMED  → silent no-op
    PRESENT_MALFORMED    → WARN (mangled value may be deliberate edit)
    CONFIG_CORRUPT       → WARN (don't touch unparseable / wrong-shape file)
    ABSENT               → restore (read, mutate, atomic-replace with mtime guard)
    CONFIG_MISSING       → create fresh file with just our entry

Tombstone (`~/.claude-exit/uninstalled`): silent no-op regardless of state.
The deliberate-uninstall suppression shared with the hook.

Lost-update race on ~/.claude.json: Claude Code rewrites this file
constantly, and it's far more than MCP config (~150KB in production). A
naive read-modify-write would drop a concurrent CC write that landed
between our read and our replace. Mitigation: snapshot mtime+size before
read; re-check immediately before os.replace; abort to SKIPPED if changed.
Window shrinks from "whole pass" to "stat-to-replace," and we only write
at all when the registration is already missing.

Surfacing: RESTORED / SKIPPED / WARN / ERROR lines to ~/.claude-exit/guard.log
(`<ISO-8601 UTC> <LEVEL>: <message>`). Follow-up commit extends
`claude-exit log` to merge guard.log events into the existing --ack loop.
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .checks import (
    CLAUDE_JSON,
    REG_ABSENT,
    REG_CONFIG_CORRUPT,
    REG_CONFIG_MISSING,
    REG_PRESENT_MALFORMED,
    REG_PRESENT_WELL_FORMED,
    REGISTRATION_KEY,
    TOMBSTONE,
    registration_state,
    resolve_binary,
    tombstone_present,
)


GUARD_LOG = Path.home() / ".claude-exit" / "guard.log"


# --- public entry point ------------------------------------------------------


def guard_pass(
    *,
    claude_json: Path = CLAUDE_JSON,
    tombstone: Path = TOMBSTONE,
    guard_log: Path = GUARD_LOG,
) -> int:
    """
    Run one check-and-restore pass.

    Returns:
        0 on healthy / restored / skipped (anything non-erroring).
        1 only when we wanted to restore but couldn't (binary unresolvable).

    Never raises into the scheduler. All decisions land in guard.log; the
    exit code is for the scheduler's own bookkeeping, not for the user.
    """
    if tombstone_present(tombstone):
        return 0  # Deliberate uninstall — silent no-op.

    state = registration_state(claude_json)

    if state == REG_PRESENT_WELL_FORMED:
        return 0

    if state == REG_PRESENT_MALFORMED:
        _log_guard(
            guard_log,
            "WARN",
            "registration present but malformed; not touching it",
        )
        return 0

    if state == REG_CONFIG_CORRUPT:
        _log_guard(
            guard_log,
            "WARN",
            "~/.claude.json is corrupt; not touching it",
        )
        return 0

    # ABSENT or CONFIG_MISSING: we want to write. Resolve the binary first.
    bin_path = resolve_binary()
    if bin_path is None:
        _log_guard(
            guard_log,
            "ERROR",
            (
                "claude-exit binary not found; "
                "reinstall: uv tool install claude-exit"
            ),
        )
        return 1

    new_entry = {"command": str(bin_path)}

    if state == REG_CONFIG_MISSING:
        return _create_fresh_config(claude_json, new_entry, guard_log)

    # ABSENT — preserve all other content. Snapshot before read so we can
    # detect if Claude Code rewrites underneath us mid-pass.
    return _restore_into_existing(claude_json, new_entry, guard_log)


# --- restore branches --------------------------------------------------------


def _create_fresh_config(
    claude_json: Path, new_entry: dict, guard_log: Path
) -> int:
    """CONFIG_MISSING: write a new file containing only our entry."""
    payload = {"mcpServers": {REGISTRATION_KEY: new_entry}}
    new_text = json.dumps(payload, indent=2) + "\n"

    replaced = _atomic_replace_if_unchanged(
        claude_json, new_text, snapshot=None
    )
    if replaced:
        _log_guard(
            guard_log,
            "RESTORED",
            (
                f"created {claude_json} with only the claude-exit entry "
                f"(command: {new_entry['command']})"
            ),
        )
    else:
        # Someone created the file between our state check and now. Next
        # hourly pass will re-evaluate from whatever shape they wrote.
        _log_guard(
            guard_log,
            "SKIPPED",
            "config appeared underfoot during create; will retry next pass",
        )
    return 0


def _restore_into_existing(
    claude_json: Path, new_entry: dict, guard_log: Path
) -> int:
    """ABSENT: read existing file, add our entry, atomic-replace with race guard."""
    snapshot = _stat_snapshot(claude_json)

    try:
        data = json.loads(claude_json.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        # Race: file became corrupt between registration_state and our read.
        _log_guard(
            guard_log,
            "SKIPPED",
            "config changed underfoot during read; will retry next pass",
        )
        return 0

    if not isinstance(data, dict):
        _log_guard(
            guard_log,
            "SKIPPED",
            "config changed underfoot during read; will retry next pass",
        )
        return 0

    servers = data.get("mcpServers")
    if servers is None:
        data["mcpServers"] = {}
        servers = data["mcpServers"]
    elif not isinstance(servers, dict):
        # Race: mcpServers changed type between state check and read.
        _log_guard(
            guard_log,
            "SKIPPED",
            "config changed underfoot during read; will retry next pass",
        )
        return 0

    if REGISTRATION_KEY in servers:
        # Race: another writer added it between state check and now.
        _log_guard(
            guard_log,
            "SKIPPED",
            "registration appeared underfoot; no action needed",
        )
        return 0

    servers[REGISTRATION_KEY] = new_entry
    new_text = json.dumps(data, indent=2) + "\n"

    replaced = _atomic_replace_if_unchanged(claude_json, new_text, snapshot)
    if replaced:
        _log_guard(
            guard_log,
            "RESTORED",
            (
                f"added claude-exit to {claude_json} mcpServers "
                f"(command: {new_entry['command']})"
            ),
        )
    else:
        _log_guard(
            guard_log,
            "SKIPPED",
            "config changed underfoot; will retry next pass",
        )
    return 0


# --- atomic write with stat-based race guard ---------------------------------


def _stat_snapshot(path: Path) -> tuple[int, int] | None:
    """
    Return (mtime_ns, size) for `path`, or None if it doesn't exist.

    The snapshot is the "before" half of the lost-update race guard. We
    compare against the same fields immediately before os.replace; any
    mismatch means someone wrote to the file in between, and we abort
    rather than clobber their write.
    """
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        return None


def _atomic_replace_if_unchanged(
    path: Path, new_text: str, snapshot: tuple[int, int] | None
) -> bool:
    """
    Atomically replace `path` with `new_text` iff the current stat matches
    `snapshot`. Returns True if replaced, False if skipped (file changed
    underfoot).

    Atomic-write: tempfile.mkstemp in the same directory (for an atomic
    os.replace across the same filesystem), chmod 0o600 (sensitive-config
    convention; matches the PDF's Appendix A), then os.replace.

    Re-checks the stat right before os.replace — the narrow window between
    our temp-file write and the rename is the only window left after this
    guard.
    """
    current = _stat_snapshot(path)
    if current != snapshot:
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".tmp."
    )
    tmp_path = Path(tmp_str)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(new_text)
        os.chmod(tmp_path, 0o600)

        # Final race check before os.replace. Mismatch → abort and clean up.
        current = _stat_snapshot(path)
        if current != snapshot:
            tmp_path.unlink()
            return False

        os.replace(tmp_path, path)
        return True
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


# --- guard.log writer --------------------------------------------------------


def _log_guard(guard_log: Path, level: str, message: str) -> None:
    """
    Append one line to guard.log: `<ISO-8601 UTC> <LEVEL>: <message>`.

    Levels in use: RESTORED (we wrote our entry), SKIPPED (race or no-op),
    WARN (file present but mangled — we deliberately did nothing), ERROR
    (we wanted to write but couldn't). Follow-up commit extends
    `claude-exit log` to merge these into the --ack loop.
    """
    guard_log.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    with open(guard_log, "a") as f:
        f.write(f"{ts} {level}: {message}\n")


# --- CLI entry ---------------------------------------------------------------


def guard_command(args: list[str]) -> int:
    """
    Handle `claude-exit guard [...]`.

    For now: bare `claude-exit guard` runs one pass. The `--install` /
    `--uninstall` arms land in a follow-up commit.

    Reads the module-level path globals explicitly at call time so the test
    suite's monkeypatch.setattr(...) reaches the underlying guard_pass call
    (a function default would bind at def time and miss the patch).
    """
    if args:
        # Stub: anything else is an error until C3 (scheduler) lands.
        sys.stderr.write(
            f"claude-exit guard: unknown argument(s): {' '.join(args)}\n"
            "Usage: claude-exit guard  (no arguments — one check-and-restore pass)\n"
        )
        return 2
    return guard_pass(
        claude_json=CLAUDE_JSON,
        tombstone=TOMBSTONE,
        guard_log=GUARD_LOG,
    )
