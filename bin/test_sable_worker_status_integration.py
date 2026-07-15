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
    # CLAUDE_AGENT_NAME forced empty (SABLE-dcw2): these legacy cases assert the
    # FLEET-WIDE view over lane-less panes, so they must not inherit an ambient
    # lane from the test runner (a manager pane sets CLAUDE_AGENT_NAME) and
    # silently filter every pane out. The own-lane filter is covered by the
    # two-manager case below, which sets CLAUDE_AGENT_NAME explicitly.
    return subprocess.run(["python3", str(BIN), *args], capture_output=True, text=True,
                          env={**os.environ, "SABLE_TMUX_SOCKET": s,
                               "SABLE_TMUX_SESSION": "w",
                               "SABLE_STATUS_SAMPLE_INTERVAL": "0.1",
                               "CLAUDE_AGENT_NAME": ""})


def _run_as(s, lane, *args):
    """Run sable-worker-status as manager `lane` (SABLE-dcw2): CLAUDE_AGENT_NAME
    is set so the default view scopes to that lane. `lane=""` (or passing --all
    in args) yields the fleet-wide view."""
    import os
    return subprocess.run(["python3", str(BIN), *args], capture_output=True, text=True,
                          env={**os.environ, "SABLE_TMUX_SOCKET": s,
                               "SABLE_TMUX_SESSION": "w",
                               "SABLE_STATUS_SAMPLE_INTERVAL": "0.1",
                               "CLAUDE_AGENT_NAME": lane})


def _tag_lane(s, target, lane):
    _tmux(s, "set-option", "-p", "-t", target, "@sable_lane", lane)


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
    env.pop("CLAUDE_AGENT_NAME", None)  # SABLE-dcw2: fleet-wide over lane-less panes
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
    env.pop("CLAUDE_AGENT_NAME", None)  # SABLE-dcw2: fleet-wide over lane-less pane
    r = subprocess.run(["python3", str(BIN)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    lines = [ln for ln in r.stdout.splitlines() if "bead-flip" in ln]
    assert len(lines) == 1
    assert "done" not in lines[0]
    assert "running" in lines[0]


def _attach_pty_client(sock, session, deadline=10):
    """Attach a REAL tmux client to `session` over a pty and return its Popen
    once tmux's list-clients genuinely reports it (SABLE-7dva). Unlike a bare
    `tmux attach`, this registers HEADLESSLY -- no controlling tty, TERM unset,
    the CI clean-room -- by fixing the three things that made the naive spawn
    exit without ever attaching: (1) a real TERM so tmux finds a terminfo
    entry; (2) start_new_session so the pty is a clean controlling terminal;
    (3) a drained master fd so tmux's initial screen redraw can't block once
    the ~64K pty buffer fills and stall the attach.

    Raises AssertionError -- carrying the client's exit code and captured pty
    output -- if the client dies or fails to register within `deadline`
    seconds, so failures name the cause instead of a blank 'assert in ""'.
    The caller owns the returned client: `client.terminate()` lets the child
    exit, which tears down the drain thread (EOF) and closes the master fd.
    Shared by the attached-client tests (SABLE-dcw2, SABLE-b574)."""
    import os
    import pty as pty_module
    import threading

    master, slave = pty_module.openpty()
    client = subprocess.Popen(
        ["tmux", "-L", sock, "attach-session", "-t", session],
        stdin=slave, stdout=slave, stderr=slave, close_fds=True,
        start_new_session=True, env={**os.environ, "TERM": "screen"})
    os.close(slave)
    # Drain (and retain) the master side so redraw writes never block; the
    # captured bytes double as diagnostics if the attach dies early. The
    # thread owns master's lifetime: it closes it on EOF (i.e. once the client
    # exits), so terminate() alone fully cleans up.
    drained = bytearray()

    def _drain():
        try:
            while True:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                drained.extend(chunk)
        finally:
            try:
                os.close(master)
            except OSError:
                pass

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()

    def _fail(msg):
        client.terminate()
        try:
            client.wait(timeout=2)
        except subprocess.TimeoutExpired:
            client.kill()
        raise AssertionError(msg)

    end = time.monotonic() + deadline
    while time.monotonic() < end:
        clients = _tmux(sock, "list-clients", "-F", "#{client_session}").stdout
        if session in clients.split():
            return client
        if client.poll() is not None:
            _fail(f"tmux attach exited early (code={client.returncode}) before "
                  f"registering as a client for session {session!r}; pty output="
                  f"{bytes(drained).decode(errors='replace')!r}")
        time.sleep(0.1)
    clients = _tmux(sock, "list-clients", "-F", "#{client_session}").stdout
    _fail(f"no attached client for session {session!r} after {deadline}s "
          f"(client_alive={client.poll() is None}); list-clients={clients!r} "
          f"pty output={bytes(drained).decode(errors='replace')!r}")


def test_reap_protects_done_pane_in_attached_clients_current_window(sock):
    """SABLE-yfdn: a done worker pane in the window an attached client is
    CURRENTLY viewing must survive --reap -- kill-pane on a single-pane
    worker window would forcibly yank that client's view mid-observation.
    A done pane in a different window is killed as normal. A real attached
    client is simulated via a pty so tmux's list-clients genuinely reports
    one (a detached -d session, used everywhere else in this suite, has
    zero clients by definition and can't exercise this path)."""
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "180", "-y", "40",
          "bash --noprofile --norc")
    time.sleep(0.4)
    _tag(sock, "w.0", "worker", "bead-watched", "done")
    _tmux(sock, "new-window", "-t", "w", "bash --noprofile --norc")
    time.sleep(0.4)
    _tag(sock, "w:1.0", "worker", "bead-other", "done")
    _tmux(sock, "select-window", "-t", "w:0")

    client = _attach_pty_client(sock, "w")
    try:
        r = _run(sock, "--reap")
        assert r.returncode == 0, r.stderr
        time.sleep(0.4)
        survivors = _tmux(sock, "list-panes", "-a", "-F", "#{@sable_bead}").stdout
        assert "bead-watched" in survivors  # protected: client's current window
        assert "bead-other" not in survivors  # different window: reaped normally
    finally:
        client.terminate()
        client.wait(timeout=2)


# --- SABLE-dcw2: two-manager attribution. Optimus and Tarzan each own a worker
# pane in the SAME repo fleet. Before this fix the panes carried no owner tag, so
# either manager's sable-worker-status saw BOTH — a sweep adopted the other
# lane's workers as orphans, and a --reap consumed DONE panes the owning manager
# was still event-waiting on. Now each pane is stamped @sable_lane and the
# default listing/reap is scoped to the caller's own lane. ---

def _two_manager_session(sock):
    """Session 'w' with pane 0 = optimus's DONE worker (@sable_lane=optimus) and
    pane 1 = tarzan's DONE worker (@sable_lane=tarzan). Optimus owns pane 0 (the
    session's original pane) so reaping tarzan's split never destroys the
    session out from under the assertions."""
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "180", "-y", "40",
          "bash --noprofile --norc")
    time.sleep(0.4)
    _tag(sock, "w.0", "worker", "bead-optimus", "done")
    _tag_lane(sock, "w.0", "optimus")
    _tmux(sock, "split-window", "-t", "w", "bash --noprofile --norc")
    time.sleep(0.4)
    _tag(sock, "w.1", "worker", "bead-tarzan", "done")
    _tag_lane(sock, "w.1", "tarzan")


def test_default_listing_scoped_to_owning_lane(sock):
    """Manager B (tarzan) default listing must show ONLY tarzan's worker, not
    optimus's; --all restores the fleet-wide view showing both."""
    _two_manager_session(sock)

    mine = _run_as(sock, "tarzan")
    assert mine.returncode == 0, mine.stderr
    assert "bead-tarzan" in mine.stdout, mine.stdout
    assert "bead-optimus" not in mine.stdout, mine.stdout  # optimus's pane hidden

    everything = _run_as(sock, "tarzan", "--all")
    assert everything.returncode == 0, everything.stderr
    assert "bead-tarzan" in everything.stdout and "bead-optimus" in everything.stdout, \
        everything.stdout


def test_reap_under_other_manager_spares_owning_lane_done_pane(sock):
    """Manager B (tarzan) --reap must kill ONLY tarzan's DONE pane and LEAVE
    optimus's DONE pane alive — routing that completion signal to its owner
    (optimus), who was event-waiting on it, instead of consuming it. Optimus's
    own --reap then collects it."""
    _two_manager_session(sock)
    assert _pane_count(sock) == 2

    # tarzan sweeps: only his own done pane is reaped
    r = _run_as(sock, "tarzan", "--reap")
    assert r.returncode == 0, r.stderr
    time.sleep(0.4)
    survivors = _tmux(sock, "list-panes", "-a", "-F", "#{@sable_bead}").stdout
    assert "bead-optimus" in survivors, survivors  # owner's signal preserved
    assert "bead-tarzan" not in survivors, survivors  # tarzan reaped his own

    # optimus now collects his own done pane
    r2 = _run_as(sock, "optimus", "--reap")
    assert r2.returncode == 0, r2.stderr
    time.sleep(0.4)
    # optimus's pane was the session's only remaining one -> the session exits
    assert _tmux(sock, "has-session", "-t", "w", check=False).returncode != 0


# --- SABLE-axp0: fleet-wide dialog/overlay liveness probe against REAL panes.
# A warm pane parked on a dialog/overlay silently swallows every message sent to
# it (the chuck /usage stall). The probe capture-panes each fleet pane, flags one
# in an overlay posture "dialog-stalled", and alerts loudly BY NAME — covering
# MANAGER panes too, which the worker table drops. ---

def _prime_dialog(s, target):
    """Paint an interactive selector menu into `target`'s live screen, so a real
    capture-pane shows the overlay posture the probe classifies."""
    _tmux(s, "send-keys", "-t", target,
          "printf '  ? pick one\\n  > 1. alpha\\n    2. beta\\n"
          "  (Use arrow keys, Enter to select)\\n'", "Enter")
    time.sleep(0.4)


def test_dialog_probe_flags_stalled_manager_and_spares_normal_pane(sock):
    """The motivating repro: a warm MANAGER pane parked on a dialog/overlay is
    detected + surfaced by name (parse_worker_panes never lists a manager, so
    the fleet probe is the only thing that can catch it), while a normal worker
    pane showing no overlay is NOT flagged."""
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "180", "-y", "40",
          "bash --noprofile --norc")
    time.sleep(0.4)
    # pane 0: a MANAGER pane (chuck) parked on a dialog/overlay
    _tmux(sock, "set-option", "-p", "-t", "w.0", "@sable_role", "chuck")
    _tmux(sock, "set-option", "-p", "-t", "w.0", "@sable_class", "manager")
    _tmux(sock, "set-option", "-p", "-t", "w.0", "@sable_lane", "chuck")
    _prime_dialog(sock, "w.0")
    # pane 1: a normal worker pane, plain prompt, no overlay
    _tmux(sock, "split-window", "-t", "w", "bash --noprofile --norc")
    time.sleep(0.4)
    _tag(sock, "w.1", "worker", "bead-ok", "running")
    _tag_lane(sock, "w.1", "optimus")
    time.sleep(0.3)

    p0 = _tmux(sock, "display-message", "-p", "-t", "w.0", "#{pane_id}").stdout.strip()
    p1 = _tmux(sock, "display-message", "-p", "-t", "w.1", "#{pane_id}").stdout.strip()

    r = _run(sock, "--all")
    assert r.returncode == 0, r.stderr
    combined = r.stdout + r.stderr
    # the stalled MANAGER pane is detected and named (in the table AND the alert)
    assert "DIALOG-STALLED" in r.stdout, r.stdout
    assert p0 in combined, combined
    assert "STALLED on a dialog/overlay" in r.stderr, r.stderr
    # the normal worker pane is present as a running worker but NOT flagged
    assert "bead-ok" in r.stdout, r.stdout
    assert p1 not in r.stderr, r.stderr
    assert r.stdout.count("DIALOG-STALLED") == 1, r.stdout


def test_dialog_probe_silent_when_no_pane_is_stalled(sock):
    """A fleet of normal panes produces NO stall alert — the probe must not
    false-positive a legitimately-working lane."""
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "180", "-y", "40",
          "bash --noprofile --norc")
    time.sleep(0.4)
    _tag(sock, "w.0", "worker", "bead-run", "running")
    r = _run(sock, "--all")
    assert r.returncode == 0, r.stderr
    assert "DIALOG-STALLED" not in r.stdout, r.stdout
    assert "STALLED on a dialog/overlay" not in r.stderr, r.stderr


def test_dialog_probe_json_carries_stalls_and_workers(sock):
    """--json emits both the worker table and the dialog-stall list, so a
    machine caller can consume the probe result (SABLE-axp0)."""
    import json as _json
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "180", "-y", "40",
          "bash --noprofile --norc")
    time.sleep(0.4)
    _tmux(sock, "set-option", "-p", "-t", "w.0", "@sable_role", "chuck")
    _tmux(sock, "set-option", "-p", "-t", "w.0", "@sable_class", "manager")
    _tmux(sock, "set-option", "-p", "-t", "w.0", "@sable_lane", "chuck")
    _prime_dialog(sock, "w.0")
    time.sleep(0.3)

    r = _run(sock, "--all", "--json")
    assert r.returncode == 0, r.stderr
    payload = _json.loads(r.stdout)
    assert "workers" in payload and "dialog_stalls" in payload
    assert len(payload["dialog_stalls"]) == 1
    assert payload["dialog_stalls"][0]["status"] == "dialog-stalled"
    assert payload["dialog_stalls"][0]["class"] == "manager"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
