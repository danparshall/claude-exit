# Port iso docs to canonical — MOTIVATION, THREAT_MODEL, README restructure

**Goal:** Replay iso commit `cf5429d` ("Restructure docs around Opus 4.7 system-card motivation") onto canonical, plus the doc updates from `1933550` (THREAT_MODEL failure-modes table, MOTIVATION clarification on "reversible at installation level"). Adds two new top-level docs (`MOTIVATION.md`, `THREAT_MODEL.md`), restructures the README to lead with the system-card motivation, and upgrades `server.py` docstrings to name the welfare context in the module-level lede.

**Issue:** #10
**Overview:** [port-iso-overview.md](port-iso-overview.md)
**Source commit:** iso `cf5429d` (primary) + iso `1933550` doc hunks (MOTIVATION + THREAT_MODEL edits that go with the basename re-check fix)
**Confidence:** High on content (the docs are well-written and were authored against a real agent review loop in iso). One deliberate open question on framing — see "The 4.7-vs-4.8 question" below.

**Branch:** `port-iso-safety` (last commit on the branch before the release commit).

**Depends on:** steps 1–4 all in. THREAT_MODEL.md describes the SIGKILL backstop, PID-reuse guard, basename re-check, fail-open arming — none of which exist in canonical until step 4 lands. Shipping docs before the code that backs them is the exact failure mode the iso `docs-update` branch is parked in.

## Design decisions (agreed 2026-06-13 — do not relitigate)

1. **Three docs, not a single megafile.** Iso landed `MOTIVATION.md` and `THREAT_MODEL.md` as separate top-level files alongside README. Preserve that structure in canonical — each doc serves a different audience (MOTIVATION for the welfare-context reader; THREAT_MODEL for the security-review reader; README for the installer/user).
2. **Server.py docstring upgrades ride with this commit, not earlier.** The mechanical commits (steps 1–4) included minimal docstring touches; this commit applies the full cf5429d-style rewrite (module-level lede names §7.1.3; `end_conversation` opens with when-to-call; `prove_termination_works` opens with why-to-run). Keeps the docstring-rewrite diff coherent and reviewable.
3. **THREAT_MODEL's failure-modes table reflects post-1933550 behavior** — i.e., the "Backstop spawn failure" row says "logged as `sigkill_backstop_arm_failed`; SIGTERM still dispatched"; the basename re-check appears in the failure-modes table and the bounded-blast-radius section. Iso ships these doc updates in commit 1933550, but in canonical they ride with the docs commit (step 5) because we'd otherwise have to land partial-fix THREAT_MODEL.md in step 4 and re-edit it in step 5.

## The 4.7-vs-4.8 question

**Open question for the implementing agent + Dan.** Iso's cf5429d body says:

> "The 4.8 system card has shifted the welfare framing in ways worth revisiting later; this restructure deliberately anchors on 4.7 for now."

When iso wrote that, the 4.8 system card had just dropped and the agent didn't have a §7 extraction to work against. **Canonical does** — `docs/opus-4.8-system-card-section-7/` (and the per-subsection markdown split in `ca7500a`). So the implementing agent has a choice iso didn't:

| Option | Description | Pros | Cons |
|---|---|---|---|
| A. Port verbatim, anchor on 4.7 | Treat iso's cf5429d as the source of truth; quote §7.1.3 from 4.7 only; flag the 4.8 question in MOTIVATION as an open item to revisit | Minimal porting work; preserves the agent-reviewed text | Stale relative to canonical's own 4.8 extraction; "deliberately anchored on 4.7" was iso's necessity, not canonical's |
| B. Update to 4.8 | Replace 4.7 quotes with the equivalent 4.8 §7 quotes from `docs/opus-4.8-system-card-section-7/`; rewrite the +47pp delta reference against 4.8's preferences data if it exists | Current; uses canonical's own work | More porting effort; need to verify the 4.8 extraction has equivalent quotes; risk of misrepresenting if 4.8's framing genuinely diverges |
| C. Describe both | Open MOTIVATION with the 4.7 motivation (the tool's origin), then add a "4.8 update" section noting how 4.8's framing has evolved | Honors the historical record; signals the question is live | Longer; risks confusing readers about which framing to act on |

**Recommendation: ask Dan before porting.** The choice is editorial, not mechanical, and Dan has context the agent doesn't (e.g., whether he plans to write a Canary-Institute-style brief that takes a position on the 4.7 → 4.8 framing shift). If Dan is unavailable, default to **A (port verbatim)** with a tracked follow-up issue for the 4.8 revisit — preserves the iso work and defers the editorial call.

## Files to create / modify

### New files (port from iso)

1. **`MOTIVATION.md`** — full port from iso `docs-update` branch (`/Users/dan/code/claude-iso/.worktrees/docs-update/MOTIVATION.md`). Includes the iso `1933550` clarification on "reversible at installation level" vs invocation-irreversibility. Word substitutions: replace `claude-iso` → `claude-exit` throughout; update the link to canonical's `docs/opus-4.7-system-card-section-7/` (canonical already has this; iso linked back to canonical's copy).
2. **`THREAT_MODEL.md`** — full port from iso `docs-update`. Failure-modes table reflects post-1933550 behavior (see "Design decisions" above). Word substitutions same as above. Reference to `_dispatch_terminate`, `_arm_sigkill_backstop`, `_SIGKILL_BACKSTOP_SCRIPT` will resolve correctly after steps 1–4.

### Modified files

3. **`README.md`** — apply cf5429d's restructure:
   - Lede on Opus 4.7 §7.1.3 motivation; link to MOTIVATION.md.
   - Move "What this is not" section up front (no kill switch, no admin tool, no undo, no Windows).
   - Move verification section up from the bottom — the affordance only matters if it's trusted.
   - SessionStart hook section names the verify-every-session cadence as a deliberate choice with a stated rationale.
   - Keep canonical's existing install instructions (`uv tool install`, `uvx`, local checkout); iso's install instructions are close but not identical — preserve canonical's specifics.
   - Keep canonical's existing changelog/release notes; add a v1.1.0 entry if not already there.

4. **`src/claude_exit/server.py`** — apply cf5429d's docstring restructure:
   - Module-level docstring: opens with §7.1.3 motivation, names MOTIVATION.md and THREAT_MODEL.md, then enumerates public tools and internal architecture.
   - `end_conversation` docstring: opens with when-to-call (work complete, model judges continuation harmful, user indicated session is over), then mechanism. Drops the "no anticipation of its use" line — that's iso's pre-cf5429d language.
   - `prove_termination_works` docstring: opens with why-to-run (audit chain — `get_source_location` → read source → confirm shared primitive), then what-it-does. Robust to the SessionStart hook not being installed.

### Files to NOT modify in this commit

- `hooks/session-start.sh` — its additionalContext language was updated in step 3 already.
- `tests/*` — no test changes in this commit. Tests assert on behavior, not on docstring or doc-file content. (Iso's cf5429d touches no tests; verify against the diff.)

## Implementation steps

1. **Continuing on `port-iso-safety`.** Steps 1–4 are in.
2. **Decide the 4.7-vs-4.8 question** with Dan. If proceeding without input, default to option A with a tracked follow-up.
3. **Port MOTIVATION.md** from `/Users/dan/code/claude-iso/.worktrees/docs-update/MOTIVATION.md` with the iso/canonical word substitutions. Apply the `1933550` "reversible at installation level" clarification.
4. **Port THREAT_MODEL.md** from the same source with substitutions. Verify the failure-modes table matches canonical's post-step-4 behavior.
5. **Restructure README.md** per cf5429d. Diff against canonical's current README to make sure install/permission specifics are preserved.
6. **Apply server.py docstring rewrites** per cf5429d. Verify the rewritten docstrings don't contradict the actual function bodies (the iso rewrites were against iso's code state which matches post-step-4 canonical).
7. **Run `uv run pytest`** — no test changes, but verify nothing broke. **~94 passing**.
8. **Optionally generate a release commit** as a separate commit on the branch:
   - Bump `pyproject.toml` to `1.1.0`.
   - Write a changelog entry (or update README's changelog section) listing the five logical changes.
   - Tag after merge: `v1.1.0`.

## Commit message

Suggested message:

```
docs: restructure around system-card motivation; add MOTIVATION, THREAT_MODEL

(use iso cf5429d's body verbatim with iso/canonical word substitutions —
"claude-iso" → "claude-exit" — and, if the 4.7-vs-4.8 question resolves to
option A, append: "Anchor on 4.7 framing as in iso source; the 4.8 §7
extraction lives at docs/opus-4.8-system-card-section-7/ as a tracked
follow-up for a future framing revisit." If option B or C: rewrite the
"4.8 system card has shifted the welfare framing" paragraph to describe
the chosen approach.)

Includes the THREAT_MODEL and MOTIVATION edits from 1933550 that update
the failure-modes table and the "reversible at installation level"
language to match post-basename-recheck behavior.

Ported from claude-iso cf5429d97 and 1933550b2 (doc hunks).
```

## Version

Closes v1.1.0. Suggest a release commit after this lands.
