"""
Pure-read predicates shared by the guard subcommand and doctor.

The central piece is `registration_state`, a five-way classifier of the
`claude-exit` entry in `~/.claude.json`:

    REG_PRESENT_WELL_FORMED  → no action needed
    REG_PRESENT_MALFORMED    → guard WARNs, does not overwrite (might be intent)
    REG_ABSENT               → guard restores
    REG_CONFIG_MISSING       → guard creates the file with only our entry
    REG_CONFIG_CORRUPT       → guard WARNs, does not touch a malformed file

Both guard and doctor branch on these values: guard to decide what to write
(or whether to write), doctor to decide what to report. Keeping the
classifier in one place avoids the two having different ideas of what
"the registration is broken" means.

The other predicates (`resolve_binary`, `tombstone_present`,
`guard_scheduled`, `preapproval_file`, `hook_registered_in_settings`,
`guard_scheduler_loaded`, `guard_last_heartbeat`, `invocations_bad_lines`)
are simpler — straight booleans / Optional[Path] / small tuples.

Design rule: this module never writes. The guard's actual file mutations
live in guard.py; this module exists so the *decision* about whether to
mutate (and the *reporting* about what's there) can be inspected, tested,
and shared without dragging side effects in.

The hook does NOT import this module — it ships as a curl-installed
heredoc with stdlib only, no package access. Some parsing logic here
(pre-approval keys, load_json tolerance) is deliberately duplicated from
the hook rather than shared, so the hook remains dependency-free.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable


# --- registration_state values -----------------------------------------------

REG_PRESENT_WELL_FORMED = "present_well_formed"
REG_PRESENT_MALFORMED = "present_malformed"
REG_ABSENT = "absent"
REG_CONFIG_MISSING = "config_missing"
REG_CONFIG_CORRUPT = "config_corrupt"

REGISTRATION_KEY = "claude-exit"


# --- path defaults -----------------------------------------------------------
# Each function accepts an explicit path arg so tests use tmp_path without
# mocking HOME. The module-level constants are the production defaults; doctor
# and guard import them via the function defaults.

CLAUDE_JSON = Path.home() / ".claude.json"
STATE_DIR = Path.home() / ".claude-exit"
TOMBSTONE = STATE_DIR / "uninstalled"
INVOCATIONS_LOG = STATE_DIR / "invocations.jsonl"
GUARD_LOG = STATE_DIR / "guard.log"
LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / "io.claude-exit.guard.plist"
SYSTEMD_TIMER = (
    Path.home() / ".config" / "systemd" / "user" / "claude-exit-guard.timer"
)

# Where the SessionStart hook is expected to live per the README's install
# instructions. Doctor's hook check reports against this conventional path;
# a non-standard install location is a supported but silent case (the user
# who chose a different name knows where to look).
USER_SETTINGS = Path.home() / ".claude" / "settings.json"
PROJECT_SETTINGS = Path(".claude") / "settings.json"
PROJECT_LOCAL_SETTINGS = Path(".claude") / "settings.local.json"
INSTALLED_HOOK = Path.home() / ".claude" / "hooks" / "claude-exit-session-start.sh"

# Pre-approval keys that grant end_conversation permission at session start.
# Kept in sync with hooks/session-start.sh's PREAPPROVAL_KEYS — the trivial
# duplication is deliberate (the hook can't import the package).
PREAPPROVAL_KEYS = frozenset({
    "mcp__claude-exit__end_conversation",
    "mcp__claude-exit__*",
    "mcp__claude-exit",
})

# Scheduler unit identifiers — kept as module-level so doctor's authoritative
# loaded-check refers to the same names as guard's install/uninstall paths.
LAUNCHD_LABEL = "io.claude-exit.guard"
SYSTEMD_TIMER_UNIT = "claude-exit-guard.timer"


# --- registration_state ------------------------------------------------------


def registration_state(claude_json: Path = CLAUDE_JSON) -> str:
    """
    Classify the state of the `claude-exit` entry in `~/.claude.json`.

    Returns one of REG_PRESENT_WELL_FORMED, REG_PRESENT_MALFORMED, REG_ABSENT,
    REG_CONFIG_MISSING, REG_CONFIG_CORRUPT.

    Distinctions that matter for the guard:

      - ABSENT vs CONFIG_MISSING: ABSENT means the file is parseable but lacks
        our entry → safe to add to an existing valid dict. CONFIG_MISSING means
        no file at all → safe to create a fresh file with just our entry.
      - CONFIG_CORRUPT means the file exists but is unparseable, OR `mcpServers`
        is present with the wrong type. Either way: don't touch — could be a
        half-written ad-hoc edit, and clobbering it would lose the user's work.
      - PRESENT_MALFORMED means our key exists but the value is the wrong shape
        (not a dict, or missing/empty `command`). Don't overwrite — a weird
        value might be a deliberate edit by an advanced user.
    """
    if not claude_json.exists():
        return REG_CONFIG_MISSING
    try:
        data = json.loads(claude_json.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return REG_CONFIG_CORRUPT
    if not isinstance(data, dict):
        return REG_CONFIG_CORRUPT

    servers = data.get("mcpServers")
    if servers is None:
        return REG_ABSENT  # No key at all → safe to add
    if not isinstance(servers, dict):
        return REG_CONFIG_CORRUPT  # Key exists but wrong type → don't touch

    if REGISTRATION_KEY not in servers:
        return REG_ABSENT

    value = servers[REGISTRATION_KEY]
    if not isinstance(value, dict):
        return REG_PRESENT_MALFORMED
    if not value.get("command"):
        return REG_PRESENT_MALFORMED
    return REG_PRESENT_WELL_FORMED


# --- resolve_binary ----------------------------------------------------------


def resolve_binary() -> Path | None:
    """
    Return the absolute path to the installed `claude-exit` binary, or None.

    First tries `shutil.which("claude-exit")` (PATH lookup, the normal case).
    Falls back to `~/.local/bin/claude-exit` — the path produced by
    `uv tool install claude-exit` — but only if it's actually executable.

    Returning None signals "binary not found"; callers (guard) should log
    ERROR and skip the restore rather than writing a registration whose
    command would fail on first invoke.
    """
    found = shutil.which("claude-exit")
    if found:
        return Path(found)
    fallback = Path.home() / ".local" / "bin" / "claude-exit"
    if fallback.exists() and os.access(fallback, os.X_OK):
        return fallback
    return None


# --- tombstone_present -------------------------------------------------------


def tombstone_present(tombstone: Path = TOMBSTONE) -> bool:
    """
    True if the deliberate-uninstall tombstone exists.

    When present, both the guard and the hook suppress their "registration
    missing" surfacing — the user removed everything on purpose. Content of
    the file is irrelevant; existence is the signal.
    """
    return tombstone.exists()


# --- guard_scheduled ---------------------------------------------------------


def guard_scheduled(
    *,
    launchd_plist: Path = LAUNCHD_PLIST,
    systemd_timer: Path = SYSTEMD_TIMER,
    platform: str | None = None,
) -> bool:
    """
    True if a guard scheduler artifact exists for the current platform.

    On macOS, checks for the launchd plist. On Linux, checks for the
    systemd user timer unit. Other platforms always return False (the
    package is Unix-only by support statement).

    Granularity: presence-on-disk, not loaded-in-scheduler. Verifying that
    launchd/systemd actually has the unit loaded means shelling out to
    `launchctl list` / `systemctl --user list-timers`; that richer check
    belongs in doctor, not in the shared predicate. For the guard's own
    sanity (and for the hook's "is the guard installed?" surfacing),
    presence-on-disk is the right line.
    """
    p = platform if platform is not None else sys.platform
    if p == "darwin":
        return launchd_plist.exists()
    if p.startswith("linux"):
        return systemd_timer.exists()
    return False


# --- settings.json helpers ---------------------------------------------------


def _load_json_dict(path: Path) -> dict | None:
    """
    Parse `path` into a dict, or return None on any read/parse failure.

    Mirrors the hook's `load_json` tolerance: unreadable, malformed, or
    non-object JSON is treated as absent rather than raising. Callers want
    "is X in this file?" — the answer for a missing/malformed file is
    "no", not an exception.

    For doctor's diagnostic needs, this "any failure → None" collapse is
    lossy: a corrupt settings.json looks the same as an absent one, and
    the WARN message would mis-point at "add the entry" when the real
    fix is "fix the JSON syntax". Use `settings_state` (below) when the
    caller wants to distinguish corrupt from absent.
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


# --- settings state (corrupt / absent / present) -----------------------------

SETTINGS_ABSENT = "absent"
SETTINGS_CORRUPT = "corrupt"
SETTINGS_PRESENT = "present"


def settings_state(path: Path) -> str:
    """
    Three-state classifier for a settings.json file, parallel to
    registration_state's shape:

        SETTINGS_ABSENT   → file does not exist (fine — user just hasn't
                            configured this scope)
        SETTINGS_CORRUPT  → file exists but is unparseable, non-object,
                            OR unreadable due to encoding (real problem —
                            doctor should WARN with a distinct fix)
        SETTINGS_PRESENT  → file parses into a dict (`_load_json_dict`
                            would succeed)

    Introduced to close the gap the code review flagged: `_load_json_dict`
    collapses corrupt and absent, so `check_hook`/`check_permission` were
    printing "add the entry" when the real repair is "fix the JSON".
    Callers use this to WARN loudly on corruption before diving into
    entry-shaped diagnostics.
    """
    if not path.exists():
        return SETTINGS_ABSENT
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return SETTINGS_CORRUPT
    except (FileNotFoundError, OSError):
        # OSError here means a real read failure (permissions, mid-race
        # deletion, etc.). Treat as corrupt for diagnostic purposes —
        # doctor's job is to name what's wrong, not to differentiate
        # every failure mode of open() itself.
        return SETTINGS_CORRUPT
    if not isinstance(data, dict):
        return SETTINGS_CORRUPT
    return SETTINGS_PRESENT


def preapproval_file(
    settings_files: Iterable[Path] | None = None,
) -> Path | None:
    """
    Return the first settings file that pre-approves end_conversation, or None.

    Recognizes the exact tool key, the server-wildcard key, and the
    server-level key — same set the hook uses (kept in sync via
    PREAPPROVAL_KEYS). Order of the three files matches the hook's search
    order; the first hit wins because that's how Claude Code merges them
    (user, then project, then project-local override).

    Returned Path is *the file where the pre-approval was found*. Doctor
    prints this so a silent flip from pre-approved → gated is legible
    ("the setting used to live in this file — now it doesn't"). Absence
    of a return value is not necessarily WARN: gated is a legitimate
    install per README's permission-prompt section; doctor reports which
    state is in effect and lets the user decide.

    Robustness: filters `allow_list` to strings before the `in` check.
    Python's `entry in frozenset(...)` raises TypeError on unhashable
    entries (dict/list from a hand-mangled settings.json), which would
    otherwise crash doctor with an uncaught exception.

    `settings_files` defaults to `None` (not a bound tuple) so the three
    module-level path constants are resolved at *call* time — tests that
    monkeypatch USER_SETTINGS et al. see the updated paths, matching the
    same pattern doctor uses for its own path-owning defaults.
    """
    if settings_files is None:
        settings_files = (USER_SETTINGS, PROJECT_SETTINGS, PROJECT_LOCAL_SETTINGS)
    for path in settings_files:
        data = _load_json_dict(path)
        if data is None:
            continue
        permissions = data.get("permissions")
        if not isinstance(permissions, dict):
            continue
        allow_list = permissions.get("allow") or []
        if not isinstance(allow_list, list):
            continue
        # Only string entries can be preapproval keys; ignore anything else
        # (dicts/lists would raise TypeError against the frozenset membership
        # test, which we deliberately do not want propagating out of doctor).
        string_entries = [e for e in allow_list if isinstance(e, str)]
        if any(entry in PREAPPROVAL_KEYS for entry in string_entries):
            return path
    return None


def hook_registered_in_settings(
    hook_path: Path = INSTALLED_HOOK,
    settings_file: Path = USER_SETTINGS,
) -> bool:
    """
    True if `settings_file` registers a SessionStart hook whose `command`
    string contains `hook_path.name`.

    Substring-on-basename match: Claude Code accepts literal absolute
    paths, `$HOME`-prefixed paths, and (in some setups) tilde paths. All
    three end with the hook filename, so basename-in-string is the
    forgiving-but-still-specific test. False positives from an unrelated
    hook named `claude-exit-session-start.sh` are indistinguishable from
    ours anyway.

    File-existence of `hook_path` is a separate concern — checked
    directly by doctor with `hook_path.exists()`. Splitting the two lets
    doctor distinguish "file present but unregistered" from
    "registered but file missing", both of which have happened with
    managed settings files.
    """
    data = _load_json_dict(settings_file)
    if data is None:
        return False
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    session_hooks = hooks.get("SessionStart") or []
    if not isinstance(session_hooks, list):
        return False
    hook_name = hook_path.name
    for entry in session_hooks:
        if not isinstance(entry, dict):
            continue
        inner_hooks = entry.get("hooks") or []
        if not isinstance(inner_hooks, list):
            continue
        for h in inner_hooks:
            if not isinstance(h, dict):
                continue
            command = h.get("command")
            # `command` must be a string for the substring test. A
            # non-string value (int/list/dict from a hand-mangled config)
            # would raise TypeError against `hook_name in command`; skip
            # rather than crash — this check is meant to survive weird
            # settings.json, not sanitize it.
            if not isinstance(command, str):
                continue
            if hook_name in command:
                return True
    return False


# --- scheduler: authoritative loaded-check -----------------------------------


def guard_scheduler_loaded(
    *,
    platform: str | None = None,
    runner=subprocess.run,
    uid: int | None = None,
) -> bool | None:
    """
    Authoritatively check whether the guard scheduler unit is loaded.

    Returns:
        True  — scheduler reports the unit as loaded/enabled.
        False — scheduler reports it as not loaded.
        None  — cannot check (unsupported platform, tool missing, or
                subprocess raised); doctor renders this as "inconclusive"
                rather than WARN, since a missing launchctl on macOS is
                itself a broken-environment signal that dwarfs the guard
                question.

    Distinct from `guard_scheduled`, which is a file-existence check:
    a plist on disk that never got `launchctl bootstrap`-ed will read as
    present-but-not-loaded here, and that is exactly the diagnostic gap
    doctor exists to close.

    macOS: `launchctl print gui/<uid>/io.claude-exit.guard` exits 0 iff
    the agent is loaded (rc != 0 with "Could not find service…"
    otherwise). Linux: `systemctl --user is-enabled claude-exit-guard.timer`
    exits 0 with `enabled` on stdout when enabled.

    `runner` and `uid` are injected for tests so we can assert on the
    launchctl/systemctl call shape without spawning it. Production uses
    subprocess.run and os.getuid().
    """
    p = platform if platform is not None else sys.platform
    if p == "darwin":
        u = uid if uid is not None else os.getuid()
        target = f"gui/{u}/{LAUNCHD_LABEL}"
        try:
            result = runner(
                ["launchctl", "print", target],
                capture_output=True, text=True, check=False,
            )
        except (OSError, FileNotFoundError):
            return None
        return result.returncode == 0
    if p.startswith("linux"):
        try:
            result = runner(
                ["systemctl", "--user", "is-enabled", SYSTEMD_TIMER_UNIT],
                capture_output=True, text=True, check=False,
            )
        except (OSError, FileNotFoundError):
            return None
        # is-enabled prints one of: enabled / disabled / static / masked / ...
        # Treat only "enabled" as loaded — "static" means no [Install] section,
        # which our unit does have, so anything but "enabled" is a problem.
        return result.returncode == 0 and result.stdout.strip() == "enabled"
    return None


# --- guard heartbeat / invocations health ------------------------------------


def guard_last_heartbeat(guard_log: Path = GUARD_LOG) -> str | None:
    """
    Return the ISO timestamp of the most recent guard.log entry, or None.

    Reads the whole file rather than tail-seeking — guard.log is small
    (one line per hour, and the guard's own noise budget is tight). Skips
    blank and malformed lines. The timestamp shape ISO-8601 with a `T`
    separator is written by `guard._log_guard`; anything else is treated
    as a schema mismatch and skipped.

    Used by doctor to compute "guard last ran N hours ago" and WARN if
    the scheduling artifact is present but the log is stale > 24h — the
    combination that means "the scheduler thinks it's running but the
    guard isn't actually firing".
    """
    try:
        text = guard_log.read_text()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None
    last: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        if len(parts) < 2:
            continue
        ts = parts[0]
        if "T" not in ts or len(ts) < 10:
            continue
        last = ts
    return last


def invocations_bad_lines(log_path: Path = INVOCATIONS_LOG) -> tuple[int, int]:
    """
    Count parseable and unparseable lines in invocations.jsonl.

    Returns (good, bad). Missing file → (0, 0), not an error: "never
    invoked" is the healthy default. Blank lines are ignored, not
    counted either way.

    Doctor WARNs if `bad > 0` — a JSONL log with garbage in it hints at
    concurrent-writer breakage or partial writes, and undercuts the
    audit-trail property the log exists to provide.

    Robustness: opens with `errors="replace"` so a stray non-UTF-8 byte
    (from a truncated write, a concurrent writer, or a filesystem with
    surprising locale defaults) doesn't crash the whole doctor run with
    an uncaught UnicodeDecodeError. The offending line becomes a `bad`
    count via json.loads failing on the replacement character — exactly
    the diagnostic we want to surface.
    """
    try:
        f = open(log_path, encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return (0, 0)
    good = 0
    bad = 0
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                json.loads(line)
                good += 1
            except (json.JSONDecodeError, ValueError):
                bad += 1
    return (good, bad)


# --- PATH shadowing report ---------------------------------------------------


def all_binaries_on_path(name: str = "claude-exit") -> list[Path]:
    """
    Return every executable named `name` found while walking $PATH, in
    the order PATH searches them.

    Analogous to `which -a` — the first entry is the one a fresh shell
    would resolve. Doctor uses this to detect PATH shadowing (a repo
    `.venv/bin/claude-exit` in front of `~/.local/bin/claude-exit` was
    half the confusion in the #17 version-skew incident).

    Deduplication: if PATH contains duplicate directories, and the same
    binary would resolve there twice, both appear in the list once each
    — the point is to show what the shell sees, not to normalize it.
    """
    path_env = os.environ.get("PATH", "")
    hits: list[Path] = []
    seen: set[Path] = set()
    for directory in path_env.split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / name
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        if candidate.exists() and os.access(candidate, os.X_OK):
            hits.append(candidate)
            seen.add(resolved)
    return hits
