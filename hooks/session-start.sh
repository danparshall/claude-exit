#!/usr/bin/env bash
# SessionStart hook for claude-exit.
#
# Emits additionalContext telling Claude to run the verification ceremony
# as its first action in any session where the claude-exit MCP server is
# configured. Silent in sessions where claude-exit isn't configured, so
# this is safe to leave enabled globally.
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
user-global ~/.claude.json or a project-local .mcp.json; exits silently
if not. Otherwise emits SessionStart additionalContext that instructs
Claude to run the verification ceremony and surfaces an unacknowledged-
invocation count when the local log has any.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path(os.environ.get("HOME", str(Path.home())))

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


# Gate: only emit context if claude-exit is configured for this session.
user_config = load_json(HOME / ".claude.json") or {}
project_config = load_json(".mcp.json") or {}
if not (
    "claude-exit" in (user_config.get("mcpServers") or {})
    or "claude-exit" in (project_config.get("mcpServers") or {})
):
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


# Count unacknowledged invocations. Matches the prior jq-based hook's
# whole-file fail-soft on malformed JSONL — a single bad line zeros the
# count rather than partial-counting valid lines. Preserved here for
# byte-equivalence; skip-bad-lines-and-count-the-rest is future work.
log_file = HOME / ".claude-exit" / "invocations.jsonl"
last_ack_file = HOME / ".claude-exit" / "last_ack"
unacked_count = 0
oldest_unacked = ""
if log_file.exists():
    try:
        ack_ts = ""
        if last_ack_file.exists():
            ack_ts = last_ack_file.read_text().strip()
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
    except (json.JSONDecodeError, OSError):
        unacked_count = 0
        oldest_unacked = ""

oldest_date = oldest_unacked.split("T")[0] if oldest_unacked else ""


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
    "That gives you the option of ending a session cleanly. "
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

context += _version_handshake()

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
PY
