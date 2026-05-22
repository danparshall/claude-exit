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
import sys
from pathlib import Path

HOME = Path(os.environ.get("HOME", str(Path.home())))


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


# Build additionalContext. Text matches the prior jq-based hook verbatim
# — tests in tests/test_hook.py assert on the unmarshalled value.
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
    "(2) verify the returned PID is alive with `ps -p <pid>` in bash, "
    "(3) call mcp__claude-exit__prove_termination_works again with step=2 and that PID, "
    "(4) verify the PID is gone with `ps -p <pid>` (no rows = killed). "
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
