#!/usr/bin/env python3
"""Integration tests for bin/sable-relink against a REAL tmux server (SABLE-to8m).

Isolated socket (-L). Proves end-to-end that a pane whose process carries an
authoritative CLAUDE_AGENT_NAME can re-register its own mutable identity tags:
running sable-relink FROM INSIDE the pane rewrites @sable_role to the process
identity and clears the stale @sable_status. Models the resumed-cockpit fix — a
finished worker window whose interactive session is now the operator.
"""
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent / "sable-relink"
HAVE_TMUX = shutil.which("tmux") is not None
pytestmark = pytest.mark.skipif(not HAVE_TMUX, reason="tmux not installed")


@pytest.fixture()
def sock():
    s = f"sable-rl-{uuid.uuid4().hex[:8]}"
    yield s
    subprocess.run(["tmux", "-L", s, "kill-server"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _tmux(s, *args, check=True):
    return subprocess.run(["tmux", "-L", s, *args],
                          capture_output=True, text=True, check=check)


def _opt(s, target, name):
    r = _tmux(s, "show-options", "-p", "-v", "-t", target, name, check=False)
    return r.stdout.strip()


def test_relink_rewrites_tag_to_process_identity_and_clears_status(sock):
    # A finished worker window resumed as the cockpit: its process identity is
    # 'lincoln' (tmux -e), but it still wears the worker's @sable_role=worker /
    # @sable_status=done. Running sable-relink from inside re-registers it.
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "200", "-y", "50",
          "-e", "CLAUDE_AGENT_NAME=lincoln", "PS1='> ' bash --noprofile --norc")
    time.sleep(0.4)
    _tmux(sock, "set-option", "-p", "-t", "w", "@sable_role", "worker")
    _tmux(sock, "set-option", "-p", "-t", "w", "@sable_bead", "SABLE-old")
    _tmux(sock, "set-option", "-p", "-t", "w", "@sable_status", "done")

    # invoke FROM the pane so $TMUX_PANE and $CLAUDE_AGENT_NAME are the pane's own
    _tmux(sock, "send-keys", "-t", "w",
          f"SABLE_TMUX_SOCKET={sock} python3 {BIN}", "Enter")
    time.sleep(1.2)

    assert _opt(sock, "w", "@sable_role") == "lincoln"   # rewritten to the authority
    assert _opt(sock, "w", "@sable_status") == ""         # stale done flag cleared


def test_relink_refuses_to_forge_a_disagreeing_role(sock):
    # The process identity is 'optimus'; asking to stamp 'lincoln' must be
    # refused (relink can only re-assert the identity the process carries), so
    # the poisoned tag is left for a human rather than silently forged.
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "200", "-y", "50",
          "-e", "CLAUDE_AGENT_NAME=optimus", "PS1='> ' bash --noprofile --norc")
    time.sleep(0.4)
    _tmux(sock, "set-option", "-p", "-t", "w", "@sable_role", "worker")

    rc_file = f"/tmp/sable-relink-rc-{uuid.uuid4().hex[:8]}"
    _tmux(sock, "send-keys", "-t", "w",
          f"SABLE_TMUX_SOCKET={sock} python3 {BIN} lincoln; echo rc=$? > {rc_file}",
          "Enter")
    time.sleep(1.2)

    rc = Path(rc_file).read_text().strip()
    assert rc == "rc=1"                                   # refused
    assert _opt(sock, "w", "@sable_role") == "worker"     # tag left untouched
    Path(rc_file).unlink(missing_ok=True)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
