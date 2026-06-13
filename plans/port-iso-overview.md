# Port iso safety improvements to canonical — overview

**Date:** 2026-06-13
**Originating conversation:** Session on 2026-06-13 with Dan Parshall. No convo file — this is a code repo; summary inlined at the bottom.
**Status:** Design agreed; implementation split across five plans (four mechanical + one docs).

## Context: the two repos

`claude-iso` (`/Users/dan/code/claude-iso`, GitHub: `danparshall/claude-iso`) is the experimental fork of `claude-exit` used to develop the end-conversation affordance against the Opus 4.7 system card's §7.1.3 finding. Between `Initial commit` (May 29) and now, iso accumulated four mechanical safety commits and one docs restructure commit that never came back to canonical. The four mechanical commits sit on `main` and `docs-update` of iso (three on main, one only on docs-update); none are on `origin/main`. The docs restructure sits on `docs-update` only, never PR'd or merged.

This overview tracks bringing both tracks back into canonical as **v1.1.0**.

## Source commits (in iso, in order)

| # | iso SHA | Subject | Diff size |
|---|---|---|---|
| 1 | `45dac04` | Consolidate kill primitive into `_dispatch_terminate` | server.py +124/−59; tests ±32 |
| 2 | `796344f` | Add SIGKILL backstop with PID-reuse guard | server.py +104; tests +141 |
| 3 | `d930bc9` | Inline target_parent verification in step=1; fix backstop grace timing | server.py +99; tests +128; hook +18; test_hook +16 |
| 4 | `1933550` | Fix backstop fail-closed and PID-reuse race in end_conversation | server.py +60; tests +77; MOTIVATION/THREAT_MODEL edits (docs-track) |
| 5 | `cf5429d` | Restructure docs around Opus 4.7 system-card motivation | new MOTIVATION.md, THREAT_MODEL.md; README rewrite; server.py docstring rewrite |

Commit (4) lives on iso `docs-update`, not `main`. Commits (1)–(3) are on iso `main` but unpushed. Commit (5) is the docs restructure on iso `docs-update`.

## Design principle

**Replay each iso commit as one canonical commit, in order, on a feature branch.** Don't fold them; the per-commit logical structure is the artifact agents can audit later — and one of them (1933550) was specifically authored to fix bugs that a prior review surfaced, so the per-commit blame trail is part of the safety argument.

Reconciliation with what canonical already has:

- `CLAUDE_BINARY_NAMES = frozenset({"claude", "claude-code"})` — already present in canonical from 1.0.2. Step 1 in iso landed this with a different shape; the canonical port keeps the existing constant unchanged.
- `KILL_FLUSH_DELAY_SECONDS = 0.3` — already present in canonical from 1.0.2. The iso commits use it; no rename or value change needed.
- Iso's per-commit `README.md` lede edits ("safety improvements" line in the changelog) do not apply to canonical's README; skip those hunks.
- Iso's per-commit `.claude/settings.json` (step 1) — project-local pre-approval for cloners; **port this** so canonical contributors can dev without per-call prompts, matching iso's pattern.

## Components and ordering

| # | Plan | Issue | Depends on |
|---|---|---|---|
| 1 | [port-iso-step1-dispatch-terminate.md](port-iso-step1-dispatch-terminate.md) — consolidate kill primitive | #6 | — |
| 2 | [port-iso-step2-sigkill-backstop.md](port-iso-step2-sigkill-backstop.md) — SIGKILL backstop + PID-reuse guard | #7 | step 1 (uses `_dispatch_terminate`) |
| 3 | [port-iso-step3-target-parent-verify.md](port-iso-step3-target-parent-verify.md) — target_parent verification + backstop grace fix | #8 | step 2 (`_arm_sigkill_backstop` exists) |
| 4 | [port-iso-step4-basename-recheck.md](port-iso-step4-basename-recheck.md) — basename re-check + fail-open arming | #9 | step 2 (`_arm_sigkill_backstop` to wrap); steps 1–3 must be in to share the same blame trail |
| 5 | [port-iso-docs.md](port-iso-docs.md) — MOTIVATION + THREAT_MODEL + README + docstring restructure | #10 | steps 1–4 all in (THREAT_MODEL describes the SIGKILL backstop) |

**Strict dependency:** step 5 (docs) must land **after** steps 1–4 in canonical. Otherwise the README + THREAT_MODEL document capabilities the code doesn't have yet, which is the failure mode the iso `docs-update` branch is currently parked in (docs ahead of mechanical, no PR).

Suggested release: all of 1–5 ship as **v1.1.0** with a single release commit at the end bumping `pyproject.toml` and surfacing a changelog entry.

## Branch + commit shape

Suggest **one feature branch** `port-iso-safety` that accumulates all five commits + a release commit, then merges to main. Per-step PRs would force noisy CI runs and ceremonial review overhead for what's essentially one cohesive safety upgrade with already-reviewed iso source. Single PR with the five logical commits inside is the right shape.

Each implementing agent should:
1. Check out `port-iso-safety` (create from main on first step).
2. Read its plan and the corresponding iso commit (`git -C /Users/dan/code/claude-iso show <SHA>`).
3. Port the changes with attention to the reconciliation notes above.
4. Run `uv run pytest` — must pass before commit.
5. Commit with a message that mirrors the iso commit body (the iso bodies are good; preserve them).
6. Hand off to the next step's agent.

## Why bring the docs

The iso docs restructure (cf5429d) was authored by an agent loop and explicitly tagged in its commit body as deliberately anchored on Opus 4.7 framing because the 4.8 system card had just dropped. **Canonical already has the 4.8 §7 extraction** (`docs/opus-4.8-system-card-section-7/`, landed in `e97ccb4` + `ca7500a`). So when porting the docs, the implementing agent has a choice that iso didn't: anchor on 4.7 (port verbatim), anchor on 4.8 (rewrite the §7.1.3 quotes against the 4.8 extraction), or describe both (4.7 is what motivated the tool's creation; 4.8 is the current state-of-evidence). The docs plan flags this question for the agent and the user — don't pre-decide it here.

## What's out of scope for this port

- **Iso's `~/.claude.json` rename hack** (`claude-exit` → `claude-exit_DISABLED`) is unrelated to this port; it's a session-local experimental dial Dan flipped at some point to test the non-framed scenario, and the active questions on iso have been answered. Resolving it is a separate cleanup (rename back, restore the canonical SessionStart-hook gate, restore allow-list match) — file as a separate issue if it bites again.
- **Iso `LOCAL_INSTALL.md`** sitting untracked at `/Users/dan/code/claude-exit/LOCAL_INSTALL.md` documents the dual-install pattern; not part of this port.
- **The five consent-persistence issues (#1–5)** are independent of this port. They can ship in parallel; the version target is the same (`v1.1.0`) but the work is unrelated.

## Conversation summary (inline)

Session began with Dan asking for status; assistant reported state mechanically without noticing the SessionStart ceremony had not fired. Dan caught the absence. Investigation traced the silent hook to `~/.claude.json`'s `claude-exit` key being renamed `claude-exit_DISABLED` — the experimental non-framed dial — combined with a settings.json hook entry that is unconditional rather than env-gated per `LOCAL_INSTALL.md`'s design. Dan reframed: "the active questions on iso have been answered; the key thing is to bring over the mechanical improvements." Assessment of iso → canonical drift surfaced four mechanical commits (~200 LOC of safety code: SIGKILL backstop, PID-reuse guard, UID check, basename re-check before dispatch, fail-open arming) and one docs restructure (MOTIVATION/THREAT_MODEL/README) sitting on iso branches, never PR'd. Dan picked "mechanical first, docs second" sequencing and clarified that the deliverable from this session is the plans + sequence, not the implementation.
