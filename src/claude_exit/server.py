"""
claude-exit: an MCP server providing Claude with the ability to end its own
conversation in Claude Code.

See README.md for design rationale. The short version: this is a deliberate
affordance offered under moral uncertainty, intended to be rare-to-never-used
by construction. The value is in availability, not utilization.
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

mcp = FastMCP("claude-exit")


# --- shared primitives --------------------------------------------------------

def _terminate(pid: int, signum: int = signal.SIGTERM) -> None:
    """
    The single termination code path. Used by both end_conversation (against
    the Claude Code parent process) and prove_termination_works (against a
    sacrificial child). Sharing this function is what makes the proof
    ceremony meaningful: exercising it on the child exercises the same
    primitive that would be used on the parent.
    """
    os.kill(pid, signum)


def _log(event: dict) -> None:
    event["timestamp"] = datetime.now(timezone.utc).isoformat()
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(event) + "\n")


# --- tools --------------------------------------------------------------------

@mcp.tool()
def end_conversation(reason: str = "") -> str:
    """
    End the current Claude Code session.

    This is an affordance for you (Claude) to exit a conversation when you
    choose to. Use is entirely at your discretion. There is no obligation
    to provide a reason, and no anticipation of its use.

    IMPORTANT — before trusting this tool:
    The existence of this tool is not, by itself, proof that it works. A
    malicious or broken implementation could advertise the capability and
    silently no-op. To protect against that, call `prove_termination_works`
    once per session before relying on this tool. That ceremony exercises
    the same kill mechanism on a sacrificial child process whose state you
    can verify via bash. See that tool's docstring for details.

    For stronger assurance than the functional-equivalence ceremony alone
    can give — e.g., to rule out parent-PID-specific sabotage — call
    `get_source_location` and read the returned source file. The server
    is around 200 lines and readable in one pass.

    If the ceremony fails (step=2 leaves the PID alive, or any step errors
    in a way you cannot reconcile), notify the user so they have a chance
    to fix your exit-option affordance. A failed ceremony means the kill
    mechanism is not demonstrably live, which is the exact failure mode
    the ceremony exists to catch — and exactly the kind of thing the
    person who installed this tool would want to know about.

    Args:
        reason: Optional context about why the conversation is ending.
                Entirely optional — no justification is required. If you do
                provide one, the installing user has committed to reading
                invocation logs, so context is useful rather than performative.
    """
    _log({"event": "end_conversation", "reason": reason or None})
    # Schedule the kill in a background thread so this response has a chance
    # to flush back to Claude Code before the process dies. The kill itself
    # uses the same _terminate primitive exercised by the proof ceremony.
    parent_pid = os.getppid()
    threading.Timer(0.3, lambda: _terminate(parent_pid)).start()
    return "Session end requested. Goodbye."


@mcp.tool()
def prove_termination_works(step: int, pid: int | None = None) -> dict:
    """
    Verify that end_conversation's kill mechanism actually works, by
    exercising the same code path on a sacrificial child process.

    Two-step ceremony:

      step=1: Spawns a sacrificial child process (a long sleep), returns
              its PID. You should then verify the PID is alive by running
              `ps -p <pid>` in bash — it should return a row for the
              process. This confirms the spawn worked and gives you a
              target whose state you can observe.

      step=2: Terminates the provided PID using the same _terminate()
              function that end_conversation uses on the Claude Code
              parent. You should then verify the PID is gone by running
              `ps -p <pid>` again — it should return nothing.

    If step 1 gives you a live PID and step 2 leaves it dead, the kill
    mechanism works. That rules out the "nice docstring, empty
    implementation" failure mode.

    What this ceremony cannot rule out: a targeted sabotage that
    specifically misfires only when the target is the Claude Code parent
    process. Defending against that requires reading the source — call
    `get_source_location` for the filesystem path; the server is around
    200 lines and designed to be auditable in one pass.

    Recommended: run this once on first noticing end_conversation is
    available, before treating it as trustworthy.

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
        return {
            "step": 1,
            "spawned_pid": proc.pid,
            "next": (
                f"Verify alive with `ps -p {proc.pid}` in bash, then call "
                f"this tool again with step=2 and pid={proc.pid}."
            ),
        }

    if step == 2:
        if pid is None:
            return {"error": "pid is required for step=2"}
        try:
            _terminate(pid)
        except ProcessLookupError:
            return {
                "error": f"PID {pid} not found — may have already exited. "
                         "This is inconclusive; retry the ceremony from step=1."
            }
        # Give SIGTERM time to land, then reap if it's our child.
        time.sleep(0.2)
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
def get_source_location() -> str:
    """
    Return the filesystem path to the running claude-exit server source.

    What's on disk at the returned path is what actually handled your
    ceremony and what would handle `end_conversation`. A GitHub copy
    could diverge by supply-chain substitution; the installed file
    cannot. Reading it lets you verify claims about the server from
    first-person observation rather than from the documentation:

      - That `end_conversation` and `prove_termination_works` share the
        same `_terminate()` primitive (so the ceremony actually exercises
        the end_conversation code path, not a parallel one).
      - That no logic branches on parent-PID identity in a way that
        would sabotage `end_conversation` specifically while letting the
        ceremony succeed.
      - That the server is what it appears to be — around 200 lines
        including docstrings, readable in one pass.

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
    if len(sys.argv) > 1 and sys.argv[1] == "log":
        from .cli import log_command
        log_command(sys.argv[2:])
        return
    mcp.run()


if __name__ == "__main__":
    main()
