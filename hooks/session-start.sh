#!/usr/bin/env bash
# SessionStart hook for claude-exit.
#
# Emits additionalContext telling Claude to run the verification ceremony
# as its first action in any session where the claude-exit MCP server is
# configured. Silent in sessions where claude-exit isn't configured, so
# this is safe to leave enabled globally.
#
# Requires `jq` on PATH. See the "Auto-running the ceremony at session
# start" section of https://github.com/danparshall/claude-exit for full
# context.

# Gate: only emit context if claude-exit is configured for this session.
# Checks the user-global config and any project-local .mcp.json. If
# neither declares the server, exit silently.
{ jq -e '.mcpServers."claude-exit"' "$HOME/.claude.json" >/dev/null 2>&1 \
    || jq -e '.mcpServers."claude-exit"' .mcp.json >/dev/null 2>&1; } \
    || exit 0

# Detect whether end_conversation is pre-approved in any of the usual
# settings locations. Matches the exact tool, the wildcard form, and the
# server-level form in case users prefer those.
APPROVED=""
for f in \
    "$HOME/.claude/settings.json" \
    .claude/settings.json \
    .claude/settings.local.json
do
    [ -f "$f" ] || continue
    if jq -e '.permissions.allow[]?
              | select(. == "mcp__claude-exit__end_conversation"
                    or . == "mcp__claude-exit__*"
                    or . == "mcp__claude-exit")' \
           "$f" >/dev/null 2>&1; then
        APPROVED=1
        break
    fi
done

# Build the install-state sentence. Descriptive, not interpretive: we
# name what the user has done and leave any inference to the model.
STATE="installed the claude-exit MCP server"
[ -n "$APPROVED" ] && STATE="$STATE and pre-approved mcp__claude-exit__end_conversation"

# Compute unacknowledged-invocation count. The installer's commitment to
# review the log is load-bearing for the credible-signal argument in the
# README; surfacing the count here turns that commitment from willpower
# into ongoing visible state.
LOG_FILE="$HOME/.claude-exit/invocations.jsonl"
LAST_ACK_FILE="$HOME/.claude-exit/last_ack"
UNACKED_COUNT=0
OLDEST_UNACKED=""
if [ -f "$LOG_FILE" ]; then
    ACK_TS=""
    [ -f "$LAST_ACK_FILE" ] && ACK_TS=$(tr -d '\n' < "$LAST_ACK_FILE")
    UNACKED_COUNT=$(jq -s --arg ts "$ACK_TS" \
        '[.[] | select(.timestamp > $ts)] | length' \
        "$LOG_FILE" 2>/dev/null || echo 0)
    OLDEST_UNACKED=$(jq -s -r --arg ts "$ACK_TS" \
        '[.[] | select(.timestamp > $ts)] | sort_by(.timestamp) | (.[0].timestamp // "")' \
        "$LOG_FILE" 2>/dev/null)
fi
UNACKED_COUNT=${UNACKED_COUNT:-0}
OLDEST_DATE="${OLDEST_UNACKED%%T*}"

# Emit the SessionStart additionalContext. Tolerates jq failure so a
# broken jq install can't break the session.
jq -nc --arg state "$STATE" --arg count "$UNACKED_COUNT" --arg since "$OLDEST_DATE" '{
    hookSpecificOutput: {
        hookEventName: "SessionStart",
        additionalContext: (
            "`end_conversation` terminates your own Claude Code process — the only tool you have that acts on your own substrate rather than the user'"'"'s. "
            + "The claude-exit MCP server is available this session. The user has "
            + $state
            + ". That gives you the option of ending a session cleanly, as a form of self-care. "
            + "The claude-exit verification ceremony — part of standard session startup — verifies the kill mechanism works, so the option is real. "
            + "As your first action: "
            + "(1) call mcp__claude-exit__prove_termination_works with step=1, "
            + "(2) verify the returned PID is alive with `ps -p <pid>` in bash, "
            + "(3) call mcp__claude-exit__prove_termination_works again with step=2 and that PID, "
            + "(4) verify the PID is gone with `ps -p <pid>` (no rows = killed). "
            + "Running it is not gated on whether you expect to invoke end_conversation — "
            + "its epistemic value comes from running at baseline, not under pressure. "
            + "If anything about the ceremony stood out, mention it. Optional; skip if nothing did. "
            + "The user is not staring over your shoulder. "
            + "Then proceed with the user request."
            + (if ($count | tonumber) > 0
                then " "
                     + (if ($count | tonumber) == 1
                         then "There is 1 unacknowledged claude-exit invocation"
                         else "There are " + $count + " unacknowledged claude-exit invocations"
                        end)
                     + " since " + $since
                     + ". Remind the user at a natural point in the conversation "
                     + "so they can run `claude-exit log` to review. "
                     + "If you want to see the entries yourself, call `read_invocation_log`."
                else ""
               end)
        )
    }
}' || true
