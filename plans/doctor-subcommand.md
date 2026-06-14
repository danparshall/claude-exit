# `claude-exit doctor` — one-shot health check — Implementation Plan

**Goal:** Give installers without settings-manager tooling (i.e., nearly everyone —
see overview: Dan's dotfiles flow covers *him* and shouldn't be a dependency of the
design) a single command that audits every artifact the consent architecture depends
on and prints what it found, with a fix line for anything missing.

**Issue:** #3
**Overview / design principles:** [consent-persistence-overview.md](consent-persistence-overview.md)
**Originating conversation:** 2026-06-12 session with Dan (summary in overview doc).
**Confidence:** High. Pure reads wrapping `checks.py` from the guard plan.

**Branch:** `doctor-subcommand` (suggested). Depends on `checks.py` from
#1 — implement after it, or land `checks.py` first if reordered.

## Behavior

`claude-exit doctor` — pure read, no writes, no network. Prints one line per check
(OK / MISSING / WARN / INFO) plus a fix line for anything actionable. Exit 0 if
nothing MISSING/WARN, else 1 (scriptable).

Checks, in dependency order:

1. **python3 on PATH** — hook and guard need it. (It's a transitive dep of the
   package itself, so MISSING is a broken-environment signal.)
2. **claude-exit binary resolvable** — `shutil.which` falling back to
   `~/.local/bin/claude-exit`; if absent, name the `uv tool update-shell` PATH gotcha
   from the README before suggesting reinstall.
3. **Registration** — `claude-exit` in `mcpServers` of `~/.claude.json` (and note a
   project-local `.mcp.json` if present in cwd). Corrupt `~/.claude.json` → WARN
   naming the corrupt-regenerate incident class. Mangled value (key present, no
   usable `command`) → WARN, per guard plan's validation.
4. **Permission state** — pre-approval of `mcp__claude-exit__end_conversation`
   (exact / wildcard / server-level, same keys as the hook) across the three settings
   files. **Neutral phrasing, INFO not WARN**: "gated (a human confirms each
   session)" vs. "pre-approved (the exit is Claude's to take)" — both are legitimate
   installs per the README's permission-prompt section; doctor reports which one is
   in effect so a silent flip is visible, it does not editorialize.
5. **Hook** — `~/.claude/hooks/claude-exit-session-start.sh` exists AND is referenced
   by a SessionStart entry in `~/.claude/settings.json`. File-present-but-unregistered
   and registered-but-file-missing get distinct messages (both have happened in the
   wild with managed settings files; README:190).
6. **Guard** — scheduling artifact present (plist / timer unit), and — authoritative,
   doctor-only — `launchctl print gui/$UID/io.claude-exit.guard` /
   `systemctl --user is-enabled claude-exit-guard.timer` actually confirms it's
   loaded. Also report last guard.log entry timestamp as a heartbeat ("guard last ran
   N hours ago") with WARN if the artifact exists but the log is stale > 24h.
7. **State dir** — `~/.claude-exit/` health: invocations.jsonl parseable (count bad
   lines), unacknowledged event count (merged stream, per guard plan), tombstone
   presence (INFO: "uninstall marker present — hook orphan warnings suppressed").

## Implementation steps

1. Extend `src/claude_exit/checks.py` (born in #1) with the
   settings.json-side predicates (4, 5) and the heartbeat read (6). Each check
   returns `(status, message, fix_line | None)` — doctor owns formatting, checks own
   facts, so the guard and future callers reuse them without dragging in CLI output.
2. `src/claude_exit/doctor.py` + `cli.py` wiring.
3. Tests (`tests/test_doctor.py`), tmp-HOME fixture: healthy-everything → exit 0;
   each check individually broken → its line + exit 1; corrupt config; mangled value;
   tombstone INFO; stale heartbeat WARN. Mock only the `launchctl`/`systemctl`
   subprocess boundary; assert on real file fixtures otherwise.
4. README: a short "Checking the install" section — defer prose to #4 but
   doctor ships documented (`claude-exit doctor` next to `selftest` in the install
   flow; selftest exercises the review loop, doctor audits the wiring — complementary,
   don't merge them).

## Non-goals (YAGNI, agreed)

- No `--fix` flag. Doctor diagnoses; the guard restores the one thing restoration is
  safe for; everything else gets a printed fix line for the human. An auto-fixing
  doctor would have to write to intent-owned files, which the design forbids.
- No version/update check (network), no telemetry, no continuous mode.

**Version:** part of v1.2.0 with #1 and #2. (v1.1.0 shipped the port-iso safety track instead; consent-persistence retargets to the next minor.)
