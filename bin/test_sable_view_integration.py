#!/usr/bin/env python3
"""Integration tests for bin/sable-view against a REAL tmux server.

Isolated socket (-L). Seeds a stand-in sable session (bash panes tagged with
@sable_role) plus a worker window, then proves: the no-arg table lists every
role, --json is machine-readable, --tail returns pane content without changing
focus, the focus form (--no-attach) selects the target window, unknown roles
exit 2 listing known roles, and a missing session exits 1 pointing at
sable-launch.
"""
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent / "sable-view"
HAVE_TMUX = shutil.which("tmux") is not None
pytestmark = pytest.mark.skipif(not HAVE_TMUX, reason="tmux not installed")

SESSION = "sview"


@pytest.fixture()
def sock():
    s = f"sable-view-{uuid.uuid4().hex[:8]}"
    yield s
    subprocess.run(["tmux", "-L", s, "kill-server"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _tmux(s, *args, check=True):
    return subprocess.run(["tmux", "-L", s, *args],
                          capture_output=True, text=True, check=check)


def _run(s, *args):
    return subprocess.run(["python3", str(BIN), *args], capture_output=True, text=True,
                          env={**os.environ, "SABLE_TMUX_SOCKET": s,
                             "SABLE_TMUX_SESSION": SESSION})


def _seed_session(s):
    """Stand-in session: window 0 holds four role panes; window 1 a worker."""
    _tmux(s, "new-session", "-d", "-s", SESSION, "-x", "180", "-y", "50", "bash")
    panes = {"lincoln": _tmux(s, "list-panes", "-t", SESSION,
                              "-F", "#{pane_id}").stdout.strip()}
    for role in ("optimus", "tarzan", "chuck"):
        panes[role] = _tmux(s, "split-window", "-t", SESSION, "-P",
                            "-F", "#{pane_id}", "bash").stdout.strip()
        _tmux(s, "select-layout", "-t", SESSION, "tiled")
    for role, pane in panes.items():
        _tmux(s, "set-option", "-p", "-t", pane, "@sable_role", role)
    wpane = _tmux(s, "new-window", "-t", SESSION, "-P", "-F", "#{pane_id}",
                  "bash").stdout.strip()
    _tmux(s, "set-option", "-p", "-t", wpane, "@sable_role", "worker")
    _tmux(s, "set-option", "-p", "-t", wpane, "@sable_bead", "SABLE-int1")
    _tmux(s, "set-option", "-p", "-t", wpane, "@sable_status", "running")
    _tmux(s, "select-window", "-t", f"{SESSION}:0")
    return panes, wpane


def test_table_lists_all_roles(sock):
    _seed_session(sock)
    r = _run(sock)
    assert r.returncode == 0, r.stderr
    for role in ("lincoln", "optimus", "tarzan", "chuck", "worker"):
        assert role in r.stdout
    assert "SABLE-int1" in r.stdout


def test_json_output(sock):
    _seed_session(sock)
    r = _run(sock, "--json")
    assert r.returncode == 0, r.stderr
    records = json.loads(r.stdout)
    roles = {rec["role"] for rec in records}
    assert {"lincoln", "optimus", "tarzan", "chuck", "worker"} <= roles


def test_tail_returns_pane_content_without_focus_change(sock):
    panes, _ = _seed_session(sock)
    _tmux(sock, "send-keys", "-t", panes["optimus"], "echo view-mark-42", "Enter")
    time.sleep(0.5)
    r = _run(sock, "optimus", "--tail")
    assert r.returncode == 0, r.stderr
    assert "view-mark-42" in r.stdout
    active = _tmux(sock, "display-message", "-t", SESSION, "-p",
                   "#{window_index}").stdout.strip()
    assert active == "0"


def test_focus_selects_target_window(sock):
    _seed_session(sock)
    r = _run(sock, "worker", "--no-attach")
    assert r.returncode == 0, r.stderr
    active = _tmux(sock, "display-message", "-t", SESSION, "-p",
                   "#{window_index}").stdout.strip()
    assert active == "1"


def test_unknown_role_exits_2_listing_known(sock):
    _seed_session(sock)
    r = _run(sock, "bogus")
    assert r.returncode == 2
    assert "optimus" in r.stderr


def test_no_session_exits_1_pointing_at_launch(sock):
    r = _run(sock)
    assert r.returncode == 1
    assert "sable-launch" in r.stderr


if __name__ == "__main__":
    import sys
    import pytest as _p
    sys.exit(_p.main([__file__, "-q"]))
