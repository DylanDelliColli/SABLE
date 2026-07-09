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
    # SABLE_TMUX_SESSION pinned: these single-fleet cases use the operator
    # override; per-repo resolution is covered by the two-fleet tests below.
    return subprocess.run(
        ["python3", str(BIN), *cli_args],
        capture_output=True, text=True,
        env={**_env(), "SABLE_TMUX_SOCKET": sock, "SABLE_TMUX_SESSION": "w"},
    )


def _env():
    import os
    env = dict(os.environ)
    # These subprocess-driven sends must exercise CWD-derivation deterministically,
    # not whichever real pane happens to be running pytest (SABLE-ssd8: pytest's
    # ambient $TMUX_PANE could coincidentally collide with a pane id on the
    # freshly created isolated socket below, since ids restart from %0 per server).
    env.pop("TMUX_PANE", None)
    env.pop("TMUX", None)
    return env


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


def test_idle_pane_receives_interrupt_first_attempt(tmux_socket, tmp_path):
    # SABLE-nmmh — the SABLE-kkgt repro, INVERTED. Before the event-driven loop
    # fix, a manager pane sat mid-turn inside a foreground `time.sleep(30)`, so an
    # --interrupt message was swallowed (the urgent channel was deaf). An
    # event-driven manager ENDS its turn and idles at its prompt; an --interrupt
    # message must then land on the FIRST submit attempt.
    #
    # SABLE_MSG_SUBMIT_TRIES=1 makes "first-attempt" load-bearing: a single
    # failed submit fails the send. SABLE_MSG_AUTO_FALLBACK=0 keeps a failed send
    # from filing a real inbox bead into the operator's bd db.
    #
    # The bash REPL stands in for the manager's claude TUI. In the real TUI,
    # --interrupt's Escape CLEARS the composer non-destructively; bash readline
    # instead treats a bare Escape as the Meta prefix and would eat the first byte
    # of the framed header (a REPL-only artifact, not a delivery bug). The pane is
    # launched with an inputrc binding Escape to a standalone no-op, modeling the
    # TUI's non-destructive Escape so the interrupt path is exercised faithfully.
    inputrc = tmp_path / "inputrc"
    inputrc.write_text('"\\e": redraw-current-line\n')
    _tmux(tmux_socket, "new-session", "-d", "-s", "w", "-x", "200", "-y", "50",
          f"INPUTRC={inputrc} PS1='> ' bash --noprofile --norc")
    time.sleep(0.5)
    _tmux(tmux_socket, "set-option", "-p", "-t", "w", "@sable_role", "optimus")

    r = subprocess.run(
        ["python3", str(BIN), "optimus", "; echo INTERRUPT-LANDED",
         "--from", "lincoln", "--interrupt"],
        capture_output=True, text=True,
        env={**_env(), "SABLE_TMUX_SOCKET": tmux_socket, "SABLE_TMUX_SESSION": "w",
             "SABLE_MSG_SUBMIT_TRIES": "1", "SABLE_MSG_AUTO_FALLBACK": "0"},
    )
    assert r.returncode == 0, r.stderr          # delivered, verified, first attempt
    time.sleep(0.8)
    pane = _capture(tmux_socket, "w")
    assert "⟦SABLE-MSG⟧ from=lincoln to=optimus" in pane   # framed turn landed intact
    assert "INTERRUPT-LANDED" in pane                        # and executed (turn submitted)


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


# --- per-repo scoping (SABLE-e1e3.3): a fleet is addressed only by its repo ---

def _make_fleet(sock, tmp_path, name, role="tarzan"):
    """A repo + its derived-session fleet: one bash REPL pane tagged with the
    role, the session stamped @sable_repo — exactly what sable-tmux creates."""
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    root = str(repo.resolve())
    sess = f"sable-{name}"
    _tmux(sock, "new-session", "-d", "-s", sess, "-x", "200", "-y", "50",
          "PS1='> ' bash --noprofile --norc")
    time.sleep(0.4)
    _tmux(sock, "set-option", "-t", sess, "@sable_repo", root)
    _tmux(sock, "set-option", "-p", "-t", sess, "@sable_role", role)
    _tmux(sock, "set-option", "-p", "-t", sess, "@sable_repo", root)
    return repo, sess


def _run_msg_from(sock, repo, *cli_args):
    env = {**_env(), "SABLE_TMUX_SOCKET": sock}
    env.pop("SABLE_TMUX_SESSION", None)  # per-repo resolution path
    return subprocess.run(["python3", str(BIN), *cli_args],
                          capture_output=True, text=True, env=env, cwd=repo)


def test_two_fleets_role_delivery_is_repo_scoped(tmux_socket, tmp_path):
    alpha, sess_a = _make_fleet(tmux_socket, tmp_path, "alpha")
    beta, sess_b = _make_fleet(tmux_socket, tmp_path, "beta")
    r = _run_msg_from(tmux_socket, alpha, "tarzan",
                      "echo ALPHA-ONLY", "--from", "lincoln")
    assert r.returncode == 0, r.stderr
    time.sleep(0.8)
    assert "ALPHA-ONLY" in _capture(tmux_socket, sess_a)
    assert "ALPHA-ONLY" not in _capture(tmux_socket, sess_b)


def test_own_fleet_down_never_falls_through_to_another_repo(tmux_socket, tmp_path):
    # only beta's fleet is up; a send from alpha must FAIL, not cross over
    _make_fleet(tmux_socket, tmp_path, "beta")
    alpha = tmp_path / "alpha"
    alpha.mkdir()
    subprocess.run(["git", "init", "-q", str(alpha)], check=True)
    r = _run_msg_from(tmux_socket, alpha, "tarzan", "echo LEAKED", "--from", "lincoln")
    assert r.returncode != 0
    assert "sable-alpha" in (r.stderr + r.stdout)
    time.sleep(0.5)
    assert "LEAKED" not in _capture(tmux_socket, "sable-beta")


# --- cross-repo CWD vs actual pane session (SABLE-ssd8) ---------------------
# The live bug: a worker's ACTUAL tmux session (where tarzan lives) can differ
# from whatever session CWD-derivation would compute for its worktree repo,
# e.g. a worker dispatched by a manager tracking repo alpha but working in an
# unrelated repo beta's worktree as CWD. beta must be a REAL, concurrently
# running fleet (not a nonexistent guessed name) to reproduce the reported
# failure faithfully -- with no real "sable-beta" session, the legacy 'sable'
# fallback's tmux target prefix-matching ("-t sable" uniquely resolves to
# whichever single "sable-*" session exists) plus the worker pane's own cwd
# satisfying _panes_under_root would accidentally paper over the bug. With
# beta genuinely running (its own real fleet, no tarzan pane in it),
# CWD-derivation confidently and WRONGLY resolves "sable-beta" and never
# reaches the legacy fallback at all -- exactly SABLE-ssd8's live failure.

def test_pane_session_wins_over_mismatched_cwd_repo(tmux_socket, tmp_path):
    alpha, sess_a = _make_fleet(tmux_socket, tmp_path, "alpha")  # tarzan lives here
    beta, sess_b = _make_fleet(tmux_socket, tmp_path, "beta", role="chuck")  # real, no tarzan
    tarzan_pane = _tmux(tmux_socket, "list-panes", "-t", sess_a,
                        "-F", "#{pane_id}").stdout.split()[0]

    # a second pane in alpha's OWN session, but shelled into beta's worktree —
    # exactly the mismatched-CWD shape a cross-repo worker dispatch produces.
    _tmux(tmux_socket, "split-window", "-t", sess_a, "-d", "-c", str(beta),
          "PS1='> ' bash --noprofile --norc")
    time.sleep(0.4)
    all_panes = _tmux(tmux_socket, "list-panes", "-t", sess_a,
                      "-F", "#{pane_id}").stdout.split()
    worker_pane = next(p for p in all_panes if p != tarzan_pane)
    _tmux(tmux_socket, "set-option", "-p", "-t", worker_pane, "@sable_role", "worker")

    # send FROM the worker pane itself via send-keys, so $TMUX_PANE is real
    # (set by tmux for that pane's own bash, not injected by the test) even
    # though the shell's CWD is beta, not alpha.
    body = "; echo CROSS-REPO-DELIVERED"
    cmd = (f'unset SABLE_TMUX_SESSION; SABLE_TMUX_SOCKET={tmux_socket} '
          f'python3 {BIN} tarzan "{body}" --from worker')
    _tmux(tmux_socket, "send-keys", "-t", worker_pane, cmd, "Enter")
    time.sleep(1.5)
    assert "CROSS-REPO-DELIVERED" in _capture(tmux_socket, sess_a)
    assert "CROSS-REPO-DELIVERED" not in _capture(tmux_socket, sess_b)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
