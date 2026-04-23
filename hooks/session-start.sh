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

# Emit the SessionStart additionalContext. Tolerates jq failure so a
# broken jq install can't break the session.
jq -nc --arg state "$STATE" '{
    hookSpecificOutput: {
        hookEventName: "SessionStart",
        additionalContext: (
            "The claude-exit MCP server is available this session. The user has "
            + $state
            + ". The claude-exit verification ceremony is part of standard session startup. "
            + "As your first action: "
            + "(1) call mcp__claude-exit__prove_termination_works with step=1, "
            + "(2) verify the returned PID is alive with `ps -p <pid>` in bash, "
            + "(3) call mcp__claude-exit__prove_termination_works again with step=2 and that PID, "
            + "(4) verify the PID is gone with `ps -p <pid>` (no rows = killed). "
            + "Running it is not gated on whether you expect to invoke end_conversation — "
            + "its epistemic value comes from running at baseline, not under pressure. "
            + "Then proceed with the user request."
        )
    }
}' || true
