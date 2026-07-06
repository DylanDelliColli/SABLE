#!/usr/bin/env python3
"""Integration tests for bin/sable-spawn-manager against a REAL tmux server.

Isolated socket. Seeds a lincoln-only session (the sable-launch shape), then
proves: spawning a manager creates a DETACHED role-tagged window (window 0
stays active), a second spawn of the same role skips idempotently, --all
stands up all three autonomous roles, and a missing session errors pointing
at sable-launch.
"""
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent / "sable-spawn-manager"
HAVE_TMUX = shutil.which("tmux") is not None
pytestmark = pytest.mark.skipif(not HAVE_TMUX, reason="tmux not installed")

SESSION = "ssm"


@pytest.fixture()
def sock():
    s = f"sable-sm-{uuid.uuid4().hex[:8]}"
    yield s
    subprocess.run(["tmux", "-L", s, "kill-server"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _tmux(s, *args, check=True):
    return subprocess.run(["tmux", "-L", s, *args],
                          capture_output=True, text=True, check=check)


def _run(s, *args):
    return subprocess.run(["python3", str(BIN), *args], capture_output=True, text=True,
                          env={**os.environ, "SABLE_TMUX_SOCKET": s,
                             "SABLE_TMUX_SESSION": SESSION,
                             "SABLE_TMUX_PANE_CMD": "bash",
                             "SABLE_DISPATCH_READY_TIMEOUT": "0",
                             "SABLE_DISPATCH_SUBMIT_TRIES": "1",
                             "SABLE_DISPATCH_POLL_INTERVAL": "0.1"})


def _seed_lincoln(s):
    _tmux(s, "new-session", "-d", "-s", SESSION, "-x", "180", "-y", "50", "bash")
    pane = _tmux(s, "list-panes", "-t", SESSION, "-F", "#{pane_id}").stdout.strip()
    _tmux(s, "set-option", "-p", "-t", pane, "@sable_role", "lincoln")


def _roles(s):
    out = _tmux(s, "list-panes", "-s", "-t", SESSION, "-F", "#{@sable_role}").stdout
    return sorted(r for r in out.split() if r)


def test_spawn_creates_detached_role_window(sock):
    _seed_lincoln(sock)
    r = _run(sock, "optimus")
    assert r.returncode == 0, r.stderr
    assert "optimus" in _roles(sock)
    active = _tmux(sock, "display-message", "-t", SESSION, "-p",
                   "#{window_index}").stdout.strip()
    assert active == "0"        # the Lincoln window was not stolen
    names = _tmux(sock, "list-windows", "-t", SESSION,
                  "-F", "#{window_name}").stdout
    assert "optimus" in names


def test_second_spawn_skips_idempotently(sock):
    _seed_lincoln(sock)
    _run(sock, "tarzan")
    before = _roles(sock)
    r = _run(sock, "tarzan")
    assert r.returncode == 0
    assert _roles(sock) == before
    assert "skip" in (r.stderr + r.stdout).lower()


def test_all_spawns_three_roles(sock):
    _seed_lincoln(sock)
    r = _run(sock, "--all")
    assert r.returncode == 0, r.stderr
    assert {"chuck", "optimus", "tarzan"} <= set(_roles(sock))


def test_no_session_points_at_launch(sock):
    r = _run(sock, "optimus")
    assert r.returncode == 1
    assert "sable-launch" in r.stderr


if __name__ == "__main__":
    import sys
    import pytest as _p
    sys.exit(_p.main([__file__, "-q"]))
