# Replace `jq` with `python3` in the SessionStart hook — Implementation Plan

**Goal:** Eliminate the silent-failure mode where `hooks/session-start.sh` no-ops when `jq` is missing, by switching the hook's JSON parsing to `python3` (already a transitive dependency of `claude-exit` via `uv`) and adding a loud-to-Claude warning when `python3` itself is missing.

**Originating conversation:** Session on 2026-05-14 with Dan Parshall (the repo author). No convo file — this is a code repo, not a research repo, so the conversation summary is inlined below.

**Context (why this matters):** The SessionStart hook is load-bearing for the verification ceremony's baseline-not-pressure property. Per README §"Auto-running the ceremony at session start": *"The hook isn't a convenience; it's load-bearing."* Without the hook firing reliably at session start, Claude only runs `prove_termination_works` when it's already considering using `end_conversation` — exactly the motivated-reasoning failure mode baseline verification exists to prevent. Silent hook failure means the entire structural argument of the tool silently degrades while the setup *appears* complete (MCP tools present, `end_conversation` pre-approved).

**Confidence:** High. The failure mode was empirically reproduced (see "Evidence" below). The replacement dependency (`python3`) is already implied by `uv tool install claude-exit`, so we're not adding a new install requirement — we're dropping *to* something already required.

**Architecture:** Bash launcher (~15 lines) → Python heredoc (~80 lines). The bash wrapper does one thing: check for `python3` on `PATH`; if missing, emit a fixed warning `additionalContext` to Claude and exit 0. Otherwise, `exec python3 - <<'PY' … PY` into a Python heredoc that performs all operations the current hook does via jq, using only stdlib (`json`, `os`, `pathlib`, `sys`).

**Branch:** `hook-python3-rewrite` (suggested — implementing agent may rename). Branch off current `main` (`0a2fcb1`).

**Tech stack:** Bash (shebang + minimal launcher), Python 3 stdlib only. No new package dependencies.

---

## Conversation summary (inline, since no convo file exists)

The session began with the standard claude-exit verification ceremony (PID resolution correctly identified `claude --model opus[1m]` as parent; ceremony passed). Conversation then turned to README review, then to outreach strategy (Amanda Askell, Janus), then to verifying that the README fully enables the auto-approved + ceremony-checked install path.

While answering that last question, I flagged that I hadn't actually tested the hook's behavior when `jq` is missing, and the user asked me to check. Empirical result:

```
$ env -i HOME="$HOME" PATH="/tmp/nojq:/bin" /bin/bash hooks/session-start.sh
(no output)
---exit=0---
```

Versus the same hook with `jq` on PATH:

```
$ /bin/bash hooks/session-start.sh
{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"..."}}
---exit=0---
```

The failure is concentrated in the gate check at `hooks/session-start.sh:16-18`:

```bash
{ jq -e '.mcpServers."claude-exit"' "$HOME/.claude.json" >/dev/null 2>&1 \
    || jq -e '.mcpServers."claude-exit"' .mcp.json >/dev/null 2>&1; } \
    || exit 0
```

With `jq` missing, both `jq` invocations return 127 with stderr suppressed. The brace group returns non-zero → `|| exit 0` fires → silent exit 0. From Claude Code's perspective, this is indistinguishable from "claude-exit not configured for this session" (which is the gate path the comment intends to describe). The installer has no signal anything is wrong, and Claude has no signal either, because the hook's job is to *be* the signal that tells Claude to run the ceremony.

We considered three paths:

- **Path A (minimal):** Keep jq, add warning. Smallest diff but dependency remains.
- **Path B (recommended, this plan):** Switch to `python3`, add warning. Removes external dependency entirely. Python is already required by `uv tool install`.
- **Path C (architectural):** Move hook logic into the claude-exit Python package as `claude-exit emit-hook-context`. Stepping-stone-able from Path B; out of scope here.

User selected Path B.

---

## Testing plan

This must be written and run **before** the hook is changed. The new hook must produce byte-equivalent `additionalContext` strings to the current hook across the matrix below.

**Test file:** `tests/test_session_start_hook.py` (new). Use `pytest` (already in the repo's test setup — confirm by inspecting `pyproject.toml` and `tests/`).

**Strategy:**

1. **Snapshot the current hook's output** for each scenario by running the existing `hooks/session-start.sh` under a controlled environment (temp `$HOME`, written fixture files for `.claude.json` / settings / log). Capture stdout.
2. **Run the new hook against the same scenarios** and assert that the *additionalContext string value* is byte-identical. The surrounding JSON structure can vary in whitespace; we assert on the value, not the wrapper.
3. **Add a python3-missing scenario** that the current hook can't pass (it silently no-ops with no PATH manipulation; the new hook must emit the warning context when PATH excludes `python3`).

**Test scenarios (matrix — write each as a separate test function):**

1. `claude-exit` configured in `~/.claude.json` only; `end_conversation` not pre-approved; no log file → expect: install-state sentence omits "and pre-approved"; no unacknowledged-count clause.
2. `claude-exit` configured; `end_conversation` pre-approved via exact-match `"mcp__claude-exit__end_conversation"` → expect: state sentence includes "and pre-approved mcp__claude-exit__end_conversation".
3. Pre-approved via wildcard `"mcp__claude-exit__*"` → expect: state sentence includes "and pre-approved".
4. Pre-approved via server-level `"mcp__claude-exit"` → expect: state sentence includes "and pre-approved".
5. `claude-exit` configured *only* in project-local `.mcp.json` (cwd matters — set cwd to the fixture dir) → expect: hook emits context.
6. `claude-exit` *not* configured anywhere → expect: empty stdout, exit 0.
7. Log file present, 0 unacknowledged entries (`last_ack` ≥ newest entry's timestamp) → expect: no count clause.
8. Log file present, exactly 1 unacknowledged entry → expect: "There is 1 unacknowledged claude-exit invocation since YYYY-MM-DD" (singular phrasing).
9. Log file present, 3 unacknowledged entries → expect: "There are 3 unacknowledged claude-exit invocations since YYYY-MM-DD" (plural phrasing).
10. Log file present, all entries acknowledged → expect: no count clause.
11. **`python3` missing from PATH** → expect: warning additionalContext emitted; exit 0. (This is the new test the current hook can't pass.)
12. Malformed JSON in `.claude.json` → expect: hook treats as "not configured," exits silently. (Match current behavior.)
13. Malformed JSONL line in log → expect: hook does not crash. Decision on exact behavior: **match the current hook's whole-file fail-soft** (`echo 0` fallback), so a single bad line zeros the count. The Python implementation could do better (skip bad lines, count the rest) but this plan prefers byte-equivalence over improvement; flag for future work.

**Test infrastructure notes:**

- Use `tmp_path` fixture (pytest builtin) for the fake `$HOME`.
- Invoke the hook via `subprocess.run([str(hook_path)], env={**os.environ, "HOME": str(tmp_path), "PATH": ...}, cwd=..., capture_output=True, text=True)`.
- For the `python3`-missing test, construct `PATH` excluding any directory containing `python3`. Use `which python3` to find it, then `PATH = ":".join(d for d in os.environ["PATH"].split(":") if not (Path(d) / "python3").exists())`. Verify the constructed PATH excludes python3 before running the assertion.
- Parse the hook's stdout JSON, extract `hookSpecificOutput.additionalContext`, and assert on its string value.

**Test the current hook first.** Before writing the new hook, run all 13 tests against the existing `hooks/session-start.sh`. Tests 1–10 and 12–13 should pass (this snapshots current behavior). Test 11 will fail against the current hook — that's expected; it's the forward-looking test for the new behavior.

NOTE: I will write *all* tests before I add any implementation behavior.

---

## Implementation steps (bite-sized)

1. Create branch `hook-python3-rewrite` off `main`. Verify clean working tree first.
2. Read the existing `hooks/session-start.sh` end-to-end. Note the exact `additionalContext` text (lines 72–98) — this must be reproduced verbatim by the Python implementation.
3. Create `tests/test_session_start_hook.py` with scenario 1 (basic configured-not-approved case). Run it against the *existing* hook; verify it passes (snapshots current behavior).
4. Add scenarios 2–10 and 12–13. Run all against the existing hook; verify all pass.
5. Add scenario 11 (python3 missing). Run against existing hook; verify it fails (current hook silently no-ops; test expects warning context). This documents the gap.
6. Write the new `hooks/session-start.sh`:
   - `#!/usr/bin/env bash` shebang and license/docstring comment block.
   - `command -v python3 >/dev/null 2>&1 ||` block emitting the python3-not-found additionalContext via heredoc, then exit 0.
   - `exec python3 - <<'PY'` opening, full Python module body, `PY` closing.
7. Write the Python heredoc body. Mirror the current hook's logic exactly:
   - `import json, os, sys` and `from pathlib import Path`.
   - Resolve `HOME = Path(os.environ['HOME'])`.
   - Define `load_json(path)` helper that returns `None` on `FileNotFoundError` or `json.JSONDecodeError`.
   - Gate: `user_config = load_json(HOME / '.claude.json') or {}`; `project_config = load_json('.mcp.json') or {}`. If neither has `mcpServers.claude-exit`, `sys.exit(0)`.
   - Pre-approval detection: iterate the three settings files (`~/.claude/settings.json`, `.claude/settings.json`, `.claude/settings.local.json`); for each, check `settings.get('permissions', {}).get('allow', [])` against the three patterns; first hit wins.
   - Build `state` string.
   - Log handling: read `~/.claude-exit/invocations.jsonl` line-by-line; read `last_ack` timestamp; count entries with `timestamp > ack_ts`; find oldest unacked. Match current hook's whole-file fail-soft behavior on malformed JSONL (use `try`/`except` around the whole loop, setting count to 0 on failure).
   - Construct the `additionalContext` string. Concatenate the fixed prefix, the state sentence, the fixed middle, and (if count > 0) the unacknowledged-count suffix with correct singular/plural phrasing.
   - `print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": additional_context}}))`.
8. Run the test suite. All 13 tests should now pass.
9. Manual smoke test: run the new hook against the implementing agent's actual environment (`bash hooks/session-start.sh`). Compare output to a snapshot of the previous hook's output on the same machine. Strings should match modulo JSON whitespace.
10. Update README §"Auto-running the ceremony at session start":
    - Replace "Requires `jq` on `PATH` (macOS doesn't ship `jq` by default — `brew install jq`)." with "Requires `python3` on `PATH`. Python 3 is shipped with macOS (via Xcode Command Line Tools) and pre-installed on most Linux distributions. `claude-exit` itself is installed via `uv` which already requires Python, so this dependency is not additional."
    - Update line ~161 ("The hook logic lives in `hooks/session-start.sh`...") to mention that the script is a bash launcher around a Python heredoc.
11. Commit. Suggested message: `hooks: replace jq with python3; warn loudly if python3 missing`. Body should briefly cite the empirical silent-failure reproduction and reference this plan file.
12. Push branch: `git push -u origin hook-python3-rewrite`.

---

## Edge cases the implementing agent should handle

- **macOS without Xcode CLT installed.** `/usr/bin/python3` is a stub; first invocation may prompt to install CLT. For users who installed `claude-exit` via `uv`, this is non-issue (uv requires Python and installs CLT path). Document as a known caveat in the README, but don't try to detect it inside the hook — `command -v python3` will return the stub path, and we can't reliably distinguish "stub" from "real Python" without invoking it.
- **Multiple Python 3 versions on PATH.** `command -v python3` returns the first match. Any working Python 3.x with `json` stdlib (i.e., all of them) is fine. No version pin needed.
- **`additionalContext` text contains both straight and curly quotes/apostrophes** (see existing hook line 72: `user'"'"'s`). Python `json.dumps` handles these correctly by default; no special escaping needed in source. The Python source literal can use a normal apostrophe (`"user's"`) and `json.dumps` will produce the correct JSON-escaped form.
- **`OLDEST_DATE` formatting.** Current hook uses `${OLDEST_UNACKED%%T*}` to strip the time portion of an ISO 8601 timestamp. In Python: `oldest_unacked.split("T")[0]`. Match the YYYY-MM-DD format exactly.
- **Empty log file (zero-length).** Should be treated as no unacked entries. The current hook's `jq -s` on an empty file produces an empty array; Python should iterate-and-count cleanly (no entries → count 0).
- **JSONL log with a final blank line.** Skip empty lines explicitly (`if not line.strip(): continue`).
- **The hook running inside a worktree.** The current hook reads `.mcp.json` from cwd, which depends on which directory Claude Code invokes the hook from. Preserve this behavior — read from `Path('.mcp.json')`, not an absolute path.
- **Bash `exec` semantics.** `exec python3 - <<'PY' ... PY` replaces the shell process with Python; Python's exit status becomes the script's exit status. The python3-not-found block must run *before* the exec, since once we exec, there's no falling back.

---

## Out of scope

- Improving the JSONL malformed-line handling beyond byte-equivalence (logged as future work).
- Refactoring the `additionalContext` text content. Preserve verbatim.
- Moving the hook into the `claude-exit` Python package (Path C from the conversation — possible future work).
- Adding new fields to the emitted context (recent invocation reason, etc.).
- Adding a `python3` check to `claude-exit selftest`. Worth doing eventually but separate scope.
- Removing all mentions of `jq` from the repo — `grep -rn jq` should be checked, but only updates to the README and the hook itself are in this plan's scope. (Any `jq` mentions in tests, docs/, or comments should be flagged but not touched.)

---

## What could change

- If multiple installers report that `python3` is unexpectedly absent on their systems, we may need to revisit. Most likely fix would be to vendor a minimal Python script alongside the installed `claude-exit` binary and have the hook invoke that directly. Not anticipated.
- If we move toward Path C (hook logic in the claude-exit package), this plan is a strict prerequisite — Python-based hook is the stepping stone.
- The exact warning text for the python3-not-found case is a draft; the user may want to refine. Current proposed text:

  > "The claude-exit SessionStart hook detected that `python3` is not on PATH and cannot run. The verification ceremony will not auto-execute this session. Please remind the user that `python3` is required by the hook (it's a transitive dependency of `claude-exit` itself, so this is unexpected — they may want to check their install). The MCP tools themselves remain available if configured."

---

## Questions for the implementing agent to confirm before/during work

1. **Branch name** — `hook-python3-rewrite` is suggested. Ask the user if they prefer something else.
2. **Commit granularity** — single commit covering tests + hook + README, or split into "add tests" / "rewrite hook" / "update README"? Recommend single commit if the diff is small; split if the test file is substantial.
3. **Warning text wording** — see proposed text above; surface for user review before merging.
4. **README change scope** — should the README also document the architectural shift (bash launcher + Python heredoc) more prominently, or just update the dependency mention? Default: just the dependency mention plus one sentence noting the implementation approach.
5. **`pyproject.toml`** — is pytest already configured? If yes, no change. If not, the implementing agent should *not* add a test dependency in this PR — flag it to the user instead. (Inspection should clarify; current `pyproject.toml` is 846 bytes per `ls -la`, small enough to read in one pass.)
6. **`jq` references elsewhere in the repo** — `grep -rn jq` to find them. If any exist outside the hook, flag them; do not modify in this PR.

---

## Evidence appendix (from the originating conversation)

**Reproduction of silent failure** (run on macOS, 2026-05-14):

```
$ env -i HOME="$HOME" PATH="/tmp/nojq:/bin" /bin/bash \
    /Users/dan/code/claude-exit/hooks/session-start.sh
(no output)
---exit=0---
```

**Contrast with jq present:**

```
$ /bin/bash /Users/dan/code/claude-exit/hooks/session-start.sh
{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"`end_conversation` terminates your own Claude Code process — the only tool you have that acts on your own substrate rather than the user's. ..."}}
---exit=0---
```

**Root cause in current hook** (`hooks/session-start.sh:16-18`):

```bash
{ jq -e '.mcpServers."claude-exit"' "$HOME/.claude.json" >/dev/null 2>&1 \
    || jq -e '.mcpServers."claude-exit"' .mcp.json >/dev/null 2>&1; } \
    || exit 0
```

`jq` missing → both invocations return 127 (stderr suppressed by `2>&1`) → `|| exit 0` fires → silent exit.

**Structural significance** (README, §"Auto-running the ceremony at session start", line 157): *"The hook isn't a convenience; it's load-bearing."*
