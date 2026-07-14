"""Tests for the version handshake between the SessionStart hook and the server.

Motivation (issue #17): the hook deploys via symlink/curl-copy and tracks the
repo, while the server deploys via `uv tool install` and freezes at install
time. The two form a coupled interface with no version handshake, so skew
is silent. These tests specify the handshake: the hook carries an
EXPECTED_SERVER_VERSION marker (kept equal to pyproject's version by CI),
compares it against the configured server at session start, and emits a
visible line for every branch except tool-not-configured and
check-ran-and-matched. No silent skips.

Also covers the `claude-exit --version` CLI flag the handshake relies on.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = REPO_ROOT / "hooks" / "session-start.sh"


def _pyproject_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text()
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    assert match, "could not find version in pyproject.toml"
    return match.group(1)


# --- harness -------------------------------------------------------------------


def _run(home: Path, path_override: str | None = None) -> tuple[int, str, str]:
    """Run the hook with HOME=home; optionally override PATH entirely."""
    env = os.environ.copy()
    env["HOME"] = str(home)
    if path_override is not None:
        env["PATH"] = path_override
    result = subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        env=env,
        cwd=home,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode, result.stdout, result.stderr


def _context(out: str) -> str:
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def _path_with_fake_bin_first(fake_bin: Path) -> str:
    """Real PATH with fake_bin prepended, so a fake claude-exit shadows any real one."""
    return f"{fake_bin}:{os.environ.get('PATH', '')}"


def _path_without_claude_exit() -> str:
    """Real PATH with every dir containing a claude-exit executable removed.

    python3 and bash must survive the filter or the hook can't run at all.
    """
    kept = [
        d for d in os.environ.get("PATH", "").split(":")
        if d and not (Path(d) / "claude-exit").exists()
    ]
    path = ":".join(kept)
    assert shutil.which("claude-exit", path=path) is None
    assert shutil.which("python3", path=path) is not None, (
        "PATH filter removed python3; test harness assumption broken"
    )
    return path


def _configure_command(home: Path, command: str = "claude-exit") -> None:
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {"claude-exit": {"command": command}}
    }))


def _configure_uvx(home: Path) -> None:
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {
            "claude-exit": {
                "command": "uvx",
                "args": ["--from", "git+https://github.com/danparshall/claude-exit", "claude-exit"],
            }
        }
    }))


def _configure_uv_run_directory(home: Path, directory: Path) -> None:
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {
            "claude-exit": {
                "command": "uv",
                "args": ["run", "--directory", str(directory), "claude-exit"],
            }
        }
    }))


def _fake_server(bin_dir: Path, version: str | None) -> None:
    """Install a fake claude-exit that answers --version (or fails if version is None)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude-exit"
    if version is None:
        # Faithful to a real <= 1.1.0 server: main() has no unknown-arg
        # handling, so `claude-exit --version` falls through to mcp.run(),
        # which serves MCP over stdio — it reads stdin until EOF, prints no
        # version to stdout, and exits. A hook that doesn't close the child's
        # stdin hangs here; the elapsed-time bound in the test catches that.
        body = "#!/usr/bin/env bash\ncat > /dev/null 2>&1\nexit 0\n"
    else:
        body = f'#!/usr/bin/env bash\nif [ "$1" = "--version" ]; then echo "{version}"; exit 0; fi\nexit 2\n'
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _fake_checkout(root: Path, version: str) -> Path:
    """A directory that looks like a claude-exit checkout for uv run --directory."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "claude-exit"\nversion = "{version}"\n'
    )
    return root


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path


# --- marker consistency (the in-repo synchronized pair, CI-enforced) ------------


def test_hook_marker_matches_pyproject_version():
    """EXPECTED_SERVER_VERSION in the hook must equal pyproject's version.

    This is the in-repo half of the handshake: hook and pyproject live in
    the same repo, so a test can force them equal. The runtime check covers
    the cross-channel half that no test can reach.
    """
    text = HOOK_SCRIPT.read_text()
    match = re.search(
        r'^EXPECTED_SERVER_VERSION\s*=\s*"([^"]+)"', text, flags=re.MULTILINE
    )
    assert match, "hook has no EXPECTED_SERVER_VERSION marker"
    assert match.group(1) == _pyproject_version()


# --- handshake branches ----------------------------------------------------------


def test_matching_version_is_silent(home):
    fake_bin = home / "fakebin"
    _fake_server(fake_bin, _pyproject_version())
    _configure_command(home)
    rc, out, _ = _run(home, path_override=_path_with_fake_bin_first(fake_bin))
    assert rc == 0
    ctx = _context(out)
    # Ceremony text still present; no handshake complaint of any flavor.
    assert "prove_termination_works" in ctx
    assert "uv tool upgrade" not in ctx
    assert "re-fetch" not in ctx
    assert "session-start.sh" not in ctx
    assert "not found on PATH" not in ctx


def test_lagging_server_warns_with_upgrade_remedy(home):
    fake_bin = home / "fakebin"
    _fake_server(fake_bin, "0.0.1")
    _configure_command(home)
    _, out, _ = _run(home, path_override=_path_with_fake_bin_first(fake_bin))
    ctx = _context(out)
    assert "0.0.1" in ctx
    assert "lags" in ctx
    assert "uv tool upgrade claude-exit" in ctx


def test_server_without_version_flag_warns_with_upgrade_remedy(home):
    """Currently-installed servers (<= 1.1.0) have no --version flag at all.

    That failure mode must itself read as 'server predates the handshake':
    the very machines that need the upgrade are the ones that can't answer.
    The fake blocks on stdin like the real legacy fall-through to mcp.run(),
    so the hook must close the child's stdin (and bound the wait) or this
    test blows its elapsed-time budget.
    """
    fake_bin = home / "fakebin"
    _fake_server(fake_bin, version=None)
    _configure_command(home)
    started = time.monotonic()
    _, out, _ = _run(home, path_override=_path_with_fake_bin_first(fake_bin))
    elapsed = time.monotonic() - started
    ctx = _context(out)
    assert "predates" in ctx
    assert "uv tool upgrade claude-exit" in ctx
    assert elapsed < 15, f"hook took {elapsed:.1f}s against a legacy server"


def test_newer_server_warns_hook_copy_stale(home):
    fake_bin = home / "fakebin"
    _fake_server(fake_bin, "99.0.0")
    _configure_command(home)
    _, out, _ = _run(home, path_override=_path_with_fake_bin_first(fake_bin))
    ctx = _context(out)
    assert "99.0.0" in ctx
    assert "hook" in ctx
    assert "re-fetch" in ctx.lower() or "session-start.sh" in ctx


def test_unresolvable_command_warns_loudly(home):
    _configure_command(home)
    _, out, _ = _run(home, path_override=_path_without_claude_exit())
    ctx = _context(out)
    assert "not found on PATH" in ctx
    assert "fail to launch" in ctx


def test_uv_run_directory_mismatch_warns_with_checkout_remedy(home):
    checkout = _fake_checkout(home / "checkout", "0.0.1")
    _configure_uv_run_directory(home, checkout)
    _, out, _ = _run(home)
    ctx = _context(out)
    assert "0.0.1" in ctx
    assert str(checkout) in ctx


def test_uv_run_directory_match_is_silent(home):
    checkout = _fake_checkout(home / "checkout", _pyproject_version())
    _configure_uv_run_directory(home, checkout)
    rc, out, _ = _run(home)
    assert rc == 0
    ctx = _context(out)
    assert "prove_termination_works" in ctx
    assert "uv tool upgrade" not in ctx
    assert str(checkout) not in ctx


@pytest.mark.parametrize("uvx_args", [
    ["--from", "git+https://github.com/danparshall/claude-exit", "claude-exit"],
    ["claude-exit"],
])
def test_uvx_config_notes_check_unavailable(home, uvx_args):
    """uvx servers can't be version-checked cheaply — the skip must be visible.

    Covers both config shapes: `uvx --from git+... claude-exit` (README) and
    bare `uvx claude-exit` (the shape test_hook.py uses incidentally).
    """
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {"claude-exit": {"command": "uvx", "args": uvx_args}}
    }))
    _, out, _ = _run(home)
    ctx = _context(out)
    assert "uvx" in ctx
    assert "handshake" in ctx


# --- CLI flag the handshake depends on -------------------------------------------


def test_cli_version_flag_prints_package_version(capsys, monkeypatch):
    from claude_exit import server

    monkeypatch.setattr("sys.argv", ["claude-exit", "--version"])
    server.main()
    out = capsys.readouterr().out.strip()
    assert out == _pyproject_version()
