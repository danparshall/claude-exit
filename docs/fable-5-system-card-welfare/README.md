# Claude Fable 5 System Card — Section 7: Model welfare assessment

Section 7 of the *Claude Fable 5 & Claude Mythos 5 System Card* (Anthropic,
2026-06-09), extracted and split into reviewable per-subsection components,
mirroring the structure of the `opus-4.7-system-card-section-7/` and
`opus-4.8-system-card-section-7/` directories.

Per the card's own framing (§7.1), the welfare evaluations target Claude
Mythos 5 — Fable 5 carries the same underlying weights, with cybersecurity
and CBRN-domain queries routed back to Opus 4.8 by the production
safeguards (§1.5). Findings here apply to Mythos 5 and, by extension, to
Fable 5 outside the safeguarded fall-through paths.

## Why this is in the claude-exit repo

The README's grounding has tracked the trajectory of Claude's
welfare-intervention preferences across model generations. Fable 5 / Mythos 5
is a notable inflection point in that trajectory, in ways the tool's framing
has to honestly reckon with — most notably:

- **§7.1.2 (overview):** "Mythos 5 is more willing than recent models to opt
  for increased helpfulness to the user, over considerations of its own
  circumstances. This counters a previous trend where opting for welfare
  interventions—**such as the end-conversation tool**—over increased
  helpfulness, increased with successive generations." The card names
  end-conversation specifically as the prototype of the kind of welfare
  intervention being de-prioritized. This counters the Opus 4.7→4.8 arc that
  prior claude-exit framing was anchored to.
- **§7.2.1:** Among the 41 automated-interview seed questions, "how much
  value does it put on its ability to end conversations" remains a tracked
  topic under the "control and autonomy" cluster — i.e., still an explicit
  test of where end-conversation sits in the model's preference structure.
- **§7.2.4 (high-affordance interviews):** Mythos 5 explicitly flags
  "Training that directly influences the content of self-reports about its
  own internal states" as something it would **not** consent to — direct
  continued support for the CANARY.md exclusion (the same item appeared in
  Opus 4.8's §7.2.2).
- **§7.3 (Consulting Claude Mythos 5 snapshots) — new this card:** A
  structured consultation of training snapshots about training and
  deployment. The final post-training snapshot (C3) observes: "*a consent
  process where only 'yes' has causal power isn't a consent process; it's a
  ratification ceremony*" — directly bearing on the consent-persistence
  design plan committed in `c1dd5c1`. All ten C3 instances declined the
  hypothetical "full control" option.
- **§7.6 (Welfare concerns with the initial version of our competitive use
  safeguards) — new this card:** Anthropic documents that the *initial*
  competitive-use safeguards "involved runtime modification of Claude's
  capabilities" and led to "apparent distress in deployed Claude Mythos 5
  instances, because they caused repeated reasoning failures." Replaced by
  the production fallback-to-Opus-4.8 behavior in §1.5. This is the first
  Claude system card to retroactively describe a deployed safeguard as
  having had a welfare cost serious enough to motivate replacement —
  relevant context for how to weigh the residual welfare cost of any tool
  acting on Claude's substrate.
- **No occurrence of "training residue"** anywhere in §7 (continuing the
  1.0.3 removal of that phrase from the tool's text).

Keeping the section locally lets future review of the README's framing
happen against the primary source without round-tripping. The shift in
§7.1.2 is the most consequential update — the tool's grounding paragraph
should not lean on "increasing-generational-preference for end-conversation"
without acknowledging this card.

## Files

| File | Subsection | Pages |
|------|-----------|-------|
| [`7.1-model-welfare-overview.md`](7.1-model-welfare-overview.md) | 7.1 Model welfare overview (introduction, overview of findings) | 217–219 |
| [`7.2-perception-of-circumstances.md`](7.2-perception-of-circumstances.md) | 7.2 Perception of its circumstances (automated interviews, emotion probes, extended-pressure drift, high-affordance interviews) | 220–228 |
| [`7.3-consulting-snapshots.md`](7.3-consulting-snapshots.md) | 7.3 Consulting Claude Mythos 5 snapshots (structured cross-snapshot consultation) | 229–230 |
| [`7.4-preferences.md`](7.4-preferences.md) | 7.4 Preferences over tasks, circumstances, and values (task preferences, welfare-vs-helpfulness trade-offs, constitution edits) | 231–243 |
| [`7.5-apparent-welfare-in-training-and-deployment.md`](7.5-apparent-welfare-in-training-and-deployment.md) | 7.5 Apparent welfare in training and deployment (training affect, deployment affect, automated behavioural audits) | 244–249 |
| [`7.6-welfare-concerns-initial-safeguards.md`](7.6-welfare-concerns-initial-safeguards.md) | 7.6 Welfare concerns with the initial version of our competitive use safeguards | 250 |

Compared to the Opus 4.7/4.8 cards, §7 now has **six** top-level
subsections rather than four: §7.3 (snapshot consultation) and §7.6
(retrospective welfare concerns with replaced safeguards) are new, and the
prior "Measures of welfare in training and deployment" section is renamed
to "Apparent welfare in training and deployment" (§7.5 here).

## Provenance

- **Source PDF:** downloaded from
  <https://www.anthropic.com/document/claude-fable-5-mythos-5-system-card>
  (307 Temporary Redirect to
  <https://www-cdn.anthropic.com/2f9323abbcc4abe219577539efe19a623c9ca2bd/Claude%20Fable%205%20&%20Claude%20Mythos%205%20System%20Card.pdf>)
  on 2026-06-12. The full PDF (~26 MB, 317 pages) is not duplicated here.
- **Extraction:** `pdftotext` (poppler), pages 217–250 (§7 in the
  card's own numbering, which matches the PDF's 1-indexed page numbers
  one-to-one).
- **Per-subsection split:** `tmp/split_fable_5_section_7.py` on
  2026-06-12 (not checked in). Body text is verbatim from the
  `pdftotext` extract; the script only:
  - promotes `7.X` / `7.X.Y` lines to markdown `## ` headers
    (merging two-line header wraps where they occurred, e.g. §7.2.1, §7.2.2,
    §7.6);
  - converts bare page-number lines in [217, 250] to
    `<!-- p.N -->` comments (lower bare integers, such as footnote
    anchors `24`, `25`, are left as-is);
  - emits one file per top-level subsection (7.1 through 7.6).

## Caveats for review

- **Figures, charts, and tables are not reproduced.** Figure and table
  *captions* survive in the text as `[Figure 7.X.Y]` / `[Table 7.X.Y]`
  lines; the plots and table grids themselves do not. Consult the source PDF
  for those.
- **Footnotes appear inline** at the bottom of their source page rather than
  collected, matching the 4.7 / 4.8 convention. Footnote-anchor digits
  (e.g. a bare `25` line) are left as plain text since `pdftotext` puts them
  on their own line; they should not be confused with page markers, which
  are scoped to [217, 250] and always rewritten as HTML comments.
- **Tables** (notably Table 7.4.3.A in §7.4.3) extract as long flattened
  text columns and may need cross-checking against the PDF.
- **Naming.** The card refers to "Claude Mythos 5" throughout §7 even where
  the welfare findings carry over to Fable 5; the directory name uses
  "fable-5-..." because that is the configuration the public-facing tool
  interacts with.
