"""
`claude-exit doctor` — one-shot health check of the consent architecture.

Pure-read audit of every artifact the wiring depends on. Prints one line per
check with a `fix:` continuation for anything actionable; exits 0 if nothing
came back MISSING or WARN, else 1 (scriptable).

Design principles (see plans/consent-persistence-overview.md and
plans/doctor-subcommand.md):

  - checks.py owns facts; doctor.py owns presentation. Every check function
    here wraps one (or a few) checks.py predicates and returns a Check
    tuple in the (status, message, fix_line-or-None) shape doctor formats.
  - No writes. No network. Check #8 spawns and reaps a sacrificial child
    (via `prove_termination_works`) — that's a subprocess, but no persistent
    state changes; verified read-only w.r.t. invocations.jsonl.
  - Neutral phrasing on permission state (INFO). Gated vs. pre-approved
    are both legitimate installs; doctor reports which is in effect so a
    silent flip is visible, not to editorialize.
  - Complementary to `selftest` — doctor audits the wiring; selftest
    exercises the review loop. Neither replaces the other.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, NamedTuple

from .checks import (
    CLAUDE_JSON,
    GATED,
    HOOK_PATH,
    LAUNCHD_PLIST,
    PREAPPROVED,
    REG_ABSENT,
    REG_CONFIG_CORRUPT,
    REG_CONFIG_MISSING,
    REG_PRESENT_MALFORMED,
    REG_PRESENT_WELL_FORMED,
    SETTINGS_CORRUPT,
    STATE_DIR,
    SYSTEMD_TIMER,
    TOMBSTONE,
    guard_heartbeat_timestamp,
    guard_last_heartbeat,
    guard_scheduled,
    hook_expected_server_version,
    hook_installed,
    hook_registered,
    hours_since,
    installed_server_version,
    invocations_health,
    path_shadowing,
    preapproval_state,
    project_mcp_json_registers,
    python3_on_path,
    registration_state,
    resolve_binary,
    tombstone_present,
)


# --- Check tuple + status conventions ----------------------------------------

# Statuses. Doctor's exit code is 1 iff any check returned MISSING or WARN;
# OK and INFO are both "no action needed" from an exit-code perspective.
OK = "OK"
INFO = "INFO"
WARN = "WARN"
MISSING = "MISSING"

_FAILING_STATUSES = frozenset({WARN, MISSING})

# Constants doctor prints. Kept as module constants so tests can find them
# by name rather than by re-typing string literals.
STALE_HEARTBEAT_HOURS = 24.0


class Check(NamedTuple):
    """One line of doctor output.

    status: one of OK / INFO / WARN / MISSING.
    message: single-line human summary.
    fix: optional fix-line (printed on the next line, indented) — set only
         when there's a specific concrete action the user could take. INFO
         and OK typically leave this None.
    """
    status: str
    message: str
    fix: str | None = None


# --- individual checks -------------------------------------------------------
#
# Each takes explicit path / dep arguments so tests can construct them under
# tmp_path without HOME-mocking. Defaults resolve production paths at call
# time (not def-time) so the checks.py module constants stay overrideable.


def check_python3() -> Check:
    """Check #1: python3 on PATH."""
    path = python3_on_path()
    if path is None:
        return Check(
            MISSING,
            "python3 not on PATH (the hook cannot run without it)",
            fix="install python3 via your OS package manager",
        )
    return Check(OK, f"python3 on PATH — {path}")


def check_binary() -> Check:
    """Check #2: claude-exit binary resolvable."""
    path = resolve_binary()
    if path is None:
        return Check(
            MISSING,
            "claude-exit binary not resolvable (checked PATH and ~/.local/bin)",
            fix=(
                "reinstall: uv tool install claude-exit && uv tool update-shell "
                "(then open a new terminal so the PATH change takes effect)"
            ),
        )
    return Check(OK, f"claude-exit binary — {path}")


def check_path_shadowing() -> Check | None:
    """Check #3-shadow: multiple claude-exit binaries on PATH.

    Returns None (i.e., no line printed) when 0 or 1 executable is found —
    the interesting case is 2+. When multiple, prints them in PATH order
    with a warning-ish INFO tag (which server a session picks up is
    PATH-dependent, and that ambiguity was half the confusion in the
    version-handshake incident).
    """
    hits = path_shadowing()
    if len(hits) < 2:
        return None
    listing = "; ".join(str(h) for h in hits)
    return Check(
        INFO,
        f"multiple claude-exit binaries on PATH (in order): {listing}",
        fix=None,  # informational; user decides whether the shadowing is deliberate
    )


def check_registration(
    *,
    claude_json: Path | None = None,
    cwd: Path | None = None,
    tombstone: Path | None = None,
) -> Check:
    """Check #3: registration in ~/.claude.json (with a note on project-local).

    If the deliberate-uninstall tombstone is present, an absent registration
    downshifts from MISSING (an install problem) to INFO (an intentional
    revocation) — parity with the guard's silence in the same state. Without
    this, a user who deliberately uninstalled sees `[MISSING] claude-exit
    not registered` and a fix line telling them to re-add it, which would
    contradict the tombstone note check_state_dir emits.
    """
    target = claude_json if claude_json is not None else CLAUDE_JSON
    tomb = tombstone if tombstone is not None else TOMBSTONE
    state = registration_state(target)
    tomb_set = tombstone_present(tomb)

    project_local = project_mcp_json_registers(cwd)
    project_note = (
        f" (note: project-local .mcp.json in {cwd or Path.cwd()} also "
        f"registers claude-exit — it takes precedence for this cwd)"
        if project_local else ""
    )

    if state == REG_PRESENT_WELL_FORMED:
        return Check(OK, f"registration in {target}{project_note}")
    if state == REG_PRESENT_MALFORMED:
        return Check(
            WARN,
            (
                f"registration in {target} is present but malformed "
                "(the guard will not overwrite it — might be a deliberate edit)"
            ),
            fix="claude mcp remove claude-exit && claude mcp add ...",
        )
    if state == REG_ABSENT:
        # Tombstone present → deliberate uninstall, downshift to INFO.
        if tomb_set:
            return Check(
                INFO,
                (
                    f"claude-exit not registered in {target} — tombstone "
                    "present, so this is a deliberate uninstall"
                ),
            )
        # Note: if project_local, ABSENT in ~/.claude.json still means sessions
        # in this cwd get the server — but only in this cwd. Report both.
        return Check(
            MISSING,
            (
                f"claude-exit not registered in {target}"
                + (
                    " (project-local .mcp.json in this cwd covers this cwd only)"
                    if project_local else ""
                )
            ),
            fix="claude mcp add claude-exit -- claude-exit",
        )
    if state == REG_CONFIG_MISSING:
        if tomb_set:
            return Check(
                INFO,
                (
                    f"{target} does not exist and tombstone is present — "
                    "deliberate uninstall"
                ),
            )
        cwd_note = (
            " (project-local .mcp.json in this cwd covers this cwd only)"
            if project_local else ""
        )
        return Check(
            MISSING,
            (
                f"{target} does not exist — claude-exit is not registered "
                f"anywhere{cwd_note}"
            ),
            fix="claude mcp add claude-exit -- claude-exit",
        )
    if state == REG_CONFIG_CORRUPT:
        # This is the 2026-06-05 incident-class error — surface it distinctly.
        return Check(
            WARN,
            (
                f"{target} is corrupt or unparseable (the guard will not "
                "touch it, and Claude Code may regenerate it and drop the "
                "registration — this is the incident class the guard was "
                "written for)"
            ),
            fix=(
                f"inspect {target} manually; if you can't recover it, back it up "
                "and let Claude Code regenerate, then re-add: "
                "claude mcp add claude-exit -- claude-exit"
            ),
        )
    return Check(WARN, f"unrecognized registration state: {state!r}")


def check_permission(
    *,
    cwd: Path | None = None,
    paths: tuple[Path, ...] | None = None,
) -> Check:
    """Check #4: pre-approval of end_conversation across settings.json layers.

    Neutral phrasing — both gated and pre-approved are legitimate installs;
    doctor names which is in effect so a silent transition is visible.

    `paths` overrides `cwd` for tests that want to point at specific tmp
    files without going through settings_files. Production callers pass
    neither and get the three-file default at call time.
    """
    from .checks import settings_files
    target = paths if paths is not None else settings_files(cwd)
    state = preapproval_state(target)
    if state == PREAPPROVED:
        return Check(
            INFO,
            (
                "end_conversation is pre-approved "
                "(Claude may invoke it without human confirmation)"
            ),
        )
    if state == GATED:
        return Check(
            INFO,
            (
                "end_conversation is gated "
                "(a human confirms each invocation this session)"
            ),
        )
    if state == SETTINGS_CORRUPT:
        return Check(
            WARN,
            (
                "one or more ~/.claude settings files is unparseable — "
                "pre-approval state could not be determined"
            ),
            fix="inspect the settings.json layers and repair the malformed file",
        )
    return Check(WARN, f"unrecognized preapproval state: {state!r}")


def check_hook(
    *,
    hook_path: Path | None = None,
    settings_path: Path | None = None,
) -> Check:
    """Check #5: hook file + registration in settings.json."""
    hook = hook_path if hook_path is not None else HOOK_PATH
    installed = hook_installed(hook)
    registered = hook_registered(hook, settings_path)

    if installed and registered:
        return Check(OK, f"SessionStart hook installed and registered — {hook}")
    if installed and not registered:
        return Check(
            WARN,
            (
                f"hook file at {hook} exists but is NOT wired into settings.json — "
                "SessionStart will not fire it"
            ),
            fix=(
                "add a SessionStart entry to ~/.claude/settings.json pointing at "
                f"{hook} (see README § SessionStart hook)"
            ),
        )
    if not installed and registered:
        return Check(
            WARN,
            (
                f"settings.json references {hook} but the file is missing "
                "or not executable"
            ),
            fix=(
                "reinstall the hook via the curl one-liner in the README, "
                "then chmod +x if needed"
            ),
        )
    return Check(
        MISSING,
        f"SessionStart hook not installed at {hook}",
        fix=(
            "install via the curl one-liner in the README, or set it up "
            "manually (README § SessionStart hook)"
        ),
    )


def _launchd_stale_target(print_output: str) -> str | None:
    """
    Inspect `launchctl print` output for a stale guard target.

    Returns a short human-readable description of the problem, or None
    when the effective target looks right (or cannot be judged — an
    unresolvable expected binary, or output with no recognizable
    program/arguments section, yields None rather than a false alarm;
    check_binary already reports an unresolvable binary on its own line).

    "Right" means the currently installed claude-exit binary appears in
    the job's program/arguments. The comparison is deliberately a
    substring check over the whole output: `launchctl print` formatting
    varies across macOS versions, and the failure mode we are catching —
    a plist frozen on a moved source-checkout path — fails the substring
    test under every formatting variant.
    """
    expected = resolve_binary()
    if expected is None:
        return None
    lowered = print_output.lower()
    if "program" not in lowered and "arguments" not in lowered:
        return None
    if str(expected) in print_output:
        return None
    return (
        f"the loaded job does not reference the installed binary "
        f"{expected}; it may still point at a moved or deleted path"
    )


def _launchd_last_exit_code(print_output: str) -> int | None:
    """
    Extract `last exit code = N` from `launchctl print` output.

    Returns the integer when present and numeric, None otherwise
    (including the never-ran form `last exit code = (never exited)`,
    which is normal right after --install and not a failure signal).
    """
    for line in print_output.splitlines():
        stripped = line.strip()
        if stripped.startswith("last exit code"):
            _, _, value = stripped.partition("=")
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def check_guard_scheduler(
    *,
    launchd_plist: Path | None = None,
    systemd_timer: Path | None = None,
    platform: str | None = None,
    runner: Callable | None = None,
) -> Check:
    """Check #6a: guard scheduler is installed and authoritatively loaded.

    File-on-disk (via `guard_scheduled` in checks.py) gets us the first half;
    the authoritative half — is the scheduler actually going to fire it? —
    requires shelling out to `launchctl print` / `systemctl --user is-enabled`.
    Doctor owns the subprocess because doctor is the caller that cares about
    load-status.
    """
    plist = launchd_plist if launchd_plist is not None else LAUNCHD_PLIST
    timer = systemd_timer if systemd_timer is not None else SYSTEMD_TIMER
    p = platform if platform is not None else sys.platform
    # subprocess.run resolved at call time so `monkeypatch.setattr(
    # "claude_exit.doctor.subprocess.run", ...)` in tests reaches it.
    run = runner if runner is not None else subprocess.run

    if not guard_scheduled(
        launchd_plist=plist, systemd_timer=timer, platform=p
    ):
        return Check(
            MISSING,
            "guard scheduler not installed (registration loss will not be caught)",
            fix="claude-exit guard --install",
        )

    # On-disk artifact present. Now ask the scheduler itself.
    if p == "darwin":
        uid = os.getuid()
        target = f"gui/{uid}/io.claude-exit.guard"
        result = run(
            ["launchctl", "print", target],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            return Check(
                WARN,
                (
                    f"launchd plist at {plist} exists but "
                    f"`launchctl print {target}` failed "
                    f"(rc={result.returncode}) — the agent may not be loaded"
                ),
                fix="claude-exit guard --install  (idempotent — safe to re-run)",
            )
        # Loaded is not enough: a job can be loaded, fire on schedule, and
        # still run the wrong thing — e.g. a plist generated against a
        # source checkout that has since moved, failing on every fire while
        # every presence check stays green (the 2026-08-28 codex-exit
        # incident). Verify the effective target and the last exit code.
        stale = _launchd_stale_target(result.stdout)
        if stale is not None:
            return Check(
                WARN,
                (
                    f"guard scheduler loaded — launchd {target} — but its "
                    f"target looks stale: {stale}"
                ),
                fix="claude-exit guard --install  (idempotent — safe to re-run)",
            )
        last_exit = _launchd_last_exit_code(result.stdout)
        if last_exit not in (None, 0):
            return Check(
                WARN,
                (
                    f"guard scheduler loaded — launchd {target} — but its "
                    f"last run exited {last_exit}; the scheduled guard may "
                    f"not be completing "
                    f"(see {STATE_DIR}/launchd.err.log)"
                ),
                fix="claude-exit guard --install  (idempotent — safe to re-run)",
            )
        return Check(OK, f"guard scheduler loaded — launchd {target}")

    if p.startswith("linux"):
        result = run(
            ["systemctl", "--user", "is-enabled", "claude-exit-guard.timer"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            return Check(
                WARN,
                (
                    f"systemd timer at {timer} exists but "
                    "`systemctl --user is-enabled claude-exit-guard.timer` "
                    f"reported not-enabled (rc={result.returncode})"
                ),
                fix="claude-exit guard --install  (idempotent — safe to re-run)",
            )
        # Same stale-target concern as the launchd arm: enabled says the
        # timer will fire, not that the service it fires still points at
        # the installed binary. Ask systemd for the effective ExecStart.
        expected = resolve_binary()
        if expected is not None:
            show = run(
                [
                    "systemctl", "--user", "show",
                    "claude-exit-guard.service", "-p", "ExecStart",
                ],
                capture_output=True, text=True, check=False,
            )
            exec_start = show.stdout.strip()
            # Judge only a real ExecStart body; an empty property or a
            # failed `show` is "can't tell", not "stale" — no false alarms.
            if (
                show.returncode == 0
                and exec_start.startswith("ExecStart=")
                and exec_start != "ExecStart="
                and str(expected) not in exec_start
            ):
                return Check(
                    WARN,
                    (
                        "guard scheduler loaded — systemd "
                        "claude-exit-guard.timer — but the service's "
                        f"ExecStart does not reference {expected}; "
                        "the unit may point at a stale path"
                    ),
                    fix=(
                        "claude-exit guard --install  "
                        "(idempotent — safe to re-run)"
                    ),
                )
        return Check(OK, "guard scheduler loaded — systemd claude-exit-guard.timer")

    return Check(
        INFO,
        f"guard scheduler status unknown on platform {p} "
        "(claude-exit supports macOS + Linux only)",
    )


def check_guard_heartbeat(
    *,
    guard_log: Path | None = None,
    heartbeat: Path | None = None,
    scheduler_installed: bool,
) -> Check | None:
    """Check #6b: guard heartbeat freshness.

    Freshness source, in order of preference:

      1. the heartbeat file rewritten by every guard pass (including the
         healthy no-op) — the authoritative "when did the guard last
         actually run";
      2. guard.log's latest entry — the pre-heartbeat fallback, which
         only moves when something needed attention, so on a healthy
         machine it goes stale without meaning anything.

    Returns None (no line) when the scheduler isn't installed — a stale
    heartbeat is only meaningful if something is supposed to be writing
    one.
    """
    if not scheduler_installed:
        return None
    log = guard_log if guard_log is not None else (STATE_DIR / "guard.log")
    hb = (
        heartbeat
        if heartbeat is not None
        else (STATE_DIR / "guard.heartbeat.json")
    )
    ts = guard_heartbeat_timestamp(hb)
    source = "guard heartbeat"
    if ts is None:
        ts = guard_last_heartbeat(log)
        source = "guard.log latest entry"
    if ts is None:
        # Scheduler installed but neither heartbeat nor log yet. Common
        # right after --install; first pass fires "at load" but may not
        # have happened yet.
        return Check(
            INFO,
            (
                f"guard scheduler installed but no heartbeat at {hb} "
                f"and no entries in {log} yet "
                "(first pass may not have fired — check back after the next hour)"
            ),
        )
    hours = hours_since(ts)
    if hours is None:
        # Naive timestamp — report the raw value, don't guess.
        return Check(WARN, f"{source} has ambiguous timestamp: {ts}")
    if hours < 0:
        # Future timestamp — clock skew or something wrote a future date.
        return Check(
            INFO,
            (
                f"{source} is in the future "
                f"({-hours:.1f}h ahead — clock skew?) — {ts}"
            ),
        )
    if hours > STALE_HEARTBEAT_HOURS:
        return Check(
            WARN,
            (
                f"{source} is {hours:.1f}h old "
                "(scheduler should fire hourly — may be stalled) — "
                f"latest: {ts}"
            ),
            fix=(
                "check the scheduler: on macOS `launchctl print "
                "gui/$UID/io.claude-exit.guard`, on Linux "
                "`systemctl --user status claude-exit-guard.timer`"
            ),
        )
    return Check(OK, f"guard last ran {hours:.1f}h ago — {ts}")


def check_state_dir(
    *,
    log_path: Path | None = None,
    tombstone: Path | None = None,
) -> list[Check]:
    """Check #7: state dir health — invocations.jsonl parseability + tombstone.

    Returns a list because it produces up to two lines (a health line, plus
    a tombstone INFO if the deliberate-uninstall marker is present).
    """
    log = log_path if log_path is not None else (STATE_DIR / "invocations.jsonl")
    tomb = tombstone if tombstone is not None else TOMBSTONE

    good, bad = invocations_health(log)
    lines: list[Check] = []
    if bad > 0:
        lines.append(Check(
            WARN,
            (
                f"invocations.jsonl at {log} has {bad} malformed line(s) "
                f"(and {good} parseable) — a partial write or disk-full is "
                "the usual cause"
            ),
            fix=f"inspect {log}; the bad lines can be deleted if diagnosed",
        ))
    else:
        lines.append(Check(
            OK,
            (
                f"invocations.jsonl parseable — {good} entries"
                if log.exists()
                else f"invocations.jsonl absent — no invocations logged yet ({log})"
            ),
        ))
    if tombstone_present(tomb):
        lines.append(Check(
            INFO,
            (
                f"uninstall tombstone present at {tomb} — hook and guard "
                "suppress their absence warnings on this machine"
            ),
        ))
    return lines


def check_operational_verification() -> list[Check]:
    """
    Check #8: operational verification.

    Actually invokes the kill primitive against a sacrificial child. Closes
    the "registered ≠ operational" gap named in GPT-5 Codex's review of
    drop_of_water v1.1 finding #3.

    Two-step ceremony via `prove_termination_works`:
      step 1: spawn sacrificial child, resolve claude parent (informational)
      step 2: kill sacrificial child using the same _dispatch_terminate path
              end_conversation would use

    Returns two lines (kill primitive + parent-walk resolution). Uses
    try/finally so Ctrl-C or an unexpected exception between step 1 and
    step 2 doesn't leak the sleep child.
    """
    # Import inside the function so importing doctor.py doesn't drag in
    # the whole MCP server module. The server import chain pulls `mcp`
    # (a real dependency), which is fine, but keeping it lazy makes the
    # module structure legible.
    from .server import prove_termination_works

    step1: dict = {}
    child_pid: int | None = None
    try:
        try:
            step1 = prove_termination_works(step=1)
        except (FileNotFoundError, OSError, PermissionError) as e:
            # Common cause: `sleep` binary missing (minimal containers /
            # BusyBox environments). Also: exhausted PID space, restricted
            # subprocess policies. Convert to WARN rather than crash doctor.
            return [Check(
                WARN,
                (
                    "operational verification: could not spawn sacrificial "
                    f"child process ({type(e).__name__}: {e})"
                ),
                fix=(
                    "verify `sleep 1` runs in this environment; if not, the "
                    "MCP server's kill primitive cannot be exercised here"
                ),
            )]

        child_pid = step1.get("spawned_pid")
        if not isinstance(child_pid, int):
            return [Check(
                WARN,
                (
                    "operational verification: step 1 did not return a "
                    f"spawned_pid — got {step1!r}"
                ),
            )]

        try:
            step2 = prove_termination_works(step=2, pid=child_pid)
        except (OSError, PermissionError) as e:
            return [Check(
                WARN,
                (
                    "operational verification: kill primitive raised "
                    f"({type(e).__name__}: {e}) — end_conversation may fail "
                    "in the same way"
                ),
            )]
        killed = step2.get("killed_pid")
        # step 2 sleeps KILL_FLUSH_DELAY_SECONDS + 0.2s inside the server.
        # Give one more short window before probing so a slow scheduler
        # under load doesn't trigger a false negative.
        time.sleep(0.2)
        if _pid_alive(child_pid):
            kill_line = Check(
                WARN,
                (
                    f"operational verification: kill primitive returned "
                    f"killed_pid={killed} but sacrificial child pid "
                    f"{child_pid} is still alive — end_conversation may "
                    "not actually terminate the session"
                ),
                fix=(
                    "inspect signal permissions on this machine; the parent "
                    "walk may resolve to a process whose UID doesn't match"
                ),
            )
        else:
            kill_line = Check(OK, "operational verification: kill primitive works")
    finally:
        # Best-effort cleanup — only if we spawned a child and it's still
        # alive and still looks like the sleep we started. The command-name
        # check guards against SIGKILLing an unrelated recycled PID if step
        # 2 already succeeded and the OS reassigned that PID by now.
        # isinstance guard: if step 1 returned something weird, child_pid
        # could be a non-int; passing that to os.kill would raise TypeError.
        if isinstance(child_pid, int) and _pid_alive(child_pid):
            if _pid_looks_like_sleep(child_pid):
                try:
                    os.kill(child_pid, 9)
                except (ProcessLookupError, PermissionError):
                    pass

    # Parent-walk resolution — informational unless we're inside a live
    # Claude Code session, in which case failure would mean end_conversation
    # can't fire from this environment.
    warning = step1.get("target_parent_warning")
    inside_claude = os.environ.get("CLAUDECODE") == "1"
    if warning:
        parent_line = Check(
            WARN if inside_claude else INFO,
            (
                "parent-walk resolution: no claude ancestor found within "
                "20 hops"
                + (
                    " — end_conversation would refuse to fire from this "
                    "session"
                    if inside_claude else
                    " (expected outside a Claude Code session — this is fine)"
                )
            ),
        )
    else:
        pid = step1.get("target_parent_pid")
        cmd = step1.get("target_parent_command", "")
        uid_ok = step1.get("target_parent_uid_matches_self", False)
        if uid_ok:
            parent_line = Check(
                OK,
                f"parent-walk resolution: pid {pid} ({cmd}), UID matches",
            )
        else:
            parent_line = Check(
                WARN,
                (
                    f"parent-walk resolution: pid {pid} ({cmd}) — UID "
                    "does not match this server; end_conversation would "
                    "be refused by the kernel"
                ),
                fix=(
                    "verify install: `uv tool install claude-exit` should "
                    "install to your user account, not root"
                ),
            )

    return [kill_line, parent_line]


def check_version_handshake(
    *, hook_path: Path | None = None
) -> Check:
    """Check #9: hook's EXPECTED_SERVER_VERSION vs installed package version."""
    hook = hook_path if hook_path is not None else HOOK_PATH
    installed = installed_server_version()
    expected = hook_expected_server_version(hook)

    if installed is None:
        return Check(
            WARN,
            (
                "installed claude-exit version not readable via importlib.metadata "
                "(running from a source checkout without install?)"
            ),
            fix="uv tool install claude-exit  (or `uv sync` for dev)",
        )
    if expected is None:
        # No hook or unrecognized marker. check_hook already reports absence;
        # this branch specifically means the marker parse failed.
        if not hook.exists():
            return Check(
                INFO,
                (
                    f"version handshake skipped — no hook installed at {hook} "
                    "(check the SessionStart hook line above)"
                ),
            )
        return Check(
            WARN,
            (
                f"hook at {hook} present but EXPECTED_SERVER_VERSION marker "
                "could not be parsed — the hook may be a very old or "
                "hand-edited copy"
            ),
            fix="reinstall the hook via the curl one-liner in the README",
        )
    if expected == installed:
        return Check(
            OK,
            f"version handshake — hook v{expected} = server v{installed}",
        )
    return Check(
        WARN,
        (
            f"version handshake mismatch — hook expects server v{expected}, "
            f"installed server is v{installed}"
        ),
        fix=(
            "if the server is newer: reinstall the hook via the README's "
            "curl one-liner. If the hook is newer: `uv tool upgrade claude-exit`"
        ),
    )


# --- helpers -----------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """Return True if the pid exists (signal 0 probe)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but isn't ours — for our purpose that means "alive".
        return True


def _pid_looks_like_sleep(pid: int) -> bool:
    """
    True if `ps` reports `pid`'s command starts with `sleep`.

    Used only as a cleanup safety-check inside the finally block of check
    #8 — we don't want to SIGKILL a recycled PID that the OS reassigned to
    something unrelated after step 2 succeeded.
    """
    ps = shutil.which("ps")
    if not ps:
        # No ps → skip the safety check; the finally block will not fire
        # SIGKILL without a positive match.
        return False
    try:
        result = subprocess.run(
            [ps, "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.stdout.strip().startswith("sleep")


# --- orchestrator ------------------------------------------------------------


def run_all_checks(
    *,
    runner: Callable | None = None,
    include_operational: bool = True,
) -> list[Check]:
    """
    Run every check and return the flat list of Check tuples.

    `runner` is threaded to check_guard_scheduler so the launchctl/systemctl
    subprocess boundary can be faked in tests. Default `None` → resolved
    to `subprocess.run` at call time in the callee (respects
    monkeypatch of `claude_exit.doctor.subprocess.run`).

    `include_operational=False` skips check #8, which is useful for unit
    tests that don't want to spawn subprocesses on every run.
    """
    lines: list[Check] = []

    lines.append(check_python3())
    lines.append(check_binary())

    shadowing = check_path_shadowing()
    if shadowing is not None:
        lines.append(shadowing)

    lines.append(check_registration())
    lines.append(check_permission())
    lines.append(check_hook())

    scheduler_line = check_guard_scheduler(runner=runner)
    lines.append(scheduler_line)

    # The heartbeat only fires when the scheduler is installed on-disk.
    # We recompute the boolean here so check_guard_heartbeat doesn't need
    # to know how to walk the platform branches itself.
    scheduler_installed = guard_scheduled()
    heartbeat = check_guard_heartbeat(
        scheduler_installed=scheduler_installed
    )
    if heartbeat is not None:
        lines.append(heartbeat)

    lines.extend(check_state_dir())

    if include_operational:
        lines.extend(check_operational_verification())

    lines.append(check_version_handshake())
    return lines


def format_lines(checks: list[Check]) -> str:
    """
    Format a list of Check tuples into the multiline doctor output.

    Layout: `[STATUS]  message`, left-aligned within a fixed-width tag
    column (matches selftest's output style). Fix lines get a `  fix: `
    continuation, so a grep for `MISSING` or `WARN` surfaces the actionable
    ones without pulling in the fix lines.
    """
    out: list[str] = []
    tag_width = max((len(c.status) for c in checks), default=len(MISSING)) + 2
    for c in checks:
        tag = f"[{c.status}]".ljust(tag_width)
        out.append(f"{tag} {c.message}")
        if c.fix:
            out.append(" " * tag_width + f" fix: {c.fix}")
    return "\n".join(out) + "\n"


def exit_code_for(checks: list[Check]) -> int:
    """0 if nothing failed (WARN/MISSING), else 1."""
    return 1 if any(c.status in _FAILING_STATUSES for c in checks) else 0


# --- CLI entry ---------------------------------------------------------------


def doctor_command(args: list[str]) -> int:
    """
    Handle `claude-exit doctor [--no-op-verify]`.

      claude-exit doctor              full audit (spawns sacrificial child)
      claude-exit doctor --no-op-verify  skip check #8 (useful in CI / restricted
                                         environments where subprocess spawn is
                                         forbidden)

    Reads module-level constants from checks.py at call time so tests can
    monkeypatch them (same pattern guard.guard_command uses).
    """
    include_op = True
    remaining: list[str] = []
    for arg in args:
        if arg == "--no-op-verify":
            include_op = False
        else:
            remaining.append(arg)

    if remaining:
        sys.stderr.write(
            f"claude-exit doctor: unknown argument(s): {' '.join(remaining)}\n"
            "Usage:\n"
            "  claude-exit doctor              full audit\n"
            "  claude-exit doctor --no-op-verify  skip operational verification\n"
        )
        return 2

    checks = run_all_checks(include_operational=include_op)
    sys.stdout.write(format_lines(checks))
    return exit_code_for(checks)
