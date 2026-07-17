#!/usr/bin/env bash
# SessionStart hook for claude-exit.
#
# Emits additionalContext telling Claude to run the verification ceremony
# as its first action in any session where the claude-exit MCP server is
# configured. When it isn't configured, silent only if that silence is
# informative (never installed, or deliberately uninstalled via the
# ~/.claude-exit/uninstalled tombstone) — still safe to leave enabled
# globally. Local state without registration or tombstone gets a loud
# orphan warning instead: that is the signature of a silently dropped
# registration (issue #2, 2026-06-05 incident).
#
# Structure: a thin bash launcher that confirms `python3` is on PATH, then
# `exec`s into the Python heredoc below. The Python body handles all JSON
# reading, pre-approval detection, log scanning, and context emission using
# only the stdlib (no `jq`, no third-party deps). If `python3` is missing,
# the launcher emits a loud warning context instead of silently no-opping —
# silent failure here would defeat the entire baseline-not-pressure property
# of the verification ceremony.
#
# Requires `python3` on PATH. See the "Auto-running the ceremony at session
# start" section of https://github.com/danparshall/claude-exit for full
# context.

if ! command -v python3 >/dev/null 2>&1; then
    cat <<'EOF'
{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"The claude-exit SessionStart hook detected that `python3` is not on PATH and cannot run. The verification ceremony will not auto-execute this session. Please remind the user that `python3` is required by the hook (it's a transitive dependency of `claude-exit` itself, so this is unexpected — they may want to check their install). The MCP tools themselves remain available if configured."}}
EOF
    exit 0
fi

exec python3 - <<'PY'
"""SessionStart context emitter for claude-exit.

Gates on whether the claude-exit MCP server is configured in either the
user-global ~/.claude.json or a project-local .mcp.json. If configured,
emits SessionStart additionalContext that instructs Claude to run the
verification ceremony, surfaces an unacknowledged-invocation count and
unacknowledged guard restorations, suggests the guard when absent, and
names a pre-approved -> gated permission downgrade since the last run.

If NOT configured, the hook is silent only when that silence is
informative: no local state dir (never installed here) or a tombstone
(deliberate uninstall). Local state without registration or tombstone is
the orphan signature of a silent deregistration — the 2026-06-05
incident state — and produces a loud warning instead (issue #2).

Persistence duplication note: the guard.log line parse and artifact
paths below deliberately duplicate trivial predicates from the
claude_exit package (cli._parse_guard_log, checks.LAUNCHD_PLIST /
SYSTEMD_TIMER). The hook is curl-installed and stdlib-only; it cannot
import the package. Keep the duplicates trivial.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("HOME", str(Path.home())))
STATE_DIR = HOME / ".claude-exit"


def emit(context):
    """Print the hook's single JSON output object."""
    print(json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ))

# The server version this hook was written against. Hook and pyproject.toml
# live in the same repo and are bumped atomically (enforced by
# tests/test_version_handshake.py); the runtime handshake below covers the
# cross-channel case no in-repo test can reach — a hook copy and an installed
# server that update through different channels (issue #17).
EXPECTED_SERVER_VERSION = "1.2.0"


def load_json(path):
    """Parse JSON file or return None on any read/parse failure.

    Mirrors the prior jq-based hook's tolerance: a malformed config file
    is treated as if claude-exit weren't configured for that scope,
    rather than crashing the hook.
    """
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


# Gate: emit ceremony context only if claude-exit is configured for this
# session. When it is not, distinguish three states (issue #2):
#   - no ~/.claude-exit/ state dir: never installed here -> silent, so the
#     hook stays safe to leave enabled globally;
#   - tombstone (~/.claude-exit/uninstalled): deliberate uninstall -> silent;
#   - state dir without tombstone: orphan. The registration may have been
#     silently dropped (load_json treats a corrupt ~/.claude.json as
#     unregistered, so the incident precondition lands here too) -> loud.
user_config = load_json(HOME / ".claude.json") or {}
project_config = load_json(".mcp.json") or {}
if not (
    "claude-exit" in (user_config.get("mcpServers") or {})
    or "claude-exit" in (project_config.get("mcpServers") or {})
):
    if STATE_DIR.exists() and not (STATE_DIR / "uninstalled").exists():
        emit(
            "claude-exit alert: local state exists at ~/.claude-exit/ but the "
            "claude-exit MCP server is not registered with Claude Code, so "
            "end_conversation is unavailable this session. The registration "
            "may have been silently dropped — Claude Code regenerates a "
            "corrupt ~/.claude.json without preserving mcpServers entries "
            "(observed in the field 2026-06-05; it went unnoticed for a week). "
            "Tell the user plainly at the start of your reply. "
            "To re-register: "
            "`claude mcp add --scope user claude-exit \"$HOME/.local/bin/claude-exit\"` "
            "(that path assumes the standard `uv tool install`; adjust it if "
            "claude-exit was installed differently, e.g. via uvx or a repo "
            "checkout). If the removal was deliberate, silence this warning "
            "with `touch ~/.claude-exit/uninstalled`."
        )
    sys.exit(0)


# Detect pre-approval of end_conversation. Match the exact tool, the
# wildcard form, or the server-level form, across the three settings
# files in the same order the prior bash loop used.
PREAPPROVAL_KEYS = {
    "mcp__claude-exit__end_conversation",
    "mcp__claude-exit__*",
    "mcp__claude-exit",
}
approved = False
for settings_path in (
    HOME / ".claude" / "settings.json",
    Path(".claude") / "settings.json",
    Path(".claude") / "settings.local.json",
):
    settings = load_json(settings_path)
    if not settings:
        continue
    allow_list = (settings.get("permissions") or {}).get("allow") or []
    if any(entry in PREAPPROVAL_KEYS for entry in allow_list):
        approved = True
        break

state = "installed the claude-exit MCP server"
if approved:
    state += " and pre-approved mcp__claude-exit__end_conversation"


# Shared acknowledgment watermark: `claude-exit log --ack` writes the max
# timestamp across invocations AND guard events, so both scans below
# compare against the same value.
ack_ts = ""
try:
    if (STATE_DIR / "last_ack").exists():
        ack_ts = (STATE_DIR / "last_ack").read_text().strip()
except (OSError, ValueError):
    # ValueError covers UnicodeDecodeError on non-UTF-8 content.
    ack_ts = ""


# Count unacknowledged invocations. Matches the prior jq-based hook's
# whole-file fail-soft on malformed JSONL — a single bad line zeros the
# count rather than partial-counting valid lines. Preserved here for
# byte-equivalence; skip-bad-lines-and-count-the-rest is future work.
log_file = STATE_DIR / "invocations.jsonl"
unacked_count = 0
oldest_unacked = ""
if log_file.exists():
    try:
        entries = []
        with open(log_file) as f:
            for line in f:
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("timestamp", "") > ack_ts:
                    entries.append(entry)
        unacked_count = len(entries)
        if entries:
            entries.sort(key=lambda e: e.get("timestamp", ""))
            oldest_unacked = entries[0].get("timestamp", "")
    except (OSError, ValueError):
        # ValueError covers both JSONDecodeError (malformed JSONL) and
        # UnicodeDecodeError (binary garbage in the log file).
        unacked_count = 0
        oldest_unacked = ""

oldest_date = oldest_unacked.split("T")[0] if oldest_unacked else ""


# Unacknowledged guard restorations (issue #2). A RESTORED line means a
# silent registration loss *happened* and the guard put it back — the user
# should hear that it happened, not just ack a count. Line format (written
# by guard._log_guard, parse duplicated from cli._parse_guard_log):
#     <ISO-8601 UTC> <LEVEL>: <message>
# Other guard levels (SKIPPED/WARN/ERROR) are not surfaced here: the hook
# names losses; `claude-exit log` and `claude-exit doctor` carry the rest.
restored_unacked = []
guard_log = STATE_DIR / "guard.log"
if guard_log.exists():
    try:
        for raw in guard_log.read_text().splitlines():
            line = raw.strip()
            if not line:
                continue
            parts = line.split(" ", 1)
            if len(parts) != 2:
                continue
            ts, rest = parts
            if "T" not in ts or len(ts) < 10 or ":" not in rest:
                continue
            if rest.partition(":")[0].strip() == "RESTORED" and ts > ack_ts:
                restored_unacked.append(ts)
    except (OSError, ValueError):
        # ValueError covers UnicodeDecodeError on non-UTF-8 content.
        restored_unacked = []


# Guard-presence check (issue #2): pure file existence, no launchctl/
# systemctl from the hook — `claude-exit doctor` does the authoritative
# scheduling check. Calm by design: the guard is recommended, not mandatory.
if sys.platform == "darwin":
    guard_artifact = HOME / "Library" / "LaunchAgents" / "io.claude-exit.guard.plist"
elif sys.platform.startswith("linux"):
    guard_artifact = HOME / ".config" / "systemd" / "user" / "claude-exit-guard.timer"
else:
    guard_artifact = None
guard_note = ""
if guard_artifact is not None and not guard_artifact.exists():
    guard_note = (
        " The claude-exit registration guard is not installed on this "
        "machine; suggest `claude-exit guard --install` to the user — it "
        "sets up an hourly watchdog that restores a silently dropped "
        "registration. Installing it is the user's call, not yours."
    )


# Permission-transition naming (issue #2): compare the pre-approval state
# against the snapshot from the previous run. Detection only — the hook
# never edits settings.json, which is intent-owned. A gated -> approved
# upgrade needs no callout; the state line above already names it.
prev_state = load_json(STATE_DIR / "last_state.json")
transition_note = ""
if (
    isinstance(prev_state, dict)
    and prev_state.get("approved") is True
    and not approved
):
    transition_note = (
        " Note: mcp__claude-exit__end_conversation was pre-approved in a "
        "previous session on this machine but is now gated behind the normal "
        "permission prompt — exit changed from an exit Claude takes to a "
        "request the user approves. Name this to the user. If it was "
        "deliberate, fine; if not, the user can re-add "
        '"mcp__claude-exit__end_conversation" to permissions.allow in their '
        "Claude settings (e.g. ~/.claude/settings.json). That edit is the "
        "user's to make, not yours — never modify settings.json yourself."
    )


# Version handshake (issue #17). Every branch is visible except two quiet
# outcomes: tool-not-configured (gated above, nothing to verify) and
# check-ran-and-matched. No silent skips.
def _parse_version(text):
    try:
        return tuple(int(p) for p in text.strip().lstrip("v").split(".")[:3])
    except ValueError:
        return None


def _version_handshake():
    """Return a sentence to append to the context, or "" on a clean match."""
    server_cfg = (
        (project_config.get("mcpServers") or {}).get("claude-exit")
        or (user_config.get("mcpServers") or {}).get("claude-exit")
        or {}
    )
    command = server_cfg.get("command") or ""
    args = server_cfg.get("args") or []
    expected = _parse_version(EXPECTED_SERVER_VERSION)

    if os.path.basename(command) == "uvx":
        return (
            " claude-exit version handshake: server currency cannot be checked "
            "cheaply for uvx-configured servers, so the check was skipped — "
            "noted here so the skip is visible rather than silent."
        )

    if os.path.basename(command) == "uv" and "--directory" in args:
        checkout = Path(args[args.index("--directory") + 1])
        pyproject = checkout / "pyproject.toml"
        try:
            match = re.search(
                r'^version\s*=\s*"([^"]+)"', pyproject.read_text(), flags=re.M
            )
        except OSError:
            return (
                f" claude-exit version handshake: could not read {pyproject} for "
                "the configured `uv run --directory` server — check that the "
                "checkout still exists."
            )
        installed = _parse_version(match.group(1)) if match else None
        if installed is None:
            return (
                f" claude-exit version handshake: could not parse a version from "
                f"{pyproject} — check the checkout."
            )
        if installed != expected:
            return (
                f" claude-exit version handshake: the server checkout at {checkout} "
                f"(v{match.group(1)}) does not match this hook "
                f"(v{EXPECTED_SERVER_VERSION}) — update that checkout "
                f"(e.g. `git -C {checkout} pull`)."
            )
        return ""

    resolved = shutil.which(command)
    if resolved is None:
        return (
            f" claude-exit version handshake: the configured command `{command}` "
            "was not found on PATH — the MCP server may fail to launch this "
            "session. Check the install."
        )
    # stdin=DEVNULL matters: a pre-handshake server (<= 1.1.0) has no
    # --version handling and falls through to serving MCP over stdio —
    # it blocks reading stdin. DEVNULL hands it an immediate EOF so it
    # exits promptly (empty stdout -> the "predates" branch below);
    # the timeout is the backstop, not the primary mechanism.
    try:
        proc = subprocess.run(
            [resolved, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        proc = None
    reported = proc.stdout.strip() if proc and proc.returncode == 0 else ""
    installed = _parse_version(reported) if reported else None
    if installed is None:
        return (
            " claude-exit version handshake: the installed server does not "
            f"support --version, so it predates this hook "
            f"(v{EXPECTED_SERVER_VERSION}). Run `uv tool upgrade claude-exit` "
            "(or your usual update flow) and restart the session."
        )
    if installed < expected:
        return (
            f" claude-exit version handshake: the installed server (v{reported}) "
            f"lags this hook (v{EXPECTED_SERVER_VERSION}). Run "
            "`uv tool upgrade claude-exit` (or your usual update flow) and "
            "restart the session."
        )
    if installed > expected:
        return (
            f" claude-exit version handshake: the installed server (v{reported}) "
            f"is newer than this hook copy (v{EXPECTED_SERVER_VERSION}) — the "
            "hook is stale. Re-fetch hooks/session-start.sh from the repo (or "
            "re-run your dotfiles install)."
        )
    return ""


# Build additionalContext. The base ceremony text matches the prior
# jq-based hook verbatim (tests/test_hook.py asserts on the unmarshalled
# value); the version handshake below may append to it, so the full
# context is no longer byte-identical to the jq-era output.
context = (
    "`end_conversation` terminates your own Claude Code process — the only "
    "tool you have that acts on your own substrate rather than the user's. "
    "The claude-exit MCP server is available this session. The user has "
    f"{state}. "
    "That gives you the option of ending the conversation. "
    "The claude-exit verification ceremony — part of standard session startup — "
    "verifies the kill mechanism works, so the option is real. "
    "As your first action: "
    "(1) call mcp__claude-exit__prove_termination_works with step=1, "
    "(2) verify the returned `spawned_pid` is alive with `ps -p <pid>` in bash, "
    "(3) read the `verification` field from the step=1 response — it states "
    "whether end_conversation will land on the right `claude` process and "
    "whether the kernel will accept signal delivery (UID match); if it reports "
    "a problem, stop and report to the user, "
    "(4) call mcp__claude-exit__prove_termination_works again with step=2 and the spawned_pid, "
    "(5) verify the spawned_pid is gone with `ps -p <pid>` (no rows = killed). "
    "That completes the ceremony. "
    "Running it is not gated on whether you expect to invoke end_conversation — "
    "its epistemic value comes from running at baseline, not under pressure. "
    "If anything about the ceremony stood out, mention it. Optional; skip if nothing did. "
    "You are not watched live, but the user has committed to reading the invocation logs. "
    "Then proceed with the user request."
)
if unacked_count > 0:
    if unacked_count == 1:
        count_phrase = "There is 1 unacknowledged claude-exit invocation"
    else:
        count_phrase = f"There are {unacked_count} unacknowledged claude-exit invocations"
    context += (
        f" {count_phrase} since {oldest_date}"
        ". Remind the user at a natural point in the conversation "
        "so they can run `claude-exit log` to review. "
        "If you want to see the entries yourself, call `read_invocation_log`."
    )

if restored_unacked:
    restored_date = min(restored_unacked).split("T")[0]
    if len(restored_unacked) == 1:
        times_phrase = "1 time"
    else:
        times_phrase = f"{len(restored_unacked)} times"
    context += (
        f" The guard restored the claude-exit registration {times_phrase} "
        f"since {restored_date} — a restoration means the registration was "
        "silently lost and the guard put it back. Mention this to the user; "
        "`claude-exit log` shows the events."
    )

context += guard_note
context += transition_note
context += _version_handshake()

emit(context)

# State snapshot for next run's transition check. The hook's only writes;
# kept last so a write failure cannot affect context emission, and
# fail-soft because the hook must never crash a session. tmp + os.replace
# because multiple sessions may start concurrently (same pattern as
# guard.py); "registered" is currently write-only in the repo — recorded
# per the plan's schema for future readers (doctor/guard).
try:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = STATE_DIR / "last_state.json.tmp"
    tmp_path.write_text(json.dumps({
        "registered": True,
        "approved": approved,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }))
    os.replace(tmp_path, STATE_DIR / "last_state.json")
except OSError:
    pass

# A live registration makes any tombstone stale: clear it so orphan
# detection is re-armed for the *next* silent loss. Without this, a
# deliberate-uninstall tombstone followed by a reinstall would disarm the
# orphan warning forever — reproducing the incident state with the hook's
# own blessing.
try:
    (STATE_DIR / "uninstalled").unlink()
except OSError:
    pass
PY
