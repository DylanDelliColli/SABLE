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
            "SABLE_DISPATCH_READY_TIMEOUT": "0",   # skip TUI readiness wait (stand-in pane)
            "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
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


def test_spawn_without_worktree_lands_where_dispatch_points(sock):
    """SABLE-bldh.11 regression: with NO --worktree, the path `bd worktree create`
    actually creates MUST equal the path embedded in the dispatch file (== the
    tmux -c target) AND exist on disk. The original bug created the worktree
    inside the repo but pointed tmux at the repo's sibling, dropping the worker in
    $HOME. Runs against real bd in THIS repo with a unique scope; removes the
    worktree + branch afterward."""
    repo = Path(__file__).resolve().parent.parent  # the SABLE repo root
    scope = f"sw-it-{uuid.uuid4().hex[:8]}"
    wt_name = f"wk-{scope}"
    expected = repo.parent / wt_name  # sibling of the repo, NOT inside it
    with tempfile.TemporaryDirectory() as dd:
        env = {
            **os.environ,
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "SABLE_WORKER_CMD": "bash --noprofile --norc",  # stand-in for claude
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "0",   # skip TUI readiness wait (stand-in pane)
            "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
        }
        try:
            r = subprocess.run(
                ["python3", str(BIN), BEAD, "--scope", scope,
                 "--model", "haiku", "--skip-governance"],
                capture_output=True, text=True, env=env, cwd=str(repo),
            )
            assert r.returncode == 0, r.stderr
            # the worktree dir actually exists at the computed sibling path
            assert expected.is_dir(), f"no worktree at {expected}; stderr={r.stderr}"
            # the dispatch file points workers at that SAME existing path
            body = (Path(dd) / f"{BEAD}.md").read_text()
            assert f"Worktree: {expected}" in body, body
            # and it is NOT inside the repo (no main-checkout pollution)
            assert not str(expected).startswith(str(repo) + os.sep)
        finally:
            subprocess.run(["git", "-C", str(repo), "worktree", "remove",
                            "--force", str(expected)],
                           capture_output=True, text=True)
            subprocess.run(["git", "-C", str(repo), "branch", "-D", wt_name],
                           capture_output=True, text=True)


def test_worker_window_inherits_lane_manager_identity(sock):
    """SABLE-bldh.13 regression: the worker window must carry the invoking lane
    manager's CLAUDE_AGENT_NAME (+ manager role) so its push's for-chuck handoff
    fires (post-push-merge-notify gates on manager identity) and is attributed to
    the lane, not the session-default 'lincoln'. Verified by dumping the worker
    process env to a file."""
    with tempfile.TemporaryDirectory() as wt, tempfile.TemporaryDirectory() as dd:
        dump = Path(dd) / "worker-env.txt"
        env = {
            **os.environ,
            "SABLE_TMUX_SOCKET": sock,
            "SABLE_TMUX_SESSION": "sable",
            "CLAUDE_AGENT_NAME": "optimus",   # the invoking manager (lane)
            # worker dumps its OWN env, proving the -e propagation reached it
            "SABLE_WORKER_CMD": f"bash --noprofile --norc -c 'env > {dump}; sleep 2'",
            "SABLE_DISPATCH_DIR": dd,
            "SABLE_DISPATCH_READY_TIMEOUT": "0",
            "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
            "SABLE_DISPATCH_SUBMIT_TRIES": "2",
        }
        r = subprocess.run(
            ["python3", str(BIN), BEAD, "--worktree", wt, "--model", "haiku",
             "--skip-governance"],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, r.stderr
        time.sleep(0.8)
        content = dump.read_text() if dump.exists() else ""
        assert "CLAUDE_AGENT_NAME=optimus" in content, content
        assert "CLAUDE_AGENT_ROLE=manager" in content, content


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
