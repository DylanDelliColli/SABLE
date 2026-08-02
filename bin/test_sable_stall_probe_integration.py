#!/usr/bin/env python3
"""Integration test for sable_stall_probe_lib.deliberate_hold against a REAL
tmux server (SABLE-cod50). Isolated socket (-L), no mocking of tmux itself --
mocking the terminal here would defeat the point, since the bug this guards
against (SABLE-cod50) is specifically about misreading a REAL pane's shape.

Two real panes are driven with `bash --noprofile --norc` and a composer-like
PS1 ("❯ "), which reproduces the exact shape deliberate_hold keys on
without needing a live `claude` process: a settled bare prompt line with
rendered content above it (the hold shape), versus literal text typed via
`tmux send-keys -l` with NO trailing Enter, which sits unsubmitted on the
prompt line exactly the way an unsent ⟦SABLE-MSG⟧ wake would (the
dropped-wake shape).

The end-to-end subprocess cases for the now-tracked driver live in
test_sable_stall_probe.py. This module stays focused on the shared pane-state
classification against real tmux capture bytes.
"""
import shutil
import subprocess
import time
import uuid

import pytest

from sable_pane_lib import pane_idle
from sable_stall_probe_lib import deliberate_hold

HAVE_TMUX = shutil.which("tmux") is not None
pytestmark = pytest.mark.skipif(not HAVE_TMUX, reason="tmux not installed")


@pytest.fixture()
def sock():
    s = f"sable-stall-{uuid.uuid4().hex[:8]}"
    yield s
    subprocess.run(["tmux", "-L", s, "kill-server"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _tmux(s, *args):
    return subprocess.run(["tmux", "-L", s, *args],
                           capture_output=True, text=True, check=True)


def _new_pane(sock):
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "120", "-y", "20",
          "bash --noprofile --norc")
    pane = _tmux(sock, "list-panes", "-t", "w", "-F", "#{pane_id}").stdout.strip()
    # A composer-like bare prompt reproduces the exact glyph deliberate_hold
    # keys on, without a live `claude` process.
    _tmux(sock, "send-keys", "-t", pane, "PS1='❯ '", "Enter")
    time.sleep(0.4)
    return pane


def test_settled_hold_pane_is_not_stall(sock):
    """A completed turn's output rendered above a settled, bare composer --
    any phrasing -- is the deliberate-hold shape, never a stall."""
    pane = _new_pane(sock)
    _tmux(sock, "send-keys", "-t", pane,
          "printf '%s\\n' 'Standing down -- nothing is owed until morning.'",
          "Enter")
    time.sleep(0.4)

    capture = _tmux(sock, "capture-pane", "-t", pane, "-p", "-J").stdout
    assert "Standing down" in capture
    assert deliberate_hold(capture, "claude") is True


def test_unanswered_wake_pane_is_stall(sock):
    """A wake typed into the pane but never submitted (no Enter) is the
    dropped-wake shape -- text still sitting unsent in the composer, never
    landed as a turn, so nothing could possibly have acted on it."""
    pane = _new_pane(sock)
    msg = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    _tmux(sock, "send-keys", "-t", pane, "-l", msg)  # literal text, NO Enter
    time.sleep(0.4)

    capture = _tmux(sock, "capture-pane", "-t", pane, "-p", "-J").stdout
    assert "SABLE-MSG" in capture
    assert deliberate_hold(capture, "claude") is False


def test_stray_transcript_glyph_cannot_hide_real_held_composer(sock):
    """SABLE-6cu65 on real tmux bytes, including the poison transcript row."""
    pane = _new_pane(sock)
    _tmux(sock, "send-keys", "-t", pane, "printf '%s\\n' '>'", "Enter")
    held = "operator text not yet submitted"
    _tmux(sock, "send-keys", "-t", pane, "-l", held)
    time.sleep(0.4)

    capture = _tmux(
        sock, "capture-pane", "-t", pane, "-p", "-J", "-e"
    ).stdout
    assert ">" in capture.splitlines(), (
        "positive control: the transcript poison row must really be present"
    )
    assert held in capture, (
        "positive control: real unsubmitted composer text must really be present"
    )
    assert not pane_idle(capture, "claude")
