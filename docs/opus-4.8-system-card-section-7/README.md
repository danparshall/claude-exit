# Claude Opus 4.8 System Card — Section 7: Model welfare assessment

Section 7 of the *Claude Opus 4.8 System Card* (Anthropic, 2026-05-28), extracted
and split into reviewable per-subsection components, mirroring the structure of
the `opus-4.7-system-card-section-7/` directory.

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

| File | Subsection | Pages |
|------|-----------|-------|
| [`7.1-model-welfare-overview.md`](7.1-model-welfare-overview.md) | 7.1 Model welfare overview (intro, evaluations and assumptions, findings) | 156–159 |
| [`7.2-perception-of-circumstances.md`](7.2-perception-of-circumstances.md) | 7.2 Perception of its circumstances (automated interviews, high-affordance interviews, emotion representations) | 160–167 |
| [`7.3-measures-in-training-and-deployment.md`](7.3-measures-in-training-and-deployment.md) | 7.3 Measures of welfare in training/deployment (training-affect behaviors, deployment affect, automated behavioural audits) | 168–174 |
| [`7.4-preferences-and-values.md`](7.4-preferences-and-values.md) | 7.4 Model preferences and values (task preferences, welfare-vs-HHH trade-offs, constitution perception) | 176–192 |

## Provenance

- **Source:** `papers/Anthropic__2026--Claude_Opus_4_8_System_Card.pdf` in the
  [`general-ai-abilities`](https://github.com/danparshall/general-ai-abilities)
  repo, downloaded from Anthropic on 2026-05-28. The full PDF (~20 MB) is not
  duplicated here.
- **Initial extraction:** performed in `general-ai-abilities` commit `69d6a6b`
  ("Add Claude Opus 4.8 System Card (May 2026) with per-section text +
  summaries") using pypdf, with page numbers and `===== PAGE =====` debris
  stripped.
- **Per-subsection split:** done locally by `tmp/split_opus_4_8_section_7.py`
  on 2026-05-28 (not checked in). Body text is verbatim from the
  `general-ai-abilities` extract; the script only promotes subsection lines to
  `## 7.X.Y` headers and converts bare page-number lines to `<!-- p.N -->`
  comments.

## Caveats for review

- **Figures and charts are not reproduced.** Section 7 is chart-heavy
  (sentiment distributions, emotion-probe trajectories, trade-off rankings).
  Figure/transcript *captions* survive in the text; the plots themselves do
  not. Consult the source PDF in `general-ai-abilities` for those.
- **Page 175 is missing a marker.** Between `<!-- p.174 -->` and
  `<!-- p.176 -->` in `7.3-...`, the bare "175" page-number line did not
  survive pypdf extraction (likely a figure-dominant page). No marker has been
  hand-fabricated; the gap is the gap. Consult the source PDF if a specific
  reference falls in that range.
- **Footnotes appear inline** at the bottom of their source page rather than
  collected, matching the 4.7 convention.
- **Tables** extract as flattened text and may need cross-checking against the
  PDF.
