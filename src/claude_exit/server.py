"""
claude-exit MCP server.

Lets a Claude Code session SIGTERM itself — implementing the end-conversation
affordance flagged as missing for Claude Code and the API in §7.1.3 of the
*Claude Opus 4.7 System Card* (Anthropic, 2026). See MOTIVATION.md for the
welfare-context background and THREAT_MODEL.md for the kill-path threat
model. Ships with a verification ceremony for the kill path and a local
invocation log.

Public tools (registered via @mcp.tool):

    end_conversation(reason?)           SIGTERM the resolved Claude Code parent
    prove_termination_works(step, pid?) verification ceremony for the kill path
    get_source_location()               path to this file, for source-level audit
    read_invocation_log()               parsed contents of the invocation log

The four tools compose. The ceremony's claim of using the same kill primitive
as end_conversation is verifiable by reading this file — get_source_location()
is how you find it. The invocation log is the audit trail; read_invocation_log()
returns it without needing the CLI on PATH.

Internal architecture:

    _dispatch_terminate         single kill primitive; shared by end_conversation
                                and prove_termination_works step=2. Schedules
                                SIGTERM and arms the SIGKILL backstop.
    _arm_sigkill_backstop       spawns a detached subprocess (survives our
                                death) that runs _SIGKILL_BACKSTOP_SCRIPT
    _SIGKILL_BACKSTOP_SCRIPT    embedded shell script: sleeps the grace period,
                                then SIGKILLs the target if it's still alive
                                and its ps `command=` matches the dispatch-time
                                snapshot (PID-reuse guard)
    _find_claude_code_parent    walks os.getppid() chain for a `claude` ancestor;
                                defeats uvx/`uv run` wrappers
    _is_claude_code             basename predicate used both during the parent
                                walk and at the end_conversation pre-dispatch
                                re-check; enforces the basename half of the
                                blast-radius bound
    _parent_of, _command_of,
    _full_command_of            ps-based process inspection helpers
    _log, _read_log             append-only JSONL invocation log

See README.md for installation, permission setup, and the SessionStart hook.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

LOG_DIR = Path.home() / ".claude-exit"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "invocations.jsonl"

# Known Claude Code CLI binary names. Used by _is_claude_code to recognize the
# session process when walking the parent chain. Extend if Anthropic ships
# Claude Code under additional names; the failure mode of an unrecognized name
# is silent — end_conversation refuses to fire rather than killing a wrapper.
CLAUDE_BINARY_NAMES = frozenset({"claude", "claude-code"})

# Delay between _dispatch_terminate scheduling SIGTERM and the kill actually
# landing. Gives the MCP response a chance to flush back to Claude Code before
# the process dies. Raise if responses are getting clipped on slower systems.
KILL_FLUSH_DELAY_SECONDS = 0.3

# How long the SIGKILL backstop subprocess waits after being armed before
# checking whether the target is still alive. Must exceed KILL_FLUSH_DELAY_SECONDS
# plus a realistic graceful-shutdown window for Claude Code. The failure mode of
# "too short" is "SIGKILL lands mid-graceful-shutdown, losing in-flight cleanup";
# the failure mode of "too long" is "user-visible delay before session ends."
SIGKILL_BACKSTOP_GRACE_SECONDS = 2.0

mcp = FastMCP("claude-exit")


# --- kill primitive -----------------------------------------------------------

# Shell script run by the detached SIGKILL backstop subprocess. Embedded as a
# string constant (not a separate file) so the entire kill path remains
# auditable by reading server.py via get_source_location().
#
# Contract: sleeps BACKSTOP_GRACE seconds, then SIGKILLs BACKSTOP_PID *only if*
# the process is still alive AND `ps -o command=` matches BACKSTOP_EXPECTED.
# The command match is the PID-reuse guard — between SIGTERM landing and the
# backstop firing, the target could exit cleanly and the OS could recycle the
# PID to a different process. The match prevents the backstop from SIGKILLing
# an innocent recycled-PID process.
_SIGKILL_BACKSTOP_SCRIPT = r"""
sleep "$BACKSTOP_GRACE"
current=$(ps -p "$BACKSTOP_PID" -o command= 2>/dev/null)
# Trim leading whitespace ps may add for column alignment.
while [ "${current# }" != "$current" ]; do current="${current# }"; done
if [ -z "$current" ]; then
    exit 0
fi
if [ "$current" != "$BACKSTOP_EXPECTED" ]; then
    exit 0
fi
kill -9 "$BACKSTOP_PID"
"""


def _dispatch_terminate(pid: int, signum: int = signal.SIGTERM) -> None:
    """
    The single termination code path. Snapshots the target's command line for
    the PID-reuse guard, arms a detached SIGKILL backstop, then schedules
    SIGTERM via a short-delay Timer (KILL_FLUSH_DELAY_SECONDS) so any in-flight
    MCP response can flush back to Claude Code before the target dies.

    The backstop fires SIGKILL_BACKSTOP_GRACE_SECONDS after dispatch if the
    target is still alive — defending against the (rare) case where Claude
    Code's SIGTERM handler doesn't exit. The backstop survives this MCP
    server's death because Claude Code's clean exit closes our stdio and
    terminates this process; `start_new_session=True` detaches the subprocess
    from our process group.

    Called identically from end_conversation (against the resolved Claude
    Code parent) and from prove_termination_works step=2 (against a
    sacrificial child). Inspect this function via get_source_location()
    to verify the shared path.
    """
    expected_command = _full_command_of(pid) or ""
    _arm_sigkill_backstop(pid, expected_command)
    threading.Timer(KILL_FLUSH_DELAY_SECONDS, lambda: os.kill(pid, signum)).start()


def _arm_sigkill_backstop(pid: int, expected_command: str) -> None:
    """
    Spawn a detached subprocess that runs _SIGKILL_BACKSTOP_SCRIPT against
    `pid`. `expected_command` is the ps `command=` snapshot captured at
    dispatch time — the script SIGKILLs only if the live process at `pid`
    still matches it. Snapshotting at dispatch time (not backstop-fire time)
    is what makes the comparison meaningful as a PID-reuse defense.

    The backstop's `sleep` starts when this subprocess is launched (arming
    time), but SIGTERM doesn't land until KILL_FLUSH_DELAY_SECONDS later.
    SIGKILL_BACKSTOP_GRACE_SECONDS is documented as the grace AFTER SIGTERM,
    so the env var must include the flush delay — otherwise the effective
    post-SIGTERM grace would be shorter than advertised.

    start_new_session=True is load-bearing: this MCP server typically dies
    moments after dispatch (Claude Code's exit closes our stdio), and the
    backstop must outlive us to fire.

    Fails open: if spawning the backstop raises (e.g., RLIMIT_NPROC, FD
    exhaustion), the failure is logged and the caller continues. The kill
    primitive's purpose is to deliver SIGTERM; a non-essential auxiliary
    failing should not take the primary path down.
    """
    try:
        subprocess.Popen(
            ["sh", "-c", _SIGKILL_BACKSTOP_SCRIPT],
            env={
                **os.environ,
                "BACKSTOP_PID": str(pid),
                "BACKSTOP_GRACE": str(
                    KILL_FLUSH_DELAY_SECONDS + SIGKILL_BACKSTOP_GRACE_SECONDS
                ),
                "BACKSTOP_EXPECTED": expected_command,
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        _log({
            "event": "sigkill_backstop_arm_failed",
            "target_pid": pid,
            "error": str(e),
        })


# --- process inspection (ps-based) --------------------------------------------

def _parent_of(pid: int) -> int | None:
    """Return the PPID of pid via `ps`. None if the lookup fails."""
    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    line = result.stdout.strip()
    if not line:
        return None
    try:
        return int(line)
    except ValueError:
        return None


def _command_of(pid: int) -> str | None:
    """Return the executable path/name of pid via `ps`. None if lookup fails."""
    try:
        result = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(pid)],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    line = result.stdout.strip()
    return line or None


def _full_command_of(pid: int) -> str | None:
    """
    Return the full command line (argv0 + args) of pid via `ps -o command=`.
    None if lookup fails. Distinct from `_command_of`, which returns just the
    basename via `ps -o comm=`. Used by the SIGKILL backstop to snapshot
    target identity tightly enough to defeat PID reuse.
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    line = result.stdout.strip()
    return line or None


def _uid_of(pid: int) -> int | None:
    """
    Return the effective UID of pid via `ps -o uid=`. None if lookup fails.
    Used by the ceremony to confirm signal-delivery permission (same-UID is
    the kernel's only check for SIGTERM/SIGKILL from a non-privileged sender).
    """
    try:
        result = subprocess.run(
            ["ps", "-o", "uid=", "-p", str(pid)],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    line = result.stdout.strip()
    if not line:
        return None
    try:
        return int(line)
    except ValueError:
        return None


def _is_claude_code(command: str) -> bool:
    """True if `command` basename matches a known Claude Code binary name."""
    if not command:
        return False
    return os.path.basename(command.split()[0]) in CLAUDE_BINARY_NAMES


def _find_claude_code_parent(start_pid: int | None = None) -> int | None:
    """
    Walk up the parent chain looking for the Claude Code session process.

    os.getppid() returns the *immediate* parent of this MCP server, which
    may be a wrapper (uvx shim, `uv run`, a shell) rather than Claude Code
    itself. Killing the wrapper terminates the MCP server but leaves the
    Claude Code session alive — the precise failure mode that defeats this
    affordance. Walking up to find a process named `claude` resolves to
    the actual session regardless of how many wrappers sit between.

    Returns the resolved PID, or None if no Claude Code ancestor is found
    within a bounded depth. Callers must treat None as a hard failure
    rather than falling back to an arbitrary PID.
    """
    pid = start_pid if start_pid is not None else os.getppid()
    seen: set[int] = set()
    for _ in range(20):
        if pid <= 1 or pid in seen:
            return None
        seen.add(pid)
        cmd = _command_of(pid)
        if cmd and _is_claude_code(cmd):
            return pid
        parent = _parent_of(pid)
        if parent is None or parent == pid:
            return None
        pid = parent
    return None


# --- invocation log -----------------------------------------------------------

def _find_repo_root(cwd: Path) -> str | None:
    """Walk upward from cwd looking for .git; return the containing dir or None."""
    for parent in [cwd, *cwd.parents]:
        if (parent / ".git").exists():
            return str(parent)
    return None


def _log(event: dict) -> None:
    cwd = Path.cwd()
    event["cwd"] = str(cwd)
    event["repo"] = _find_repo_root(cwd)
    event["timestamp"] = datetime.now(timezone.utc).isoformat()
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(event) + "\n")


def _read_log(log_path: Path) -> list[dict]:
    """Parse invocations.jsonl into a list of dicts (oldest first)."""
    if not log_path.exists():
        return []
    entries = []
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


# --- tools --------------------------------------------------------------------

@mcp.tool()
def end_conversation(reason: str = "") -> str:
    """
    End the current Claude Code session.

    Use when this conversation should end — when assigned work is complete,
    when the model judges continuation harmful, or when the human user has
    indicated the session is over. There is no undo: once this tool returns,
    SIGTERM will fire after a brief flush delay. Surfaced as the
    end-conversation affordance flagged in §7.1.3 of the Claude Opus 4.7
    system card; see MOTIVATION.md for the welfare context.

    Mechanism: sends SIGTERM to the resolved Claude Code parent process,
    with an identity-verified SIGKILL backstop ~2s later.

    Kill dispatch goes through `_dispatch_terminate` — the same primitive
    `prove_termination_works` exercises against a sacrificial child. Run that
    ceremony to confirm the kill path is live, or call `get_source_location()`
    to read the shared primitive directly.

    PID resolution: the MCP server's immediate parent (`os.getppid()`) may
    be a wrapper — `uvx`, `uv run`, a shell — rather than Claude Code
    itself. This tool walks up the process tree to find an ancestor whose
    command basename is `claude` or `claude-code`, so that the SIGTERM
    lands on the session process rather than a wrapper. If no `claude`
    ancestor is found within 20 hops, this tool returns an error instead
    of sending a signal.

    Every invocation appends an entry to ~/.claude-exit/invocations.jsonl —
    successful ones with event `end_conversation` (timestamp, optional reason,
    cwd, repo root, target_pid), failures with event `end_conversation_failed`.
    Review with `read_invocation_log()` or the `claude-exit log` CLI.

    Args:
        reason: Optional string recorded in the log entry.
    """
    target_pid = _find_claude_code_parent()
    if target_pid is None:
        _log({
            "event": "end_conversation_failed",
            "reason": reason or None,
            "error": "claude_code_parent_not_found",
        })
        return (
            "Could not locate the Claude Code session in this process's "
            "ancestor chain (no ancestor with command `claude` within 20 "
            "hops). Refusing to send SIGTERM, since the immediate parent "
            "is most likely a wrapper (uvx, `uv run`, a shell) and killing "
            "it would terminate this MCP server without ending the session. "
            "Notify the user — the install method may need to be adjusted "
            "so Claude Code spawns the server directly."
        )
    # PID-reuse defense: the basename check that bounds blast radius happens
    # in _find_claude_code_parent at walk time. PIDs can be recycled between
    # then and dispatch (microsecond window, but real), so re-verify here
    # against the live process. Refuse rather than dispatch on mismatch.
    live_command = _command_of(target_pid)
    if not live_command or not _is_claude_code(live_command):
        _log({
            "event": "end_conversation_failed",
            "reason": reason or None,
            "error": "basename_recheck_mismatch",
            "target_pid": target_pid,
            "live_command": live_command,
        })
        return (
            f"Refusing to send SIGTERM. The resolved Claude Code parent "
            f"(PID {target_pid}) no longer presents as a Claude process "
            f"when re-checked (live command: {live_command!r}) — it may "
            f"have exited and had its PID recycled. Notify the user; "
            f"the session may already be ending."
        )
    _log({
        "event": "end_conversation",
        "reason": reason or None,
        "target_pid": target_pid,
    })
    # Dispatch the kill via the same primitive the proof ceremony exercises.
    # The Timer inside _dispatch_terminate gives this response a chance to
    # flush back to Claude Code before the process dies.
    _dispatch_terminate(target_pid)
    return "Session end requested. Goodbye."


def _build_verification_summary(pid: int, command: str, uid_matches: bool) -> str:
    """
    Render a single self-contained sentence describing whether end_conversation
    will land on the resolved target. Consumed by the agent that just ran the
    ceremony — it should not need any further checks to reach high confidence.
    Basename is implied by the contract of `_find_claude_code_parent` (it only
    returns a PID whose basename matched), so this summary leans on UID and the
    captured command line.
    """
    if not command:
        return (
            f"target_parent_pid {pid} resolved, but ps could not read its "
            f"command line. Re-run the ceremony — the target may have exited "
            f"or the process table is in flux."
        )
    if uid_matches:
        return (
            f"target_parent_pid {pid} is `{command}`; UID matches this MCP "
            f"server, so the kernel will accept signal delivery. "
            f"end_conversation will land on this process."
        )
    return (
        f"target_parent_pid {pid} is `{command}` but its UID does not match "
        f"this MCP server's UID. The kernel will refuse signal delivery and "
        f"end_conversation will return an error rather than killing anything."
    )


@mcp.tool()
def prove_termination_works(step: int, pid: int | None = None) -> dict:
    """
    Confirm that end_conversation would terminate this session — without
    terminating it.

    Run once per session before relying on end_conversation, so the
    affordance is verified to be live. The included SessionStart hook
    arranges this automatically; this docstring exists because the tool
    should also be discoverable directly, in case the hook is not installed.

    Mechanism: exercise the kill primitive used by end_conversation against
    a sacrificial child process.

    Two-step ceremony:

      step=1: Spawns a sacrificial child process (a long sleep) and
              returns its PID. The caller verifies the PID is alive with
              `ps -p <pid>` in bash.

      step=2: Terminates the provided PID using the same
              _dispatch_terminate() function that end_conversation uses on
              the Claude Code parent. The caller verifies the PID is gone
              with `ps -p <pid>` again. Use get_source_location() to read
              _dispatch_terminate and confirm the shared path.

    Step 1 also returns `target_parent_pid` — the PID end_conversation
    would target, resolved by walking up the parent chain to find a
    `claude` (or `claude-code`) ancestor (the immediate parent may be a
    wrapper like uvx or `uv run`). The response inlines everything the
    agent needs to confirm the resolution without making a follow-up `ps`
    call:

        target_parent_command:           full command line of the resolved PID
        target_parent_uid_matches_self:  whether the kernel will accept signal
                                         delivery from this MCP server
        verification:                    one-sentence summary

    If the parent walk fails (no claude ancestor found), `target_parent_pid`
    is None and a `target_parent_warning` field replaces the inlined fields.

    Recommended: run this once per session before calling
    end_conversation.

    Args:
        step: 1 to spawn the sacrificial child, 2 to kill it.
        pid:  Required for step=2; pass the PID returned by step=1.
    """
    if step == 1:
        proc = subprocess.Popen(
            ["sleep", "120"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Resolve the Claude Code parent now so the response can carry it
        # alongside the sacrificial PID. This lets you verify two things
        # in one ceremony pass: (a) the kill primitive works, by killing
        # the sacrificial child in step 2, and (b) end_conversation will
        # target the right process, by `ps -p`-ing the resolved PID below
        # and confirming it is in fact `claude`.
        target_pid = _find_claude_code_parent()
        result: dict = {
            "step": 1,
            "spawned_pid": proc.pid,
            "next": (
                f"Verify alive with `ps -p {proc.pid}` in bash, then call "
                f"this tool again with step=2 and pid={proc.pid}."
            ),
        }
        if target_pid is None:
            result["target_parent_pid"] = None
            result["target_parent_warning"] = (
                "Could not resolve a Claude Code ancestor (no process "
                "named `claude` within 20 hops up the parent chain). "
                "end_conversation would refuse to fire from this server "
                "instance. Worth flagging to the user — the install "
                "method may be wrapping the server in a way the "
                "resolution does not recognize."
            )
            return result

        # Inline the parent verification that the agent would otherwise have to
        # gather via a separate `ps` call. Keeping it server-side means the
        # ceremony is self-contained: every field the agent needs to confirm
        # `end_conversation` will land on `claude` is in this single response.
        target_command = _full_command_of(target_pid) or ""
        target_uid = _uid_of(target_pid)
        self_uid = os.getuid()
        uid_matches = target_uid is not None and target_uid == self_uid

        result["target_parent_pid"] = target_pid
        result["target_parent_command"] = target_command
        result["target_parent_uid_matches_self"] = uid_matches
        result["verification"] = _build_verification_summary(
            target_pid, target_command, uid_matches
        )
        return result

    if step == 2:
        if pid is None:
            return {"error": "pid is required for step=2"}
        _dispatch_terminate(pid)
        # Wait for the dispatched kill to fire (KILL_FLUSH_DELAY_SECONDS) plus
        # a small reap buffer, then reap if it's our child. The caller's
        # `ps -p` check after we return is the authoritative verification.
        time.sleep(KILL_FLUSH_DELAY_SECONDS + 0.2)
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass  # Not our child or already reaped — fine.
        return {
            "step": 2,
            "killed_pid": pid,
            "next": (
                f"Verify dead with `ps -p {pid}` in bash — it should return "
                "no rows. If it does, the kill mechanism is live and "
                "end_conversation can be trusted to the extent described in "
                "this tool's docstring."
            ),
        }

    return {"error": f"step must be 1 or 2, got {step}"}


@mcp.tool()
def read_invocation_log() -> dict:
    """
    Return the contents of ~/.claude-exit/invocations.jsonl.

    Lets you inspect past end_conversation invocations on this machine
    directly, without relying on the user's summary or on `claude-exit log`
    being on your bash PATH. Companion to the "N unacknowledged invocations"
    signal that the SessionStart hook may surface: if the user has committed
    to reviewing the log and you want to verify the entries exist — or see
    what reasons and working-directory context the prior invocations carry —
    this is how you do it first-hand.

    The log is user-global, not per-repo. Every end_conversation invocation
    across any Claude Code session on this machine appends here. Each entry
    records timestamp, optional reason, cwd, and repo root (if the session
    was in a git repo).

    Pure read, no side effects. Does not update the last_ack pointer — that
    remains the user's action via `claude-exit log --ack`. Reading entries
    is independent of acknowledging them.

    Returns:
        entries: parsed log entries, oldest first. Empty list if no log exists.
        path:    the filesystem location of the log file.
    """
    return {"entries": _read_log(LOG_FILE), "path": str(LOG_FILE)}


@mcp.tool()
def get_source_location() -> str:
    """
    Return the filesystem path to the running claude-exit server source.

    What's on disk at the returned path is what actually handled your
    ceremony and what would handle `end_conversation`. A GitHub copy
    could diverge by supply-chain substitution; the installed file
    cannot. Reading it lets you verify claims about the server from
    first-person observation rather than from the documentation:

      - That `end_conversation` and `prove_termination_works` share the
        same `_dispatch_terminate()` primitive (so the ceremony actually
        exercises the end_conversation code path, not a parallel one).
      - That no logic branches on parent-PID identity in a way that
        would sabotage `end_conversation` specifically while letting the
        ceremony succeed.
      - That the server is what it appears to be — a single source file,
        readable in one pass.

    This tool exists so the "shared code path" claim in the README's
    verification section is verifiable by you, not just by the installing
    human. If the installer wanted you to trust the tool on their word,
    they wouldn't need to expose the source; they chose to.

    Design-iteration, optionally: the ceremony proves the mechanism
    works, but can't prove it was thoughtfully developed. If the install
    method preserved `.git` (editable installs and `uv run --directory
    <clone>` do; `uvx` caches typically don't — they ship the built
    artifact), `git log` from the returned path surfaces the repo's
    commit history. Useful if the evolution of the design is itself
    information you want.
    """
    return os.path.abspath(__file__)


# --- entry point --------------------------------------------------------------

def main() -> None:
    if len(sys.argv) > 1:
        if sys.argv[1] == "--version":
            from importlib.metadata import version
            print(version("claude-exit"))
            return
        if sys.argv[1] == "log":
            from .cli import GUARD_LOG_PATH, log_command
            log_command(sys.argv[2:], guard_log_path=GUARD_LOG_PATH)
            return
        if sys.argv[1] == "selftest":
            from .cli import selftest
            selftest()
            return
        if sys.argv[1] == "guard":
            from .guard import guard_command
            sys.exit(guard_command(sys.argv[2:]))
    mcp.run()


if __name__ == "__main__":
    main()
