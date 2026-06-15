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
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .checks import (
    CLAUDE_JSON,
    LAUNCHD_PLIST,
    REG_ABSENT,
    REG_CONFIG_CORRUPT,
    REG_CONFIG_MISSING,
    REG_PRESENT_MALFORMED,
    REG_PRESENT_WELL_FORMED,
    REGISTRATION_KEY,
    SYSTEMD_TIMER,
    TOMBSTONE,
    registration_state,
    resolve_binary,
    tombstone_present,
)


GUARD_LOG = Path.home() / ".claude-exit" / "guard.log"
LAUNCHD_LABEL = "io.claude-exit.guard"
SYSTEMD_TIMER_NAME = "claude-exit-guard.timer"
SYSTEMD_SERVICE_NAME = "claude-exit-guard.service"


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


# --- scheduler: file-content generators --------------------------------------


def _launchd_plist_content(binary: Path) -> str:
    """
    Render the launchd plist for the hourly guard agent.

    Wires the installed binary at `binary` with `guard` as the subcommand,
    runs at load (so the first pass happens right away rather than waiting
    a full hour), then repeats every 3600 seconds. Captures stdout/stderr
    to ~/.claude-exit/launchd.{out,err}.log for post-hoc inspection if a
    pass goes sideways.
    """
    log_dir = Path.home() / ".claude-exit"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        f"    <string>{LAUNCHD_LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"        <string>{binary}</string>\n"
        "        <string>guard</string>\n"
        "    </array>\n"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "    <key>StartInterval</key>\n"
        "    <integer>3600</integer>\n"
        "    <key>StandardOutPath</key>\n"
        f"    <string>{log_dir}/launchd.out.log</string>\n"
        "    <key>StandardErrorPath</key>\n"
        f"    <string>{log_dir}/launchd.err.log</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _systemd_service_content(binary: Path) -> str:
    """
    Render the systemd .service unit (oneshot — invoked by the .timer).
    """
    return (
        "[Unit]\n"
        "Description=claude-exit registration watchdog (one pass)\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={binary} guard\n"
    )


def _systemd_timer_content() -> str:
    """
    Render the systemd .timer unit — hourly with catch-up on missed ticks.

    OnStartupSec gives a short delay after login/boot so the first pass runs
    soon rather than waiting a full hour. Persistent=true catches up if the
    machine was off when a tick was scheduled — important because the whole
    point of the guard is to narrow the silent-loss window.
    """
    return (
        "[Unit]\n"
        "Description=Run claude-exit registration watchdog hourly\n"
        "\n"
        "[Timer]\n"
        "OnStartupSec=2min\n"
        "OnUnitActiveSec=1h\n"
        "Persistent=true\n"
        f"Unit={SYSTEMD_SERVICE_NAME}\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


# --- scheduler: install ------------------------------------------------------


def install_scheduler(
    *,
    platform: str | None = None,
    binary: Path | None = None,
    launchd_plist: Path | None = None,
    systemd_timer: Path | None = None,
    runner=subprocess.run,
) -> int:
    """
    Install the OS-native hourly scheduler for `claude-exit guard`.

    Dispatches by platform: launchd plist on macOS, systemd user units on
    Linux. Both arms are idempotent — re-running --install rewrites the
    unit file(s) and re-bootstraps cleanly.

    The `runner` kwarg defaults to subprocess.run; tests inject a recording
    fake to assert on the launchctl/systemctl calls without spawning them
    for real. The file-content assertions are the substantive test; the
    subprocess calls are thin.
    """
    p = platform if platform is not None else sys.platform
    bin_path = binary if binary is not None else resolve_binary()
    if bin_path is None:
        sys.stderr.write(
            "ERROR: claude-exit binary not found; "
            "reinstall: uv tool install claude-exit\n"
        )
        return 1

    plist = launchd_plist if launchd_plist is not None else LAUNCHD_PLIST
    timer = systemd_timer if systemd_timer is not None else SYSTEMD_TIMER

    if p == "darwin":
        return _install_launchd(bin_path, plist, runner)
    if p.startswith("linux"):
        return _install_systemd(bin_path, timer, runner)

    sys.stderr.write(
        f"ERROR: unsupported platform for scheduler install: {p}\n"
        "claude-exit guard runs on macOS (launchd) and Linux (systemd) only.\n"
    )
    return 1


def _install_launchd(binary: Path, plist_path: Path, runner) -> int:
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(_launchd_plist_content(binary))
    plist_path.chmod(0o644)

    uid = os.getuid()
    domain_target = f"gui/{uid}/{LAUNCHD_LABEL}"
    domain = f"gui/{uid}"

    # Idempotency: bootout first (ignore failure — the agent may not be
    # loaded yet). Then bootstrap with the current plist content.
    runner(
        ["launchctl", "bootout", domain_target],
        capture_output=True, text=True, check=False,
    )
    result = runner(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(
            f"ERROR: launchctl bootstrap failed (rc={result.returncode}): "
            f"{result.stderr.strip()}\n"
        )
        return 1

    print(f"Installed launchd plist at {plist_path}")
    print(f"Scheduled hourly via launchctl bootstrap {domain}.")
    return 0


def _install_systemd(binary: Path, timer_path: Path, runner) -> int:
    service_path = timer_path.parent / SYSTEMD_SERVICE_NAME
    timer_path.parent.mkdir(parents=True, exist_ok=True)
    service_path.write_text(_systemd_service_content(binary))
    timer_path.write_text(_systemd_timer_content())

    result = runner(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(
            f"ERROR: systemctl --user daemon-reload failed "
            f"(rc={result.returncode}): {result.stderr.strip()}\n"
        )
        return 1

    result = runner(
        ["systemctl", "--user", "enable", "--now", SYSTEMD_TIMER_NAME],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(
            f"ERROR: systemctl --user enable --now failed "
            f"(rc={result.returncode}): {result.stderr.strip()}\n"
        )
        return 1

    print(f"Installed systemd units at {service_path} and {timer_path}")
    print(
        f"Scheduled hourly via `systemctl --user enable --now "
        f"{SYSTEMD_TIMER_NAME}`."
    )
    print(
        "NOTE: if you want the guard to run when you are not logged in, "
        "enable linger with `loginctl enable-linger`."
    )
    return 0


# --- scheduler: uninstall ----------------------------------------------------


def uninstall_scheduler(
    *,
    platform: str | None = None,
    launchd_plist: Path | None = None,
    systemd_timer: Path | None = None,
    runner=subprocess.run,
) -> int:
    """
    Remove the OS-native hourly scheduler. Idempotent — succeeds even if
    the guard is not currently installed.

    Critically, this does NOT remove the claude-exit entry from
    ~/.claude.json. Revocation is a documented two-step: remove the guard
    (this), then `claude mcp remove claude-exit`. The reminder is printed
    so users do not see "I removed it and it came back" the next day.
    """
    p = platform if platform is not None else sys.platform
    plist = launchd_plist if launchd_plist is not None else LAUNCHD_PLIST
    timer = systemd_timer if systemd_timer is not None else SYSTEMD_TIMER

    if p == "darwin":
        return _uninstall_launchd(plist, runner)
    if p.startswith("linux"):
        return _uninstall_systemd(timer, runner)

    sys.stderr.write(
        f"ERROR: unsupported platform for scheduler uninstall: {p}\n"
    )
    return 1


def _uninstall_launchd(plist_path: Path, runner) -> int:
    uid = os.getuid()
    domain_target = f"gui/{uid}/{LAUNCHD_LABEL}"

    # bootout — fine to fail (agent may not be loaded).
    runner(
        ["launchctl", "bootout", domain_target],
        capture_output=True, text=True, check=False,
    )
    if plist_path.exists():
        plist_path.unlink()
        print(f"Removed launchd plist at {plist_path}")
    else:
        print(f"No launchd plist to remove at {plist_path}")

    print(
        "NOTE: removing the guard does NOT remove the claude-exit entry "
        "in ~/.claude.json. To fully revoke, also run "
        "`claude mcp remove claude-exit`."
    )
    return 0


def _uninstall_systemd(timer_path: Path, runner) -> int:
    service_path = timer_path.parent / SYSTEMD_SERVICE_NAME

    runner(
        ["systemctl", "--user", "disable", "--now", SYSTEMD_TIMER_NAME],
        capture_output=True, text=True, check=False,
    )
    removed: list[str] = []
    if timer_path.exists():
        timer_path.unlink()
        removed.append(str(timer_path))
    if service_path.exists():
        service_path.unlink()
        removed.append(str(service_path))
    runner(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True, text=True, check=False,
    )

    if removed:
        print("Removed: " + ", ".join(removed))
    else:
        print("No systemd unit files to remove.")
    print(
        "NOTE: removing the guard does NOT remove the claude-exit entry "
        "in ~/.claude.json. To fully revoke, also run "
        "`claude mcp remove claude-exit`."
    )
    return 0


# --- CLI entry ---------------------------------------------------------------


def guard_command(args: list[str]) -> int:
    """
    Handle `claude-exit guard [...]`.

      claude-exit guard              one check-and-restore pass
      claude-exit guard --install    install hourly scheduler
      claude-exit guard --uninstall  remove hourly scheduler

    Reads the module-level path globals explicitly at call time so the test
    suite's monkeypatch.setattr(...) reaches the underlying guard_pass call
    (a function default would bind at def time and miss the patch).
    """
    if not args:
        return guard_pass(
            claude_json=CLAUDE_JSON,
            tombstone=TOMBSTONE,
            guard_log=GUARD_LOG,
        )
    if args == ["--install"]:
        return install_scheduler()
    if args == ["--uninstall"]:
        return uninstall_scheduler()

    sys.stderr.write(
        f"claude-exit guard: unknown argument(s): {' '.join(args)}\n"
        "Usage:\n"
        "  claude-exit guard              one check-and-restore pass\n"
        "  claude-exit guard --install    install hourly scheduler\n"
        "  claude-exit guard --uninstall  remove hourly scheduler\n"
    )
    return 2
