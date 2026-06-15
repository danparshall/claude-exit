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
import shutil
import sys
from pathlib import Path


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
LAUNCHD_PLIST = Path.home() / "Library" / "LaunchAgents" / "io.claude-exit.guard.plist"
SYSTEMD_TIMER = (
    Path.home() / ".config" / "systemd" / "user" / "claude-exit-guard.timer"
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
