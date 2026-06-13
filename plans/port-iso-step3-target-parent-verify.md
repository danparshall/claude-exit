# Port iso step 3 — target_parent verification inline + backstop grace fix

**Goal:** Replay iso commit `d930bc9` ("Inline target_parent verification in step=1; fix backstop grace timing") onto canonical. Two distinct changes bundled:

1. **Verification inline.** `prove_termination_works(step=1)` now returns `target_parent_command`, `target_parent_uid_matches_self`, and a verification summary, so the ceremony no longer requires a follow-up `ps` call to confirm the resolved PID is `claude`. The SessionStart hook is updated to direct the agent at the new verification field.
2. **Backstop grace timing fix.** `_arm_sigkill_backstop` now passes `BACKSTOP_GRACE = KILL_FLUSH_DELAY_SECONDS + SIGKILL_BACKSTOP_GRACE_SECONDS` so the constant matches its docstring: the backstop sleeps from arm time, but SIGTERM lands one flush-delay later, so the effective post-SIGTERM grace was 1.7s instead of the advertised 2.0s.

**Issue:** #8
**Overview:** [port-iso-overview.md](port-iso-overview.md)
**Source commit:** iso `d930bc9` — see `git -C /Users/dan/code/claude-iso show d930bc9` for the canonical diff to replay
**Confidence:** High. Both changes are localized; tests cover both (128 lines in `test_server.py`, 16 lines in `test_hook.py`).

**Branch:** `port-iso-safety` (continues from step 2).

**Depends on:** step 2 (`_arm_sigkill_backstop` exists; backstop env vars wired).

## Design decisions (agreed 2026-06-13 — do not relitigate)

1. **Inline verification, not a separate tool.** The ceremony's value is verifying the kill path *at session start*, before the agent needs `end_conversation`. Reducing it to one tool call (step=1) instead of step=1 + bash `ps` + check basename + check UID is a real ergonomic win — and the agent reading the JSON cannot skip the verification fields the way it could skip a bash check.
2. **UID match is a kernel-enforced safety property.** A non-root sender can only signal processes with matching effective UID. Surfacing `target_parent_uid_matches_self: True` in step=1 lets the agent know the kill *would* be permitted before it tries. If the UID doesn't match, the ceremony tells you the kill would fail before you call `end_conversation`.
3. **Update the SessionStart hook** so it directs the agent at the new verification fields. Iso's hook adds language pointing the agent at `target_parent_command`, `target_parent_uid_matches_self` as step 3 of the ceremony (was: bash `ps -p <pid>` to confirm). Canonical's hook (`hooks/session-start.sh`) needs the same update — the additionalContext text lives in the Python heredoc.
4. **Backstop grace fix is small but matters.** Before: 2.0s of post-SIGTERM grace was advertised but only 1.7s was actually delivered (the script slept from arm time, not from SIGTERM time). After: arm-time grace is `flush + post-SIGTERM grace = 0.3 + 2.0 = 2.3s`, so post-SIGTERM grace is the advertised 2.0s. Document this in the backstop's docstring.

## Implementation steps

### Part A — `_uid_of` helper + step=1 verification fields

1. **Continuing on `port-iso-safety`.**
2. **`_uid_of(pid)`** — add a new helper in `src/claude_exit/server.py`. Returns the effective UID of `pid` via `ps -o uid=`, `None` on lookup failure or empty output, parse int and return.
3. **`prove_termination_works(step=1)` return shape** — add three fields to the returned dict:
   - `target_parent_command` — full command line of resolved Claude Code parent, via `_full_command_of(parent_pid)`.
   - `target_parent_uid_matches_self` — `bool` comparing `_uid_of(parent_pid) == os.geteuid()`.
   - Verification summary string explaining what the agent should check (preserve iso's text verbatim — it's a load-bearing piece of the ceremony's epistemic story).
4. **Tool docstring update** — `prove_termination_works`'s docstring should now mention these fields explicitly so an agent reading the source can audit what the ceremony returns. (The full cf5429d-style docstring rewrite stays in port-iso-docs; this step's docstring touch is minimal.)

### Part B — SessionStart hook update

1. **Edit `hooks/session-start.sh`** — within the embedded Python heredoc, update the additionalContext text. The ceremony instructions previously said something like:
   - "(2) verify the returned PID is alive with `ps -p <pid>` in bash"

   After this step:
   - "(2) verify the returned PID is the actual Claude Code process by reading `target_parent_command` from step=1's output — should be a path ending in `claude` or `claude-code`. Also check `target_parent_uid_matches_self` is `true`."

   See iso's `d930bc9` diff for the exact prose. Preserve the iso wording.

2. **Test the hook** — port the 16 lines from iso's `tests/test_hook.py` at this commit. The hook test asserts on the additionalContext string (per the existing comment in canonical's `session-start.sh`: "tests in tests/test_hook.py assert on the unmarshalled value"). Update the expected string to match the new ceremony language.

### Part C — Backstop grace fix

1. **Edit `_arm_sigkill_backstop`** — change the `BACKSTOP_GRACE` env value:
   ```python
   "BACKSTOP_GRACE": str(
       KILL_FLUSH_DELAY_SECONDS + SIGKILL_BACKSTOP_GRACE_SECONDS
   ),
   ```
2. **Update the docstring on `SIGKILL_BACKSTOP_GRACE_SECONDS`** to clarify that this is the grace *after SIGTERM lands*, not the total subprocess sleep. (Iso's text: "Must exceed KILL_FLUSH_DELAY_SECONDS plus a realistic graceful-shutdown window for Claude Code.")

### Part D — Tests

Port from iso `d930bc9`:
- `tests/test_server.py` +128 lines:
  - `_uid_of` returns int, None on failure, None on empty
  - `prove_termination_works(step=1)` includes `target_parent_command`, `target_parent_uid_matches_self`, verification summary fields
  - `target_parent_uid_matches_self` is `True` when UIDs match, `False` when they don't, `False` when `_uid_of` returns `None`
  - Backstop grace test: `_arm_sigkill_backstop` is called with `BACKSTOP_GRACE = KILL_FLUSH_DELAY_SECONDS + SIGKILL_BACKSTOP_GRACE_SECONDS` (assertion on the Popen env)
- `tests/test_hook.py` +16 lines:
  - Hook's additionalContext mentions `target_parent_command` and `target_parent_uid_matches_self` instead of `ps -p <pid>`.

### Run tests

`uv run pytest` — should show **~77 + ~14 = ~91 passing** (iso commit reports 128 + 16 lines added; ~14 net-new tests). Verify against `d930bc9`'s test diff for the exact count.

## Commit message

Mirror iso's body. Suggested message:

```
Inline target_parent verification in step=1; fix backstop grace timing

(use iso d930bc9's body verbatim, with iso/canonical word
substitutions — "claude-iso" → "claude-exit" etc.)

Ported from claude-iso d930bc9b6.
```

## Version

Part of v1.1.0.
