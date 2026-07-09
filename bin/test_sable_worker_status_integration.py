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
    # SABLE_STATUS_SAMPLE_INTERVAL shrunk: SABLE-1kbo windowed sampling adds a
    # real sleep between two internal listing reads (default 1.5s); none of
    # these cases are timing-sensitive themselves, so keep them fast.
    return subprocess.run(["python3", str(BIN), *args], capture_output=True, text=True,
                          env={**os.environ, "SABLE_TMUX_SOCKET": s,
                               "SABLE_TMUX_SESSION": "w",
                               "SABLE_STATUS_SAMPLE_INTERVAL": "0.1"})


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


def test_reap_kills_beadless_done_producer_with_valid_deliverable(sock, tmp_path):
    """SABLE-exab: a producer pane spawned WITHOUT a bead tag (e.g. victor
    with no bead passed, as in the tz7h.5 acceptance run) renders an EMPTY
    @sable_bead placeholder in real tmux -F output. The parser must not let
    that shift status/class/deliverable into the wrong columns -- the pane
    must still be reaped."""
    deliverable = tmp_path / "d.json"
    deliverable.write_text('{"ok": true}')
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "180", "-y", "40",
          "bash --noprofile --norc")
    time.sleep(0.4)
    # deliberately no @sable_bead set at all
    _tmux(sock, "set-option", "-p", "-t", "w.0", "@sable_role", "victor")
    _tmux(sock, "set-option", "-p", "-t", "w.0", "@sable_status", "done")
    _tmux(sock, "set-option", "-p", "-t", "w.0", "@sable_class", "producer")
    _tmux(sock, "set-option", "-p", "-t", "w.0", "@sable_deliverable", str(deliverable))
    assert _pane_count(sock) == 1

    r = _run(sock, "--reap")
    assert r.returncode == 0, r.stderr
    time.sleep(0.4)
    # the killed pane was the session's only one -- the whole server exits,
    # so list-panes itself now fails rather than reporting zero panes
    assert _tmux(sock, "has-session", "-t", "w", check=False).returncode != 0


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

    env = {**os.environ, "SABLE_TMUX_SOCKET": sock, "SABLE_STATUS_SAMPLE_INTERVAL": "0.1"}
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


def test_reports_stale_done_tag_as_running_after_sampling_window(sock):
    """SABLE-1kbo mid-turn-labeled-done: a pane whose @sable_status briefly
    reads 'done' (a stale tag -- e.g. a reused pane's prior occupant, before
    the fresh worker's init overwrites it) and then flips to 'running'
    within the windowed-sampling interval must be reported running, not
    done -- a single instant snapshot would misreport it."""
    import os
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "180", "-y", "40",
          "bash --noprofile --norc")
    time.sleep(0.4)
    _tag(sock, "w.0", "worker", "bead-flip", "done")
    # fires mid-window, well before the second internal sample at ~1.5s
    subprocess.Popen(
        ["bash", "-c", f"sleep 0.6 && tmux -L {sock} set-option -p -t w.0 "
                       f"@sable_status running"])

    env = {**os.environ, "SABLE_TMUX_SOCKET": sock, "SABLE_TMUX_SESSION": "w",
           "SABLE_STATUS_SAMPLE_INTERVAL": "1.5"}
    r = subprocess.run(["python3", str(BIN)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.splitlines() if "bead-flip" in ln]
    assert len(lines) == 1
    assert "done" not in lines[0]
    assert "running" in lines[0]


def test_reap_protects_done_pane_in_attached_clients_current_window(sock):
    """SABLE-yfdn: a done worker pane in the window an attached client is
    CURRENTLY viewing must survive --reap -- kill-pane on a single-pane
    worker window would forcibly yank that client's view mid-observation.
    A done pane in a different window is killed as normal. A real attached
    client is simulated via a pty so tmux's list-clients genuinely reports
    one (a detached -d session, used everywhere else in this suite, has
    zero clients by definition and can't exercise this path)."""
    import os
    import pty as pty_module

    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "180", "-y", "40",
          "bash --noprofile --norc")
    time.sleep(0.4)
    _tag(sock, "w.0", "worker", "bead-watched", "done")
    _tmux(sock, "new-window", "-t", "w", "bash --noprofile --norc")
    time.sleep(0.4)
    _tag(sock, "w:1.0", "worker", "bead-other", "done")
    _tmux(sock, "select-window", "-t", "w:0")

    master, slave = pty_module.openpty()
    client = subprocess.Popen(["tmux", "-L", sock, "attach-session", "-t", "w"],
                               stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    try:
        time.sleep(0.5)
        assert "w" in _tmux(sock, "list-clients", "-F", "#{client_session}").stdout

        r = _run(sock, "--reap")
        assert r.returncode == 0, r.stderr
        time.sleep(0.4)
        survivors = _tmux(sock, "list-panes", "-a", "-F", "#{@sable_bead}").stdout
        assert "bead-watched" in survivors  # protected: client's current window
        assert "bead-other" not in survivors  # different window: reaped normally
    finally:
        os.close(master)
        client.terminate()
        client.wait(timeout=2)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
