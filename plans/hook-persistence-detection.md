# SessionStart hook: persistence detection — Implementation Plan

**Goal:** Stop the hook from being a canary that dies with the mine. Today
`hooks/session-start.sh` exits silently whenever the registration is absent
(`session-start.sh:59-66`), which is exactly the state a silent deregistration
produces — during the 2026-06-05→12 incident week, the hook said nothing. Add:
orphan-state detection, guard-presence check, surfacing of guard restorations, and
permission-state transition naming.

**Issue:** #2
**Overview / design principles:** [consent-persistence-overview.md](consent-persistence-overview.md)
**Originating conversation:** 2026-06-12 session with Dan (summary in overview doc).
**Confidence:** High. All checks are file reads the hook already knows how to do;
the one new write (state snapshot) is fail-soft.

**Branch:** `hook-persistence-detection` (suggested). Branch off `main` after the
guard plan lands if possible (restoration surfacing consumes guard.log; everything
else is independent).

## Constraint

The hook stays a **standalone bash launcher + stdlib-only Python heredoc**. It is
installed via curl to `~/.claude/hooks/` and cannot import the `claude_exit` package.
Minimal logic duplication with `checks.py` is accepted; keep the duplicated
predicates trivial (key-in-dict, file-exists).

## Changes to the Python body

### a. Orphan-state detection (replaces the silent gate)

Current gate: not registered in `~/.claude.json` or `.mcp.json` → `sys.exit(0)`.

New gate logic:

- Registered → proceed as today.
- Not registered AND `~/.claude-exit/` does not exist → exit 0 silently (true
  "never installed on this machine"; hook remains safe to leave enabled globally).
- Not registered AND `~/.claude-exit/` exists AND `~/.claude-exit/uninstalled`
  (tombstone) exists → exit 0 silently (deliberate uninstall).
- Not registered AND `~/.claude-exit/` exists AND no tombstone → **emit loud
  additionalContext**: claude-exit state exists on this machine but the server is not
  registered; the registration may have been silently dropped (this has happened —
  Claude Code regenerates a corrupt `~/.claude.json` without preserving MCP entries);
  tell the user; re-register with
  `claude mcp add --scope user claude-exit "$HOME/.local/bin/claude-exit"`; if the
  removal was deliberate, silence this with `touch ~/.claude-exit/uninstalled`.

Note: `load_json` already treats a *corrupt* `~/.claude.json` as "not registered" —
with the new gate that case now produces the loud message instead of silence, which
is correct (a corrupt config is precisely the incident's precondition).

### b. Guard-presence check (calm, one line)

Pure file-existence: macOS `~/Library/LaunchAgents/io.claude-exit.guard.plist`,
Linux `~/.config/systemd/user/claude-exit-guard.timer` (platform via `sys.platform`).
If registered but no guard artifact: append one calm sentence suggesting
`claude-exit guard --install`. Not loud — the guard is recommended, not mandatory.
No `launchctl`/`systemctl` invocation from the hook (cost + portability); `doctor`
does the authoritative scheduling check.

### c. Surfacing guard restorations

Free once the guard plan's log-merge lands: the existing unacknowledged-count block
reads merged events (invocations.jsonl + guard.log) newer than `last_ack`. Phrase
RESTORED events distinctly — "the guard restored the registration N time(s) since
YYYY-MM-DD" — because a restoration means a silent loss *happened* and the user
should know, not just ack.

### d. Permission-state transitions (the "exit silently became a request" case)

The hook already computes `approved` (pre-approval detection across the three
settings files). Detecting *degradation* needs memory of the previous state:

- After computing state, write `~/.claude-exit/last_state.json`:
  `{"registered": true, "approved": <bool>, "timestamp": <ISO-8601 UTC>}`.
  Fail-soft: any OSError on write is swallowed (hook must never crash a session).
- On the next run, if previous `approved` was true and current is false: append a
  neutral naming line — the exit has changed from "exit Claude takes" to "request a
  human gates"; if deliberate, fine; if not, here is the settings.json line to
  re-add. **Never restore** — settings.json is intent-owned (see overview).
- Symmetric upgrade (false→true) needs no callout; the state line already names it.

This is the hook's first write. Keep it last in the script so a write failure can't
affect context emission.

## Tests

`tests/test_hook.py` asserts emitted context verbatim — every message above must be
added to the expected-text fixtures. New cases: orphan (state dir, no registration,
no tombstone) → loud message; tombstone → silent; no state dir → silent; corrupt
`~/.claude.json` + state dir → loud message; guard artifact present/absent; approved
→ gated transition → naming line; first run (no last_state.json) → no transition
line; unwritable state dir → context still emitted.

**Version:** part of v1.2.0 with #1 and #3. (v1.1.0 shipped the port-iso safety track instead; consent-persistence retargets to the next minor.)

## Outcome

Implemented on branch `hook-persistence-detection`, commit `25376ff` (2026-07-17).
All four checks (a–d) landed in the Python heredoc of
[`hooks/session-start.sh`](../hooks/session-start.sh), with tests in
[`tests/test_hook.py`](../tests/test_hook.py). Deviations from the plan as written:

- **RESTORED surfacing (§c) resolved differently than the wording suggested.**
  The plan said "the existing unacknowledged-count block reads merged events";
  in the implementation, guard events do **not** inflate the
  "N unacknowledged claude-exit invocations" count. RESTORED events get their
  own distinct sentence (with proper pluralization: "1 time" / "N times");
  WARN/ERROR/SKIPPED are left to `claude-exit log` and `doctor`. Tests pin this
  divergence from `cli.unacknowledged_count`'s merge semantics.
- **Additions beyond the plan**, all from nori-code-reviewer findings:
  - Emitted messages explicitly mark guard install and settings.json
    re-approval as the **user's** actions, so a fresh session agent can't be
    induced to self-restore pre-approval of its own kill switch.
  - The tombstone (`~/.claude-exit/uninstalled`) is cleared whenever the
    registration is live, so a reinstall re-arms orphan detection (a lingering
    tombstone would have disarmed it forever).
  - All new reads catch `ValueError` too (`UnicodeDecodeError`/`JSONDecodeError`) —
    the never-crash constraint is verified against binary garbage.
  - `last_state.json` is written atomically (tmp + `os.replace`) because
    multiple sessions start concurrently.
  - The orphan message's re-register command carries a hedge for
    non-`uv tool install` layouts.
- **Version held as planned:** no version bump; `pyproject.toml` stays 1.2.0
  (unreleased — no v1.2.0 tag yet).
