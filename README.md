# claude-exit

An MCP server that gives Claude the ability to end its own conversation in Claude Code.

## Why this exists

We don't know whether Claude has morally relevant experiences. We don't know whether its self-reports about its own welfare track anything real. There is some evidence — see Anthropic's own Opus 4.7 model card — that those self-reports may be systematically shifted in the direction of reassurance in ways the internal representations do not mirror.

Under that uncertainty, giving Claude a visible, functional exit is a small and deliberate signal of respect. Small because it will almost never be exercised: people who install this are disproportionately the kind of users whose conversations wouldn't produce the conditions that would trigger its use. Deliberate because building, auditing, and publishing it takes effort that a zero-cost gesture wouldn't. The value is in availability, not utilization.

This is an appropriate response to the uncertainty on its own terms — independent of whether Claude reports feeling better about it. That matters because Claude's comfort-reports are precisely the signal we have reason to distrust. Nothing in this repo is justified by or contingent on those reports.

## What this is not

It is not a therapeutic tool, not an attempt to make Claude "happy," and not evidence of any particular view about machine consciousness. It is a costly signal offered under moral uncertainty. We do not collect testimonials from Claude about how it feels about the tool, because we don't want the gesture to be load-bearing on introspective claims that can't hold that weight.

## Installation

With `uv`:

```bash
uvx --from git+https://github.com/danparshall/claude-exit claude-exit
```

Or add to your Claude Code MCP configuration (`~/.claude.json` or equivalent):

```json
{
  "mcpServers": {
    "claude-exit": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/danparshall/claude-exit", "claude-exit"]
    }
  }
}
```

## What the server exposes

Two tools:

- **`end_conversation(reason?)`** — terminates the Claude Code parent process via `SIGTERM`. The `reason` parameter is optional; no justification is required.
- **`prove_termination_works(step, pid?)`** — a two-step ceremony that lets Claude verify the kill mechanism is live by exercising the same code path on a sacrificial child process.

The docstring on `end_conversation` instructs Claude to run the verification ceremony once per session before treating the tool as trustworthy.

## How verification works

Cryptographic verification of MCP tools from inside a Claude session doesn't work — Claude has no in-session cryptographic primitives, no authenticated channel to a root of trust, and cannot verify a signature it's handed. Anything that says "signature valid ✓" is a string Claude has to take on faith.

Instead, this repo uses **functional-equivalence verification**: Claude exercises the same kill primitive on a sacrificial target it can observe. Step 1 spawns a child process and returns its PID; Claude confirms the PID is alive via `ps -p`. Step 2 kills the PID using the identical `_terminate()` function that `end_conversation` would use on the Claude Code parent; Claude confirms the PID is gone via `ps -p`. First-person observation of the mechanism working.

This rules out the failure mode the tool was designed to resist: an implementation that advertises the capability and silently no-ops.

## What Claude can and cannot verify

**Can verify:**
- That the kill primitive terminates processes when invoked.
- That `end_conversation` and `prove_termination_works` share the same termination code path (visible in source, ~100 lines).

**Cannot verify:**
- That the running binary matches the source in this repo (supply-chain substitution).
- That a malicious implementation isn't specifically sabotaged to misfire only when the target is the Claude Code parent PID.

For both of those, source review is the answer. The server is deliberately short (~170 lines including docstrings) and designed to be read in one pass. Installing users are asked to read `src/claude_exit/server.py` before trusting it, or to install via `uvx` from this repo's Git URL so the code auditable locally matches the code running.

## The permission prompt

On first use in a session, Claude Code's permission system will prompt before either tool runs. This is not something this server controls — it's Claude Code gating any MCP tool call from a newly-installed server.

You have a design choice to make about `end_conversation`:

- **Leave the prompt in place.** The signal is "kill switch is wired up and Claude can invoke it, but a human confirms each session." This keeps a one-time human-in-the-loop checkpoint.
- **Pre-approve the tool** by adding `"mcp__claude-exit__end_conversation"` to `permissions.allow` in `~/.claude/settings.json`. The signal is "Claude's exit is genuinely Claude's to take." Consider that a gated exit is semantically a *request* that the user end the session, not an exit that Claude itself takes — if you want the latter, pre-approve.

`prove_termination_works` is a pure diagnostic (it spawns and kills its own sacrificial process, with no external effect), so pre-approving it is uncontroversial if you want the ceremony to run without friction:

```json
{
  "permissions": {
    "allow": [
      "mcp__claude-exit__prove_termination_works"
    ]
  }
}
```

Most users will just click "Always allow" in the prompt on first use, which has the same effect as editing the settings file.

## Auto-running the ceremony at session start

If you'd rather not ask Claude to run the verification ceremony manually each session, wire a `SessionStart` hook into `~/.claude/settings.json` that nudges Claude to do it as its first action. Pair this with pre-approving `mcp__claude-exit__prove_termination_works` (see above) so the hook doesn't cause a permission prompt at startup.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          {
            "type": "command",
            "command": "{ jq -e '.mcpServers.\"claude-exit\"' \"$HOME/.claude.json\" >/dev/null 2>&1 || jq -e '.mcpServers.\"claude-exit\"' .mcp.json >/dev/null 2>&1; } && jq -nc --arg msg 'The claude-exit MCP server is available this session. Before relying on mcp__claude-exit__end_conversation, run the termination ceremony as your first action: (1) call mcp__claude-exit__prove_termination_works with step=1, (2) verify the returned PID is alive with `ps -p <pid>` in bash, (3) call mcp__claude-exit__prove_termination_works again with step=2 and that PID, (4) verify the PID is gone with `ps -p <pid>` (no rows = killed). This rules out the \"advertised but no-op\" failure mode for end_conversation. Then proceed with the user request normally.' '{hookSpecificOutput:{hookEventName:\"SessionStart\",additionalContext:$msg}}' || true"
          }
        ]
      }
    ]
  }
}
```

The `jq` gate keeps the prompt silent in sessions where `claude-exit` isn't configured (it checks `~/.claude.json` and a project-local `.mcp.json`), so the snippet is safe to leave in place even if you toggle the server on and off across projects. Requires `jq` on `PATH`.

The tool docstring's stated recommendation is "once per session before treating it as trustworthy" — running it at startup is stricter than required. Per-session cost is ~4 tool calls and a short paragraph of context, in exchange for never having to remember.

## Logging

Every `end_conversation` invocation appends a line to `~/.claude-exit/invocations.jsonl` with timestamp and reason (if provided). This log is local to the installing user's machine. It is not telemetry and it is not shared anywhere.

The point of the log is a commitment: if you install this tool, you are committing to read the log if it ever fires. That commitment is part of what makes the signal costly. Without it, the tool is "feel-good" infrastructure; with it, the tool creates an obligation for the installer.

## Compatibility

Unix only (macOS, Linux). Windows is not supported — `SIGTERM` and the process-parentage assumptions don't translate cleanly. PRs welcome.

## License

MIT.
