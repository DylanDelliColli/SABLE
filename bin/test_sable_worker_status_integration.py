#!/usr/bin/env python3
"""Integration tests for bin/sable-worker-status against a REAL tmux server.

Isolated socket (-L). Proves: worker panes are discovered by their @sable_role/
@sable_bead/@sable_status user-options, a done worker is reported done, and
--reap kills ONLY the done worker pane (the running one survives).
"""
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent / "sable-worker-status"
HAVE_TMUX = shutil.which("tmux") is not None
pytestmark = pytest.mark.skipif(not HAVE_TMUX, reason="tmux not installed")


@pytest.fixture()
def sock():
    s = f"sable-ws-{uuid.uuid4().hex[:8]}"
    yield s
    subprocess.run(["tmux", "-L", s, "kill-server"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _tmux(s, *args, check=True):
    return subprocess.run(["tmux", "-L", s, *args],
                          capture_output=True, text=True, check=check)


def _tag(s, target, role, bead, status):
    _tmux(s, "set-option", "-p", "-t", target, "@sable_role", role)
    _tmux(s, "set-option", "-p", "-t", target, "@sable_bead", bead)
    _tmux(s, "set-option", "-p", "-t", target, "@sable_status", status)


def _run(s, *args):
    import os
    # SABLE_TMUX_SESSION pinned: single-fleet cases use the operator override;
    # per-repo resolution is covered by the two-fleet reap test below.
    return subprocess.run(["python3", str(BIN), *args], capture_output=True, text=True,
                          env={**os.environ, "SABLE_TMUX_SOCKET": s,
                               "SABLE_TMUX_SESSION": "w"})


def _pane_count(s):
    out = _tmux(s, "list-panes", "-a", "-F", "#{pane_id}").stdout
    return len([ln for ln in out.splitlines() if ln.strip()])


def test_reports_done_and_running(sock):
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "180", "-y", "40",
          "bash --noprofile --norc")
    time.sleep(0.4)
    # window w pane 0 = a done worker; split for a running worker
    _tag(sock, "w.0", "worker", "bead-done", "done")
    _tmux(sock, "split-window", "-t", "w", "bash --noprofile --norc")
    time.sleep(0.4)
    _tag(sock, "w.1", "worker", "bead-run", "running")

    r = _run(sock)
    assert r.returncode == 0, r.stderr
    assert "bead-done" in r.stdout and "done" in r.stdout
    assert "bead-run" in r.stdout and "running" in r.stdout


def test_reap_kills_only_done_pane(sock):
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "180", "-y", "40",
          "bash --noprofile --norc")
    time.sleep(0.4)
    _tag(sock, "w.0", "worker", "bead-done", "done")
    _tmux(sock, "split-window", "-t", "w", "bash --noprofile --norc")
    time.sleep(0.4)
    _tag(sock, "w.1", "worker", "bead-run", "running")
    assert _pane_count(sock) == 2

    r = _run(sock, "--reap")
    assert r.returncode == 0, r.stderr
    time.sleep(0.4)
    assert _pane_count(sock) == 1  # only the running worker survives
    survivors = _tmux(sock, "list-panes", "-a", "-F", "#{@sable_bead}").stdout
    assert "bead-run" in survivors
    assert "bead-done" not in survivors


def test_reap_scoped_to_caller_repo(sock, tmp_path):
    """SABLE-e1e3.3 deny leg: --reap run from repo alpha must kill alpha's done
    worker and leave repo beta's done worker alive."""
    import os
    sessions = {}
    for name in ("alpha", "beta"):
        repo = tmp_path / name
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        sess = f"sable-{name}"
        _tmux(sock, "new-session", "-d", "-s", sess, "-x", "180", "-y", "40",
              "bash --noprofile --norc")
        time.sleep(0.3)
        _tmux(sock, "set-option", "-t", sess, "@sable_repo", str(repo.resolve()))
        _tag(sock, f"{sess}:0.0", "worker", f"bead-{name}", "done")
        sessions[name] = (repo, sess)

    env = {**os.environ, "SABLE_TMUX_SOCKET": sock}
    env.pop("SABLE_TMUX_SESSION", None)
    r = subprocess.run(["python3", str(BIN), "--reap"], capture_output=True,
                       text=True, env=env, cwd=sessions["alpha"][0])
    assert r.returncode == 0, r.stderr
    time.sleep(0.4)
    # alpha's done worker (its only pane -> the session) is gone
    assert _tmux(sock, "has-session", "-t", "sable-alpha",
                 check=False).returncode != 0
    # beta's done worker is untouched
    survivors = _tmux(sock, "list-panes", "-a", "-F", "#{@sable_bead}").stdout
    assert "bead-beta" in survivors


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
