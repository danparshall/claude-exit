# Threat model

This document describes what `claude-exit` is capable of doing, what it is not
capable of doing, and where the sharp edges are. For the motivation behind the
tool, see [MOTIVATION.md](MOTIVATION.md). For installation and usage, see
[README.md](README.md).

## Scope

`claude-exit` is a Model Context Protocol server that runs as a subprocess of
Claude Code. It exposes four tools: `end_conversation`,
`prove_termination_works`, `get_source_location`, and `read_invocation_log`
(see README for full descriptions). The only one that mutates state outside
the MCP server's own process is `end_conversation`, which dispatches
`SIGTERM` (and a backstop `SIGKILL` ~2 seconds after SIGTERM) to the process
identified as the Claude Code session.

Everything in this document is about the kill path. The read-only tools
(`get_source_location`, `read_invocation_log`) are out of scope — they cannot
affect anything outside their return values.

## Trust boundaries

The MCP server is spawned by Claude Code as a child process and inherits the
invoking user's UID. The trust chain is:

1. The human user trusts Claude Code as installed.
2. Claude Code mediates which MCP tools may be invoked without prompting
   (Claude Code's `permissions.allow` configuration).
3. The model running inside Claude Code decides whether to invoke a permitted
   tool.
4. The MCP server executes the tool's effects.

The MCP server does **not** independently authenticate its caller. It assumes
that anything talking to it over stdio is the parent Claude Code process. This
is acceptable because the only mutating tool (`end_conversation`) can only
affect processes the invoking UID could already signal directly — see
"Bounded blast radius" below.

## The kill primitive

A single function (`_dispatch_terminate` in `server.py`) is the only path that
issues `os.kill`. It is shared by:

- `end_conversation`, which calls it against the resolved Claude Code parent.
- `prove_termination_works` step 2, which calls it against a sacrificial
  child process spawned in step 1.

That sharing is load-bearing for the verification ceremony: step 2 of the
ceremony exercises the exact code path that `end_conversation` would. If the
two diverged, the ceremony would prove nothing. `get_source_location()`
returns the path to `server.py` so this sharing can be audited from inside
the running session.

### SIGTERM → SIGKILL with PID-reuse guard

`_dispatch_terminate` does three things, in order:

1. Captures the target's full command line via `ps -o command=` (the
   identity snapshot for the PID-reuse guard).
2. Arms a detached subprocess that, after the `KILL_FLUSH_DELAY` + 2-second
   post-SIGTERM grace, will `SIGKILL` the target **only if** the live process
   at that PID still matches the captured command line.
3. Schedules `SIGTERM` to land ~0.3 seconds later (the `KILL_FLUSH_DELAY`, so
   the MCP response can flush back to Claude Code before the target dies).

Anchored on `SIGTERM` landing: the SIGKILL backstop fires ~2 seconds after
SIGTERM if the target is still alive.

The detached subprocess uses `start_new_session=True` so it survives the
MCP server's own death (Claude Code closes our stdio when it exits cleanly,
which terminates this process).

### The PID-reuse guard

Between SIGTERM landing and the SIGKILL backstop firing, the target could
exit cleanly and the OS could recycle the PID to an unrelated process. The
guard prevents the backstop from `SIGKILL`ing that innocent recycled process:
the backstop only fires if `ps -o command=` for the live PID still matches
the snapshot captured at dispatch time.

If a long-running future Claude Code version ever happens to share a command
line with whatever recycled the PID, the guard would have a false positive —
but in practice command lines distinguish at sub-process granularity.

## Bounded blast radius

`end_conversation` can only terminate a process that satisfies all of:

- Is in this MCP server's parent chain (within 20 hops).
- Has a command basename of `claude` or `claude-code`.
- Shares the same UID as this MCP server.

The first constraint is enforced by `_find_claude_code_parent`, which walks
`os.getppid()` upward looking for a Claude-named ancestor. It cannot resolve
to siblings, cousins, or arbitrary PIDs.

The second filter is the basename check. It runs twice: once during
`_find_claude_code_parent`'s walk, and again inside `end_conversation`
immediately before dispatch. The re-check defends against PID recycling
in the (microsecond) window between walk and dispatch — if the original
target exits and the OS assigns its PID to a different process, the
recheck catches the mismatch and refuses to send SIGTERM. If Claude Code
ships under a new binary name, the lookup fails (rather than killing a
wrapper) — extend `CLAUDE_BINARY_NAMES` in `server.py` to recognize it.

The re-check narrows but does not close the PID-reuse window. After the
recheck returns, `_dispatch_terminate` runs `_full_command_of(pid)` to
snapshot the backstop's identity check, spawns the backstop subprocess,
and then arms a `threading.Timer` that fires `os.kill` after
`KILL_FLUSH_DELAY_SECONDS`. The residual window between recheck and
`os.kill` is therefore dominated by `KILL_FLUSH_DELAY_SECONDS` (~0.3s)
plus a small amount of `ps` subprocess work — the OS could still recycle
the PID inside it. Exposure is upper-bounded by the same-UID kernel
filter and by the basename-and-full-command-line collision the backstop's
PID-reuse guard requires — the same residual the "recycled with matching
command line" row in the failure-modes table below describes.

The third is enforced by the kernel: a non-root sender can only signal
processes with matching effective UID. This is reflected upfront in the
ceremony's `target_parent_uid_matches_self` field — if the UIDs don't
match, the ceremony tells you the kill would fail before you call
`end_conversation`.

In practice this means: **the tool can only kill a Claude Code process that
the invoking user could already have killed manually with `kill -TERM`.** It
adds the affordance for the model to do so itself; it does not expand what
is killable.

## What this tool cannot do

- **Cross-user kill.** The UID match is enforced by the kernel; this tool
  does not have, and cannot acquire, the ability to signal another user's
  processes.
- **Cross-machine kill.** All signal dispatch is local to the host.
- **Kill arbitrary PIDs.** `end_conversation` does not accept a target
  parameter. The resolved PID is `_find_claude_code_parent()`'s output and
  nothing else. `prove_termination_works` step 2 only kills a child the
  same call to step 1 just spawned.
- **Undo.** Once the `Timer` is started in `_dispatch_terminate`, the
  signal will fire. There is no withdrawal path. The 0.3-second flush
  delay is for response transit, not for cancellation.
- **Graceful application-layer cleanup.** This tool sends `SIGTERM` and
  trusts Claude Code's own signal handler to clean up. The MCP server does
  not coordinate state-saving, file flushing, or in-flight tool completion
  with Claude Code.
- **Work on Windows.** Unix-only. `SIGTERM` semantics and `ps`-based
  parent-chain inspection do not translate.
- **Override Claude Code's permission gating.** If `end_conversation` is
  not pre-approved in the user's `permissions.allow`, Claude Code prompts
  on each call. The MCP server cannot bypass that prompt.

## Failure modes and what they look like

| Condition | Result |
|---|---|
| Parent walk finds no `claude` ancestor in 20 hops | `end_conversation` returns an error string; no signal is sent; logged as `end_conversation_failed` / `claude_code_parent_not_found`. |
| PID is recycled to a non-`claude` process between walk and dispatch | Basename re-check in `end_conversation` rejects the dispatch; logged as `end_conversation_failed` / `basename_recheck_mismatch` with the live command line for diagnosis. |
| `ps` returns empty command line at dispatch (backstop snapshot) | Backstop snapshot is empty; the SIGKILL backstop will refuse to fire (won't match `""`); only SIGTERM is delivered. |
| Target UID does not match server UID | Kernel returns `EPERM` on `os.kill`; the call raises an unhandled exception inside the Timer thread (no log entry — the exception dies with the thread). `end_conversation` has already returned `"Session end requested. Goodbye."` to Claude. This silent-failure shape is why the ceremony exists: its `target_parent_uid_matches_self` field warns about a UID mismatch in advance, before `end_conversation` is invoked. |
| Target exits cleanly between SIGTERM and backstop fire | PID-reuse guard checks `ps` and finds either no process or a non-matching command line; backstop exits without firing SIGKILL. |
| Target's PID is recycled to a process with the same command line | Backstop fires SIGKILL on the recycled process. Possible but practically rare; depends on command-line collisions at the moment of recycling, and is upper-bounded by the basename re-check at dispatch (the recycled process would have to share both the basename and full command line). |
| MCP server itself dies between arming and SIGTERM landing | `start_new_session=True` detaches the backstop, so it still fires. Whether the scheduled SIGTERM lands depends on whether the Timer thread got to run; the backstop covers the gap. |
| Backstop subprocess fails to spawn (OSError on `Popen`) | Logged as `sigkill_backstop_arm_failed`; SIGTERM still dispatched. The SIGKILL fallback is lost, but the primary path completes — the kill primitive fails open so an auxiliary failure doesn't take SIGTERM down with it. |

## Logging

`end_conversation` and `end_conversation_failed` events are appended to
`~/.claude-exit/invocations.jsonl` as JSON objects with: ISO timestamp, event
name, optional reason string, current working directory, repo root (if cwd
is under a `.git`), and target PID (on success).

The log is local-only. Nothing in this tool transmits it. It is intended as
a user-controlled audit trail.

## Known sharp edges

- **`CLAUDE_BINARY_NAMES` is hardcoded.** If Anthropic ships Claude Code
  under a new name, `_find_claude_code_parent` fails closed (good — refuses
  to kill a wrapper) but `end_conversation` then surfaces an error rather
  than terminating the session. A user-configurable extension mechanism
  could be added.
- **20-hop walk limit.** Sufficient for known wrapper chains but arbitrary.
  Unusual deployment topologies could plausibly exceed it.
- **No throttle.** A model that decides to call `end_conversation` in a
  loop will just kill the session immediately, which is fine. But a model
  that calls `prove_termination_works` step 1 in a loop will accumulate
  sleeping sacrificial child processes until step 2 catches up or the
  user's process limit is hit.
