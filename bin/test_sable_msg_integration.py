#!/usr/bin/env python3
"""Integration tests for bin/sable-msg against a REAL tmux server.

Uses an isolated tmux socket (-L) so it never touches the operator's session.
Proves end-to-end: the role->pane registry (@sable_role user-option) resolves,
the message is delivered as a real keystroke turn, and a message sent while the
target pane is BUSY is queued and runs when free (the verified spike behavior).
"""
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent / "sable-msg"
HAVE_TMUX = shutil.which("tmux") is not None
pytestmark = pytest.mark.skipif(not HAVE_TMUX, reason="tmux not installed")


@pytest.fixture()
def tmux_socket():
    sock = f"sable-it-{uuid.uuid4().hex[:8]}"
    yield sock
    subprocess.run(["tmux", "-L", sock, "kill-server"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _tmux(sock, *args, check=True):
    return subprocess.run(["tmux", "-L", sock, *args],
                          capture_output=True, text=True, check=check)


def _capture(sock, target):
    return _tmux(sock, "capture-pane", "-t", target, "-p").stdout


def _start_pane(sock):
    # A bash REPL stand-in for an agent pane; tag it with @sable_role=optimus.
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "200", "-y", "50",
          "PS1='> ' bash --noprofile --norc")
    time.sleep(0.5)
    _tmux(sock, "set-option", "-p", "-t", "w", "@sable_role", "optimus")
    return "w"


def _run_msg(sock, *cli_args):
    return subprocess.run(
        ["python3", str(BIN), *cli_args],
        capture_output=True, text=True,
        env={**_env(), "SABLE_TMUX_SOCKET": sock},
    )


def _env():
    import os
    return dict(os.environ)


def test_message_delivered_to_registered_pane(tmux_socket):
    _start_pane(tmux_socket)
    r = _run_msg(tmux_socket, "optimus",
                 "echo SABLE-MSG-DELIVERED", "--from", "lincoln")
    assert r.returncode == 0, r.stderr
    time.sleep(0.8)
    pane = _capture(tmux_socket, "w")
    # the header is injected verbatim and the body executed by the REPL
    assert "⟦SABLE-MSG⟧ from=lincoln to=optimus" in pane
    assert "SABLE-MSG-DELIVERED" in pane


def test_message_to_unknown_role_fails(tmux_socket):
    _start_pane(tmux_socket)
    r = _run_msg(tmux_socket, "ghost", "hello", "--from", "lincoln")
    assert r.returncode != 0
    assert "ghost" in (r.stderr + r.stdout)


def test_wrapped_message_in_narrow_pane_is_actually_submitted(tmux_socket):
    # SABLE-1umr: in a narrow pane the framed message wraps across composer
    # lines; the old box check false-positived "landed" and could return
    # delivered without the line ever being submitted. $((40+2)) only expands
    # if the REPL actually EXECUTED the line — the echoed input shows it
    # unexpanded, so the assertion cannot pass on a stuck composer.
    _tmux(tmux_socket, "new-session", "-d", "-s", "w", "-x", "60", "-y", "20",
          "PS1='> ' bash --noprofile --norc")
    time.sleep(0.5)
    _tmux(tmux_socket, "set-option", "-p", "-t", "w", "@sable_role", "optimus")
    body = "; echo WRAP-$((40+2))-VERIFIED end of a long directive body padding"
    r = _run_msg(tmux_socket, "optimus", body, "--from", "lincoln")
    assert r.returncode == 0, r.stderr
    time.sleep(1.0)
    pane = _capture(tmux_socket, "w")
    assert "WRAP-42-VERIFIED" in pane


def test_message_queues_while_target_busy(tmux_socket):
    _start_pane(tmux_socket)
    # make the pane busy for 3s
    _tmux(tmux_socket, "send-keys", "-t", "w",
          "echo BUSY-START; sleep 3; echo BUSY-END", "Enter")
    time.sleep(0.3)  # now mid-command
    r = _run_msg(tmux_socket, "optimus",
                 "echo QUEUED-RAN", "--from", "lincoln")
    assert r.returncode == 0, r.stderr
    time.sleep(4)
    pane = _capture(tmux_socket, "w")
    assert "BUSY-END" in pane
    assert "QUEUED-RAN" in pane
    # ordering: the queued echo ran only after the busy block finished
    assert pane.index("BUSY-END") < pane.rindex("QUEUED-RAN")


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
