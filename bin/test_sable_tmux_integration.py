#!/usr/bin/env python3
"""Integration test for bin/sable-tmux against a REAL tmux server.

Isolated socket (-L); stand-in pane command (SABLE_TMUX_PANE_CMD=bash) so no
real claude launches. Proves: the session is laid out with one pane per role,
each pane tagged @sable_role=<role> (the registry the other tools read), and
CLAUDE_AGENT_NAME is set per pane via tmux -e (verified by echoing it).
"""
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent / "sable-tmux"
HAVE_TMUX = shutil.which("tmux") is not None
pytestmark = pytest.mark.skipif(not HAVE_TMUX, reason="tmux not installed")
ROLES = ["lincoln", "optimus", "tarzan", "chuck"]


@pytest.fixture()
def sock():
    s = f"sable-tx-{uuid.uuid4().hex[:8]}"
    yield s
    subprocess.run(["tmux", "-L", s, "kill-server"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _tmux(s, *args):
    return subprocess.run(["tmux", "-L", s, *args],
                          capture_output=True, text=True, check=True)


def test_layout_tags_and_identity(sock):
    env = {**os.environ, "SABLE_TMUX_SOCKET": sock,
           "SABLE_TMUX_PANE_CMD": "bash --noprofile --norc"}
    r = subprocess.run(["python3", str(BIN), "--session", "sable"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    time.sleep(0.6)

    # one pane per role, each tagged @sable_role
    listing = _tmux(sock, "list-panes", "-a", "-F",
                    "#{pane_id} #{@sable_role}").stdout
    roles_seen = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2:
            roles_seen[parts[1]] = parts[0]
    for role in ROLES:
        assert role in roles_seen, f"{role} missing from {listing}"

    # CLAUDE_AGENT_NAME set per pane (via tmux -e) — verify by echoing it
    for role, pane in roles_seen.items():
        _tmux(sock, "send-keys", "-t", pane, "echo RID=$CLAUDE_AGENT_NAME", "Enter")
    time.sleep(0.6)
    for role, pane in roles_seen.items():
        cap = _tmux(sock, "capture-pane", "-t", pane, "-p").stdout
        assert f"RID={role}" in cap, f"identity not set for {role}: {cap}"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
