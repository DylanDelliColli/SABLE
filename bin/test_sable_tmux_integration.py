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


def test_autostart_kicks_autonomous_roles_only(sock, tmp_path):
    """SABLE-bldh.14: --autostart kicks optimus/tarzan/chuck into their operating
    loops but NOT lincoln (the operator's pane). Stand-in panes record the lines
    they receive, keyed by their per-pane CLAUDE_AGENT_NAME."""
    rec = tmp_path / "rec"
    rec.mkdir()
    script = tmp_path / "fake-pane.sh"
    # Emulate a claude prompt box ("❯ ") so deliver_text's submit-detection
    # (dispatch_landed) behaves as it does against a real TUI: the typed line sits
    # in the box until Enter, then is consumed. Record each submitted line under
    # this pane's role name; the loop keeps the pane alive.
    script.write_text(
        'while true; do printf "\\342\\235\\257 "; IFS= read -r line || break; '
        'echo "$line" >> "$REC_DIR/$CLAUDE_AGENT_NAME.txt"; done\n'
    )
    env = {
        **os.environ,
        "SABLE_TMUX_SOCKET": sock,
        "SABLE_TMUX_PANE_CMD": f"bash --noprofile --norc {script}",
        "REC_DIR": str(rec),
        "SABLE_DISPATCH_READY_TIMEOUT": "0",      # stand-in pane has no claude prompt
        "SABLE_DISPATCH_POLL_INTERVAL": "0.05",
        "SABLE_DISPATCH_SUBMIT_TRIES": "1",
    }
    r = subprocess.run(["python3", str(BIN), "--session", "sable", "--autostart"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    time.sleep(1.0)

    # autonomous roles each received the kick
    for role in ("optimus", "tarzan", "chuck"):
        f = rec / f"{role}.txt"
        assert f.exists(), f"{role} got no kick ({sorted(p.name for p in rec.iterdir())})"
        assert "SABLE-AUTOSTART" in f.read_text(), f"{role} kick missing tag: {f.read_text()!r}"

    # lincoln (operator pane) must NOT be kicked
    lf = rec / "lincoln.txt"
    assert not lf.exists() or "SABLE-AUTOSTART" not in lf.read_text(), \
        "lincoln should not be auto-kicked"


def test_default_session_derives_from_repo(sock, tmp_path):
    """SABLE-e1e3.2: without --session (and without SABLE_TMUX_SESSION), the
    session name derives from the repo the tool runs in; the session records
    its repo root (@sable_repo session option — the collision guard) and every
    pane carries the @sable_repo tag."""
    repo = tmp_path / "alpha"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    root = str(repo.resolve())
    env = {**os.environ, "SABLE_TMUX_SOCKET": sock,
           "SABLE_TMUX_PANE_CMD": "bash --noprofile --norc"}
    env.pop("SABLE_TMUX_SESSION", None)
    r = subprocess.run(["python3", str(BIN), "--roles", "lincoln"],
                       capture_output=True, text=True, env=env, cwd=root)
    assert r.returncode == 0, r.stderr
    time.sleep(0.4)

    assert subprocess.run(["tmux", "-L", sock, "has-session", "-t", "sable-alpha"],
                          capture_output=True).returncode == 0
    opt = _tmux(sock, "show-options", "-v", "-t", "sable-alpha", "@sable_repo").stdout.strip()
    assert opt == root
    tags = _tmux(sock, "list-panes", "-s", "-t", "sable-alpha",
                 "-F", "#{@sable_repo}").stdout.split()
    assert tags and all(t == root for t in tags), tags


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
