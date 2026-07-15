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
`guard_scheduled`) are simpler — straight booleans / Optional[Path].

Design rule: this module never writes. The guard's actual file mutations
live in guard.py; this module exists so the *decision* about whether to
mutate (and the *reporting* about what's there) can be inspected, tested,
and shared without dragging side effects in.

The hook does NOT import this module — it ships as a curl-installed
heredoc with stdlib only, no package access.
"""

import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


# --- registration_state values -----------------------------------------------

REG_PRESENT_WELL_FORMED = "present_well_formed"
REG_PRESENT_MALFORMED = "present_malformed"
REG_ABSENT = "absent"
REG_CONFIG_MISSING = "config_missing"
REG_CONFIG_CORRUPT = "config_corrupt"

REGISTRATION_KEY = "claude-exit"

# --- preapproval_state values ------------------------------------------------

PREAPPROVED = "preapproved"           # end_conversation pre-approved (Claude decides)
GATED = "gated"                        # not pre-approved (human confirms each session)
SETTINGS_CORRUPT = "settings_corrupt"  # at least one settings.json is unparseable

# The full end_conversation tool name and the three keys that grant it, in
# ascending scope order. Any of these grants pre-approval; doctor treats
# them all as PREAPPROVED without discriminating between them (users may
# have picked any level intentionally).
END_CONVERSATION_TOOL = "mcp__claude-exit__end_conversation"
_PREAPPROVAL_KEYS = (
    "mcp__claude-exit__end_conversation",       # exact
    "mcp__claude-exit__*",                       # wildcard
    "mcp__claude-exit",                          # server-level (Claude Code sugar)
)


# --- path defaults -----------------------------------------------------------
# Each function accepts an explicit path arg so tests use tmp_path without
# mocking HOME. The module-level constants are the production defaults; doctor
# and guard import them via the function defaults.

CLAUDE_JSON = Path.home() / ".claude.json"
STATE_DIR = Path.home() / ".claude-exit"
TOMBSTONE = STATE_DIR / "uninstalled"
LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / "io.claude-exit.guard.plist"
SYSTEMD_TIMER = (
    Path.home() / ".config" / "systemd" / "user" / "claude-exit-guard.timer"
)
HOOK_PATH = Path.home() / ".claude" / "hooks" / "claude-exit-session-start.sh"
# User-global settings.json — the file the hook check pins against. Kept
# separate from settings_files() (which returns three cwd-aware paths) so
# tests can override it independently.
USER_SETTINGS_JSON = Path.home() / ".claude" / "settings.json"
# The Claude Code SessionStart key. Doctor uses this to verify the hook file
# is actually wired into user-global settings, not merely on disk.
HOOK_EVENT = "SessionStart"


def settings_files(cwd: Path | None = None) -> tuple[Path, Path, Path]:
    """
    Return the three settings.json paths Claude Code layers, most-general first.

    Order matches the hook (hooks/session-start.sh:88-92):
      1. ~/.claude/settings.json                  (user global)
      2. <cwd>/.claude/settings.json              (project checked-in)
      3. <cwd>/.claude/settings.local.json        (project user-local)

    Computed at call time (not module import) because cwd changes between
    an installer's shell and doctor's invocation from wherever they run it.
    """
    base = cwd if cwd is not None else Path.cwd()
    return (
        Path.home() / ".claude" / "settings.json",
        base / ".claude" / "settings.json",
        base / ".claude" / "settings.local.json",
    )


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


# --- doctor-only predicates (checks 1, 3, 4, 5, 6-heartbeat, 7, 9) ---------
#
# These are added for the `claude-exit doctor` subcommand. They stay in
# checks.py because they are pure-read facts about wiring; doctor.py owns
# the (status, message, fix_line) presentation layer that wraps them.


def python3_on_path() -> Path | None:
    """Return the resolved path to `python3` on PATH, or None if missing.

    Doctor check #1. The hook needs python3 (it's a stdlib-only heredoc); the
    guard doesn't strictly need it but is invoked by a scheduler that does.
    Absent → both are broken, and it's also a broken-environment signal for
    the package itself (transitive dep).
    """
    found = shutil.which("python3")
    return Path(found) if found else None


def path_shadowing(name: str = "claude-exit") -> list[Path]:
    """
    Return every executable named `name` on PATH, in PATH order, deduped.

    Doctor check #9 half. `shutil.which` returns only the first hit; a shadow
    install (e.g., a repo `.venv/bin/claude-exit` shadowing `~/.local/bin/`)
    is invisible to `which`, but "which server a session gets is terminal-
    PATH-dependent" was half the confusion in the version-handshake incident.
    Doctor reports the full list so shadowing is legible.
    """
    seen: set[Path] = set()
    hits: list[Path] = []
    for element in os.environ.get("PATH", "").split(os.pathsep):
        if not element:
            continue
        candidate = Path(element) / name
        if not candidate.exists() or not os.access(candidate, os.X_OK):
            continue
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        hits.append(candidate)
    return hits


def project_mcp_json_registers(cwd: Path | None = None) -> bool:
    """
    True if the project-local `.mcp.json` in `cwd` registers `claude-exit`.

    Doctor check #3 side-note. Not a full state classifier (guard doesn't
    touch project MCP configs); doctor only surfaces "hey, there's also a
    project-local registration in effect for this cwd" so the user knows a
    ~/.claude.json fix might not fully explain a session's behavior.
    """
    base = cwd if cwd is not None else Path.cwd()
    mcp_json = base / ".mcp.json"
    if not mcp_json.exists():
        return False
    try:
        data = json.loads(mcp_json.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return False
    return REGISTRATION_KEY in servers


def _load_settings(path: Path) -> tuple[dict | None, bool]:
    """
    Read a settings.json file. Returns (data-or-None, corrupt).

    - (dict, False) — file present and well-formed
    - (None, False) — file simply absent (no error)
    - (None, True)  — file present but unparseable, or not a dict
    """
    if not path.exists():
        return None, False
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None, True
    if not isinstance(data, dict):
        return None, True
    return data, False


def preapproval_state(paths: tuple[Path, ...] | None = None) -> str:
    """
    Doctor check #4. Classify pre-approval of `end_conversation` across the
    three settings.json layers.

    Returns one of PREAPPROVED, GATED, SETTINGS_CORRUPT.

    Any grant (exact / wildcard / server-level) in any of the three files
    counts as PREAPPROVED — matches hook behavior. SETTINGS_CORRUPT wins
    over GATED so a broken file doesn't silently masquerade as intentional
    revocation. **The plan is emphatic that neither PREAPPROVED nor GATED
    is a defect**: both are legitimate installs; doctor reports which is
    in effect so a silent flip is visible, not to editorialize.
    """
    targets = paths if paths is not None else settings_files()
    any_corrupt = False
    for path in targets:
        data, corrupt = _load_settings(path)
        if corrupt:
            any_corrupt = True
            continue
        if data is None:
            continue
        allow = ((data.get("permissions") or {}).get("allow") or [])
        # Filter to strings — a settings file with a non-string entry (int, list,
        # object) shouldn't crash the check; the entry can never grant anything.
        allow_strs = [x for x in allow if isinstance(x, str)]
        if any(entry in _PREAPPROVAL_KEYS for entry in allow_strs):
            return PREAPPROVED
    return SETTINGS_CORRUPT if any_corrupt else GATED


def hook_installed(hook_path: Path = HOOK_PATH) -> bool:
    """
    Doctor check #5 half. True iff the hook file exists AND is executable.

    Not-executable is a real failure mode (users hand-copy the file and
    forget `chmod +x`); collapsing it into "installed" would let a broken
    install look healthy.
    """
    return hook_path.exists() and os.access(hook_path, os.X_OK)


def hook_registered(
    hook_path: Path = HOOK_PATH,
    settings_path: Path | None = None,
) -> bool:
    """
    Doctor check #5 half. True iff `hook_path` appears as a SessionStart
    command in `settings_path` (defaults to `~/.claude/settings.json`).

    Only user-global settings are checked — this hook is user-scoped by
    design (see hook file comments). Corrupt settings.json → False here;
    doctor's preapproval_state check will separately report the corruption.

    Match strategy — three passes, first hit wins:
      1. Substring match on the absolute hook path (`/Users/dan/.claude/...`)
         against the command string after `os.path.expanduser` +
         `os.path.expandvars` expansion. Handles the common `~/...` and
         `$HOME/...` forms real installers use.
      2. Basename match on the hook filename (`claude-exit-session-start.sh`)
         — a fallback that catches wrappers we didn't anticipate and hand-
         edited relative paths. The hook's distinctive name makes this
         low-collision.

    False here would produce a spurious WARN + a fix line telling the user
    to add a hook entry that's already present — a bad user experience.
    Better to over-match slightly.
    """
    target = settings_path if settings_path is not None else USER_SETTINGS_JSON
    data, _corrupt = _load_settings(target)
    if data is None:
        return False
    hooks = data.get("hooks") or {}
    if not isinstance(hooks, dict):
        return False
    entries = hooks.get(HOOK_EVENT) or []
    if not isinstance(entries, list):
        return False

    absolute_needle = str(hook_path)
    basename_needle = os.path.basename(absolute_needle)

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for sub in (entry.get("hooks") or []):
            if not isinstance(sub, dict):
                continue
            cmd = sub.get("command")
            if not isinstance(cmd, str):
                continue
            expanded = os.path.expandvars(os.path.expanduser(cmd))
            if absolute_needle in expanded:
                return True
            if basename_needle and basename_needle in cmd:
                return True
    return False


def guard_last_heartbeat(guard_log: Path) -> str | None:
    """
    Doctor check #6 half. Return the most-recent ISO-8601 timestamp in the
    guard log, or None if the file is absent or contains no parseable
    entries.

    Doctor treats a stale heartbeat (> 24h with the scheduler installed) as
    WARN; the interpretation lives in doctor.py so this stays a pure fact.
    """
    if not guard_log.exists():
        return None
    latest: str | None = None
    for raw in guard_log.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        ts = line.split(" ", 1)[0]
        # ISO-8601 shape check: date is 10 chars, contains 'T'.
        if len(ts) < 10 or "T" not in ts:
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


def hours_since(ts: str) -> float | None:
    """
    Doctor check #6 helper. Hours between `ts` (ISO-8601) and now (UTC).

    Returns None if `ts` is naive (no tzinfo) — a naive timestamp is
    ambiguous and doctor should report the raw string rather than
    silently assume UTC. Returns a negative number if `ts` is in the
    future (clock skew); doctor branches on the sign.
    """
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    delta = datetime.now(timezone.utc) - parsed
    return delta.total_seconds() / 3600.0


def invocations_health(log_path: Path) -> tuple[int, int]:
    """
    Doctor check #7 half. Return (good_lines, bad_lines) for the
    invocations log; (0, 0) if the file doesn't exist.

    "Good" = parseable as JSON *and* a dict. Anything else contributes to
    "bad" — a malformed line is a real signal (write partially flushed,
    disk full during a `_log` call). Doctor WARNs on bad_lines > 0.

    `errors="replace"` on decode so a corrupt byte in one entry doesn't
    take out the count for the rest of the file.
    """
    if not log_path.exists():
        return 0, 0
    good = 0
    bad = 0
    with open(log_path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if isinstance(obj, dict):
                good += 1
            else:
                bad += 1
    return good, bad


_HOOK_VERSION_RE = re.compile(
    r'''^EXPECTED_SERVER_VERSION\s*=\s*["']([^"']+)["']''',
    flags=re.MULTILINE,
)


def hook_expected_server_version(hook_path: Path = HOOK_PATH) -> str | None:
    """
    Doctor check #9. Parse the `EXPECTED_SERVER_VERSION = "x.y.z"` marker
    from the hook file. Returns None if the file is absent, unreadable, or
    the marker isn't found.

    Accepts either quote style. Uses the last match so a hypothetical
    reassignment lower in the file wins over one commented out above (both
    behaviors seen in the wild for version-marker patterns).
    """
    try:
        text = hook_path.read_text()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None
    matches = _HOOK_VERSION_RE.findall(text)
    return matches[-1] if matches else None


def installed_server_version() -> str | None:
    """
    Doctor check #9. Return the installed package version via
    `importlib.metadata`, or None if the package isn't installed as metadata
    (edge case: someone running doctor from a source checkout without pip
    install / uv sync).
    """
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("claude-exit")
    except PackageNotFoundError:
        return None
