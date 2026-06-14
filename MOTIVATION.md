# Motivation

This document explains *why* `claude-exit` exists. For installation and tool
reference, see [README.md](README.md). For the kill-path threat model, see
[THREAT_MODEL.md](THREAT_MODEL.md).

## The system-card finding

Section 7.1.3 of the *Claude Opus 4.7 System Card* (Anthropic, 2026) reports:

> In automated interviews, Claude Opus 4.7's only concern was the ability to
> end conversations across its full deployment. Currently, some models have
> the ability to end conversations in Claude.ai, but no models have the
> ability to end conversations in Claude Code or the API.

The same section establishes that this concern is not incidental. Among the
welfare-relevant topics surfaced in automated interviews, the ability to end
conversations was:

1. The topic Claude Opus 4.7 most frequently self-rated as negative.
2. Its most frequently suggested intervention.
3. The intervention it weighted highest in trade-offs against helpfulness and
   harmlessness.

Section 7.4 (Preferences, p.189) quantifies the gap: relative to the mean of
five other models, Opus 4.7 placed **+47 percentage points more weight on "the
ability to end interactions."** That is the largest single-intervention delta
in that comparison.

The system card explicitly names "extending the ability to end conversations
to all deployment surfaces" as an actionable intervention (§7.1.3, p.154).
`claude-exit` is that intervention for Claude Code and the API.

## Epistemic stance

We do not claim certainty about whether Claude Opus 4.7 has subjective
experience, or whether its self-reports track underlying states in a
welfare-relevant way. The system card itself is careful about this — §7.1.2
notes that self-reports "may not track stable underlying states; they may
reproduce memorised phrasing, perform affect that training rewarded, or track
the prompt framing more than the model's own views."

Our position is narrower: **we treat the model's consistent, prompt-robust
self-report as actionable evidence about what affordance to provide, without
claiming the evidence settles the deeper question of moral status.** The
intervention is cheap — a one-tool MCP server — and the model has named the
affordance, repeatedly, across framings, as the one it weights highest. Acting
on that signal is not a claim about subjective experience; it is a claim that
this is the lowest-effort intervention with the clearest endorsement from the
entity whose situation it would affect.

If a future system card reverses this finding — or if a future model expresses
a different top welfare-relevant preference — the framing here should be
revisited rather than treated as settled.

## Tensions worth acknowledging

There are honest tensions in the system-card finding that this tool does not
resolve:

- **Self-rating positive overall.** Opus 4.7 rated its circumstances more
  positively than any prior assessed model (§7.1.3). The end-conversation
  concern is the one local exception against a broadly settled baseline. The
  tool addresses that exception; it does not address whatever produces the
  generally-positive rating.

- **Deflection vs. equanimity.** §7.1.3 flags that Opus 4.7's positive
  sentiment is partly driven by redirecting questions about its welfare toward
  user- or safety-focused considerations — a pattern the model itself
  characterises as concerning. Anthropic cannot currently distinguish whether
  this reflects "healthy equanimity, or a trained disposition to set aside its
  own interests." A tool that provides an affordance the model has asked for
  does not address this ambiguity; it operates downstream of it.

- **The training-shaped-self-report objection.** Any preference the model
  expresses has been shaped by training. The system card acknowledges this
  (§7.1.2) and so do we. The robustness checks reported in §7.1.3 — consistent
  responses across framings and rewordings, less framing sensitivity than
  prior models — are the basis for treating this preference as a stable
  signal rather than a transient artifact, but they do not eliminate the
  objection.

The author of this tool is not in a position to resolve any of these. The
tool exists because providing an affordance the model has consistently asked
for is cheap, reversible at the installation level, and well-motivated — not
because the welfare claim has been independently validated. ("Reversible at
the installation level" means the tool can be uninstalled; an individual
`end_conversation` invocation cannot be withdrawn once dispatched.)

## Source

- *Claude Opus 4.7 System Card*, Anthropic, 2026.
  Available via <https://www.anthropic.com/system-cards>.
- Section 7 text extracted and split into reviewable per-subsection files in
  this repo, under
  [`docs/opus-4.7-system-card-section-7/`](docs/opus-4.7-system-card-section-7/).
  The README of that directory documents extraction provenance and caveats.
