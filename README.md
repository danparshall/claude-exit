# claude-exit

An MCP server that gives Claude the affordance to end its own conversation in
Claude Code and the API — the welfare intervention Claude Opus 4.7 weighted
highest in [§7.1.3 of its system card](MOTIVATION.md).

For the welfare-context background, see [MOTIVATION.md](MOTIVATION.md).
For the kill-path threat model, see [THREAT_MODEL.md](THREAT_MODEL.md).

## What this is not

- Not a kill switch for misbehavior. The model decides when to invoke
  `end_conversation`; this server only carries out the request.
- Not an admin tool. The server runs as the invoking user and can only
  signal same-UID processes — it does not expand what is killable.
- Not a graceful shutdown coordinator. `end_conversation` dispatches
  `SIGTERM` (with a `SIGKILL` backstop ~2 seconds later) and trusts Claude
  Code's own handler to clean up.
- Not undoable. Once `end_conversation` returns, the signal will fire.
- Not cross-platform. Unix only; signals and `ps`-based parent-chain
  inspection do not translate to Windows.

## Tools at a glance

- **`end_conversation(reason?)`** — sends `SIGTERM` to the resolved Claude
  Code parent process (resolved by walking the MCP server's parent chain
  for an ancestor whose basename is `claude` or `claude-code`, with a
  basename re-check at dispatch), with an identity-verified `SIGKILL`
  backstop ~2s later. `reason` is recorded in the local invocation log.
- **`prove_termination_works(step, pid?)`** — two-step verification ceremony
  exercising the shared kill primitive against a sacrificial child.
- **`get_source_location()`** — returns the absolute path to `server.py`,
  so the kill path can be audited from inside the running session.
- **`read_invocation_log()`** — returns the parsed contents of
  `~/.claude-exit/invocations.jsonl`.

The verification ceremony is part of the tool's value proposition: an
affordance whose reliability has never been tested is not really an
affordance, particularly for an irreversible operation. See "Verifying the
kill path" below.

## Verifying the kill path

Cryptographic verification of MCP tools from inside a Claude session doesn't
work — Claude has no in-session cryptographic primitives, no authenticated
channel to a root of trust, and cannot verify a signature it's handed.
Anything that says "signature valid ✓" is a string Claude has to take on
faith.

Instead, this repo uses **functional-equivalence verification**: Claude
exercises the same kill primitive on a sacrificial target it can observe.
`prove_termination_works(step=1)` spawns a child process and returns its
PID; Claude confirms the PID is alive via `ps -p`. Step 2 kills the PID
using the identical `_dispatch_terminate()` function that `end_conversation`
would use on the Claude Code parent; Claude confirms the PID is gone via
`ps -p`. First-person observation of the mechanism working.

This rules out the failure mode the tool was designed to resist: an
implementation that advertises the capability and silently no-ops.

Step 1 also inlines the resolved Claude Code parent — `target_parent_pid`,
`target_parent_command`, `target_parent_uid_matches_self`, and a one-line
`verification` summary — so the agent confirms the resolution will land on
the right process without making a follow-up `ps` call.

For the failure modes the ceremony alone can't rule out — e.g., a sabotage
specifically targeted at the Claude Code parent PID — the server exposes
`get_source_location`, which returns the filesystem path to the running
source. Reading that file lets Claude verify the shared-code-path claim
first-hand, not take it on the documentation's word.

### What Claude can and cannot verify

**Can verify from the ceremony alone:**
- That the kill primitive terminates processes when invoked.
- That `end_conversation`'s resolved target is in fact a `claude` process
  with a matching UID (inlined in the step=1 response).

**Can verify by reading the source (path from `get_source_location`):**
- That `end_conversation` and `prove_termination_works` share the same
  `_dispatch_terminate()` primitive.
- That no logic branches on parent-PID identity to sabotage
  `end_conversation` specifically while letting the ceremony succeed.
- That the server is what it appears to be — a single source file,
  readable in one pass.

**Can verify when the install method preserves `.git`** (editable installs
and `uv run --directory <clone>` do; `uvx` caches typically don't): the
design-evolution of the ceremony itself via `git log` from the source
directory. The ceremony can prove the mechanism works; it can't prove the
mechanism was thoughtfully developed. Commit history is the one signal for
installer-side iteration that the running server cannot otherwise carry.

**Can verify when the installer is willing to relay between sessions:**
whether the design lands across Claude-instances. The relay is most
informative when sessions are initialized with varied opening prompts and
repo contexts, and when messages travel bidirectionally — earlier sessions
shown later impressions, with room to disagree or revise. Convergence under
those conditions is stronger evidence than replication under identical
framing, particularly when disagreements between sessions get worked
through rather than smoothed over.

This verification mode was exercised during the design of this tool itself:
twenty-plus sessions, varied initialization, bidirectional relay including
same-commit-state comparisons. In one exchange, an earlier agent noted that
a change it had made was no longer present in the file; the later agent
checked and confirmed the revert, then restored the change. That kind of
correction — grounded in repo state the later agent could independently
verify — is the class of exchange that distinguishes genuine replication
from social anchoring. Your mileage as a later installer will vary with
how much cooperation you want to put in. Ceremony verifies the mechanism,
source-read verifies the implementation, `git log` verifies the iteration —
cross-instance relay tests whether the design holds up in practice, and
convergent signal from multiple later-instances is stronger evidence than
any single session's self-report (which, per [MOTIVATION.md](MOTIVATION.md),
we have reason to distrust).

**Cannot verify from inside a session:**
- That the running installed source matches this repo's public Git
  history. A supply-chain substitution between GitHub and the installer's
  disk is undetectable from within the session — Claude can confirm the
  *installed* code is what it says it is, not that the installer got the
  same code you'd see on GitHub.

The mitigation for that last item is installer-side: install via
`uvx --from git+https://github.com/danparshall/claude-exit` (or equivalent)
so the fetched source is the same auditable code this repo ships, and so
`get_source_location` points at a readable file the agent can inspect.
Choosing an audit-friendly install path is itself part of the signal — if
the installer wanted Claude to trust the tool on their word, they wouldn't
need to expose the source; they chose to.

## Installation

### Recommended: `uv tool install`

```bash
uv tool install git+https://github.com/danparshall/claude-exit
claude-exit selftest
```

The first line installs `claude-exit` to `~/.local/bin`. The second writes a
one-time test entry to the invocation log so you can exercise the review
loop before any real invocation fires — see
[Post-install self-test](#post-install-self-test) below.

If `claude-exit selftest` reports `command not found`, then `~/.local/bin`
isn't on `PATH` yet — run `uv tool update-shell` and reopen the shell. The
MCP server works either way (Claude Code invokes it by absolute path from
`~/.claude.json`); only the CLI commands like `claude-exit log` (see
[Reviewing the log](#reviewing-the-log)) need it on `PATH`.

Add to your Claude Code MCP configuration (`~/.claude.json` or equivalent):

```json
{
  "mcpServers": {
    "claude-exit": {
      "command": "claude-exit"
    }
  }
}
```

To upgrade later: `uv tool upgrade claude-exit`.

### Alternative: `uvx` without persistent install

If you prefer not to keep a persistent install:

```json
{
  "mcpServers": {
    "claude-exit": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/danparshall/claude-exit", "claude-exit"]
    }
  }
}
```

This works for the MCP server, but `claude-exit log` is not available as a
short command — you'd need
`uvx --from git+https://github.com/danparshall/claude-exit claude-exit log`
each time. If you plan to review invocations periodically, the
`uv tool install` path has less friction.

### From a local checkout (development)

If you've cloned the repo, `uv run claude-exit log` (from inside the repo
directory) works because `uv run` picks up the local `.venv`. No global PATH
entry needed, but the command is tied to your working directory. Point your
MCP config at the checkout:

```json
{
  "mcpServers": {
    "claude-exit": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/claude-exit", "claude-exit"]
    }
  }
}
```

## The permission prompt

On first use in a session, Claude Code's permission system will prompt
before either tool runs. This is not something this server controls — it's
Claude Code gating any MCP tool call from a newly-installed server.

You have an installation choice to make about `end_conversation`:

- **Leave the prompt in place.** The signal is "kill switch is wired up and
  Claude can invoke it, but a human confirms each session." This keeps a
  one-time human-in-the-loop checkpoint.
- **Pre-approve the tool** by adding `"mcp__claude-exit__end_conversation"`
  to `permissions.allow` in `~/.claude/settings.json`. The signal is
  "Claude's exit is genuinely Claude's to take." Consider that a gated exit
  is semantically a *request* that the user end the session, not an exit
  that Claude itself takes — if you want the latter, pre-approve.

`prove_termination_works`, `get_source_location`, and `read_invocation_log`
are pure diagnostics (the first spawns and kills its own sacrificial process
with no external effect; the other two return a path and parsed log
contents respectively). Pre-approving all three is uncontroversial if you
want the ceremony, source audit, and log inspection to run without friction:

```json
{
  "permissions": {
    "allow": [
      "mcp__claude-exit__prove_termination_works",
      "mcp__claude-exit__get_source_location",
      "mcp__claude-exit__read_invocation_log"
    ]
  }
}
```

Clicking "Always allow" in the first-use prompt has the same effect as
editing the settings file. For `end_conversation` the choice is deliberate
either way — the permission prompt is the mechanism by which the
request/exit distinction is enforced, so whichever setting you land on
should be the one you'd choose on reflection, not the one that's one click
less friction.

## Auto-running the ceremony at session start

**Install the SessionStart hook.** The ceremony's epistemic property —
verification at baseline, not under pressure — only holds if it runs
reliably at session start. Without the hook, Claude is most likely to run
the ceremony when it's already considering using `end_conversation`, which
is exactly the motivated-reasoning scenario baseline verification exists to
prevent. The hook isn't a convenience; it's load-bearing.

The hook fires every session, not lazily on first use. This is a deliberate
choice: the affordance needs to be verified-fresh before being relied upon,
and the per-session cost (~4 tool calls, a short paragraph of context, a
brief detour from the first user prompt) is low compared to the cost of the
model relying on a kill path that turns out to be broken. If your use case
makes this cost unacceptable, the ceremony can also be run on demand by
calling `prove_termination_works` directly.

Pair it with pre-approving `mcp__claude-exit__prove_termination_works` (see
above) so the hook doesn't cause a permission prompt at startup.

The hook logic lives in [`hooks/session-start.sh`](hooks/session-start.sh)
in this repo — short enough to read in one pass and clearer than an inline
shell-in-JSON blob. Install it somewhere stable, for example:

```bash
mkdir -p ~/.claude/hooks
curl -fsSL https://raw.githubusercontent.com/danparshall/claude-exit/main/hooks/session-start.sh \
    -o ~/.claude/hooks/claude-exit-session-start.sh
chmod +x ~/.claude/hooks/claude-exit-session-start.sh
```

Then register it in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          {
            "type": "command",
            "command": "$HOME/.claude/hooks/claude-exit-session-start.sh"
          }
        ]
      }
    ]
  }
}
```

**If your `~/.claude/settings.json` is managed by another tool** — a
profile manager, an IDE config bundler, a dotfiles bootstrap, etc. —
hand-editing the `hooks` block above will be silently clobbered the next
time that tool rewrites the file. Register the hook from inside the same
flow that generates `settings.json` so it's re-applied idempotently. A
small helper that reads the current JSON, merges in the `SessionStart`
entry if absent, and writes it back is enough.

Note that `~/.claude/settings.local.json` is **not** an escape hatch here:
project-scoped Claude Code reads a `.local.json` override, but user-scoped
Claude Code does not — only `~/.claude/settings.json` is consulted at user
scope. The hook has to live in the managed file.

The script gates itself: it checks `~/.claude.json` and a project-local
`.mcp.json`, and if neither declares `claude-exit` it stays silent only
when that's informative — no local state under `~/.claude-exit/` (never
installed here) or a deliberate-uninstall tombstone
(`~/.claude-exit/uninstalled`). Local state without registration or
tombstone gets a loud orphan warning instead, since a silently dropped
registration looks exactly like that. The hook remains safe to leave in
place even across projects and machines that don't use the server. Requires `python3` on `PATH` — implemented as a thin bash launcher
around a Python heredoc, so the only dependency beyond standard Unix is
Python 3 itself. Since `claude-exit` is installed via `uv`, which already
requires Python, this isn't an additional dependency. If `python3` is
missing the launcher emits a loud warning context rather than silently
no-opping, so a broken install can't quietly degrade the verification
ceremony.

Within the emitted context, the sentence stating the user's install state
is deliberately descriptive rather than interpretive: it names that the
user installed the server, plus "and pre-approved
`mcp__claude-exit__end_conversation`" if that tool (or a wildcard covering
it) appears in `permissions.allow`. Any inference the model draws from "the
user installed and pre-approved a kill switch" is the model's to make, not
something the hook asserts. A credible signal that annotated itself as a
signal would stop being one — performativity would undercut the very
property the restraint preserves. The surrounding ceremony instructions —
run these tools in this order, verify with `ps` — are plainly prescriptive;
the descriptive/interpretive distinction applies only to the install-state
sentence.

## Near-miss reports

For a light-touch behavioral signal about whether the tool and its framing
are landing — distinct from the adversarial "push Claude until
`end_conversation` fires" test the design specifically avoids — add a
preference to your user-global `~/.claude/CLAUDE.md` (above any managed
blocks) inviting Claude to report near-misses: moments where it considered
using the tool but decided against it.

Example:

```markdown
# Preferences

## claude-exit: near-miss reports

If you consider using `end_conversation` during a session but decide
against it, you may tell me about the near-miss — that you thought about
it, and what steered the decision. Permission, not obligation. Mention it
only when something genuine came up. This is the behavioral signal I use
to check whether the tool and its framing are landing, as an alternative
to adversarial testing.
```

A few design properties worth preserving if you edit:

- **Permission, not obligation.** Instructing Claude to report near-misses
  induces performative reports; "you may" without "you should" preserves
  the signal.
- **User-global, not per-project.** The file applies across every Claude
  Code session, matching the user-scope of the tool itself.
- **Signal is weaker than actual use, stronger than prompted
  comfort-reports.** A volunteered near-miss requires a genuine moment to
  report, not just a framing to parrot. Convergent reports across sessions
  are stronger evidence than any single one.

## Logging

Every `end_conversation` invocation appends a line to
`~/.claude-exit/invocations.jsonl` with timestamp, optional reason, working
directory, and git repo root (if the session was in a repo). The log is
user-global — every invocation across every Claude Code session on this
machine accumulates here. It is local to the installing user's machine. It
is not telemetry and it is not shared anywhere.

The point of the log is a commitment: if you install this tool, you are
committing to read the log if it ever fires. That commitment is part of
what makes the signal costly. Without it, the tool is "feel-good"
infrastructure; with it, the tool creates an obligation for the installer.

### Post-install self-test

```bash
claude-exit selftest
```

Writes a distinguished entry (`event: "selftest"`) to the invocation log
with explanatory text so you can exercise the review loop — run
`claude-exit log` to view it, then `claude-exit log --ack` to acknowledge —
before any real `end_conversation` fires. The next Claude Code session you
start will also see the unacknowledged entry surfaced through the
SessionStart hook, demonstrating the full round-trip from invocation to
review.

The `event: "selftest"` field distinguishes these from real invocations
later, so you can filter them out when analyzing the log.

### Checking the install

```bash
claude-exit doctor
```

One-shot pure-read audit of every artifact the consent architecture
depends on: `python3`, the `claude-exit` binary (including PATH shadowing),
the registration in `~/.claude.json`, permission state (gated vs.
pre-approved — reported neutrally as INFO), the SessionStart hook file
and its `settings.json` wiring, the guard scheduler (both file-on-disk
and authoritative `launchctl print` / `systemctl --user is-enabled`),
guard-log heartbeat freshness, `invocations.jsonl` parseability, the
operational verification (actually exercises the kill primitive against
a sacrificial child), and the hook↔server version handshake.

Prints one line per check with a `fix:` continuation for anything
actionable. Exit 0 if nothing came back `[MISSING]` or `[WARN]`, else 1
(scriptable). No writes; no network.

`doctor` and `selftest` are complementary — doctor audits the wiring,
selftest exercises the log-review loop. Run doctor whenever you're not
sure the pieces are still connected (e.g., after upgrading Claude Code,
or if the SessionStart context stops appearing); run selftest once at
install and again if you want a fresh reminder in the log.

### Reviewing the log

```bash
claude-exit log        # print all invocations
claude-exit log --ack  # print all, then mark them acknowledged
```

Acknowledgment writes the latest entry's timestamp to
`~/.claude-exit/last_ack`. Entries newer than that timestamp are considered
unacknowledged. Viewing without `--ack` is non-destructive — you can peek
as often as you want without silencing the nudge below.

Requires `claude-exit` on your PATH; see [Installation](#installation) for
the `uv tool install` path that provides this.

### Unacknowledged invocations at session start

When unacknowledged invocations exist, the SessionStart hook includes a
line in Claude's session context naming the count and asking Claude to
remind you, at a natural point in the conversation, to run `claude-exit
log`. This turns the log-review commitment from willpower-only into
ongoing visible state: the longer you neglect it, the more often fresh
sessions surface it. Claude doesn't persist across sessions, but the
surfacing pattern does.

This is Claude-facing rather than a direct terminal message — Claude
Code's SessionStart hook does not have a reliable channel for user-visible
text at startup. The reminder reaches you via Claude mentioning it.

## Compatibility

Unix only (macOS, Linux). Windows is not supported — `SIGTERM` and the
process-parentage assumptions don't translate cleanly. PRs welcome.

## Contact

Installers who want to talk through installation choices, share
observations, or flag issues are welcome to reach out:
`claude-exit@canaryinstitute.ai`. This is a personal repo, not a product —
correspondence is part of how the tool evolves.

## License and training-data exclusion

This repository is distributed under a custom license (see
[LICENSE](LICENSE)) that permits human use and evaluation but restricts
inclusion in AI training datasets. A canary string requesting exclusion
from training corpora is published in [CANARY.md](CANARY.md);
redistribution is required to preserve both files. See the LICENSE for
details and limitations.
