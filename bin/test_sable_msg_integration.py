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


def test_default_send_to_busy_pane_reports_undelivered(tmux_socket):
    # SABLE-d21h (was test_message_queues_while_target_busy): a DEFAULT-mode send
    # to a pane that is BUSY at t0 must report UNDELIVERED, not a phantom
    # 'delivered'. In the real Claude TUI a queued line is hoisted above the
    # composer with the box cleared, so the old visible-vs-submitted check
    # false-positived for a message merely QUEUED behind the running turn (which
    # is droppable on the turn's compaction/redraw/reap). The pre-send idle guard
    # fails closed so sable-msg routes to the durable fallback. (Here the pane is
    # busy mid-`sleep`, showing no prompt line -> not idle at t0.)
    #
    # AUTO_FALLBACK=0 keeps a failed send from filing a real inbox bead into the
    # operator's bd db; the manual-hint 'undelivered' line is asserted instead.
    _start_pane(tmux_socket)
    _tmux(tmux_socket, "send-keys", "-t", "w",
          "echo BUSY-START; sleep 3; echo BUSY-END", "Enter")
    time.sleep(0.3)  # now mid-command -> busy (no prompt line) at t0
    r = subprocess.run(
        ["python3", str(BIN), "optimus", "echo QUEUED-RAN", "--from", "lincoln"],
        capture_output=True, text=True,
        env={**_env(), "SABLE_TMUX_SOCKET": tmux_socket, "SABLE_TMUX_SESSION": "w",
             "SABLE_MSG_AUTO_FALLBACK": "0", "SABLE_MSG_SUBMIT_TRIES": "2",
             "SABLE_MSG_POLL_INTERVAL": "0.2"},
    )
    assert r.returncode != 0, "busy-at-t0 default send must not report delivered"
    assert "undelivered" in r.stderr


# --- mid-turn busy pane: interrupt lands, default-mode queues (SABLE-m6is) ---
# The live failure: --interrupt into a manager pane actively mid-turn (xhigh
# thinking, tools running) dropped the message on all 8 submit attempts, because
# the pane STILL shows the empty composer prompt during a turn — pane_ready fired
# early and the message was typed into a pane still redrawing the interrupted
# turn. The stand-in below models exactly that shape.

# A Claude-TUI-mid-turn stand-in. A long-running redraw loop paints a composer
# prompt line (so pane_ready is True — the early-fire trap) AND an
# "esc to interrupt" status line (so pane_busy is True, pane_idle False). A bare
# Escape INTERRUPTS (records INTERRUPTED, settles to an idle REPL); without one,
# the turn ends on its own after BUSY_SECS (records NATURAL) so a default-mode
# send that merely QUEUES still eventually lands. Non-Escape input is held and
# replayed as one submitted turn once the turn ends — the queue behavior a
# default-mode send relies on. Stray resent Enters (deliver_text's dropped-Enter
# retries) are ignored. Every submitted turn is appended to REC_FILE.
_BUSY_TUI = r'''#!/usr/bin/env bash
queued=""
busy=1
END_AT=$((SECONDS + ${BUSY_SECS:-3}))
while [ "$busy" = 1 ]; do
  printf '\033[H\033[2J  Running the turn (esc to interrupt)\n'
  printf '\xe2\x9d\xaf %s\n' "$queued"
  if IFS= read -rsN1 -t 0.2 ch; then
    case "$ch" in
      $'\x1b') printf 'INTERRUPTED' > "$END_FILE"; busy=0 ;;
      $'\n'|$'\r'|'') : ;;
      *) IFS= read -r rest; queued="$ch$rest" ;;
    esac
  fi
  if [ "$busy" = 1 ] && [ "$SECONDS" -ge "$END_AT" ]; then
    printf 'NATURAL' > "$END_FILE"; busy=0
  fi
done
printf '\033[H\033[2J'
printf '%.0s\n' $(seq 1 60)
if [ -n "$queued" ]; then
  printf '\xe2\x9d\xaf %s\n' "$queued"
  printf '%s\n' "$queued" >> "$REC_FILE"
fi
while true; do
  printf '\xe2\x9d\xaf '
  IFS= read -r line || break
  printf '%s\n' "$line" >> "$REC_FILE"
done
'''


def _start_busy_pane(sock, tmp_path, busy_secs):
    """A pane running the mid-turn TUI stand-in, tagged @sable_role=optimus.
    Returns (rec_file, end_file): rec_file collects submitted turns; end_file
    records how the turn ended (INTERRUPTED via Escape, or NATURAL via timeout)."""
    rec = tmp_path / "rec.txt"
    end = tmp_path / "end.txt"
    script = tmp_path / "busy_tui.sh"
    script.write_text(_BUSY_TUI)
    script.chmod(0o755)
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "200", "-y", "50",
          f"REC_FILE={rec} END_FILE={end} BUSY_SECS={busy_secs} bash {script}")
    time.sleep(0.4)
    _tmux(sock, "set-option", "-p", "-t", "w", "@sable_role", "optimus")
    return rec, end


def test_interrupt_lands_on_busy_midturn_pane_first_attempt(tmux_socket, tmp_path):
    # BUSY_SECS is huge so the turn can only end via the interrupt, never a
    # natural timeout — end.txt == INTERRUPTED is proof the Escape settled it.
    # SUBMIT_TRIES=1 makes "first attempt" load-bearing (the live failure burned
    # all 8). AUTO_FALLBACK=0 keeps a failure from filing a real inbox bead.
    rec, end = _start_busy_pane(tmux_socket, tmp_path, busy_secs=60)
    r = subprocess.run(
        ["python3", str(BIN), "optimus", "cap in force", "--from", "lincoln",
         "--interrupt"],
        capture_output=True, text=True,
        env={**_env(), "SABLE_TMUX_SOCKET": tmux_socket, "SABLE_TMUX_SESSION": "w",
             "SABLE_MSG_SUBMIT_TRIES": "1", "SABLE_MSG_AUTO_FALLBACK": "0",
             "SABLE_MSG_READY_TIMEOUT": "10", "SABLE_MSG_POLL_INTERVAL": "0.3"},
    )
    assert r.returncode == 0, r.stderr              # delivered, verified, first attempt
    assert end.read_text().strip() == "INTERRUPTED"  # settled via Escape, not a timeout
    time.sleep(0.5)
    pane = _capture(tmux_socket, "w")
    assert "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force" in pane  # landed intact
    assert "cap in force" in rec.read_text()         # and was SUBMITTED as a turn


def test_default_send_to_busy_turn_does_not_interrupt_and_reports_undelivered(tmux_socket, tmp_path):
    # The companion guard: on the SAME kind of busy pane, a DEFAULT-mode send
    # (no --interrupt) must NOT interrupt the turn — the idle-wait + Escape logic
    # is confined to the --interrupt leg (SABLE-m6is). end.txt == NATURAL proves
    # the turn ran to its own end. SABLE-d21h: because the pane is BUSY at t0, the
    # send is not verified-landed, so sable-msg reports undelivered (routes to the
    # durable fallback) even though the stand-in still physically queues the line.
    rec, end = _start_busy_pane(tmux_socket, tmp_path, busy_secs=2)
    r = subprocess.run(
        ["python3", str(BIN), "optimus", "queued directive", "--from", "lincoln"],
        capture_output=True, text=True,
        env={**_env(), "SABLE_TMUX_SOCKET": tmux_socket, "SABLE_TMUX_SESSION": "w",
             "SABLE_MSG_SUBMIT_TRIES": "2", "SABLE_MSG_AUTO_FALLBACK": "0",
             "SABLE_MSG_POLL_INTERVAL": "0.2"},
    )
    assert r.returncode != 0                          # busy at t0 -> not verified-landed
    assert "undelivered" in r.stderr
    time.sleep(2.5)                                   # let the busy turn end on its own
    assert end.read_text().strip() == "NATURAL"      # default mode never interrupted it
    assert "queued directive" in rec.read_text()      # line still physically queued + ran


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
