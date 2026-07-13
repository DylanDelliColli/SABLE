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


def test_table_shows_worker_count_vs_cap(sock):
    """SABLE-mmdt acceptance: the operator must see live worker count vs cap
    without leaving the cockpit — the status table carries a count/cap line."""
    _seed_session(sock)  # seeds exactly one running worker pane
    r = subprocess.run(["python3", str(BIN)], capture_output=True, text=True,
                       env={**os.environ, "SABLE_TMUX_SOCKET": sock,
                            "SABLE_TMUX_SESSION": SESSION,
                            "SABLE_MAX_WORKERS": "4"})
    assert r.returncode == 0, r.stderr
    assert "1/4" in r.stdout
    assert "SABLE_MAX_WORKERS" in r.stdout


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


def test_focus_outside_tmux_uses_grouped_session_independent_focus(sock):
    """A deep-dive from a second terminal must NOT yank the operator's Lincoln
    view: outside tmux the focus form goes through a GROUPED session whose
    current window is the target, while the original session stays put."""
    _seed_session(sock)
    r = _run(sock, "worker", "--no-attach")
    assert r.returncode == 0, r.stderr
    # the original session's active window is untouched (Lincoln keeps window 0)
    active = _tmux(sock, "display-message", "-t", SESSION, "-p",
                   "#{window_index}").stdout.strip()
    assert active == "0"
    # a grouped view session exists and its current window is the worker's
    sessions = _tmux(sock, "list-sessions", "-F",
                     "#{session_name} #{session_group}").stdout
    view_names = [ln.split()[0] for ln in sessions.splitlines()
                  if ln.split()[0].startswith(f"{SESSION}-view-")]
    assert view_names, f"no grouped view session in: {sessions}"
    vactive = _tmux(sock, "display-message", "-t", view_names[0], "-p",
                    "#{window_index}").stdout.strip()
    assert vactive == "1"
    # and both sessions are in the same group (shared windows)
    groups = {ln.split()[1] for ln in sessions.splitlines() if len(ln.split()) > 1}
    assert len(groups) == 1


# --- SABLE-d8tl: destroy-unattached vs the still-detached grouped session ----
#
# A stub tmux that models the REAL server behavior which crashed the deep-dive:
# a DETACHED session dies the instant destroy-unattached turns on. It logs every
# invocation (command sequences split on ';') and keeps session state in a JSON
# file across invocations.
STUB_TMUX = """#!/usr/bin/env python3
import json, os, sys

state_path = os.environ["SV_STUB_STATE"]
log_path = os.environ["SV_STUB_LOG"]
args = sys.argv[1:]
with open(log_path, "a") as f:
    f.write(json.dumps(args) + "\\n")
with open(state_path) as f:
    state = json.load(f)

def save():
    with open(state_path, "w") as f:
        json.dump(state, f)

cmds, cur = [], []
for a in args:
    if a == ";":
        cmds.append(cur); cur = []
    else:
        cur.append(a)
cmds.append(cur)

for cmd in cmds:
    if not cmd:
        continue
    name = cmd[0]
    if name == "has-session":
        continue
    if name == "list-panes":
        sys.stdout.write("0 %0 lincoln  \\n1 %4 worker SABLE-int1 running\\n")
        continue
    if name == "new-session":
        state["view"] = {"attached": False, "alive": True}
        save()
        continue
    if name == "set-option" and "destroy-unattached" in cmd:
        if state.get("view", {}).get("alive") and not state["view"]["attached"]:
            state["view"]["alive"] = False   # tmux reaps a detached session NOW
            save()
        continue
    if name in ("select-window", "select-pane", "attach", "switch-client"):
        if not state.get("view", {}).get("alive"):
            sys.stderr.write("cannot find session\\n")
            sys.exit(1)
        if name in ("attach", "switch-client"):
            state["view"]["attached"] = True
            save()
        continue
sys.exit(0)
"""


def test_attach_path_survives_destroy_unattached_race(tmp_path):
    """SABLE-d8tl regression: with a tmux that destroys a detached session the
    moment destroy-unattached is set, the second-terminal deep-dive must still
    complete — the option may only be enabled AFTER attach, chained onto the
    attach exec, never as a pre-attach command."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "tmux"
    stub.write_text(STUB_TMUX)
    stub.chmod(0o755)
    state = tmp_path / "state.json"
    state.write_text("{}")
    log = tmp_path / "log.jsonl"
    log.write_text("")
    env = {**os.environ,
           "PATH": f"{stub_dir}:{os.environ['PATH']}",
           "SV_STUB_STATE": str(state),
           "SV_STUB_LOG": str(log),
           "SABLE_TMUX_SESSION": SESSION}
    env.pop("TMUX", None)               # outside-tmux deep-dive: the attach path
    env.pop("SABLE_TMUX_SOCKET", None)  # bare `tmux` resolves to the stub
    r = subprocess.run(["python3", str(BIN), "worker"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "CalledProcessError" not in r.stderr

    calls = [json.loads(line) for line in log.read_text().splitlines()]
    # never issued as its own pre-attach invocation...
    solo = [c for c in calls if c and c[0] == "set-option" and "destroy-unattached" in c]
    assert not solo, f"pre-attach destroy-unattached: {solo}"
    # ...it rides the attach invocation, after the attach command
    attach_calls = [c for c in calls if c and c[0] in ("attach", "switch-client")]
    assert attach_calls, f"no attach exec recorded: {calls}"
    seq = attach_calls[-1]
    assert "destroy-unattached" in seq
    assert seq.index("destroy-unattached") > seq.index(";")
    # the stub's lifecycle model agrees: the view session survived to attach
    final = json.loads(state.read_text())
    assert final["view"]["alive"] and final["view"]["attached"]


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
