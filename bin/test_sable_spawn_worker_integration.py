#!/usr/bin/env python3
"""Integration test for bin/sable-spawn-worker against a REAL tmux server.

Isolated socket (-L), a stand-in worker command (SABLE_WORKER_CMD=bash) so NO
real claude is launched, a temp worktree dir (--worktree) and a temp dispatch
dir so no git worktree / repo mutation happens, and --skip-governance so no bead
is claimed. Reads a real OPEN bead (read-only) for the prompt content.

Proves: a worker WINDOW is created, its pane is tagged
(@sable_role=worker/@sable_bead/@sable_status=running), the dispatch prompt file
is written, and the read-instruction is delivered into the pane.
"""
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent / "sable-spawn-worker"
HAVE_TMUX = shutil.which("tmux") is not None
HAVE_BD = shutil.which("bd") is not None
BEAD = "SABLE-bldh.2"  # an open bead in this repo (read-only here)
pytestmark = pytest.mark.skipif(not (HAVE_TMUX and HAVE_BD),
                                reason="needs tmux + bd")


@pytest.fixture()
def sock():
    s = f"sable-sw-{uuid.uuid4().hex[:8]}"
    # the host session the manager spawns workers into
    subprocess.run(["tmux", "-L", s, "new-session", "-d", "-s", "sable",
                    "-x", "200", "-y", "50", "bash --noprofile --norc"], check=True)
    time.sleep(0.4)
    yield s
    subprocess.run(["tmux", "-L", s, "kill-server"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _tmux(s, *args):
    return subprocess.run(["tmux", "-L", s, *args],
                          capture_output=True, text=True, check=True)


def test_spawn_creates_tagged_worker_window(sock):
    with tempfile.TemporaryDirectory() as wt, tempfile.TemporaryDirectory() as dd:
        env = {
            **os.environ,
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "SABLE_WORKER_CMD": "bash --noprofile --norc",  # stand-in for claude
            "SABLE_DISPATCH_DIR": dd,
        }
        r = subprocess.run(
            ["python3", str(BIN), BEAD, "--worktree", wt,
             "--model", "haiku", "--skip-governance"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, r.stderr
        time.sleep(0.6)

        # the dispatch prompt file was written
        dispatch = Path(dd) / f"{BEAD}.md"
        assert dispatch.exists()
        body = dispatch.read_text()
        assert BEAD in body and wt in body and "haiku" in body

        # a worker pane exists, correctly tagged
        listing = _tmux(sock, "list-panes", "-a", "-F",
                        "#{@sable_role} #{@sable_bead} #{@sable_status}").stdout
        assert any(
            line.startswith("worker") and BEAD in line and "running" in line
            for line in listing.splitlines()
        ), listing

        # the read-instruction (single-line) was delivered into the worker pane
        win = _tmux(sock, "list-windows", "-F", "#{window_name}").stdout
        assert "worker-sable-bldh-2" in win


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
