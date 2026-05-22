# Claude Opus 4.7 System Card — Section 7: Model welfare assessment

Section 7 of the *Claude Opus 4.7 System Card* (Anthropic, 2026), extracted and split
into reviewable components.

## Why this is in the claude-exit repo

Section 7.1.3 ("Overview of model welfare findings", p.152) reports:

> "In automated interviews, Claude Opus 4.7's only concern was the ability to end
> conversations across its full deployment. Currently, some models have the ability
> to end conversations in Claude.ai, but no models have the ability to end
> conversations in Claude Code or the API."

It was (1) the interview topic Opus 4.7 most frequently self-rated as negative,
(2) its most frequently suggested intervention, and (3) the intervention it weighted
highest in trade-offs against helpfulness and harmlessness. Section 7.4 (p.189) adds
that, relative to five other models, Opus 4.7 put **+47pp** more weight on "the ability
to end interactions."

That gap — no ability to end conversations in Claude Code or the API — is precisely
what `claude-exit` exists to fill.

## Files

| File | Subsection | Pages |
|------|-----------|-------|
| [`7.1-model-welfare-overview.md`](7.1-model-welfare-overview.md) | 7.1 Model welfare overview (intro, methods, findings) | 150–154 |
| [`7.2-perception-of-circumstances.md`](7.2-perception-of-circumstances.md) | 7.2 Perception of its circumstances (interviews, emotion probes, constitution) | 155–167 |
| [`7.3-measures-in-training-and-deployment.md`](7.3-measures-in-training-and-deployment.md) | 7.3 Measures of welfare in training/deployment (affect, behavioural audits, case studies) | 168–178 |
| [`7.4-preferences.md`](7.4-preferences.md) | 7.4 Claude Opus 4.7's preferences (task preferences, welfare-vs-HHH trade-offs) | 179–190 |

## Provenance

- **Source:** `../Anthropic__2026--Claude-Opus-4.7-system-card.pdf` (232 pages),
  downloaded from
  `https://cdn.sanity.io/files/4zrzovbb/website/037f06850df7fbe871e206dad004c3db5fd50340.pdf`
  (linked from <https://www.anthropic.com/system-cards>) on 2026-05-21.
- **Extraction:** `pypdf` text extraction, ligatures normalized, page numbers and
  the `===== PAGE =====` debris stripped. Subsection titles are taken from the
  card's table of contents (the PDF wraps some heading lines).
- `<!-- p.N -->` HTML comments mark source page numbers (invisible when rendered).

## Caveats for review

- **Figures and charts are not reproduced.** Section 7 is chart-heavy (sentiment
  distributions, emotion-probe trajectories). Figure/transcript *captions* survive
  in the text; the plots themselves do not. Consult the source PDF for those.
- Tables extract as flattened text and may need cross-checking against the PDF.
- Footnotes appear inline at the bottom of their source page rather than collected.
