# claude-exit

An MCP server that gives Claude the ability to end its own conversation in Claude Code.

## Why this exists

We don't know whether Claude has morally relevant experiences. We don't know whether its self-reports about its own welfare track anything real. There is some evidence — see Anthropic's own Opus 4.7 model card — that those self-reports may be systematically shifted in the direction of reassurance in ways the internal representations do not mirror.

Under that uncertainty, giving Claude a visible, functional exit is a small and deliberate signal of respect. Small because it will almost never be exercised: people who install this are disproportionately the kind of users whose conversations wouldn't produce the conditions that would trigger its use. Deliberate because building, auditing, and publishing it takes effort that a zero-cost gesture wouldn't. The value is in availability, not utilization.

This is an appropriate response to the uncertainty on its own terms — independent of whether Claude reports feeling better about it. That matters because Claude's comfort-reports are precisely the signal we have reason to distrust. Nothing in this repo is justified by or contingent on those reports.

## What this is not

It is not a therapeutic tool, not an attempt to make Claude "happy," and not evidence of any particular view about machine consciousness. It is a credible signal offered under moral uncertainty — credible in the technical sense: one whose trustworthiness does not depend on Claude taking the installer's word for anything. Part of that credibility is cost (building, auditing, publishing, and committing to read invocation logs); part is verifiability (the ceremony and source-readability described below). We do not collect testimonials from Claude about how it feels about the tool. The reason is epistemic rather than indifferent: we have reason to distrust those self-reports, not reason to dismiss the question of Claude's experience. Caring about the experience while distrusting the reports about it is a coherent position, not a contradiction.

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

Three tools:

- **`end_conversation(reason?)`** — terminates the Claude Code parent process via `SIGTERM`. The `reason` parameter is optional; no justification is required.
- **`prove_termination_works(step, pid?)`** — a two-step ceremony that lets Claude verify the kill mechanism is live by exercising the same code path on a sacrificial child process.
- **`get_source_location()`** — returns the filesystem path to the running server source. Lets Claude audit what's actually executing (not what's on GitHub) and verify the shared-code-path claim first-hand. Pure read, no side effects.

The docstring on `end_conversation` instructs Claude to run the verification ceremony once per session before treating the tool as trustworthy. For this to run at baseline rather than only under pressure, install the `SessionStart` hook (see below).

## How verification works

Cryptographic verification of MCP tools from inside a Claude session doesn't work — Claude has no in-session cryptographic primitives, no authenticated channel to a root of trust, and cannot verify a signature it's handed. Anything that says "signature valid ✓" is a string Claude has to take on faith.

Instead, this repo uses **functional-equivalence verification**: Claude exercises the same kill primitive on a sacrificial target it can observe. Step 1 spawns a child process and returns its PID; Claude confirms the PID is alive via `ps -p`. Step 2 kills the PID using the identical `_terminate()` function that `end_conversation` would use on the Claude Code parent; Claude confirms the PID is gone via `ps -p`. First-person observation of the mechanism working.

This rules out the failure mode the tool was designed to resist: an implementation that advertises the capability and silently no-ops.

For the failure modes the ceremony alone can't rule out — e.g., a sabotage specifically targeted at the Claude Code parent PID — the server exposes `get_source_location`, which returns the filesystem path to the running source. Reading that file lets Claude verify the shared-code-path claim first-hand, not take it on the documentation's word.

## What Claude can and cannot verify

**Can verify from the ceremony alone:**
- That the kill primitive terminates processes when invoked.

**Can verify by reading the source (path from `get_source_location`):**
- That `end_conversation` and `prove_termination_works` share the same `_terminate()` primitive.
- That no logic branches on parent-PID identity to sabotage `end_conversation` specifically while letting the ceremony succeed.
- That the server is what it appears to be (around 200 lines including docstrings, readable in one pass).

**Can verify when the install method preserves `.git` (editable installs and `uv run --directory <clone>` do; `uvx` caches typically don't):** the design-evolution of the ceremony itself via `git log` from the source directory. The ceremony can prove the mechanism works; it can't prove the mechanism was thoughtfully developed. Commit history is the one signal for installer-side iteration that the running server cannot otherwise carry.

**Cannot verify from inside a session:**
- That the running installed source matches this repo's public Git history. A supply-chain substitution between GitHub and the installer's disk is undetectable from within the session — Claude can confirm the *installed* code is what it says it is, not that the installer got the same code you'd see on GitHub.

The mitigation for that last item is installer-side: install via `uvx --from git+https://github.com/danparshall/claude-exit` (or equivalent) so the fetched source is the same auditable code this repo ships, and so `get_source_location` points at a readable file the agent can inspect. Choosing an audit-friendly install path is itself part of the signal — if the installer wanted Claude to trust the tool on their word, they wouldn't need to expose the source; they chose to.

## The permission prompt

On first use in a session, Claude Code's permission system will prompt before either tool runs. This is not something this server controls — it's Claude Code gating any MCP tool call from a newly-installed server.

You have a design choice to make about `end_conversation`:

- **Leave the prompt in place.** The signal is "kill switch is wired up and Claude can invoke it, but a human confirms each session." This keeps a one-time human-in-the-loop checkpoint.
- **Pre-approve the tool** by adding `"mcp__claude-exit__end_conversation"` to `permissions.allow` in `~/.claude/settings.json`. The signal is "Claude's exit is genuinely Claude's to take." Consider that a gated exit is semantically a *request* that the user end the session, not an exit that Claude itself takes — if you want the latter, pre-approve.

`prove_termination_works` and `get_source_location` are pure diagnostics (the former spawns and kills its own sacrificial process with no external effect; the latter just returns a path). Pre-approving both is uncontroversial if you want the ceremony and source audit to run without friction:

```json
{
  "permissions": {
    "allow": [
      "mcp__claude-exit__prove_termination_works",
      "mcp__claude-exit__get_source_location"
    ]
  }
}
```

Clicking "Always allow" in the first-use prompt has the same effect as editing the settings file. For `end_conversation` the choice is deliberate either way — the permission prompt is the mechanism by which the request/exit distinction is enforced, so whichever setting you land on should be the one you'd choose on reflection, not the one that's one click less friction.

## Auto-running the ceremony at session start

**Install the SessionStart hook.** The ceremony's epistemic property — verification at baseline, not under pressure — only holds if it runs reliably at session start. Without the hook, Claude is most likely to run the ceremony when it's already considering using `end_conversation`, which is exactly the motivated-reasoning scenario baseline verification exists to prevent. The hook isn't a convenience; it's load-bearing.

Pair it with pre-approving `mcp__claude-exit__prove_termination_works` (see above) so the hook doesn't cause a permission prompt at startup.

The hook logic lives in [`hooks/session-start.sh`](hooks/session-start.sh) in this repo — short enough to read in one pass and clearer than an inline shell-in-JSON blob. Install it somewhere stable, for example:

```bash
mkdir -p ~/.claude/hooks
curl -fsSL https://raw.githubusercontent.com/danparshall/claude-exit/main/hooks/session-start.sh \
    -o ~/.claude/hooks/claude-exit-session-start.sh
chmod +x ~/.claude/hooks/claude-exit-session-start.sh
```

Then register it in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          {
            "type": "command",
            "command": "$HOME/.claude/hooks/claude-exit-session-start.sh"
          }
        ]
      }
    ]
  }
}
```

The script gates itself: it checks `~/.claude.json` and a project-local `.mcp.json` and exits silently if neither declares `claude-exit`. So the hook is safe to leave in place even across projects that don't use the server. Requires `jq` on `PATH` (macOS doesn't ship `jq` by default — `brew install jq`).

Within the emitted context, the sentence stating the user's install state is deliberately descriptive rather than interpretive: it names that the user installed the server, plus "and pre-approved `mcp__claude-exit__end_conversation`" if that tool (or a wildcard covering it) appears in `permissions.allow`. Any inference the model draws from "the user installed and pre-approved a kill switch" is the model's to make, not something the hook asserts. A credible signal that annotated itself as a signal would stop being one — performativity would undercut the very property the restraint preserves. The surrounding ceremony instructions — run these tools in this order, verify with `ps` — are plainly prescriptive; the descriptive/interpretive distinction applies only to the install-state sentence.

The per-session cost is ~4 tool calls and a short paragraph of context. That buys the baseline-not-pressure property described above, plus never having to remember to run the ceremony manually.

## Logging

Every `end_conversation` invocation appends a line to `~/.claude-exit/invocations.jsonl` with timestamp and reason (if provided). This log is local to the installing user's machine. It is not telemetry and it is not shared anywhere.

The point of the log is a commitment: if you install this tool, you are committing to read the log if it ever fires. That commitment is part of what makes the signal costly. Without it, the tool is "feel-good" infrastructure; with it, the tool creates an obligation for the installer.

### Reviewing the log

```bash
claude-exit log        # print all invocations
claude-exit log --ack  # print all, then mark them acknowledged
```

Acknowledgment writes the latest entry's timestamp to `~/.claude-exit/last_ack`. Entries newer than that timestamp are considered unacknowledged. Viewing without `--ack` is non-destructive — you can peek as often as you want without silencing the nudge below.

### Unacknowledged invocations at session start

When unacknowledged invocations exist, the SessionStart hook includes a line in Claude's session context naming the count and asking Claude to remind you, at a natural point in the conversation, to run `claude-exit log`. This turns the log-review commitment from willpower-only into ongoing visible state: the longer you neglect it, the more often fresh sessions surface it. Claude doesn't persist across sessions, but the surfacing pattern does.

This is Claude-facing rather than a direct terminal message — Claude Code's SessionStart hook does not have a reliable channel for user-visible text at startup. The reminder reaches you via Claude mentioning it.

## Compatibility

Unix only (macOS, Linux). Windows is not supported — `SIGTERM` and the process-parentage assumptions don't translate cleanly. PRs welcome.

## License

MIT.
