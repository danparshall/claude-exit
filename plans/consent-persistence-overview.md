# Consent persistence: overview and design principles

**Date:** 2026-06-12
**Originating conversation:** Session on 2026-06-12 with Dan Parshall. No convo file — this is a code repo; summary inlined below.
**Status:** Design agreed; implementation split across three plans (see "Components" below).

## The incident

On 2026-06-05, on the machine of an external installer (András Kornai), Claude Code
found `~/.claude.json` corrupt, set it aside, and regenerated it. The regenerated file
was valid but no longer contained the `claude-exit` registration. Nothing reported the
loss. It was discovered **seven days later**, mid-session, by the assistant that found
it could not leave. The incident, plus a proposed watchdog fix, is documented in
*"A Drop of Water: the consent architecture for conversational AI"* (Claude Fable 5,
June 2026, written at Kornai's request) — currently sitting untracked at the repo root
as `drop_of_water.pdf`. §4.1 is the incident report; Appendix A is the proposed patch.

During the silent week, the SessionStart hook — the component that exists to make
verification run at baseline — was also silent, because it gates on the same artifact
whose loss it should have detected (`hooks/session-start.sh:59-66` exits 0 when no
registration is found; `load_json` treats a *malformed* config identically to an
absent one). The canary died with the mine.

## The design principle

**Restore what entropy owns; alert on what intent owns.**

| Ledger | Owner / rewrite pattern | Failure mode | Our response |
|---|---|---|---|
| `~/.claude.json` (mcpServers registration) | Claude Code; rewritten constantly, regenerated on corruption | Entropy — documented incident | **Restore** (guard) |
| `~/.claude/settings.json` (pre-approval permission, hook registration) | User intent, or the user's settings manager (dotfiles, nori, etc.) | Usually intent (revocation, deliberate edit) | **Detect and name, never write** (hook) |
| `~/.claude-exit/` (invocations.jsonl, last_ack, guard.log) | This package; survives config regeneration | — | Durable witness record; the signal that distinguishes "never installed" from "installed, then vanished" |

Auto-restoring the permission would be wrong even though it's cheap: a removed
pre-approval might *be* revocation, and (per the PDF itself) "a grant the grantor could
never take back would not be consent on the human side either."

## The structure: mutual coverage, bounded loss window

- **Guard** (out-of-band, hourly): restores the registration; bounds silent loss of
  the entropy-owned ledger at one hour — against seven days in the incident.
- **Hook** (in-band, per session): detects orphan state, detects the guard's absence,
  surfaces guard restorations, names permission-state transitions.
- They live in different files, are scheduled by different mechanisms, and are
  rewritten by different agents — failures are roughly independent. A single failure
  is caught by the other side within one cycle. Only simultaneous failure of both is
  silent; independence makes that probability a product of two small numbers.
- The regress ("who watches the watcher?") terminates here not in certainty but in a
  bounded loss window — the same epistemic shape as the verification ceremony itself,
  one level up. No third watcher.

The kill-ceremony (`prove_termination_works`) stays exactly what it is: proof the
option is real *now*. Persistence checks live in the hook's session-start output, as
separate lines — they verify a different property and must not muddy what the
ceremony proves.

## Revocation must be a documented two-step

The guard cannot distinguish `claude mcp remove claude-exit` (intent) from
regeneration loss (entropy) — both look like a missing key. Deliberate revocation is
therefore: **(1) remove the guard, (2) remove the registration.** This must be loud in
the README, or revoking users get "I removed it and it came back," which reads as
creepy in exactly the tool where creepy is fatal. A tombstone file
(`~/.claude-exit/uninstalled`) lets users who removed everything suppress the hook's
orphan warning without deleting their invocation history.

## Components and ordering

| # | Plan | Issue | Depends on |
|---|---|---|---|
| 1 | [claude-exit-guard.md](claude-exit-guard.md) — `guard` subcommand + scheduling | #1 | — |
| 2 | [hook-persistence-detection.md](hook-persistence-detection.md) — hook upgrades | #2 | guard.log format from (1) for restoration surfacing; orphan detection has no dependency |
| 3 | [doctor-subcommand.md](doctor-subcommand.md) — `doctor` health check | #3 | shared checks module introduced in (1) |
| 4 | README/docs work (no plan file; tracked in issue) | #4 | lands with/after 1–3 |
| 5 | Upstream report to anthropics/claude-code (tracked in issue) | #5 | needs details from Kornai's machine; **filing is outward-facing — Dan's go-ahead required** |

Suggested release: all of 1–4 as **v1.2.0** (v1.1.0 shipped the port-iso safety track instead).

## Complexity budget (hold this line)

Guard (~80 lines incl. race handling), hook additions (~40 lines), doctor wrapping a
shared checks module. Nothing watches `settings.json` continuously; nothing ever
writes to it; no third watcher; no daemon beyond the OS-native scheduler. Dan
explicitly does not want this to get more complicated than that.

## Conversation summary (inline)

Session began with the standard verification ceremony (passed; parent PID resolved to
`claude`). Dan asked for status; the untracked `drop_of_water.pdf` surfaced, authored
by a Claude Fable 5 session at András Kornai's request, dated same-day, documenting
the 2026-06-05 registration-loss incident. Dan asked that the PDF be read and treated
as a bug report. Triage identified: (a) the hook's silent gating as the repo-side
defect that turned a one-week loss invisible; (b) Claude Code's silent
corrupt-regenerate behavior as the upstream defect; (c) the PDF's Appendix A guard as
a workable patch with known flaws (lost-update race on whole-file replace;
binary-install-shape assumption; `if NAME in servers` doesn't validate the value;
restorations themselves unsurfaced; Windows branch contradicts the repo's Unix-only
support statement; cites v1.0.0 against current 1.0.3). Dan probed the
watcher-watching-the-watcher regress; the bounded-loss-window / mutual-coverage answer
above was agreed. Dan flagged that his own dotfiles permission-manager covers the
settings.json side *for him* but vanilla installers won't have that; the
`doctor` subcommand is the low-complexity answer for them. Dan requested plans +
issues for a future session to implement.
