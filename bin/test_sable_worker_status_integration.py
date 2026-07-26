#!/usr/bin/env python3
"""Integration tests for bin/sable-worker-status against a REAL tmux server.

Isolated socket (-L). Proves: worker panes are discovered by their @sable_role/
@sable_bead/@sable_status user-options, a done worker is reported done, and
--reap kills ONLY the done worker pane (the running one survives).
"""
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent / "sable-worker-status"
VIEW_BIN = Path(__file__).resolve().parent / "sable-view"
HAVE_TMUX = shutil.which("tmux") is not None
pytestmark = pytest.mark.skipif(not HAVE_TMUX, reason="tmux not installed")

# SABLE-517s: a tmux session's panes inherit the tmux SERVER's global
# environment, captured from the env of whichever client call first starts
# the server on this socket (typically this test file's first _tmux() call).
# When the test runner ITSELF is a live SABLE pane (CLAUDE_AGENT_NAME set,
# e.g. a manager running its own test suite), that identity leaks into every
# plain bash pane these tests spawn -- and pane_is_live_nonworker_agent (see
# sable_pane_lib.py) then reads it back via /proc/<pid>/environ and spares
# the pane from --reap, since it looks like a live non-worker agent. Scrub
# the ambient SABLE identity vars from every tmux client call's env so panes
# start clean regardless of what agent is running the test; tests that need
# a pane to carry a specific identity stamp it explicitly via per-pane -e
# (see _split_pane below), which always wins over this scrubbed baseline.
#
# TMUX/TMUX_PANE leak the same way into --reap SUBPROCESS envs (not just
# panes): resolve_session()'s calling_pane_session() (sable_pane_lib.py)
# reads TMUX_PANE from the CLI process's own env and looks that pane id up
# ON THE TEST'S ISOLATED SOCKET. When the outer test runner is itself inside
# tmux and a --reap subprocess inherits its TMUX_PANE, that id can coincide
# with an unrelated pane on the fresh test socket (pane ids are small and
# sequential per-server), hijacking session resolution to the wrong repo's
# fleet. Only scrub these where a test deliberately exercises cwd/pane-derived
# resolution by omitting SABLE_TMUX_SESSION -- when SABLE_TMUX_SESSION is
# pinned it wins first in resolve_session()'s precedence and TMUX_PANE is
# never consulted, so leaving it be there is harmless and documents that.
_AMBIENT_SABLE_VARS = ("CLAUDE_AGENT_NAME", "SABLE_WORKER_PANE", "SABLE_TMUX_SESSION",
                        "TMUX", "TMUX_PANE")


def _scrubbed_env():
    env = dict(os.environ)
    for var in _AMBIENT_SABLE_VARS:
        env.pop(var, None)
    return env


@pytest.fixture()
def sock():
    s = f"sable-ws-{uuid.uuid4().hex[:8]}"
    yield s
    subprocess.run(["tmux", "-L", s, "kill-server"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _tmux(s, *args, check=True):
    return subprocess.run(["tmux", "-L", s, *args],
                          capture_output=True, text=True, check=check,
                          env=_scrubbed_env())


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

    # _scrubbed_env(), not raw os.environ: this subprocess deliberately omits
    # SABLE_TMUX_SESSION to exercise cwd/pane-derived resolution, so ambient
    # TMUX/TMUX_PANE must not leak in either -- see the SABLE-517s note above
    # _AMBIENT_SABLE_VARS for why a leaked TMUX_PANE can hijack resolution to
    # the wrong repo's session on the test's isolated socket.
    env = {**_scrubbed_env(), "SABLE_TMUX_SOCKET": sock, "SABLE_STATUS_SAMPLE_INTERVAL": "0.1"}
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


def _prime_idle_composer_with_numbered_block(s, target):
    """Paint a healthy IDLE composer: a queued-message-style numbered list AND a
    horizontal separator row, then a bare prompt — the exact chrome the SABLE-axp0
    superset classifier misread as a dialog (SABLE-tz9f)."""
    _tmux(s, "send-keys", "-t", target,
          "printf '  1. rebase\\n  2. run tests\\n"
          "  3. push\\n  \\342\\224\\200\\342\\224\\200\\342\\224\\200\\342\\224\\200\\n'",
          "Enter")
    time.sleep(0.4)


def test_dialog_probe_does_not_flag_idle_composer_with_numbered_block(sock):
    """SABLE-tz9f: a healthy pane whose visible area holds a numbered list +
    separator rows (ordinary composer / queued-message chrome) must NOT be
    flagged DIALOG-STALLED — the systematic false positive that told the operator
    to Esc a healthy pane. The probe now requires a positive selector/dismiss
    affordance, which plain numbered text lacks."""
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "180", "-y", "40",
          "bash --noprofile --norc")
    time.sleep(0.4)
    _tmux(sock, "set-option", "-p", "-t", "w.0", "@sable_role", "optimus")
    _tmux(sock, "set-option", "-p", "-t", "w.0", "@sable_class", "manager")
    _tmux(sock, "set-option", "-p", "-t", "w.0", "@sable_lane", "optimus")
    _prime_idle_composer_with_numbered_block(sock, "w.0")
    time.sleep(0.3)

    r = _run(sock, "--all")
    assert r.returncode == 0, r.stderr
    assert "DIALOG-STALLED" not in r.stdout, r.stdout
    assert "STALLED on a dialog/overlay" not in r.stderr, r.stderr


def test_dialog_probe_alert_surfaces_evidence_snippet(sock):
    """SABLE-ccxc: the DIALOG-STALLED alert must carry the matched pane-text line
    so an operator can judge true-vs-false from the alert itself. A real selector
    menu is flagged AND its affordance line appears in the output."""
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "180", "-y", "40",
          "bash --noprofile --norc")
    time.sleep(0.4)
    _tmux(sock, "set-option", "-p", "-t", "w.0", "@sable_role", "chuck")
    _tmux(sock, "set-option", "-p", "-t", "w.0", "@sable_class", "manager")
    _tmux(sock, "set-option", "-p", "-t", "w.0", "@sable_lane", "chuck")
    _prime_dialog(sock, "w.0")
    time.sleep(0.3)

    r = _run(sock, "--all")
    assert r.returncode == 0, r.stderr
    assert "DIALOG-STALLED" in r.stdout, r.stdout
    # the matched affordance line is surfaced in the alert (stdout table or stderr)
    combined = r.stdout + r.stderr
    assert "arrow keys" in combined or "Enter to select" in combined, combined


def test_default_view_reports_other_lanes_instead_of_false_empty(sock):
    """SABLE-1g8i: a manager (tarzan) whose OWN lane has no worker panes but whose
    fleet DOES (optimus's running + done workers) must NOT print a bare 'no worker
    panes' — that false-empties the fleet, the exact divergence from sable-view.
    The message instead names the lane, counts the other-lane panes, and points at
    --all; --all then lists both."""
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "180", "-y", "40",
          "bash --noprofile --norc")
    time.sleep(0.4)
    _tag(sock, "w.0", "worker", "SABLE-jfg6.3", "running")
    _tag_lane(sock, "w.0", "optimus")
    _tmux(sock, "split-window", "-t", "w", "bash --noprofile --norc")
    time.sleep(0.4)
    _tag(sock, "w.1", "worker", "SABLE-done9", "done")
    _tag_lane(sock, "w.1", "optimus")

    # tarzan's own-lane default: no tarzan panes, but the fleet is NOT idle
    mine = _run_as(sock, "tarzan")
    assert mine.returncode == 0, mine.stderr
    assert mine.stdout.strip() != "no worker panes", mine.stdout
    assert "--all" in mine.stdout, mine.stdout
    assert "tarzan" in mine.stdout, mine.stdout

    # --all restores the fleet-wide view, showing both other-lane workers
    everything = _run_as(sock, "tarzan", "--all")
    assert everything.returncode == 0, everything.stderr
    assert "SABLE-jfg6.3" in everything.stdout and "SABLE-done9" in everything.stdout, \
        everything.stdout


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


# --- SABLE-n87ov: reporting a stall reproduces the symptom on the reporter's
# own audience. The probe used to grep the WHOLE visible pane for a dialog
# affordance substring, so a sable-msg relay QUOTING that substring (to help
# the recipient recognise the earlier true positive) rendered the same text
# into the recipient's healthy pane and re-triggered the detector on it. Fixed
# by anchoring the match to the pane's CURRENT CURSOR REGION -- content after
# the last bare composer prompt line -- since a live overlay owns the bottom
# of the pane (nothing, least of all a composer, follows it) while a mention
# is followed, on an otherwise-idle pane, by the reappeared empty composer. ---

def _echo_dialog_text_as_mention(s, target):
    """Print the SAME dialog-affordance text a real overlay would show, but as
    ordinary scrollback output -- a stand-in for a sable-msg relay quoting a
    stall report -- followed by a bare composer prompt glyph proving the pane
    is otherwise idle, not actually parked on a dialog."""
    _tmux(s, "send-keys", "-t", target,
          "printf '\\342\\237\\246SABLE-MSG\\342\\237\\247 pane w0 stalled -- "
          "matched (Use arrow keys, Enter to select)\\n\\342\\235\\257\\n'", "Enter")
    time.sleep(0.4)


def test_dialog_probe_ignores_mention_but_still_flags_real_overlay(sock):
    """The bead's core two-pane repro: pane 0 genuinely shows the select-overlay
    text (no composer follows it); pane 1 shows the IDENTICAL affordance text
    as an ordinary echoed mention, followed by an idle composer glyph. Exactly
    ONE pane -- the real overlay -- is reported DIALOG-STALLED."""
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "180", "-y", "40",
          "bash --noprofile --norc")
    time.sleep(0.4)
    _tmux(sock, "set-option", "-p", "-t", "w.0", "@sable_role", "tarzan")
    _tmux(sock, "set-option", "-p", "-t", "w.0", "@sable_class", "manager")
    _tmux(sock, "set-option", "-p", "-t", "w.0", "@sable_lane", "tarzan")
    _prime_dialog(sock, "w.0")
    _tmux(sock, "split-window", "-t", "w", "bash --noprofile --norc")
    time.sleep(0.4)
    _tmux(sock, "set-option", "-p", "-t", "w.1", "@sable_role", "lincoln")
    _tmux(sock, "set-option", "-p", "-t", "w.1", "@sable_class", "manager")
    _tmux(sock, "set-option", "-p", "-t", "w.1", "@sable_lane", "lincoln")
    _echo_dialog_text_as_mention(sock, "w.1")
    time.sleep(0.3)

    p0 = _tmux(sock, "display-message", "-p", "-t", "w.0", "#{pane_id}").stdout.strip()
    p1 = _tmux(sock, "display-message", "-p", "-t", "w.1", "#{pane_id}").stdout.strip()

    r = _run(sock, "--all")
    assert r.returncode == 0, r.stderr
    combined = r.stdout + r.stderr
    assert r.stdout.count("DIALOG-STALLED") == 1, r.stdout
    assert p0 in combined, combined                 # the real overlay IS flagged
    assert p1 not in r.stderr, r.stderr              # the mention is NOT flagged
    assert "STALLED on a dialog/overlay" in r.stderr, r.stderr


def test_dialog_probe_positive_control_neither_flagged_once_dismissed(sock):
    """Positive control for the case above: once the genuinely-stalled pane's
    overlay is dismissed (back to a plain idle composer) and the mention pane
    is likewise idle, NEITHER pane is flagged -- the fix isn't 'detect
    nothing', it correctly clears once the real condition is gone."""
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "180", "-y", "40",
          "bash --noprofile --norc")
    time.sleep(0.4)
    _tmux(sock, "set-option", "-p", "-t", "w.0", "@sable_role", "tarzan")
    _tmux(sock, "set-option", "-p", "-t", "w.0", "@sable_class", "manager")
    _tmux(sock, "set-option", "-p", "-t", "w.0", "@sable_lane", "tarzan")
    _tmux(sock, "send-keys", "-t", "w.0", "printf 'back to normal\\n\\342\\235\\257\\n'", "Enter")
    time.sleep(0.4)
    _tmux(sock, "split-window", "-t", "w", "bash --noprofile --norc")
    time.sleep(0.4)
    _tmux(sock, "set-option", "-p", "-t", "w.1", "@sable_role", "lincoln")
    _tmux(sock, "set-option", "-p", "-t", "w.1", "@sable_class", "manager")
    _tmux(sock, "set-option", "-p", "-t", "w.1", "@sable_lane", "lincoln")
    _echo_dialog_text_as_mention(sock, "w.1")
    time.sleep(0.3)

    r = _run(sock, "--all")
    assert r.returncode == 0, r.stderr
    assert "DIALOG-STALLED" not in r.stdout, r.stdout
    assert "STALLED on a dialog/overlay" not in r.stderr, r.stderr


# --- reaper liveness guard: a live agent we didn't spawn as a worker survives
# --reap (SABLE-to8m, generalized by SABLE-k8o5) ------------------------------
# Resuming an interactive claude inside a finished worker window left it carrying
# the worker's stale @sable_status=done and thus reap-eligible. The @sable_* tags
# are mutable and lie; the pane's live process env does not. The reaper consults
# that authority: it refuses to kill a pane whose live process is a SABLE agent it
# did not spawn as a worker (no SABLE_WORKER_PANE marker) — the operator cockpit
# OR a resumed manager. Both env vars are stamped per-pane via tmux -e so the
# result is hermetic regardless of the ambient SABLE_WORKER_PANE/CLAUDE_AGENT_NAME
# of the pane running the test (SABLE-k8o5; -e VAR= overrides an inherited value).

def _split_pane(sock, *env_pairs):
    """Split window `w`, stamping each 'K=V' pair as a per-pane tmux -e, and
    return the new pane's id. Per-pane -e (never session-wide new-session -e,
    which would leak into every pane) keeps each pane's identity isolated."""
    e_args = []
    for pair in env_pairs:
        e_args += ["-e", pair]
    return _tmux(sock, "split-window", "-t", "w", "-P", "-F", "#{pane_id}",
                 *e_args, "bash --noprofile --norc").stdout.strip()


def test_reap_spares_pane_whose_live_process_is_the_cockpit(sock):
    # A real done worker (lane identity + worker marker) -> reaped. A resumed
    # cockpit — process identity 'lincoln', worker marker explicitly emptied so an
    # ambient SABLE_WORKER_PANE can't leak in — still wears a stale
    # @sable_role=worker / @sable_status=done. --reap kills ONLY the real worker.
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "200", "-y", "50",
          "bash --noprofile --norc")
    time.sleep(0.3)
    placeholder = _tmux(sock, "list-panes", "-t", "w", "-F", "#{pane_id}").stdout.strip()
    worker = _split_pane(sock, "CLAUDE_AGENT_NAME=optimus", "SABLE_WORKER_PANE=1")
    cockpit = _split_pane(sock, "CLAUDE_AGENT_NAME=lincoln", "SABLE_WORKER_PANE=")
    time.sleep(0.3)
    _tag(sock, worker, "worker", "bead-real", "done")
    _tag(sock, cockpit, "worker", "bead-stale", "done")   # poisoned/leftover tags
    _tmux(sock, "kill-pane", "-t", placeholder)           # drop the ambient-env placeholder
    time.sleep(0.3)
    assert _pane_count(sock) == 2

    r = _run(sock, "--reap")
    assert r.returncode == 0, r.stderr
    time.sleep(0.4)
    assert _pane_count(sock) == 1  # the cockpit survives; the real worker is reaped
    survivors = _tmux(sock, "list-panes", "-a", "-F", "#{@sable_bead}").stdout
    assert "bead-stale" in survivors      # cockpit pane spared
    assert "bead-real" not in survivors   # genuine done worker reaped
    assert "NOT reaping" in r.stderr and "cockpit" in r.stderr


def test_reap_spares_pane_whose_live_process_is_a_resumed_manager(sock):
    # SABLE-k8o5: the generalization. `worker` is a genuine done worker OWNED by
    # optimus (CLAUDE_AGENT_NAME=optimus AND the SABLE_WORKER_PANE=1 spawn marker).
    # `manager` is the optimus MANAGER resumed into a sibling finished window: the
    # IDENTICAL CLAUDE_AGENT_NAME=optimus, but NO worker marker (emptied via -e).
    # Their identities match exactly; only the worker marker differs. --reap must
    # kill the worker and spare the live manager — proving the guard keys on the
    # marker, not a hardcoded 'lincoln'/agent-name list, and never over-blocks a
    # real done worker that shares its manager's lane name.
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "200", "-y", "50",
          "bash --noprofile --norc")
    time.sleep(0.3)
    placeholder = _tmux(sock, "list-panes", "-t", "w", "-F", "#{pane_id}").stdout.strip()
    worker = _split_pane(sock, "CLAUDE_AGENT_NAME=optimus", "SABLE_WORKER_PANE=1")
    manager = _split_pane(sock, "CLAUDE_AGENT_NAME=optimus", "SABLE_WORKER_PANE=")
    time.sleep(0.3)
    _tag(sock, worker, "worker", "bead-real", "done")
    _tag(sock, manager, "worker", "bead-stale", "done")   # stale/leftover worker tags
    _tmux(sock, "kill-pane", "-t", placeholder)
    time.sleep(0.3)
    assert _pane_count(sock) == 2

    r = _run(sock, "--reap")
    assert r.returncode == 0, r.stderr
    time.sleep(0.4)
    assert _pane_count(sock) == 1  # the manager survives; the real worker is reaped
    survivors = _tmux(sock, "list-panes", "-a", "-F", "#{@sable_bead}").stdout
    assert "bead-stale" in survivors      # resumed manager spared
    assert "bead-real" not in survivors   # genuine done worker (same lane) reaped
    assert "NOT reaping" in r.stderr and "optimus" in r.stderr and "manager" in r.stderr


# --- SABLE-c008: --reap refused to kill a done-unconfirmed pane superseded
# by a live revise-successor pane for the SAME bead. Incident (2026-07-16,
# i8kv): pane %4 (i8kv v1) stayed done-unconfirmed after its revise-successor
# %5 was spawned into the SAME worktree; --reap left %4 alive, so tarzan had
# to manually kill it to prevent a stray wake into the worktree %5 was
# actively editing (the SABLE-nhrb cross-worktree isolation class). ---

def _write_open_bd_stub(stub_dir: Path) -> None:
    """A stand-in `bd` CLI that reports EVERY bead as open (status
    in_progress, never closed) — drives confirm_done_status to downgrade a
    done-tagged pane to 'done-unconfirmed' before reaping_decision ever sees
    it, exercising the FULL pipeline (not just reaping_decision in
    isolation). Placed first on PATH for the --reap subprocess."""
    script = stub_dir / "bd"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        'if sys.argv[1:2] == ["show"]:\n'
        '    print(\'[{"status": "in_progress"}]\')\n'
        "else:\n"
        '    print("[]")\n'
    )
    script.chmod(0o755)


def test_reap_kills_done_unconfirmed_pane_superseded_by_live_successor(sock, tmp_path):
    """P1 is done but its bead is OPEN in the stubbed bd, so
    confirm_done_status downgrades it to done-unconfirmed. P2 is a LIVE
    pane tagged for the SAME bead -- the revise-successor signal. --reap
    must kill P1 (superseded) and leave P2 (the live successor) alive."""
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    _write_open_bd_stub(stub_dir)

    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "180", "-y", "40",
          "bash --noprofile --norc")
    time.sleep(0.4)
    _tag(sock, "w.0", "worker", "SABLE-c008-x1", "done")
    _tmux(sock, "split-window", "-t", "w", "bash --noprofile --norc")
    time.sleep(0.4)
    _tag(sock, "w.1", "worker", "SABLE-c008-x1", "running")
    assert _pane_count(sock) == 2
    survivor_pane = _tmux(sock, "display-message", "-p", "-t", "w.1",
                          "#{pane_id}").stdout.strip()

    env = {**_scrubbed_env(), "SABLE_TMUX_SOCKET": sock, "SABLE_TMUX_SESSION": "w",
           "SABLE_STATUS_SAMPLE_INTERVAL": "0.1",
           "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}"}
    r = subprocess.run(["python3", str(BIN), "--reap"], capture_output=True,
                       text=True, env=env)
    assert r.returncode == 0, r.stderr
    time.sleep(0.4)
    assert _pane_count(sock) == 1  # only the live successor survives
    remaining = _tmux(sock, "list-panes", "-a", "-F", "#{pane_id}").stdout.strip()
    assert remaining == survivor_pane, remaining


def test_reap_spares_lone_done_unconfirmed_pane(sock, tmp_path):
    """Regression: a done-unconfirmed pane with NO live sibling for its bead
    must NOT be reaped -- its work may still matter (the pre-c008 safeguard
    this fix must not erode)."""
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    _write_open_bd_stub(stub_dir)

    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "180", "-y", "40",
          "bash --noprofile --norc")
    time.sleep(0.4)
    _tag(sock, "w.0", "worker", "SABLE-c008-x2", "done")
    assert _pane_count(sock) == 1

    env = {**_scrubbed_env(), "SABLE_TMUX_SOCKET": sock, "SABLE_TMUX_SESSION": "w",
           "SABLE_STATUS_SAMPLE_INTERVAL": "0.1",
           "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}"}
    r = subprocess.run(["python3", str(BIN), "--reap"], capture_output=True,
                       text=True, env=env)
    assert r.returncode == 0, r.stderr
    time.sleep(0.4)
    assert _pane_count(sock) == 1  # lone done-unconfirmed pane survives


# --- SABLE-1tzv: sable-view listed a done-tagged worker pane correctly while
# sable-worker-status --reap, run moments later, printed 'no worker panes' and
# reaped nothing -- read live as "reap vs view detection disagree". The
# REVISED hypothesis (confirmed here): sable-view (parse_panes/_FORMAT above)
# has NO lane concept at all -- it always lists the whole fleet -- while
# sable-worker-status's DEFAULT view is scoped to the caller's own lane
# (SABLE-dcw2, landed after this incident). The two tools were being compared
# under DIFFERENT scopes, not disagreeing about the SAME scope. The honest
# fleet-wide equivalent is `sable-worker-status --all` (also already
# implicit in --reap: main() reaps exactly the `workers` list it prints, so
# view and reap can never diverge for a given scope by construction). This
# locks that parity in: under the SAME (fleet-wide) scope, sable-view and
# `sable-worker-status --all` agree on a done worker pane, and --reap --all
# actually collects what both list. ---

def _run_view(s, *args):
    return subprocess.run(["python3", str(VIEW_BIN), *args], capture_output=True,
                          text=True, env={**_scrubbed_env(), "SABLE_TMUX_SOCKET": s,
                                          "SABLE_TMUX_SESSION": "w"})


def test_view_and_reap_all_scope_agree_on_done_worker_pane(sock):
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "180", "-y", "40",
          "bash --noprofile --norc")
    time.sleep(0.4)
    pane = _tmux(sock, "display-message", "-p", "-t", "w.0", "#{pane_id}").stdout.strip()
    _tag(sock, pane, "worker", "SABLE-1tzv-x", "done")

    view = _run_view(sock, "--json")
    assert view.returncode == 0, view.stderr
    view_hit = [p for p in json.loads(view.stdout) if p["pane"] == pane]
    assert view_hit and view_hit[0]["status"] == "done", view.stdout

    status = _run_as(sock, "", "--all", "--json")
    assert status.returncode == 0, status.stderr
    status_hit = [w for w in json.loads(status.stdout)["workers"] if w["pane"] == pane]
    assert status_hit and status_hit[0]["status"] == "done", status.stdout

    reap = _run_as(sock, "", "--all", "--reap")
    assert reap.returncode == 0, reap.stderr
    time.sleep(0.3)
    # the lone pane was killed -- the server itself may have exited with it
    # (no clients attached, no panes left), so tolerate a failed list-panes
    # exactly like that: zero panes.
    remaining = _tmux(sock, "list-panes", "-a", "-F", "#{pane_id}", check=False)
    assert remaining.returncode != 0 or not remaining.stdout.strip()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
