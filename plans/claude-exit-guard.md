# `claude-exit guard` — registration watchdog — Implementation Plan

**Goal:** Ship the registration watchdog from `drop_of_water.pdf` Appendix A as a
packaged subcommand: `claude-exit guard` performs one check-and-restore pass over the
`claude-exit` entry in `~/.claude.json`; `claude-exit guard --install` /
`--uninstall` manage the OS-native hourly scheduling. Bounds silent registration loss
at one hour (vs. seven days in the 2026-06-05 incident).

**Issue:** #1
**Overview / design principles:** [consent-persistence-overview.md](consent-persistence-overview.md)
**Originating conversation:** 2026-06-12 session with Dan (summary in overview doc).
**Confidence:** High on the core restore logic (Appendix A was deployed in anger on
Kornai's machine and worked — "the repair and the document are products of the same
conversation"). Medium on scheduling details; verify launchd/systemd snippets on real
machines before release.

**Branch:** `guard-subcommand` (suggested). Branch off current `main`.

## Design decisions (agreed 2026-06-12 — do not relitigate)

1. **Restore the registration only.** The guard never writes to
   `~/.claude/settings.json` (intent-owned; see overview). It may *read* it to log
   hook-registration absence as a WARN in guard.log, but keep even that minimal.
2. **Packaged subcommand, not a standalone script.** The PDF ships a standalone
   dependency-free `.py` for people without the repo; we ship in-package
   (`src/claude_exit/guard.py`) with the scheduler invoking the installed binary
   (`~/.local/bin/claude-exit guard` by absolute path). Trade-off accepted: if the
   binary vanishes, the guard dies with it — correct when the removal is deliberate
   (revocation), and `doctor` catches the accidental case. One source of truth beats
   a second copy aging in `~/bin`.
3. **Unix only** (launchd + systemd user timers), matching the package's support
   statement. The PDF's "(all platforms)" §4.2 with a Windows branch contradicts the
   repo README ("Windows is not supported — SIGTERM and the process-parentage
   assumptions don't translate") — flag back to Kornai, do not implement.
4. **Tombstone:** if `~/.claude-exit/uninstalled` exists, the guard exits 0 without
   restoring. This is the deliberate-uninstall suppression shared with the hook.
5. **Revocation is a two-step** (remove guard, then registration) and the README must
   say so loudly — README work tracked in #4.

## Core logic (adapted from Appendix A; flaws fixed)

Appendix A baseline: resolve binary via `shutil.which` falling back to
`~/.local/bin/claude-exit`; load `~/.claude.json` (missing → `{}`, unparseable →
WARN and return 0, never touch a corrupt file); if `claude-exit` already in
`mcpServers` → return 0 (don't clobber edits); else insert
`{"command": <resolved path>}` and atomically replace via `tempfile.mkstemp` in the
same directory + `chmod 0600` + `os.replace`; log RESTORED.

Fixes on top:

- **Lost-update race:** `~/.claude.json` is rewritten constantly by Claude Code and
  is far more than MCP config (155KB on Dan's desktop). Read-modify-replace can drop
  a concurrent CC write. Mitigation: `stat` the file before reading; re-`stat`
  immediately before `os.replace`; if mtime/size changed, abort this pass (log
  `SKIPPED: config changed underfoot`) and let the next hourly run retry. Window
  shrinks from "whole pass" to "stat-to-replace," and the guard only writes at all
  when the registration is already missing.
- **Install-shape assumption:** restoration produces the `{"command": <binary>}`
  shape. Users on the README's `uvx` or `uv run --directory` shapes get
  `ERROR: claude-exit binary not found; reinstall: uv tool install claude-exit` in
  guard.log instead of a restoration. Acceptable; document that the guard assumes the
  `uv tool install` path.
- **Value validation:** Appendix A's `if NAME in servers: return 0` accepts a mangled
  value. Add: if the key exists but the value is not a dict with a truthy `command`,
  log `WARN: registration present but malformed; not touching it` (still never
  overwrite — a weird value might be a deliberate edit).
- **Surfacing:** every RESTORED/ERROR/WARN line goes to `~/.claude-exit/guard.log`
  (`<ISO-8601 UTC> <message>`). Extend `claude-exit log` to merge guard.log events
  with invocations.jsonl by timestamp, and let the existing `--ack` / `last_ack`
  mechanism cover both — one review loop, no second ack file. The hook then surfaces
  unacknowledged guard events for free (see hook plan).

## Scheduling

- `claude-exit guard --install`:
  - macOS: write `~/Library/LaunchAgents/io.claude-exit.guard.plist`
    (ProgramArguments = [<abs path to claude-exit>, "guard"], RunAtLoad=true,
    StartInterval=3600), then `launchctl bootstrap gui/$(id -u) <plist>`.
  - Linux: write `~/.config/systemd/user/claude-exit-guard.{service,timer}`
    (Type=oneshot; OnStartupSec=2min, OnUnitActiveSec=1h, Persistent=true), then
    `daemon-reload` + `enable --now claude-exit-guard.timer`. Mention
    `loginctl enable-linger` in output but don't run it (needs judgment about the
    user's setup).
  - Idempotent: re-running `--install` rewrites and re-bootstraps cleanly.
- `claude-exit guard --uninstall`: bootout/disable and remove the files it wrote.
  Print a reminder that uninstalling the *guard* does not remove the *registration*
  (and vice versa — the two-step).
- Plain `claude-exit guard`: single pass, exit 0 on healthy/restored/skipped, 1 on
  ERROR. This is also what doctor and tests exercise.

## Implementation steps

1. Introduce `src/claude_exit/checks.py` — shared pure-read predicates (registration
   present? value well-formed? binary resolvable? tombstone? guard scheduled?). Used
   here and by `doctor` (#3). The hook does NOT import this — it stays a
   standalone stdlib heredoc (installed via curl, no package access).
2. `src/claude_exit/guard.py` — core pass per above; entry wired into `cli.py`.
3. Scheduler install/uninstall in `guard.py` (platform-dispatched).
4. `claude-exit log` merge of guard.log events; `--ack` covers both streams.
5. Tests (`tests/test_guard.py`), all against a tmp-HOME fixture:
   missing registration → restored + logged; present → untouched; present-but-mangled
   → WARN, untouched; corrupt JSON → untouched + WARN; missing file → created with
   only our entry; tombstone → no-op; mtime-changed-underfoot → SKIPPED, file
   untouched; binary unresolvable → ERROR, no write; log merge + ack round-trip.
   Scheduling: generate-files logic unit-tested; actual launchctl/systemctl calls
   mocked (NEVER test just the mocks — the file-content assertions are the real
   tests; the subprocess calls are thin).
6. README: defer to #4, but the `--install` snippet belongs in the
   Installation section alongside the hook.

**Version:** part of v1.2.0 with #2 and #3. (v1.1.0 shipped the port-iso safety track instead; consent-persistence retargets to the next minor.)
