# Port iso step 2 — SIGKILL backstop with PID-reuse guard

**Goal:** Replay iso commit `796344f` ("Add SIGKILL backstop with PID-reuse guard") onto canonical. Add a detached subprocess that SIGKILLs the target ~2s after SIGTERM lands **only if** the live process at that PID still matches a command-line snapshot taken at dispatch time. Closes the residual trust gap in the verification chain: previously the agent had to assume Claude Code exits on SIGTERM (empirically holds, but unverified). With the backstop, session termination is guaranteed barring kernel-level pathology — SIGKILL is uncatchable.

**Issue:** #7
**Overview:** [port-iso-overview.md](port-iso-overview.md)
**Source commit:** iso `796344f` — see `git -C /Users/dan/code/claude-iso show 796344f` for the canonical diff to replay
**Confidence:** High. The mechanism is well-tested in iso (+141 lines of tests on this commit). The fail-open fix for arming failure comes in step 4; this step's `_arm_sigkill_backstop` deliberately has no try/except around `subprocess.Popen`. **Do not pre-port the fix here** — the per-commit blame trail is part of the safety argument.

**Branch:** `port-iso-safety` (continues from step 1).

**Depends on:** step 1 (`_dispatch_terminate` exists; helper renames applied).

## Design decisions (agreed 2026-06-13 — do not relitigate)

1. **Embedded shell script, not a vendored file.** `_SIGKILL_BACKSTOP_SCRIPT` is a string constant in `server.py`. Preserves the single-file audit property: an agent verifying the kill path via `get_source_location()` reads exactly one Python file and sees the entire backstop, including its sleep duration, identity check, and SIGKILL call.
2. **PID-reuse guard is the load-bearing safety property.** Between SIGTERM landing and the backstop firing, the target could exit cleanly and the OS could recycle the PID. The command-line match (`ps -o command=`) prevents the backstop from SIGKILLing an innocent recycled-PID process. If the match fails, the backstop exits 0.
3. **`start_new_session=True` is load-bearing.** This MCP server typically dies moments after dispatch (Claude Code's exit closes our stdio). The backstop must outlive the server to fire. The new session detaches the subprocess from the server's process group, so the server's death does not propagate.
4. **No try/except around `Popen` yet.** The fail-closed bug + lying-log bug are surfaced and fixed in step 4 (`1933550`). Replay this step without the fix so the per-commit story is auditable.
5. **Backstop grace is `SIGKILL_BACKSTOP_GRACE_SECONDS = 2.0`**, passed via env var `BACKSTOP_GRACE`. Iso later (in step 3) discovered this should be `KILL_FLUSH_DELAY_SECONDS + SIGKILL_BACKSTOP_GRACE_SECONDS` to match its post-SIGTERM-anchored docstring; step 2 ports the not-yet-corrected version, step 3 ports the correction.

## What the script does (contract)

```sh
sleep "$BACKSTOP_GRACE"
current=$(ps -p "$BACKSTOP_PID" -o command= 2>/dev/null)
# Trim leading whitespace (ps column alignment).
while [ "${current# }" != "$current" ]; do current="${current# }"; done
if [ -z "$current" ]; then exit 0; fi             # already gone
if [ "$current" != "$BACKSTOP_EXPECTED" ]; then   # PID recycled to different process
    exit 0
fi
kill -9 "$BACKSTOP_PID"
```

Three env vars carry inputs: `BACKSTOP_PID`, `BACKSTOP_GRACE` (seconds), `BACKSTOP_EXPECTED` (the `ps -o command=` snapshot at dispatch time). Embedded as a string constant `_SIGKILL_BACKSTOP_SCRIPT` (use a raw triple-quoted string so the `${current# }` doesn't get pylint-mangled).

## Implementation steps

1. **Continuing on `port-iso-safety`** — step 1's commit is already on the branch.
2. **Constants** — add to `src/claude_exit/server.py` near `KILL_FLUSH_DELAY_SECONDS`:
   ```python
   SIGKILL_BACKSTOP_GRACE_SECONDS = 2.0
   ```
3. **Embedded script** — add `_SIGKILL_BACKSTOP_SCRIPT` as a module-level string constant. Use the iso source verbatim; the trim-whitespace dance is important for `ps` output portability across macOS / Linux.
4. **`_full_command_of(pid)`** — add a new helper that returns `ps -o command=` output (full command line + args), `None` on lookup failure. Distinct from `_command_of` (which returns just the basename via `ps -o comm=`). This is what the backstop snapshots for the PID-reuse guard.
5. **`_arm_sigkill_backstop(pid, expected_command)`** — add. Spawns the script via `subprocess.Popen` with:
   - `["sh", "-c", _SIGKILL_BACKSTOP_SCRIPT]`
   - env: `{**os.environ, "BACKSTOP_PID": str(pid), "BACKSTOP_GRACE": str(SIGKILL_BACKSTOP_GRACE_SECONDS), "BACKSTOP_EXPECTED": expected_command}`
   - `stdout=subprocess.DEVNULL`, `stderr=subprocess.DEVNULL`
   - `start_new_session=True`
   - **No try/except yet** — step 4 wraps this.
6. **Update `_dispatch_terminate`** — now does three things in order:
   - Snapshot full command: `expected_command = _full_command_of(pid) or ""`
   - Arm backstop: `_arm_sigkill_backstop(pid, expected_command)`
   - Schedule SIGTERM: `threading.Timer(KILL_FLUSH_DELAY_SECONDS, lambda: os.kill(pid, signum)).start()` (this is unchanged from step 1)
7. **Run `uv run pytest`** — existing tests must still pass (the SIGTERM Timer is still scheduled; the backstop is additive). The backstop tests are new — see below.

## Tests to port (from iso `796344f` `tests/test_server.py` +141 lines)

Five test groups in iso. Port all five:

1. **`_full_command_of` returns ps output, None on failure** — direct call, mocked `subprocess.run`.
2. **`_arm_sigkill_backstop` spawns detached subprocess with correct env vars** — mock `subprocess.Popen`, assert the call arguments include `BACKSTOP_PID`, `BACKSTOP_GRACE`, `BACKSTOP_EXPECTED`, and `start_new_session=True`.
3. **Embedded script kills on alive + command match** — spawn a real `sh -c "sleep 30"` child; arm the backstop against its PID with its `ps -o command=` snapshot; wait `BACKSTOP_GRACE + small buffer`; assert child is killed.
4. **Embedded script no-ops when target gone** — kill the child before the backstop fires; assert no error, exit 0.
5. **Embedded script no-ops on command mismatch** — spawn a child; pass a deliberately mismatched `BACKSTOP_EXPECTED` to the backstop; assert child is alive after `BACKSTOP_GRACE + small buffer` (PID-reuse defense). Then clean up the child.
6. **Command snapshot captured at dispatch time and propagated to backstop** — mock `_full_command_of` to return a sentinel string; call `_dispatch_terminate`; assert `_arm_sigkill_backstop` was called with that sentinel.

Test count after this step: **63 + ~14 = ~77 passing** (iso reports ~14 net-new tests in this commit; verify against `796344f`'s test diff).

## Commit message

Mirror the iso commit body. Suggested message:

```
Add SIGKILL backstop with PID-reuse guard

(use iso 796344f's body verbatim, with iso/canonical word
substitutions — "claude-iso" → "claude-exit" etc.)

Ported from claude-iso 796344f17.
```

## Version

Part of v1.1.0.
