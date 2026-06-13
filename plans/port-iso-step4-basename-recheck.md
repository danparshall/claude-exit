# Port iso step 4 — basename re-check before dispatch + fail-open backstop arming

**Goal:** Replay iso commit `1933550` ("Fix backstop fail-closed and PID-reuse race in end_conversation") onto canonical. Two distinct bug fixes, both surfaced by prior review of iso's threat-model claims against its code:

1. **Fail-open backstop arming.** Wrap `_arm_sigkill_backstop`'s `subprocess.Popen` in `try/except OSError`. If arming fails (RLIMIT_NPROC, FD exhaustion, etc.), log `sigkill_backstop_arm_failed` and continue rather than propagating the exception. Before this fix: arming failure propagated through `_dispatch_terminate` *before* `Timer.start()` scheduled SIGTERM, and through `end_conversation`, which had already written the success log entry — fail-closed with a lying log. THREAT_MODEL claimed fail-open; code disagreed.
2. **Basename re-check before dispatch.** Add a basename re-check in `end_conversation` against the live process right before calling `_dispatch_terminate`. Refuse with `end_conversation_failed/basename_recheck_mismatch` on miss. Before this fix: PIDs could recycle between `_find_claude_code_parent`'s walk (in `end_conversation`) and the kill snapshot (in `_dispatch_terminate`); if the resolved PID exited and got recycled to a non-`claude` process, SIGTERM and SIGKILL would both land on the recycled process; the backstop's PID-reuse guard would pass against its own (post-recycle) snapshot, providing no defense.

**Issue:** #9
**Overview:** [port-iso-overview.md](port-iso-overview.md)
**Source commit:** iso `1933550` — see `git -C /Users/dan/code/claude-iso show 1933550` for the canonical diff to replay
**Confidence:** High. Both bugs are well-characterized in the iso commit body; tests cover both (77 lines, 3 new tests + 1 updated).

**Branch:** `port-iso-safety` (continues from step 3).

**Depends on:** step 2 (`_arm_sigkill_backstop` to wrap). Logically independent of step 3, but the per-commit blame trail wants step 3's commit between steps 2 and 4 in canonical's history.

## Design decisions (agreed 2026-06-13 — do not relitigate)

1. **Basename re-check lives in `end_conversation`, not `_dispatch_terminate`.** The kill primitive must remain usable for `prove_termination_works(step=2)`, which kills a sacrificial sleep child whose basename is `sh` or `sleep`, not `claude`. The basename constraint is `end_conversation`'s contract, not the kill primitive's. (Iso commit body, paragraph 2: "The check lives in end_conversation rather than _dispatch_terminate because prove_termination_works(step=2) needs the primitive to kill arbitrary PIDs.")
2. **Fail-open on arming failure, not fail-closed.** The kill primitive's purpose is to deliver SIGTERM; a non-essential auxiliary failing should not take the primary path down. Logging `sigkill_backstop_arm_failed` preserves the diagnostic signal. THREAT_MODEL.md's failure-modes table will reflect this in port-iso-docs (step 5).
3. **The re-check is logged on miss, not silently dropped.** `end_conversation_failed/basename_recheck_mismatch` with the live command line for diagnosis. Match the iso log shape so the invocation-log audit tooling sees the same event names.

## Implementation steps

### Part A — Fail-open arming

1. **Edit `_arm_sigkill_backstop`** in `src/claude_exit/server.py`. Wrap the `subprocess.Popen(...)` call in `try/except OSError as e:`:
   ```python
   try:
       subprocess.Popen(
           ["sh", "-c", _SIGKILL_BACKSTOP_SCRIPT],
           env={...},
           stdout=subprocess.DEVNULL,
           stderr=subprocess.DEVNULL,
           start_new_session=True,
       )
   except OSError as e:
       _log({
           "event": "sigkill_backstop_arm_failed",
           "target_pid": pid,
           "error": str(e),
       })
   ```
2. **Update docstring** on `_arm_sigkill_backstop` to state the fail-open contract. Iso's text: "Fails open: if spawning the backstop raises (e.g., RLIMIT_NPROC, FD exhaustion), the failure is logged and the caller continues."

### Part B — Basename re-check before dispatch

1. **Edit `end_conversation`** in `src/claude_exit/server.py`. Immediately before the call to `_dispatch_terminate(parent_pid)`, add:
   ```python
   # Defend against PID recycling between _find_claude_code_parent's walk
   # and _dispatch_terminate's snapshot. If the resolved PID exited and
   # was recycled to a non-claude process, SIGTERM would land on the
   # innocent recycled process. The re-check refuses dispatch on mismatch.
   live_command = _command_of(parent_pid)
   if not _is_claude_code(live_command or ""):
       _log({
           "event": "end_conversation_failed",
           "reason": "basename_recheck_mismatch",
           "target_pid": parent_pid,
           "live_command": live_command,
       })
       return f"End conversation failed: PID {parent_pid} no longer a claude process (live command: {live_command!r})."
   ```
   Adjust the exact log fields + return string to match iso's `1933550` verbatim.
2. **`_is_claude_code`** is already present in canonical from 1.0.2 (`_is_claude_code(command)` checking basename ∈ `CLAUDE_BINARY_NAMES`). Reuse it.

### Part C — Tests

Port from iso `1933550` `tests/test_server.py` (+77 lines, 3 new tests + 1 updated):

1. **`test_arm_sigkill_backstop_logs_failure_on_oserror`** — patch `subprocess.Popen` to raise `OSError("EMFILE")`; call `_arm_sigkill_backstop`; assert `sigkill_backstop_arm_failed` written to log with `target_pid` and `error` fields.
2. **`test_dispatch_terminate_still_schedules_sigterm_when_arming_fails`** — patch `_arm_sigkill_backstop` to log-and-no-op (or patch `subprocess.Popen` to raise); call `_dispatch_terminate(pid)`; assert `threading.Timer` was still constructed with `KILL_FLUSH_DELAY_SECONDS` and started (i.e., SIGTERM is still scheduled even though the backstop failed to arm).
3. **`test_end_conversation_refuses_on_basename_recheck_mismatch`** — patch `_find_claude_code_parent` to return a PID; patch `_command_of` (the live recheck) to return a non-claude basename (e.g., `bash`); call `end_conversation`; assert no kill is scheduled, return string mentions the basename mismatch, log entry has `basename_recheck_mismatch` reason.
4. **Update existing test** that exercises the `end_conversation` happy path — now needs to stub `_command_of` to return `"claude"` so the re-check passes. Iso commit body specifies: "one existing test updated to stub `_command_of` for the new positive re-check path."

### Run tests

`uv run pytest` — should show **~91 + 3 = ~94 passing** (canonical's count at the end of step 4).

(Iso reports "78 passing" at this commit. The delta vs canonical's ~94 reflects canonical's pre-existing tests that iso may not have — the absolute counts will diverge; what matters is +3 new from this commit relative to step 3's count.)

## Commit message

Mirror iso's body — it's an unusually careful body that explains both bugs, the test discipline, and the doc updates. Suggested message:

```
Fix backstop fail-closed and PID-reuse race in end_conversation

(use iso 1933550's body verbatim, with iso/canonical word
substitutions — "claude-iso" → "claude-exit" etc.)

The "Doc updates: THREAT_MODEL.md ..." paragraph in iso's body
refers to changes that are NOT part of this canonical commit
(they ride with port-iso-docs in step 5). When mirroring the
body for canonical's commit, drop that paragraph OR replace it
with: "Doc updates ride with the doc-restructure commit
(port-iso-docs / cf5429d)."

Ported from claude-iso 1933550b2.
```

## Version

Part of v1.1.0. Last mechanical commit before the docs port (step 5) and the release bump.
