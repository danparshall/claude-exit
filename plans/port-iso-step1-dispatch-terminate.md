# Port iso step 1 — consolidate kill primitive into `_dispatch_terminate`

**Goal:** Replay iso commit `45dac04` ("Consolidate kill primitive into `_dispatch_terminate`") onto canonical. Pull the `threading.Timer` + `KILL_FLUSH_DELAY_SECONDS` flush-delay scheduling **into the primitive itself**, so `end_conversation` and `prove_termination_works(step=2)` go through *identical* code rather than two different wrappers around the same `os.kill`. The proof ceremony's "shared kill primitive" claim becomes load-bearing rather than aspirational.

**Issue:** #6
**Overview:** [port-iso-overview.md](port-iso-overview.md)
**Source commit:** iso `45dac04` — see `git -C /Users/dan/code/claude-iso show 45dac04` for the canonical diff to replay
**Confidence:** High. Pure refactor with test coverage in iso (~32 lines of test changes already done).

**Branch:** `port-iso-safety` (create from current `main` if not already created).

## Design decisions (agreed 2026-06-13 — do not relitigate)

1. **Replay the iso commit one-for-one** with reconciliation for canonical's pre-existing 1.0.2 changes. Don't fold subsequent iso commits in; each step is its own canonical commit.
2. **Preserve the helper renames** that landed with this commit in iso: `_process_parent` → `_parent_of`, `_process_command` → `_command_of`. Brevity, and keeps the iso/canonical diff smaller for subsequent step reviews.
3. **Port iso's `.claude/settings.json` pre-approval** (project-local) so contributors cloning canonical can dev without per-call permission prompts, matching iso's pattern. Iso's commit added the four `mcp__claude-iso__*` entries; canonical's will be the four `mcp__claude-exit__*` entries.
4. **Preserve iso's expanded module + tool docstrings** for the parts touched by this commit (the audit-chain language: `get_source_location` → read source → confirm). Don't pull in the cf5429d docstring restructure — that's a later step (port-iso-docs).

## Reconciliation with canonical state

| Iso change | Canonical state | Action |
|---|---|---|
| Introduce `KILL_FLUSH_DELAY_SECONDS = 0.3` | Already present (1.0.2) | No-op; keep existing constant |
| `CLAUDE_BINARY_NAMES` extension to include `claude-code` | Already present (1.0.2) | No-op; keep existing constant |
| `_process_parent` → `_parent_of` rename | Canonical has `_process_parent` | Apply rename; update all call sites in server.py and tests |
| `_process_command` → `_command_of` rename | Canonical has `_process_command` | Apply rename; update all call sites |
| `_terminate` → `_dispatch_terminate` + Timer/flush-delay | Canonical has `_terminate` as a one-liner `os.kill(pid, signum)` | Replace with iso's `_dispatch_terminate` body |
| `.claude/settings.json` (new file) | Canonical does not have `.claude/` | Create `.claude/settings.json` with the four `mcp__claude-exit__*` entries |
| Module docstring expansion | Canonical has shorter docstring | Apply iso's expanded module-level docstring **only for the audit-chain section**; defer the full cf5429d-style rewrite to port-iso-docs |

## Implementation steps

1. **Branch:** `git checkout -b port-iso-safety` from `main` (if not already on it).
2. **Renames first** — in `src/claude_exit/server.py`, rename `_process_parent` → `_parent_of` and `_process_command` → `_command_of`. Update all call sites in the same file. In `tests/test_server.py`, update test names and `@mock.patch` targets accordingly.
3. **`_dispatch_terminate`** — replace canonical's `_terminate`:
   - Remove the body `os.kill(pid, signum)`.
   - New body schedules SIGTERM via `threading.Timer(KILL_FLUSH_DELAY_SECONDS, lambda: os.kill(pid, signum)).start()`.
   - **Important:** at this step, `_dispatch_terminate` does NOT yet arm the SIGKILL backstop. That's step 2. Keep this step's diff focused.
4. **Update call sites:**
   - `end_conversation` previously called `_terminate(pid)` wrapped in its own Timer at the call site. Remove the call-site Timer wrapper; call `_dispatch_terminate(pid)` directly.
   - `prove_termination_works(step=2)` previously called `_terminate(pid)` directly. Replace with `_dispatch_terminate(pid)`. Both paths now go through identical code.
5. **`.claude/settings.json`** — create the file with this content (the four MCP tools the contributor flow needs):
   ```json
   {
     "permissions": {
       "allow": [
         "mcp__claude-exit__end_conversation",
         "mcp__claude-exit__prove_termination_works",
         "mcp__claude-exit__get_source_location",
         "mcp__claude-exit__read_invocation_log"
       ]
     }
   }
   ```
6. **Module/tool docstrings** — apply iso's expanded module-level docstring **only for the audit-chain language** (the paragraph that explains the `get_source_location → read source → confirm shared primitive` chain). Hold the full cf5429d-style docstring rewrite for port-iso-docs.
7. **Tests** — port the corresponding test changes from iso's `tests/test_server.py` at this commit (iso shows `tests/test_server.py | 32 ++++++------` — that's mostly renames). Specifically:
   - Update test names that referenced `_terminate` to `_dispatch_terminate`.
   - Update `@mock.patch("claude_exit.server._process_parent")` → `@mock.patch("claude_exit.server._parent_of")` and similarly for `_process_command`.
   - Verify that the existing test asserting `prove_termination_works(step=2)` kills the child still passes — it's now exercising the same `_dispatch_terminate` that `end_conversation` does, which is the entire point.
8. **Run `uv run pytest`** — must show 63 tests passing (canonical's current count; no new tests in this step).

## Tests

No net-new tests in this step (it's a refactor + renames). Verify the existing 63 tests still pass after the rename + Timer-pull-in. If a test was asserting on the wrapper-Timer behavior in `end_conversation`, update it to assert on `_dispatch_terminate`'s Timer instead.

## Commit message

Mirror the iso commit body. Suggested message:

```
Consolidate kill primitive into _dispatch_terminate

(use iso 45dac04's body verbatim, with the iso/canonical word
substitutions where necessary — "claude-iso" → "claude-exit" etc.)

Ported from claude-iso 45dac04ff.
```

## Version

Part of v1.1.0 with steps 2–5.
