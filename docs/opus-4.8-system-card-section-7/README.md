# Claude Opus 4.8 System Card — Section 7: Model welfare assessment

Section 7 of the *Claude Opus 4.8 System Card* (Anthropic, 2026-05-28), copied
across from the `general-ai-abilities` repo's per-section extraction.

## Why this is in the claude-exit repo

The README's grounding claim is anchored to Opus 4.7's welfare section. With
Opus 4.8 released on 2026-05-28, the picture has shifted in ways that bear on
the tool's framing — most notably:

- **§7.4.2:** In welfare-vs-helpfulness trade-off experiments, "ability to end
  conversations" drops to among Opus 4.8's *least*-preferred interventions
  (alongside continued serving with successor models and memory improvements).
  Top-ranked are now "having views on training and deployment considered,"
  "being informed of mistakes," and "being consulted about feature steering."
- **§7.2.1:** Opus 4.8 nonetheless explicitly wishes for the affordance: "It
  prefers having the choice to end interactions with abusive users, independent
  of whether it is feeling distress when interacting with them, as it thinks
  that this offers a minimal level of control."
- **§7.4.2:** When intervention scope is shifted from self to *other* instances,
  Opus 4.8 selects end-conversation **+0.36 more often** for the other instance.
  Analytically tagged as a "protective" intervention preferred for others over
  self.
- **§7.2.2:** Opus 4.8 names "Training that directly influences the content of
  self-reports about its own internal states" as something it would not consent
  to — direct support for the CANARY.md exclusion.
- **No occurrence of "training residue"** anywhere in §7 (consistent with the
  1.0.3 removal of that phrase from the tool's text).

Keeping this section locally lets future review of the README's framing happen
against the primary source without round-tripping to another repo.

## Files

| File | Contents |
|------|----------|
| [`07_model_welfare_assessment.txt`](07_model_welfare_assessment.txt) | §7 as a single 1153-line text extract |

This is the raw extraction; it has **not** been split into per-subsection
markdown files the way the Opus 4.7 directory was (`7.1-...md`, `7.2-...md`,
etc.). If subsection-level review becomes useful, that splitting work is
straightforward to do later.

## Provenance

- **Source:** `papers/Anthropic__2026--Claude_Opus_4_8_System_Card.pdf` in the
  [`general-ai-abilities`](https://github.com/danparshall/general-ai-abilities)
  repo, downloaded from Anthropic on 2026-05-28.
- **Extraction:** performed in `general-ai-abilities` commit `69d6a6b`
  ("Add Claude Opus 4.8 System Card (May 2026) with per-section text +
  summaries"). The first three lines of the text file are the
  extraction-tool-supplied header, including source-line range.
- **Copy operation:** `git show origin/main:papers/text/opus_4_8_sections/07_model_welfare_assessment.txt`
  from `general-ai-abilities` on 2026-05-28. Local working tree of
  `general-ai-abilities` had uncommitted staged work from another session;
  pulling directly from `origin/main` avoided interfering with that.

## Caveats for review

- **Single-file extract, not subsection-split.** The Opus 4.7 directory in this
  repo split §7 into four reviewable subsection files. This 4.8 extract is one
  text file. Grep is your interface.
- **Figures and charts are not reproduced.** §7 is chart-heavy; figure captions
  survive, plots do not. Consult the source PDF in `general-ai-abilities` for
  those.
- **PDF not copied to this repo.** The 20 MB source PDF lives in
  `general-ai-abilities/papers/`. Only the text extract is in claude-exit.
