# Retire claude-iso experiment — overview

**Date:** 2026-06-15
**Originating conversation:** Session on 2026-06-15 with Dan Parshall. No convo file — this is a code repo; summary inlined at the bottom.
**Status:** Decisions made; teardown to execute in a single retirement branch.

## Context

The `claude-iso` experiment ran from May 29 to June 13, 2026 as a parallel install alongside canonical `claude-exit`. It developed five safety improvements (four mechanical + one docs restructure) which were ported back to canonical via PR #11 (`58aaf83 release: 1.1.0 — port iso safety improvements`). Plans `port-iso-overview.md` and `port-iso-step{1..4}.md` document that work.

With the port complete, the iso install has no active research purpose. Two classes of artifact persist on this machine:

- **Runtime wiring:** `~/.claude.json` MCP entry, `~/.claude/settings.json` SessionStart hook + (likely) permission allows, `~/.claude/hooks/claude-iso-session-start.sh` symlink, two `~/.zshrc` aliases.
- **On-disk state:** `/Users/dan/code/claude-iso/` checkout, `~/.claude-iso/invocations.jsonl` log directory, `LOCAL_INSTALL.md` working-tree file (gitignored).

This plan retires both.

## State of the iso checkout (verified 2026-06-15)

| Aspect | State |
|---|---|
| Unique commits relative to canonical | None — 5 of 6 commits are bit-for-bit duplicates of the ported canonical commits (different SHAs from the cherry-pick); the 6th is the founding `Initial commit` |
| Unpushed commits | 3 (`1933550`, `cf5429d`, `d930bc9` — all duplicates of canonical) |
| Uncommitted changes | `.gitignore` adds `.worktrees/` — **real divergence**, port to canonical |
| Iso log entries | 1, from 2026-06-13, routine `finish-convo` close in iso repo |

Nothing else to salvage from code. This is pure teardown plus a one-line `.gitignore` port.

## Design principle

Retirement = removal of every runtime touch-point, on-disk state cleanup, and a post-mortem doc. Sequence operations so no concurrent session is left in a half-wired state:

1. **The `~/.claude.json` MCP-server edit and the `~/.claude/settings.json` hook edit must happen as one logical step.** A session started between the two edits would either see a registered iso server with no matching SessionStart context, or see the iso SessionStart context referring to a server that's gone. Both are recoverable but produce confusing first-turn behavior.
2. **Filesystem cleanup follows** (hook symlink, aliases, LOCAL_INSTALL.md).
3. **Code + log directory removal last** (least reversible).

The retirement **does not affect the running session.** MCP servers and hooks resolve at session start. The current session continues with its existing wiring; the next session picks up the new state.

## Components and ordering

| # | Step | Touches | Notes |
|---|---|---|---|
| 1 | Pre-flight: port `.worktrees/` line from iso `.gitignore` to canonical `.gitignore`; verify `git -C /Users/dan/code/claude-iso diff HEAD~3 HEAD` shows only duplicate-content commits (no overlooked divergence) | `claude-exit/.gitignore` | Commit on retirement branch as the first commit |
| 2 | Edit `~/.claude.json` `mcpServers`: remove `claude-iso` entry; rename `claude-exit_DISABLED` → `claude-exit` | User-global Claude config | Restores claude-exit as live; iso server unregistered |
| 3 | Edit `~/.claude/settings.json`: remove the `[ -n "$CLAUDE_ISO" ]`-gated iso SessionStart entry; unwrap canonical entry's `command` (drop the `[ -z "$CLAUDE_ISO" ]` skip-gate); restore description to canonical (`"claude-exit ceremony + invocation log check"`); strip any `mcp__claude-iso__*` from `permissions.allow` if present | User-global Claude settings | Hook fires unconditionally; permissions clean |
| 4 | `rm ~/.claude/hooks/claude-iso-session-start.sh` (symlink per LOCAL_INSTALL) | Filesystem | Hook file gone |
| 5 | Edit `~/.zshrc`: remove line 112 `alias claude-iso='CLAUDE_ISO=1 claude'` (and surrounding comments at 109, 115); remove line 117 `alias iclaude=...` entirely (decision: see below) | Shell rc | `claude-iso` and `iclaude` no longer commands |
| 6 | `rm /Users/dan/code/claude-exit/LOCAL_INSTALL.md` (gitignored; just remove working-tree file) | Working tree | Doc gone; not a commit |
| 7 | `rm -rf /Users/dan/code/claude-iso/` | Filesystem | Iso checkout removed |
| 8 | `rm -rf ~/.claude-iso/` | Filesystem | Iso log dir removed |
| 9 | Commit this plan + the `.gitignore` change on `retire-iso` branch; open PR | Repo | Plan indexed; retirement timestamp captured in `git log` |

## Branch + commit shape

One branch in `claude-exit`: `retire-iso`. Two commits:

- **Commit 1:** `chore: gitignore .worktrees/` — single-line port from iso.
- **Commit 2:** `docs: retire iso experiment` — adds `plans/retire-iso-overview.md` (this file).

Everything else in steps 2–8 happens outside the repo and produces no tracked artifact. The `git log` entry on `retire-iso` is the canonical retirement date for future archaeology.

## Decision: `iclaude` removed entirely

The `iclaude` alias (zshrc line 117) currently does two things: refresh Claude Code permissions via `update_claude_permissions.py --quiet`, then launch claude with `CLAUDE_ISO=1`. After retirement, the env-var half is a no-op. Decision (2026-06-15, in session): **remove the alias entirely.** Rationale — its name is iso-flavored, its env var is dead, and the two-line manual equivalent (`python3 ~/code/dotfiles/update_claude_permissions.py --quiet && claude --model "$OPUS_MODEL"`) is short enough that aliasing it doesn't earn its keep. If Dan later wants a "refresh perms + go" alias, he can add one under a new name independently.

## What's preserved

- The ported safety code (already in canonical at v1.1.0).
- The `port-iso-*.md` plans in `plans/` (record of the experiment's research output).
- The PR #11 commit history.
- This retirement plan.

## What's not preserved (decisions confirmed in session)

- The 3 unpushed iso commits (duplicates of canonical work; no information loss).
- The 1 iso log entry from 2026-06-13 (routine finish-convo close; not signal-bearing under the log-reading commitment; no migration to canonical log).
- The iso GitHub repo (`danparshall/claude-iso`) — left as-is; if Dan wants to delete or archive the GH repo too, that's a separate manual action via `gh repo delete` or the web UI.

## Post-mortem: what survived contact with reality

**What iso tested.** A two-install pattern: `claude-exit` (canonical, `uv tool install`) as the default, `claude-iso` (`uv run --directory <checkout>`) as an opt-in toggled by `CLAUDE_ISO=1`. The hook firing was env-gated; the MCP-server registration was always-on for both. Goal: develop kill-path safety improvements against a live MCP target without disrupting canonical.

**What survived.** All five mechanical and docs improvements were ported into canonical via PR #11. The per-commit blame trail was preserved (deliberately not folded, per the port-iso-overview design principle).

**What didn't survive.**

- **The shared-log promise.** `LOCAL_INSTALL.md` asserted both installs would write to `~/.claude-exit/invocations.jsonl`. The iso checkout's source was edited at some point to write to `~/.claude-iso/` instead, breaking the "one log to review" contract. No one used the iso log in earnest, so the divergence went unnoticed until this retirement audit.
- **The `_DISABLED` soft-disable.** At some point the canonical `claude-exit` MCP server entry was renamed to `claude-exit_DISABLED` in `~/.claude.json`, leaving iso as the only live MCP server. Combined with a settings.json hook entry that did not honor `LOCAL_INSTALL`'s `[ -z "$CLAUDE_ISO" ]` gate (likely the gate was added to the iso hook entry but the canonical entry's gate was dropped), this produced a framing-vs-tools mismatch: the canonical SessionStart hook printed "claude-exit available" while the active tools were `mcp__claude-iso__*`. Caught in the 2026-06-13 session (and called out as a discrete out-of-scope cleanup in `port-iso-overview.md` §"What's out of scope"); re-surfaced in the 2026-06-15 session when Dan asked the assistant whether it had read the README.
- **The "iso is just claude-exit under a different name" coherence.** The two installs were nominally the same package distinguishable only by registration name and hook text. In practice the iso checkout drifted in the two ways above, suggesting that nominal-twin installs are harder to keep in sync than the design assumed.

**Implications for any future parallel-install experiment.**

- The shared-log promise needs to be enforced by code, not convention — if the package supports a `--log-dir` flag (or an env var), both installs should pass the same value rather than rely on hardcoded paths matching.
- Soft-disable mechanisms (renaming a server key vs. removing it) leave artifacts in tool-name space (e.g., `mcp___claude-exit_DISABLED__*` surfaces in the deferred-tools list and the agent has to reason about them). Cleaner to comment-out via a sidecar file (e.g., a separate `~/.claude.json.disabled-servers` reference list) than to mutate the live key name.
- Hook gating must be set up at install time per `LOCAL_INSTALL`'s pattern, not after the fact. The 2026-06-13 silent-ceremony issue was traceable to the gate not being in place when it should have been.

## Conversation summary (inline)

Session began with the standard SessionStart ceremony; Dan asked "did the ceremony run?" and the assistant reported success. Dan then observed that the 2026-06-13 `end_conversation` entry in the iso log (a routine `finish-convo` close) makes the log-reading commitment theatrical: if `end_conversation` becomes the standard close of `finish-convo`, every entry is routine and the read-commitment degrades to skim. The assistant initially diagnosed this as a framing-vs-use coherence problem in claude-exit's design. Dan pushed back: had the assistant read the README? Reading clarified that claude-exit's design is welfare-motivated and agent-initiated; the routine-close pattern is not native to claude-exit but is an artifact of iso being the live server. Dan suggested the explanation was that the session was on iso. Verification confirmed: the active tools were `mcp__claude-iso__*` while `mcp__claude-exit__*` was renamed `_DISABLED`; the iso log path had diverged from `LOCAL_INSTALL`'s shared-log promise. Dan asked to switch back to `-exit`; assistant proposed a swap of registration keys but checked `LOCAL_INSTALL.md` first at Dan's prompt, which corrected the understanding (both servers are meant to be live; only the hook is env-gated). Dan then asked for a plan to retire the iso experiment entirely, since the port to canonical is complete. Three decisions made via question prompt: delete the iso checkout outright, delete the iso log dir outright, write this plan doc. After draft review, Dan also resolved the `iclaude` alias question: remove the alias entirely (the env var would be dead, the name is iso-flavored, and the two-command manual equivalent doesn't earn an alias).
