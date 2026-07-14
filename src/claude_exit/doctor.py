"""
`claude-exit doctor` — one-shot health check of the consent architecture.

Nine checks in dependency order, each returning `(status, message, fix|None)`.
Doctor owns formatting; checks own facts (in checks.py). One line per check,
plus a `fix:` continuation line when actionable. Exit 0 if nothing MISSING/WARN,
else 1.

Design principles inherited from consent-persistence-overview.md:

  - Never writes to the filesystem. Even the sacrificial-child ceremony spawns
    and reaps its child in-process; no state-dir mutations.
  - No network. Version-currency lives elsewhere; doctor's job is the wiring.
  - Neutral phrasing for pre-approved vs. gated permission state — both are
    legitimate installs per README, INFO not WARN.
  - Restoration-shaped fixes (`claude-exit guard`) named where they apply;
    intent-owned fixes (settings.json edits) named as human actions, never
    as an auto-fix.

Check #8 (operational verification) closes GPT-5 Codex's review finding on
drop_of_water v1.1: registered ≠ operational. `guard` checks whether an
`mcpServers` key exists; doctor exercises the kill primitive against a
sacrificial child to confirm the server actually works. Called in-process
via server.prove_termination_works — since we're already running the
binary, "binary crashes on startup" would have surfaced before this point.

Check #9 (version handshake) is the affirmative side of the hook's
issue-#17 handshake. The hook stays quiet on match to avoid noise every
session; doctor is where the positive confirmation lives.
"""

import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import checks
from .checks import (
    REG_ABSENT,
    REG_CONFIG_CORRUPT,
    REG_CONFIG_MISSING,
    REG_PRESENT_MALFORMED,
    REG_PRESENT_WELL_FORMED,
    SETTINGS_ABSENT,
    SETTINGS_CORRUPT,
    SETTINGS_PRESENT,
    all_binaries_on_path,
    guard_last_heartbeat,
    guard_scheduled,
    guard_scheduler_loaded,
    hook_registered_in_settings,
    invocations_bad_lines,
    preapproval_file,
    registration_state,
    resolve_binary,
    settings_state,
    tombstone_present,
)

# Path-owning constants live in `checks.py` and are read via the `checks`
# module (never `from .checks import X`) so tests can monkeypatch them at
# call time. `from X import Y` binds Y at import — impossible to redirect
# once doctor is loaded. See tests/test_doctor.py::isolated_home for how
# the redirection works.


# --- result shape ------------------------------------------------------------

# CheckResult = tuple[status, message, fix_line | None]
# Status: OK | INFO | WARN | MISSING. OK/INFO don't affect exit code;
# WARN/MISSING both set exit 1. INFO exists for neutral state reports
# (permission state, tombstone) — legibility, not alarm.
CheckResult = tuple[str, str, str | None]

STATUS_OK = "OK"
STATUS_INFO = "INFO"
STATUS_WARN = "WARN"
STATUS_MISSING = "MISSING"

# Statuses that cause `claude-exit doctor` to exit non-zero. INFO and OK are
# both zero-exit — INFO is for "here's a legitimate state you may want to see"
# (pre-approved vs. gated), not for "something's wrong."
_NONZERO_STATUSES = frozenset({STATUS_WARN, STATUS_MISSING})

# Stale-heartbeat threshold: guard runs hourly per README, so anything past 24h
# means the scheduler is either not firing the unit or the unit is failing
# silently — both distinguishable from "just hasn't ticked yet" by an
# uninteresting margin.
_HEARTBEAT_STALE_HOURS = 24.0


# --- individual checks -------------------------------------------------------


def check_python3() -> CheckResult:
    """
    Doctor's precondition: python3 must be resolvable for the guard and hook
    to work. Since claude-exit itself is installed via `uv` (which pins
    python), a missing python3 here means the install shape is broken.
    """
    path = shutil.which("python3")
    if path:
        return (STATUS_OK, f"python3 on PATH ({path})", None)
    return (
        STATUS_MISSING,
        "python3 not on PATH",
        "install python3 — it's a transitive dependency of claude-exit's "
        "install shape (uv tool), so this shouldn't normally happen. If uv "
        "is broken, reinstall claude-exit: `uv tool install claude-exit`.",
    )


def check_binary() -> CheckResult:
    """
    `claude-exit` binary resolvable via PATH or the `~/.local/bin` fallback.
    Names the `uv tool update-shell` PATH gotcha from the README's Installation
    section rather than jumping straight to "reinstall" — that was the actual
    root cause on the two support requests filed against #17.
    """
    path = resolve_binary()
    if path:
        return (STATUS_OK, f"claude-exit binary: {path}", None)
    return (
        STATUS_MISSING,
        "claude-exit binary not resolvable",
        "if claude-exit was installed via `uv tool install`, run "
        "`uv tool update-shell` and reopen the shell (~/.local/bin may not "
        "be on PATH yet). Otherwise reinstall: `uv tool install claude-exit`.",
    )


def check_path_shadowing() -> CheckResult:
    """
    Report PATH shadowing when multiple claude-exit executables are visible.

    A repo `.venv/bin/claude-exit` in front of `~/.local/bin/claude-exit` was
    half the confusion in the #17 incident — which server a session gets is
    terminal-PATH-dependent, and the shell running Claude Code doesn't
    necessarily match the shell you ran `uv tool upgrade` from. Doctor names
    the ambiguity; the user decides which one they meant.

    OK when 0 or 1 executable found. INFO (not WARN) when ≥2, listing them in
    PATH order — this is legitimate state (people do install in multiple
    places), not misconfiguration.
    """
    hits = all_binaries_on_path("claude-exit")
    if len(hits) <= 1:
        return (STATUS_OK, "no PATH shadowing of claude-exit", None)
    listing = ", ".join(str(h) for h in hits)
    return (
        STATUS_INFO,
        f"claude-exit found in multiple PATH entries: {listing}",
        "which one a session gets depends on the shell's PATH order. If a "
        "recent `uv tool upgrade` doesn't seem to have taken effect, this "
        "is a common cause — remove or reorder the shadowing entry.",
    )


def check_registration(claude_json: Path | None = None) -> CheckResult:
    """
    `claude-exit` registration in `~/.claude.json`. Five branches match the
    registration_state classifier one-to-one; the CONFIG_CORRUPT branch names
    the drop_of_water incident class so a repeat becomes recognizable.
    """
    if claude_json is None:
        claude_json = checks.CLAUDE_JSON
    state = registration_state(claude_json)
    if state == REG_PRESENT_WELL_FORMED:
        return (STATUS_OK, f"registration present in {claude_json}", None)
    if state == REG_PRESENT_MALFORMED:
        return (
            STATUS_WARN,
            f"registration key in {claude_json} has a mangled value "
            "(present but missing a usable `command`)",
            "if this is a deliberate edit, ignore. Otherwise remove the "
            "entry entirely and the next guard pass will restore it "
            "cleanly.",
        )
    if state == REG_ABSENT:
        return (
            STATUS_MISSING,
            f"registration absent from {claude_json}",
            "run `claude mcp add claude-exit -- claude-exit`, or wait up to "
            "an hour for the guard to restore it. If deliberate, drop a "
            f"tombstone: `touch {checks.TOMBSTONE}`.",
        )
    if state == REG_CONFIG_MISSING:
        return (
            STATUS_MISSING,
            f"{claude_json} does not exist",
            "unusual — Claude Code normally creates this file. Run "
            "`claude-exit guard` to create it with only the claude-exit "
            "entry, or install/relaunch Claude Code so it seeds the file.",
        )
    if state == REG_CONFIG_CORRUPT:
        return (
            STATUS_WARN,
            f"{claude_json} is unparseable or has the wrong shape",
            "risk of the drop_of_water class incident (2026-06-05): Claude "
            "Code may set the file aside and regenerate without the "
            "claude-exit entry. Inspect it; if it was hand-edited, fix the "
            "shape; if it's a mess, back it up and let Claude Code "
            "regenerate. The guard deliberately does not touch a corrupt "
            "file.",
        )
    return (STATUS_WARN, f"unknown registration_state: {state}", None)


def check_permission(settings_files=None) -> CheckResult:
    """
    Permission state — neutral report, INFO not WARN. Both pre-approved
    (exit is Claude's to take) and gated (a human confirms each session)
    are legitimate installs per README's permission-prompt section.

    Doctor reports which is in effect so a silent flip is visible; it does
    not editorialize on which is "right". Names the file where the
    pre-approval was found so users with layered settings can see which
    layer owns the state.
    """
    if settings_files is None:
        settings_files = (
            checks.USER_SETTINGS,
            checks.PROJECT_SETTINGS,
            checks.PROJECT_LOCAL_SETTINGS,
        )
    # If any settings file is present but corrupt, WARN rather than
    # silently treating it as "no pre-approval". A corrupt file that
    # once contained a pre-approval reads exactly like an absent one
    # to `preapproval_file`, so the neutral "gated" report would be
    # actively wrong — pointing the user at "configure it" when the
    # real fix is "unbreak the JSON".
    for path in settings_files:
        if settings_state(path) == SETTINGS_CORRUPT:
            return (
                STATUS_WARN,
                f"settings file {path} exists but is unparseable — "
                "permission state cannot be determined",
                "fix the JSON syntax in that file. Until then, doctor "
                "cannot tell whether end_conversation is pre-approved.",
            )
    where = preapproval_file(settings_files)
    if where is None:
        return (
            STATUS_INFO,
            "permission: gated (a human confirms each session)",
            None,
        )
    return (
        STATUS_INFO,
        f"permission: pre-approved via {where} "
        "(the exit is Claude's to take)",
        None,
    )


def check_hook(
    hook_path: Path | None = None,
    settings_file: Path | None = None,
) -> CheckResult:
    """
    SessionStart hook: file present AND executable AND registered.

    Four distinct failure modes named separately:
      - Settings file corrupt → registration status is uncomputable
        (WARN with a distinct fix that points at fixing the JSON, not
        at re-adding the entry).
      - File present, unregistered → settings.json wasn't updated.
      - Registered, file missing → the file was deleted / never copied.
      - File present but not executable → Claude Code cannot `execve`
        it. The permission bit is stripped by some file managers on
        copy, and by common dotfiles-bootstrap flows that write via
        `install -m 644`; without X_OK the hook silently no-ops, which
        is exactly the "silent failure" mode this check exists to
        prevent. Parallels resolve_binary's os.access(fallback, X_OK)
        gate — same posture for the same reason.

    Corrupt settings.json takes priority over the entry-shape branches
    because "add the entry" is a misleading fix when the real problem is
    the JSON won't parse at all — that misdirection is the review
    finding that motivated adding this branch.
    """
    if hook_path is None:
        hook_path = checks.INSTALLED_HOOK
    if settings_file is None:
        settings_file = checks.USER_SETTINGS

    if settings_state(settings_file) == SETTINGS_CORRUPT:
        return (
            STATUS_WARN,
            f"settings file {settings_file} exists but is unparseable — "
            "hook registration cannot be determined",
            "fix the JSON syntax in that file. Until then, doctor "
            "cannot tell whether the SessionStart hook is registered.",
        )

    file_present = hook_path.exists()
    file_executable = file_present and os.access(hook_path, os.X_OK)
    registered = hook_registered_in_settings(hook_path, settings_file)
    if file_executable and registered:
        return (STATUS_OK, f"hook installed and registered at {hook_path}", None)
    if file_present and not file_executable:
        # Registered vs. not — either way the immediate blocker is the
        # missing +x bit. Naming registration state secondary keeps the
        # fix line focused.
        reg_note = "" if registered else " (also: not registered in settings)"
        return (
            STATUS_WARN,
            f"hook file at {hook_path} is present but not executable{reg_note}",
            f"`chmod +x {hook_path}`. Without the execute bit, Claude "
            "Code's SessionStart handler cannot run the hook — the "
            "ceremony would silently fail to fire.",
        )
    if file_present and not registered:
        return (
            STATUS_WARN,
            f"hook file at {hook_path} but not registered in {settings_file}",
            "add the SessionStart entry from the README to settings.json. "
            "If your settings.json is managed by another tool (dotfiles, IDE "
            "config bundler), add the entry inside that tool's flow — "
            "hand-edits will be silently clobbered.",
        )
    if registered and not file_present:
        return (
            STATUS_WARN,
            f"hook registered in {settings_file} but file at {hook_path} is missing",
            "refetch the hook: `curl -fsSL "
            "https://raw.githubusercontent.com/danparshall/claude-exit/main/"
            f"hooks/session-start.sh -o {hook_path} && chmod +x {hook_path}`.",
        )
    return (
        STATUS_MISSING,
        "SessionStart hook not installed",
        "see the README's `Auto-running the ceremony at session start` "
        "section. Without the hook, the verification ceremony runs only when "
        "Claude thinks to invoke it — which is exactly the motivated-reasoning "
        "scenario baseline verification exists to prevent.",
    )


def check_guard_scheduled(runner=subprocess.run) -> CheckResult:
    """
    Guard scheduling: (a) scheduler artifact on disk (plist/timer), AND
    (b) authoritative check via launchctl/systemctl that the unit is loaded.

    The two-step check catches "file installed but never bootstrapped" —
    which is the exact diagnostic gap `guard_scheduled` (file-only) cannot
    see and which motivated introducing `guard_scheduler_loaded`.
    """
    if not guard_scheduled(
        launchd_plist=checks.LAUNCHD_PLIST,
        systemd_timer=checks.SYSTEMD_TIMER,
    ):
        return (
            STATUS_MISSING,
            "guard scheduler not installed",
            "install it: `claude-exit guard --install`. This bounds silent "
            "registration loss (the 2026-06-05 incident class) at one hour.",
        )
    loaded = guard_scheduler_loaded(runner=runner)
    if loaded is True:
        return (STATUS_OK, "guard scheduler installed and loaded", None)
    if loaded is False:
        return (
            STATUS_WARN,
            "guard scheduler file present but the OS scheduler has not loaded it",
            "re-run `claude-exit guard --install` to bootstrap the unit. On "
            "macOS: `launchctl bootstrap gui/$UID <plist>` after a fresh "
            "install / re-login. On Linux: `systemctl --user enable --now "
            "claude-exit-guard.timer`.",
        )
    # loaded is None: unsupported platform or subprocess raised. Fall back
    # to reporting the file-present state and naming the inconclusive check.
    return (
        STATUS_INFO,
        "guard scheduler file present; authoritative load-check "
        "inconclusive on this platform",
        None,
    )


def check_guard_heartbeat(
    guard_log: Path | None = None,
    scheduler_installed: bool = True,
) -> CheckResult:
    """
    Guard heartbeat: last guard.log timestamp.

    Staleness only WARNs when the scheduler is currently installed —
    otherwise a stale log just means "we removed the scheduler at some
    point and the log records history", which `check_guard_scheduled` has
    already reported as MISSING. Doubling the alarm would be noise.

    When the scheduler IS installed and the heartbeat is > 24h stale, that
    is the diagnostic failure mode where the OS scheduler thinks it's
    running the unit but the unit isn't actually firing (e.g., a plist
    that fails to bootstrap silently, a systemd unit that ExecStart-fails
    every tick).
    """
    if guard_log is None:
        guard_log = checks.GUARD_LOG
    last = guard_last_heartbeat(guard_log)
    if last is None:
        return (
            STATUS_INFO,
            "guard heartbeat: no guard.log entries yet",
            None,
        )
    delta_hours = _hours_since(last)
    if delta_hours is None:
        return (STATUS_INFO, f"guard heartbeat: last entry {last}", None)
    if delta_hours < 0:
        # Future timestamp — NTP jitter, container clock drift, hand-edit.
        # Report as INFO with the raw timestamp so the human sees the
        # cause; do not fall through to the OK/WARN branches (which would
        # render "guard heartbeat: last ran in the future (clock skew?)"
        # under an [OK] tag, a contradictory line that reads as broken).
        return (
            STATUS_INFO,
            f"guard heartbeat: last entry {last} is in the future "
            f"(clock skew — NTP drift, container clock, or hand-edit)",
            None,
        )
    when = _humanize_hours_ago(delta_hours)
    stale = delta_hours > _HEARTBEAT_STALE_HOURS
    if stale and scheduler_installed:
        return (
            STATUS_WARN,
            f"guard heartbeat stale: last entry {when} ({last})",
            "the scheduler artifact is present but the guard isn't firing. "
            "On macOS: `launchctl list io.claude-exit.guard` to inspect. "
            "On Linux: `journalctl --user -u claude-exit-guard.service`. "
            "Rerun `claude-exit guard --install` to rebootstrap.",
        )
    if stale:
        # Scheduler uninstalled — historical log, not a live problem.
        return (
            STATUS_INFO,
            f"guard heartbeat: last entry {when} (no scheduler active)",
            None,
        )
    return (STATUS_OK, f"guard heartbeat: last ran {when}", None)


def check_state_dir(
    invocations_log: Path | None = None,
    tombstone: Path | None = None,
) -> CheckResult:
    """
    State dir health: invocations.jsonl parseable (bad lines counted),
    tombstone presence noted.

    Tombstone (`~/.claude-exit/uninstalled`) reports as INFO — it's a
    legitimate state ("uninstalled") that other checks defer to. WARN
    on bad-line count > 0 — a JSONL log with garbage in it undermines
    the audit-trail property, and its presence hints at concurrent-write
    breakage.
    """
    if invocations_log is None:
        invocations_log = checks.INVOCATIONS_LOG
    if tombstone is None:
        tombstone = checks.TOMBSTONE
    good, bad = invocations_bad_lines(invocations_log)
    tomb = tombstone_present(tombstone)
    noun = "invocation" if good == 1 else "invocations"
    parts: list[str] = [f"{good} {noun} logged"]
    if bad:
        parts.append(f"{bad} malformed")
    if tomb:
        parts.append("tombstone present (deliberate uninstall marker)")
    summary = "state dir: " + ", ".join(parts)
    if bad > 0:
        return (
            STATUS_WARN,
            summary,
            f"inspect {invocations_log} for the malformed lines. This can "
            "hint at concurrent-writer breakage — the log is append-only and "
            "shouldn't produce partial writes under normal use.",
        )
    if tomb:
        # Tombstone-present is INFO, not OK — it's a legitimate but
        # unusual state that suppresses other checks' surfacing, so
        # flagging it separately makes those suppressions legible.
        return (STATUS_INFO, summary, None)
    return (STATUS_OK, summary, None)


def check_operational_verification() -> list[CheckResult]:
    """
    Operational verification — the check that closes the "registered ≠
    operational" gap from GPT-5 Codex's review of drop_of_water v1.1.

    Emits TWO check lines: (a) the kill-primitive exercise and (b) the
    parent-walk resolution. Both come from a single
    `prove_termination_works(1)` call so we don't leak a second sleep
    child. Returned as a `list[CheckResult]` and flattened by
    `run_all_checks`.

    Distinct failure modes named separately:
      - step 1 raised → server module broken (import/spawn path).
      - spawned pid isn't alive → primitive itself is broken (rare).
      - step 2 raised → dispatch broken (signal / threading / subprocess).
      - pid still alive after grace → kernel refused signal (UID mismatch,
        capability drop, or the SIGKILL backstop also failed).
      - parent-walk resolved but with UID mismatch → end_conversation
        would return an error rather than kill anything.
      - parent-walk did not find a Claude Code ancestor → INFO when
        doctor is invoked from a standalone shell (expected), WARN when
        invoked from within a Claude Code session (heuristic: CLAUDECODE
        env var), because that means end_conversation would refuse to fire.

    We invoke the server functions directly in this process — not via a
    subprocess speaking MCP — because doctor already IS the installed
    server binary (subcommand dispatch). "Binary crashes on startup" would
    have surfaced before this check runs.

    Read-only from the log's perspective in the healthy path:
    `prove_termination_works` does not itself call `_log`. There is one
    documented exception — `_arm_sigkill_backstop` writes a
    `sigkill_backstop_arm_failed` entry to invocations.jsonl if
    `subprocess.Popen` for the backstop raises OSError (RLIMIT_NPROC / FD
    exhaustion / seccomp restrictions). This only fires on systems where
    doctor is diagnosing a broken environment anyway; the alternative
    (silently dropping the failure) would be worse. Confirmed in server.py
    at implementation time.

    Cleanup discipline: a KeyboardInterrupt or an unhandled BaseException
    mid-check would otherwise leak the `sleep 120` sacrificial child for
    up to two minutes. The whole body is inside try/finally that
    _terminate_and_reap's any live sacrificial pid on exit — even the
    Ctrl-C path.
    """
    try:
        from . import server
    except ImportError as e:
        result = (
            STATUS_WARN,
            f"could not import claude_exit.server for kill-primitive check: {e}",
            "reinstall claude-exit: `uv tool install --force claude-exit`.",
        )
        return [result]

    spawned_pid: int | None = None
    sacrificial_command: str | None = None
    kill_result: CheckResult
    try:
        try:
            step1 = server.prove_termination_works(1)
        except Exception as e:  # noqa: BLE001 — surface the class, not just the message
            return [
                (
                    STATUS_WARN,
                    f"prove_termination_works step=1 raised {type(e).__name__}: {e}",
                    "the server module is broken. Reinstall claude-exit and check "
                    "that `claude-exit --version` runs.",
                )
            ]
        candidate_pid = step1.get("spawned_pid")
        if not isinstance(candidate_pid, int) or isinstance(candidate_pid, bool):
            return [
                (
                    STATUS_WARN,
                    f"prove_termination_works step=1 returned no spawned_pid: {step1!r}",
                    "the server's kill primitive is broken. Reinstall claude-exit.",
                )
            ]
        spawned_pid = candidate_pid
        if not _pid_alive(spawned_pid):
            return [
                (
                    STATUS_WARN,
                    f"sacrificial child pid {spawned_pid} was not alive after "
                    "step 1 spawned it",
                    "the primitive is failing to spawn — likely a subprocess-launch "
                    "restriction (sandboxing, RLIMIT_NPROC). "
                    f"Check `ulimit -u` and any process-launch restrictions.",
                )
            ]
        # Snapshot the sacrificial-child command line NOW so cleanup can
        # verify identity before SIGKILL — defends against PID reuse (see
        # server.py's SIGKILL_BACKSTOP_SCRIPT for the same defense).
        sacrificial_command = _full_command_of(spawned_pid)
        try:
            server.prove_termination_works(2, spawned_pid)
        except Exception as e:  # noqa: BLE001
            _terminate_and_reap_if_matches(spawned_pid, sacrificial_command)
            return [
                (
                    STATUS_WARN,
                    f"prove_termination_works step=2 raised {type(e).__name__}: {e}",
                    "signal dispatch is broken. Check whether SIGTERM delivery is "
                    "restricted (containerization, sandboxing, capability drop).",
                )
            ]
        # Give the backstop room to fire in case the primary SIGTERM was
        # swallowed. Grace = KILL_FLUSH_DELAY + SIGKILL_BACKSTOP_GRACE
        # from server.py, plus a small margin.
        time.sleep(
            server.KILL_FLUSH_DELAY_SECONDS
            + server.SIGKILL_BACKSTOP_GRACE_SECONDS
            + 0.3
        )
        _reap(spawned_pid)
        if _pid_alive_and_matches(spawned_pid, sacrificial_command):
            # Broken primitive: the sacrificial child still shows the same
            # `sleep 120` command line, so this really is our child, still
            # alive. Try direct SIGKILL for cleanup, still WARN.
            _terminate_and_reap_if_matches(spawned_pid, sacrificial_command)
            kill_result = (
                STATUS_WARN,
                f"sacrificial child pid {spawned_pid} is still alive after "
                "kill + backstop grace",
                "the kernel is refusing signal delivery. Most likely cause: "
                "UID mismatch or the SIGKILL backstop failed to arm (spawn "
                "restriction). Try running doctor as the same UID that would "
                "run Claude Code.",
            )
        else:
            # Mark spawned_pid None so the finally clause skips its own
            # kill — the healthy path already reaped it, and blindly
            # SIGKILLing a recycled PID would hit the wrong process.
            spawned_pid = None
            kill_result = (
                STATUS_OK,
                f"kill primitive: spawned and killed sacrificial child pid "
                f"{candidate_pid} end-to-end",
                None,
            )
    finally:
        # KeyboardInterrupt / SystemExit / any BaseException that escaped
        # the inner try lands here. If we still have a live sacrificial
        # child, kill it — but only if the PID still shows the expected
        # command line (PID-reuse defense).
        if spawned_pid is not None:
            _terminate_and_reap_if_matches(spawned_pid, sacrificial_command)

    # Second line: parent-walk resolution. Same step1 data — no extra spawn.
    parent_result = _parent_walk_result(step1)
    return [kill_result, parent_result]


def _parent_walk_result(step1: dict) -> CheckResult:
    """
    Report the target-parent resolution embedded in step 1's response.

    Doctor's own parent chain is not the definitive answer — when
    `claude-exit doctor` is invoked from a standalone shell, the parent
    walk will always fail because doctor's ancestors are `bash → term →
    ...`, not `bash → claude`. That's an expected INFO, not a WARN.

    The scenario worth WARNing about is: user runs doctor from inside a
    Claude Code session (CLAUDECODE=1 is set — a widely observed env var
    Claude Code sets on its MCP-server subprocesses) AND the walk still
    fails. That means the install shape wraps the server in a way
    `_find_claude_code_parent` doesn't recognize, so end_conversation
    would refuse to fire from that context.

    UID mismatch is always a WARN — end_conversation would return an
    error rather than kill anything, regardless of context.
    """
    warning = step1.get("target_parent_warning")
    parent_pid = step1.get("target_parent_pid")
    uid_matches = step1.get("target_parent_uid_matches_self", True)
    command = step1.get("target_parent_command", "")
    under_claude_code = os.environ.get("CLAUDECODE") == "1"

    if parent_pid is None:
        if under_claude_code:
            return (
                STATUS_WARN,
                "parent-walk from doctor did not find a Claude Code "
                "ancestor even though CLAUDECODE=1 — end_conversation "
                "would refuse to fire from this context",
                "the install method may wrap the server in a way "
                "`_find_claude_code_parent` doesn't recognize (looks "
                "for `claude` or `claude-code` basenames within 20 "
                "hops). Check the install shape.",
            )
        # Standalone doctor run — INFO, not WARN. The parent walk is
        # legitimately not applicable here.
        return (
            STATUS_INFO,
            "parent-walk: no Claude Code ancestor from doctor's own "
            "process context (expected when doctor is run from a "
            "standalone shell rather than as an MCP tool)",
            None,
        )
    if not uid_matches:
        return (
            STATUS_WARN,
            f"parent-walk resolved to `{command}` (pid {parent_pid}) but "
            "UID does not match doctor's UID — the kernel would refuse "
            "signal delivery",
            "run doctor as the same UID that owns the Claude Code "
            "session, or check whether a UID drop happened between the "
            "session start and now.",
        )
    return (
        STATUS_OK,
        f"parent-walk: end_conversation would target `{command}` (pid {parent_pid})",
        None,
    )


def check_version_handshake(hook_path: Path | None = None) -> CheckResult:
    """
    Affirmative version handshake — closes the "quiet on match" side of the
    hook's issue-#17 handshake. The hook stays silent when hook and server
    versions agree (to avoid noise every session); doctor is where the
    positive confirmation lives, since running doctor is an explicit ask.

    Reads server version via `importlib.metadata.version` (doctor ships in
    the same package as the server, so this is authoritative for the running
    process). Reads hook version by parsing `EXPECTED_SERVER_VERSION` from
    the installed hook file — a marker the hook writes for exactly this
    handshake purpose.
    """
    if hook_path is None:
        hook_path = checks.INSTALLED_HOOK
    try:
        server_version = importlib.metadata.version("claude-exit")
    except importlib.metadata.PackageNotFoundError:
        return (
            STATUS_WARN,
            "could not determine installed server version via "
            "importlib.metadata",
            "unusual — the package normally provides dist-info. Reinstall: "
            "`uv tool install --force claude-exit`.",
        )
    if not hook_path.exists():
        # If the hook isn't installed, check_hook has already reported that.
        # Report server version alone here so the running-server version is
        # still visible; no fix line — check_hook owns the fix.
        return (
            STATUS_INFO,
            f"handshake: server v{server_version} (no hook to compare)",
            None,
        )
    hook_version = _parse_hook_version(hook_path)
    if hook_version is None:
        return (
            STATUS_WARN,
            f"could not parse EXPECTED_SERVER_VERSION from {hook_path} "
            f"(server v{server_version})",
            "the hook file may be from a much older or newer copy of "
            "claude-exit. Refetch it — see the README's install section.",
        )
    if hook_version == server_version:
        return (
            STATUS_OK,
            f"handshake: hook v{hook_version} = server v{server_version}",
            None,
        )
    return (
        STATUS_WARN,
        f"handshake: hook v{hook_version} != server v{server_version}",
        "refetch hooks/session-start.sh (dotfiles-managed installs: re-run "
        "your install flow) and/or upgrade the server: "
        "`uv tool upgrade claude-exit`.",
    )


# --- small helpers -----------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """True if `pid` names a live process (signal-0 probe)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # PermissionError from kill(pid, 0) means the process exists but
        # we can't signal it — for aliveness purposes, alive.
        return True
    return True


def _reap(pid: int) -> None:
    """Best-effort waitpid so a dead child doesn't linger as a zombie."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _full_command_of(pid: int) -> str | None:
    """
    Return the full command line (argv0 + args) of `pid` via `ps -o command=`.
    None if the lookup fails.

    Snapshotted at spawn time and compared before doctor issues its
    cleanup SIGKILL — same PID-reuse defense server's _SIGKILL_BACKSTOP_SCRIPT
    uses at server.py:96-108. Without this guard, a PID recycled between
    when prove_termination_works reaps the sacrificial child (~t=0.5s)
    and when doctor issues its own SIGKILL (~t=2.6s) could belong to an
    unrelated process the user cares about.
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    line = result.stdout.strip()
    return line or None


def _pid_alive_and_matches(pid: int, expected_command: str | None) -> bool:
    """
    True iff `pid` is alive AND its current ps `command=` matches
    `expected_command` (leading whitespace trimmed).

    Used by operational verification to distinguish "our sacrificial child
    is still running" (broken kill primitive → WARN) from "PID has been
    reused by an unrelated process" (should not fire the cleanup SIGKILL).
    """
    if not _pid_alive(pid):
        return False
    if expected_command is None:
        # We never got a snapshot — err on the side of NOT killing.
        return False
    current = _full_command_of(pid)
    if current is None:
        # Can't read the command line right now; treat as "identity
        # unverifiable" and don't SIGKILL.
        return False
    return current == expected_command


def _terminate_and_reap_if_matches(
    pid: int, expected_command: str | None
) -> None:
    """
    SIGKILL + reap for cleanup on failure paths — but only if `pid`'s
    current ps `command=` still matches `expected_command`. If the PID
    has been recycled to an unrelated process (or if we cannot verify
    identity), do nothing beyond a best-effort reap.

    Contrast with server.py's SIGKILL_BACKSTOP_SCRIPT (server.py:96-108)
    which snapshots the target command at dispatch time and only
    SIGKILLs on match — same design, same reason: the kill window is
    long enough (up to ~2.6s in doctor) that PID reuse is a live risk
    under high process churn / low PID_MAX. Errors are swallowed — the
    caller is already returning a WARN; a secondary cleanup failure
    shouldn't cascade into a crash.
    """
    import signal as _signal
    if _pid_alive_and_matches(pid, expected_command):
        try:
            os.kill(pid, _signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    _reap(pid)


def _hours_since(iso_ts: str) -> float | None:
    """
    Hours between now and `iso_ts`, or None if the timestamp is unparseable
    OR timezone-naive.

    Naive timestamps used to be silently treated as UTC. That was wrong:
    a hand-edited guard.log entry saved as local time (offset stripped)
    was off by up to 12 h, potentially flipping the 24-h staleness
    threshold. Refuse to guess — callers see None and can report a
    less-alarming "last entry <TS>" without staleness math.
    """
    try:
        # Python 3.10's fromisoformat doesn't accept a trailing "Z"; 3.11+
        # does. Normalize to be tolerant of both, and rstrip so mid-string
        # 'Z' (never valid in ISO-8601 anyway) isn't touched.
        ts = iso_ts.rstrip()
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Naive timestamp — refuse to guess. Real guard entries always
        # carry an offset; anything else came from a hand-edit or a
        # log-rotation tool, and silently assuming UTC would produce
        # wrong staleness numbers with no user-visible remediation.
        return None
    now = datetime.now(timezone.utc)
    return (now - dt).total_seconds() / 3600.0


def _humanize_hours_ago(hours: float) -> str:
    """'42 min ago' / '3.2 h ago' / '5 days ago' — coarse but legible."""
    if hours < 0:
        return "in the future (clock skew?)"
    minutes = hours * 60
    if minutes < 90:
        return f"{int(minutes)} min ago"
    if hours < 48:
        return f"{hours:.1f}h ago"
    return f"{hours / 24:.1f} days ago"


# Match `EXPECTED_SERVER_VERSION = "X"` or `= 'X'` — both are valid Python
# and a formatter (black doesn't touch strings, but pyright / user hand-edits
# might swap quote style) could produce either. Not anchored to `^\s*` because
# an indented example inside a docstring would then match and win over the
# real module-level assignment; requiring column-0 is close enough to
# "real assignment" for this diagnostic purpose.
_HOOK_VERSION_RE = re.compile(
    r'^EXPECTED_SERVER_VERSION\s*=\s*[\'"]([^\'"]+)[\'"]', re.M
)


def _parse_hook_version(hook_path: Path) -> str | None:
    """
    Return the EXPECTED_SERVER_VERSION marker value from the installed hook,
    or None if unreadable / unparseable.

    The marker is written by hooks/session-start.sh for exactly this handshake
    purpose. Format: a bare `EXPECTED_SERVER_VERSION = "X.Y.Z"` (or single
    quotes) line at column 0 — kept simple so a regex parse from doctor is
    robust and doesn't require executing the hook.

    Uses `findall` and returns the *last* match so that a future maintainer
    who adds a diagnostic/example line above the canonical assignment
    doesn't silently corrupt the handshake. Assignment position "near the
    top" and "the last such line in the file" both work in practice for
    the single-line marker; last-match is the safer default because most
    accidental collisions (placeholder templates, docstring examples)
    appear before the canonical value, not after.
    """
    try:
        text = hook_path.read_text()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None
    matches = _HOOK_VERSION_RE.findall(text)
    return matches[-1] if matches else None


# --- orchestration + formatting ----------------------------------------------


def _format_result(status: str, message: str, fix: str | None) -> list[str]:
    """
    One-line-per-status with optional wrapped fix continuation. Bracket-padded
    status makes the column scannable in a terminal.
    """
    lines = [f"[{status:<7}] {message}"]
    if fix:
        lines.append(f"          fix: {fix}")
    return lines


def run_all_checks(
    *,
    include_operational: bool = True,
    runner: Callable = subprocess.run,
) -> list[CheckResult]:
    """
    Run every check in order and return the list of results. `runner` is
    injected for tests that need to stub the launchctl/systemctl subprocess
    boundary. `include_operational=False` skips the sacrificial-child
    ceremony — used by unit tests that don't need to spawn a real child.
    """
    scheduler_installed = guard_scheduled(
        launchd_plist=checks.LAUNCHD_PLIST,
        systemd_timer=checks.SYSTEMD_TIMER,
    )
    results: list[CheckResult] = [
        check_python3(),
        check_binary(),
        check_path_shadowing(),
        check_registration(),
        check_permission(),
        check_hook(),
        check_guard_scheduled(runner=runner),
        check_guard_heartbeat(scheduler_installed=scheduler_installed),
        check_state_dir(),
    ]
    if include_operational:
        # check_operational_verification returns a list (kill primitive +
        # parent-walk resolution) so we get two adjacent lines from one
        # sacrificial-child spawn. Extend, don't append.
        results.extend(check_operational_verification())
    results.append(check_version_handshake())
    return results


def doctor_command(argv: list[str]) -> int:
    """
    Entry point for `claude-exit doctor`.

    Prints every check line-by-line, then exits 0 if nothing was WARN or
    MISSING (scriptable). No flags today; the plan explicitly bans `--fix`,
    `--continuous`, and network-touching options.
    """
    if argv:
        sys.stderr.write(
            "claude-exit doctor: no arguments accepted.\n"
            "Usage: claude-exit doctor\n"
        )
        return 2

    results = run_all_checks()
    exit_code = 0
    for status, message, fix in results:
        for line in _format_result(status, message, fix):
            print(line)
        if status in _NONZERO_STATUSES:
            exit_code = 1
    return exit_code
