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


def _server_env():
    """The env the tmux SERVER (and thus every pane without an explicit -e) is
    started under. CLAUDE_AGENT_NAME is stripped (SABLE-to8m): pane identity must
    be set explicitly per pane (tmux -e / _start_pane_as), never inherited from
    whoever runs pytest — otherwise a runner that is itself a SABLE agent (a
    manager pane exports CLAUDE_AGENT_NAME) would leak its identity into every
    stand-in pane and the recipient identity cross-check would (correctly, but
    unhelpfully for these fixtures) refuse role sends whose tag != that leaked
    identity."""
    import os
    env = dict(os.environ)
    env.pop("CLAUDE_AGENT_NAME", None)
    return env


def _tmux(sock, *args, check=True):
    return subprocess.run(["tmux", "-L", sock, *args],
                          capture_output=True, text=True, check=check,
                          env=_server_env())


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


def test_default_send_to_busy_pane_that_frees_reports_delivered_h0jw(tmux_socket, tmp_path):
    # SABLE-h0jw: the delayed-confirmation happy path, end-to-end against a REAL
    # tmux server + real sable-msg subprocess. The pane is BUSY at t0 (mid-turn,
    # 'esc to interrupt'); our line queues behind that turn. The turn ends WITHIN
    # the poll budget (busy_secs=1, budget ~=SUBMIT_TRIES*POLL_INTERVAL) and the
    # queued line submits as its own turn (recorded to REC_FILE). sable-msg must
    # then report DELIVERED — NOT the d21h fail-close-at-t0 that would have filed a
    # redundant noise bead for a message that actually landed. AUTO_FALLBACK=0 so a
    # (pre-fix) failure can't write a real inbox bead; a generous budget clears the
    # 1s turn. end.txt == NATURAL proves the turn was never interrupted.
    rec, end = _start_busy_pane(tmux_socket, tmp_path, busy_secs=1)
    r = subprocess.run(
        ["python3", str(BIN), "optimus", "cap in force", "--from", "lincoln"],
        capture_output=True, text=True,
        env={**_env(), "SABLE_TMUX_SOCKET": tmux_socket, "SABLE_TMUX_SESSION": "w",
             "SABLE_MSG_AUTO_FALLBACK": "0", "SABLE_MSG_SUBMIT_TRIES": "20",
             "SABLE_MSG_POLL_INTERVAL": "0.25"},
    )
    assert "cap in force" in rec.read_text(), \
        "precondition: the queued line must have really submitted as a turn"
    assert r.returncode == 0, r.stderr                 # delayed confirmation -> delivered
    assert "delivered" in r.stderr
    assert end.read_text().strip() == "NATURAL"        # default mode never interrupted it


# --- queued-composer footer + idempotent retry (SABLE-msxj) -----------------
# Recurrence of the h0jw class AFTER h0jw merged (LINCOLN 2026-07-15, optimus
# pane %47): the real Claude-TUI does NOT hoist a queued line above the
# composer and clear the box the way h0jw's stand-in above models — it leaves
# the line VISIBLE in the composer and appends a 'Press up to edit queued
# messages' footer. h0jw's box-based signals never recognized that footer, so
# a genuinely delivered-queued send timed out the poll budget and was scored
# undelivered (SABLE-l8a5: closed false-fail). Worse, retrying the send typed
# the same text into the pane a SECOND time, producing a duplicate turn.

# A busy-TUI stand-in identical to _BUSY_TUI except it renders the real TUI's
# queued-messages footer once a line is queued — modeling the exact posture
# SABLE-msxj reports. ARRIVALS_FILE records every distinct line the script
# actually reads off the pty (append, not overwrite) — the ground truth for
# "how many times was this text really typed into the pane", independent of
# `queued`'s final value (which a second identical send would not visibly
# change). REC_FILE keeps recording the line that ultimately submits once the
# turn ends, exactly as _BUSY_TUI does.
#
# The busy->idle transition is TEST-CONTROLLED via markers, not a wall-clock
# BUSY_SECS window (SABLE-wcbj: sable-msg's t0 capture, and its own tight poll
# budget racing the footer redraw, both raced that window under host load —
# same class as the l7uv STUCK_BOX flake, fixed the same way in a5d9304). The
# stand-in signals busy-entry (BUSY_READY, before the first read) and stays
# busy until the test releases it (GO_IDLE); on release it records NATURAL to
# END_FILE, mirroring the wall-clock timeout's own end-of-turn signal.
#
# Reads a whole LINE per iteration, not one character at a time (SABLE-wcbj,
# the actual root cause under load — a generous poll budget alone did not fix
# it, and pinning the locale to byte-oriented C did not fix it either, proven
# by direct reproduction outside pytest: both left an identical corruption,
# the leading byte of the '\xe2\x9f\xa6' (⟦) that opens every SABLE-MSG framing
# header silently dropped, producing a pane the footer-recognition regex can
# never match no matter how long sable-msg's poll budget is). The character-
# at-a-time loop this replaced existed only to detect a bare Escape between
# characters of a queued line — but neither test using this stand-in ever
# sends --interrupt/Escape, so that detection is unneeded here and its own
# ~5Hz redraw-vs-read cadence was the actual race window. A per-read timeout
# long enough that it only ever fires while genuinely idle (never mid-transfer
# of the already-typed text) removes the race outright: reproduced directly
# (bypassing pytest) under synthetic 4-core CPU load, 300/300 clean with this
# design after both the SUBMIT_TRIES=40 budget and either LC_ALL fix still
# failed at roughly 1-in-100 to 1-in-300.
_QUEUED_FOOTER_TUI = r'''#!/usr/bin/env bash
queued=""
busy=1
: > "$BUSY_READY"                                  # signal busy-entry BEFORE first read
while [ "$busy" = 1 ]; do
  printf '\033[H\033[2J  Running the turn (esc to interrupt)\n'
  printf '\xe2\x9d\xaf %s\n' "$queued"
  if [ -n "$queued" ]; then
    printf '  Press up to edit queued messages\n'
  fi
  if IFS= read -r -t 2 line; then
    queued="$line"
    printf '%s\n' "$queued" >> "$ARRIVALS_FILE"
  fi
  if [ "$busy" = 1 ] && [ -e "$GO_IDLE" ]; then
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


def _start_queued_footer_pane(sock, tmp_path):
    """Marker-driven variant of _start_busy_pane specialized for the queued-
    footer posture (SABLE-msxj / SABLE-wcbj). Returns (rec, end, arrivals,
    busy_ready, go_idle): rec/end/arrivals as before; busy_ready signals
    busy-entry before the first read (so a caller can gate t0 capture to the
    busy phase); go_idle is the test->stand-in release that ends the busy
    phase, replacing the wall-clock BUSY_SECS window that raced under load."""
    rec = tmp_path / "rec.txt"
    end = tmp_path / "end.txt"
    arrivals = tmp_path / "arrivals.txt"
    busy_ready = tmp_path / "busy_ready"
    go_idle = tmp_path / "go_idle"
    script = tmp_path / "queued_footer_tui.sh"
    script.write_text(_QUEUED_FOOTER_TUI)
    script.chmod(0o755)
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "200", "-y", "50",
          f"REC_FILE={rec} END_FILE={end} ARRIVALS_FILE={arrivals} "
          f"BUSY_READY={busy_ready} GO_IDLE={go_idle} bash {script}")
    time.sleep(0.4)
    _tmux(sock, "set-option", "-p", "-t", "w", "@sable_role", "optimus")
    return rec, end, arrivals, busy_ready, go_idle


def test_default_send_to_busy_pane_with_queued_footer_confirms_delivered_msxj(tmux_socket, tmp_path):
    # SABLE-msxj: the running turn never ends (the test never releases
    # GO_IDLE) so any confirmation must come from recognizing the
    # queued-messages footer itself, not from h0jw's turn-boundary signals
    # (which require the turn to actually end or our line to echo as its own
    # prompt). Pre-fix this exhausted the poll budget and reported undelivered
    # even though the line was genuinely queued (SABLE-l8a5).
    #
    # SABLE-wcbj: waiting for BUSY_READY (instead of racing a wall-clock
    # BUSY_SECS window) guarantees sable-msg's t0 capture lands during the
    # busy phase; the stand-in's full-line read (see _QUEUED_FOOTER_TUI above)
    # keeps the typed text from getting corrupted under host load, and a
    # generous SUBMIT_TRIES budget is extra headroom on top of that — the
    # footer stays the SOLE confirmation source either way, since GO_IDLE is
    # never touched.
    rec, end, arrivals, busy_ready, go_idle = _start_queued_footer_pane(tmux_socket, tmp_path)
    assert _wait_until(busy_ready.exists, timeout=10), \
        "stand-in never signalled busy-entry"
    r = subprocess.run(
        ["python3", str(BIN), "optimus", "cap in force", "--from", "lincoln"],
        capture_output=True, text=True,
        env={**_env(), "SABLE_TMUX_SOCKET": tmux_socket, "SABLE_TMUX_SESSION": "w",
             "SABLE_MSG_AUTO_FALLBACK": "0", "SABLE_MSG_SUBMIT_TRIES": "40",
             "SABLE_MSG_POLL_INTERVAL": "0.2"},
    )
    assert r.returncode == 0, r.stderr           # footer alone must confirm -> delivered
    assert "delivered" in r.stderr
    assert not end.exists(), "the turn must still be running (never reached NATURAL end)"
    # arrivals.txt is guaranteed to exist here (SABLE-du3w's race is structurally
    # closed by this test's design): r.returncode == 0 means sable-msg's poll
    # already observed the queued-footer posture, which requires the stand-in
    # to have already appended to ARRIVALS_FILE.
    assert arrivals.read_text().count("cap in force") == 1


def test_second_send_on_still_busy_pane_does_not_double_queue_msxj(tmux_socket, tmp_path):
    # THE bead's double-queue repro, end-to-end: two independent sable-msg
    # invocations target the SAME still-busy pane while the first message is
    # still sitting queued (footer showing). The second call's t0 capture
    # already contains the message, so it must skip retyping and just
    # re-confirm the existing queued line -- a single arrival, not two.
    #
    # SABLE-wcbj: BUSY_READY/GO_IDLE markers (not a wall-clock BUSY_SECS
    # window + fixed sleep) make both the busy phase and the busy->idle
    # transition deterministic under host load. The turn is released to idle
    # only after the first send's queued arrival is confirmed, so the second
    # send's t0 capture is guaranteed to observe the already-queued line.
    rec, end, arrivals, busy_ready, go_idle = _start_queued_footer_pane(tmux_socket, tmp_path)
    assert _wait_until(busy_ready.exists, timeout=10), \
        "stand-in never signalled busy-entry"
    kwargs = dict(
        capture_output=True, text=True,
        env={**_env(), "SABLE_TMUX_SOCKET": tmux_socket, "SABLE_TMUX_SESSION": "w",
             "SABLE_MSG_AUTO_FALLBACK": "0", "SABLE_MSG_SUBMIT_TRIES": "40",
             "SABLE_MSG_POLL_INTERVAL": "0.2"},
    )
    r1 = subprocess.run(
        ["python3", str(BIN), "optimus", "cap in force", "--from", "lincoln"], **kwargs)
    assert r1.returncode == 0, r1.stderr
    assert _wait_until(lambda: arrivals.read_text().count("cap in force") == 1, timeout=10), \
        "first send must have queued exactly once before the second send starts"
    r2 = subprocess.run(
        ["python3", str(BIN), "optimus", "cap in force", "--from", "lincoln"], **kwargs)
    assert r2.returncode == 0, r2.stderr

    go_idle.touch()  # release the busy turn now that both sends are confirmed queued
    assert _wait_until(lambda: end.exists() and end.read_text().strip() == "NATURAL",
                       timeout=10), \
        "busy turn never reached its (test-controlled) natural end"
    assert _wait_until(lambda: rec.exists() and rec.read_text().count("cap in force") == 1,
                       timeout=10), \
        "only a single copy of the message must ultimately submit"
    assert arrivals.read_text().count("cap in force") == 1, \
        "the second send must not have retyped an already-queued message"


# --- idle-pane redraw race: report-NOT-landed-when-it-DID (SABLE-uh4b) --------
# The INVERSE of the m6is/d21h swallow. A message sent to an IDLE standing-by
# pane really SUBMITS, but the capture taken in the redraw window right after
# Enter shows the just-submitted message echoed into the transcript as its own
# prompt-glyph line ("❯ <msg>") with the turn already running BELOW it (esc to
# interrupt) and the empty composer not yet repainted. The old box_start scan
# mistook that echo for the still-unsubmitted composer and reported UNDELIVERED
# on all 8 attempts — filing a duplicate durable fallback bead for a message
# that had actually landed, blocking a P0 worker release.

# An idle-pane TUI stand-in that PERMANENTLY holds the post-Enter redraw frame.
# At t0 it shows an empty composer prompt and no busy status (so the pane is
# idle at send time). Once it reads a submitted line it appends it to REC_FILE
# (proof the line was truly submitted as a turn, not a phantom) and then forever
# repaints ONLY the redraw-race frame: the submitted echo "❯ <msg>" as the last
# prompt-glyph line, with a running-turn "esc to interrupt" status BELOW it and
# NO empty composer repainted. Every sable-msg capture therefore lands in the
# redraw window — which the fix must read as LANDED.
_REDRAW_TUI = r'''#!/usr/bin/env bash
printf '\033[H\033[2J'
printf '\xe2\x9d\xaf '                 # empty idle composer (❯ + space), no busy line
line=""
while IFS= read -r line; do
  [ -n "$line" ] && break             # ignore stray blank Enters until the msg arrives
done
printf '%s\n' "$line" >> "$REC_FILE"   # proof: the line was submitted as a turn
while true; do
  printf '\033[H\033[2J'
  printf '\xe2\x9d\xaf '; printf '%s\n' "$line"          # submitted echo = last glyph line
  printf '  Running the turn (esc to interrupt)\n'       # running turn BELOW the echo
  sleep 0.2
done
'''


def test_idle_pane_redraw_race_reports_landed_not_undelivered(tmux_socket, tmp_path):
    # Real tmux + real sable-msg: the message truly submits (REC_FILE records it),
    # and sable-msg must report DELIVERED via the redraw-race capture. Pre-fix this
    # reported undelivered (rc != 0) despite REC_FILE holding the line — the exact
    # z776 false-negative. AUTO_FALLBACK=0 keeps a (pre-fix) failure from writing a
    # real inbox bead; SUBMIT_TRIES>1 lets the loop resend Enter as it would live.
    rec = tmp_path / "rec.txt"
    script = tmp_path / "redraw_tui.sh"
    script.write_text(_REDRAW_TUI)
    script.chmod(0o755)
    _tmux(tmux_socket, "new-session", "-d", "-s", "w", "-x", "200", "-y", "50",
          f"REC_FILE={rec} bash {script}")
    time.sleep(0.5)
    _tmux(tmux_socket, "set-option", "-p", "-t", "w", "@sable_role", "optimus")

    r = subprocess.run(
        ["python3", str(BIN), "optimus", "GO push your worktree branch now",
         "--from", "lincoln"],
        capture_output=True, text=True,
        env={**_env(), "SABLE_TMUX_SOCKET": tmux_socket, "SABLE_TMUX_SESSION": "w",
             "SABLE_MSG_AUTO_FALLBACK": "0", "SABLE_MSG_SUBMIT_TRIES": "5",
             "SABLE_MSG_POLL_INTERVAL": "0.2"},
    )
    assert "GO push your worktree branch now" in rec.read_text(), \
        "precondition: the message must have really submitted as a turn"
    assert r.returncode == 0, r.stderr          # and sable-msg must report it LANDED
    assert "delivered" in r.stderr


# --- busy-at-t0 submit-race: text stuck in the editable composer (SABLE-l7uv) -
# The false-undelivered class msxj's footer path did NOT retire. Repro (SABLE-
# mgyh, explicitly "NOT the queued-behind-a-turn state"): the pane is BUSY at t0
# (finishing the prior turn), so deliver_text takes the busy leg and sends Enter
# exactly ONCE — that Enter is absorbed in the busy->idle redraw. The prior turn
# then ends and our line is left sitting UN-submitted in the now-EDITABLE composer
# with NO queued-messages footer, so it never auto-submits and submitted_own_turn
# can never confirm it. Pre-fix the busy leg never resent Enter -> the poll budget
# timed out -> false 'undelivered' while the full message sat visibly stuck.

# A TUI stand-in that models exactly that posture. BUSY phase: a prior turn runs
# ('esc to interrupt'); our typed line is read (its terminating Enter absorbed
# here), stored, but NOT submitted. IDLE phase: no turn running, the line sits in
# the EDITABLE composer ('❯ <line>') with NO busy status and NO footer — it will
# NEVER submit on its own. Only a bare Enter (the l7uv self-heal resend) submits
# it, recording it to REC_FILE once.
#
# The busy->idle transition is TEST-CONTROLLED via markers, not a wall-clock
# BUSY_SECS window (which raced sable-msg's t0 capture and made the busy-at-t0
# path selection nondeterministic under load). The stand-in signals when it is
# busy (BUSY_READY, first thing in the loop) and when it has captured our typed
# line (STUCK_READ), and it stays busy until the test releases it (GO_IDLE). The
# test uses those markers to guarantee sable-msg captures during the busy phase
# and that the line is present in the editable box before the pane falls idle.
_STUCK_BOX_TUI = r'''#!/usr/bin/env bash
stuck=""
: > "$BUSY_READY"                                  # signal busy-entry BEFORE first read
busy=1
while [ "$busy" = 1 ]; do
  printf '\033[H\033[2J  Baking the prior turn (esc to interrupt)\n'
  printf '\xe2\x9d\xaf %s\n' "$stuck"
  if IFS= read -rsN1 -t 0.2 ch; then
    case "$ch" in
      $'\n'|$'\r'|'') : ;;                         # absorbed Enter — does nothing
      *) IFS= read -r rest; stuck="$ch$rest"; : > "$STUCK_READ" ;;  # our line + (absorbed) Enter
    esac
  fi
  [ -e "$GO_IDLE" ] && busy=0                       # stay busy until the test releases us
done
submitted=0
while true; do
  if [ "$submitted" = 0 ]; then
    printf '\033[H\033[2J'
    printf '\xe2\x9d\xaf %s\n' "$stuck"            # editable composer holding our text
    printf '  ddc@host:~/wt\n'
  fi
  IFS= read -r line || break
  if [ "$submitted" = 0 ] && [ -n "$stuck" ]; then
    printf '%s\n' "$stuck" >> "$REC_FILE"          # bare Enter submitted the stuck line
    submitted=1
    printf '\033[H\033[2J'
    printf '\xe2\x97\x8f %s\n' "$stuck"            # transcript echo (● <line>)
    printf '\xe2\x9d\xaf \n'                        # empty composer prompt
    printf '  ddc@host:~/wt\n'
  fi
done
'''


def _wait_until(pred, timeout=10.0, interval=0.1):
    """Poll pred() until it is truthy or the timeout elapses. Returns pred()'s
    final value so callers can assert on it. Deterministic replacement for the
    fixed sleeps that raced the stand-in under host load (SABLE-l7uv revise)."""
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


def _start_stuck_box_pane(sock, tmp_path):
    """A pane running the stuck-editable-composer stand-in, tagged
    @sable_role=optimus. Returns (rec, busy_ready, stuck_read, go_idle) marker
    paths: rec collects the line submitted once the self-heal Enter fires (empty
    pre-fix); busy_ready/stuck_read are stand-in->test signals; go_idle is the
    test->stand-in release that ends the busy phase."""
    rec = tmp_path / "rec.txt"
    busy_ready = tmp_path / "busy_ready"
    stuck_read = tmp_path / "stuck_read"
    go_idle = tmp_path / "go_idle"
    script = tmp_path / "stuck_box_tui.sh"
    script.write_text(_STUCK_BOX_TUI)
    script.chmod(0o755)
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "200", "-y", "50",
          f"REC_FILE={rec} BUSY_READY={busy_ready} STUCK_READ={stuck_read} "
          f"GO_IDLE={go_idle} bash {script}")
    time.sleep(0.4)
    _tmux(sock, "set-option", "-p", "-t", "w", "@sable_role", "optimus")
    return rec, busy_ready, stuck_read, go_idle


def test_busy_at_t0_text_stuck_in_editable_box_self_heals_and_delivers_l7uv(tmux_socket, tmp_path):
    # THE SABLE-l7uv repro, end-to-end against a REAL tmux server + real sable-msg.
    # The pane is BUSY at t0; the single busy-leg Enter is absorbed, then the line
    # sits stuck in the editable composer. The fix must resend Enter once the pane
    # is no longer working, submitting the line (REC_FILE) and reporting DELIVERED.
    # Pre-fix: rc != 0, REC_FILE empty, a durable fallback bead would be filed for a
    # message left visibly stuck. AUTO_FALLBACK=0 keeps a (pre-fix) failure from
    # writing a real inbox bead.
    #
    # Fully marker-driven (SABLE-l7uv revise — the wall-clock BUSY_SECS form flaked
    # ~2/3 under load): (A) wait for BUSY_READY so sable-msg's t0 capture lands
    # DURING the busy phase (deterministic busy-at-t0 self-heal path); (B) release
    # GO_IDLE only after STUCK_READ proves sable-msg has typed the line into the
    # busy composer, so it is guaranteed present in the editable box when the pane
    # falls idle; (C) poll REC_FILE with a budget instead of a single racing read.
    # sable-msg runs via Popen so the test can drive GO_IDLE while it polls.
    rec, busy_ready, stuck_read, go_idle = _start_stuck_box_pane(tmux_socket, tmp_path)
    assert _wait_until(busy_ready.exists, timeout=10), \
        "stand-in never signalled busy-entry"
    proc = subprocess.Popen(
        ["python3", str(BIN), "optimus", "cap in force", "--from", "lincoln"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**_env(), "SABLE_TMUX_SOCKET": tmux_socket, "SABLE_TMUX_SESSION": "w",
             "SABLE_MSG_AUTO_FALLBACK": "0", "SABLE_MSG_SUBMIT_TRIES": "60",
             "SABLE_MSG_POLL_INTERVAL": "0.25"},
    )
    try:
        assert _wait_until(stuck_read.exists, timeout=15), \
            "sable-msg never typed the line into the busy composer"
        go_idle.touch()                              # release busy->idle; line now stuck
        out, err = proc.communicate(timeout=45)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert _wait_until(lambda: rec.exists() and "cap in force" in rec.read_text(),
                       timeout=10), \
        "the stuck line must have been submitted by the self-heal Enter"
    assert proc.returncode == 0, err             # self-heal -> delivered, no fallback
    assert "delivered" in err


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


# --- duplicate bead tags after REVISE re-spawn (SABLE-qq6r) ------------------
# The live repro: a manager re-spawns a worker into the SAME worktree (REVISE
# protocol), creating a fresh pane before the old one is reaped. Both panes end
# up tagged with the same @sable_bead — sable-msg --bead must route to the
# LIVE one, not whichever the done-unaware lookup happened to pick.

def _start_worker_pane(sock, session, window_name, bead, status):
    """A bash REPL stand-in for a worker pane in its own window (so it
    coexists with other worker panes under one @sable_role=worker registry,
    matching a real fleet where duplicate-bead panes live in the same tmux
    session), tagged @sable_role=worker, @sable_bead=<bead>,
    @sable_status=<status>."""
    _tmux(sock, "new-window", "-t", session, "-n", window_name,
          "PS1='> ' bash --noprofile --norc")
    time.sleep(0.3)
    target = f"{session}:{window_name}"
    pane_id = _tmux(sock, "list-panes", "-t", target, "-F", "#{pane_id}").stdout.strip()
    _tmux(sock, "set-option", "-p", "-t", pane_id, "@sable_role", "worker")
    _tmux(sock, "set-option", "-p", "-t", pane_id, "@sable_bead", bead)
    _tmux(sock, "set-option", "-p", "-t", pane_id, "@sable_status", status)
    return pane_id


def test_bead_message_routes_to_running_pane_not_stale_done_duplicate(tmux_socket):
    # THE bead repro, live: old done-but-unreaped pane and fresh running pane
    # both tagged SABLE-pi5m. --bead delivery must land in the running one.
    _tmux(tmux_socket, "new-session", "-d", "-s", "w", "-x", "200", "-y", "50",
          "PS1='> ' bash --noprofile --norc")
    time.sleep(0.3)
    done_pane = _start_worker_pane(tmux_socket, "w", "old", "SABLE-pi5m", "done")
    running_pane = _start_worker_pane(tmux_socket, "w", "new", "SABLE-pi5m", "running")

    r = _run_msg(tmux_socket, "SABLE-pi5m", "echo BEAD-MSG-LANDED",
                 "--from", "optimus", "--bead")
    assert r.returncode == 0, r.stderr
    time.sleep(0.8)
    assert "BEAD-MSG-LANDED" in _capture(tmux_socket, running_pane)
    assert "BEAD-MSG-LANDED" not in _capture(tmux_socket, done_pane)


def test_bead_message_only_done_pane_reports_undelivered_with_reap_hint(tmux_socket):
    # Only a done-but-unreaped pane matches — must fail loudly with a reap
    # hint, never silently deliver into the dead composer and report success.
    _tmux(tmux_socket, "new-session", "-d", "-s", "w", "-x", "200", "-y", "50",
          "PS1='> ' bash --noprofile --norc")
    time.sleep(0.3)
    done_pane = _start_worker_pane(tmux_socket, "w", "old", "SABLE-ghost", "done")

    r = _run_msg(tmux_socket, "SABLE-ghost", "echo SHOULD-NOT-LAND",
                 "--from", "optimus", "--bead")
    assert r.returncode != 0
    assert "done" in r.stderr
    assert "reap" in r.stderr.lower()
    time.sleep(0.5)
    assert "SHOULD-NOT-LAND" not in _capture(tmux_socket, done_pane)


# --- poisoned identity tag: env is the authority, not @sable_role (SABLE-to8m) -
# The 2026-07-07 incident: a stale/corrupted @sable_role=lincoln tag on an
# unrelated WORKER pane sank two manager escalations into it (a fake-lincoln
# sink). @sable_role is mutable global tmux state any process can overwrite; the
# authority is the CLAUDE_AGENT_NAME of the process actually running in the pane.
# sable-msg now cross-checks the recipient's process identity before delivering,
# so a poisoned tag can no longer receive traffic addressed to the role it forges.

def _start_pane_with_identity(sock, identity, role_tag):
    """A bash REPL stand-in whose PANE PROCESS carries CLAUDE_AGENT_NAME=identity
    (the authority, stamped via tmux -e exactly as the real spawn tooling does),
    then tagged @sable_role=role_tag — which may DISAGREE with identity to model
    a poisoned tag."""
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "200", "-y", "50",
          "-e", f"CLAUDE_AGENT_NAME={identity}",
          "PS1='> ' bash --noprofile --norc")
    time.sleep(0.5)
    _tmux(sock, "set-option", "-p", "-t", "w", "@sable_role", role_tag)
    return "w"


def test_poisoned_lincoln_tag_on_worker_pane_refuses_delivery(tmux_socket):
    # THE bead repro, live: a worker pane (its real process identity is its
    # manager lane, 'optimus') is poisoned with @sable_role=lincoln. A
    # lincoln-addressed message must NOT deliver into it — the process-identity
    # cross-check catches env('optimus') != role('lincoln') and refuses.
    # AUTO_FALLBACK=0 keeps the refusal from filing a real inbox bead (and keeps
    # the case bd/dolt-free for the ci-verify clean room).
    _start_pane_with_identity(tmux_socket, identity="optimus", role_tag="lincoln")
    r = subprocess.run(
        ["python3", str(BIN), "lincoln", "echo POISON-SHOULD-NOT-LAND",
         "--from", "optimus"],
        capture_output=True, text=True,
        env={**_env(), "SABLE_TMUX_SOCKET": tmux_socket, "SABLE_TMUX_SESSION": "w",
             "SABLE_MSG_AUTO_FALLBACK": "0"},
    )
    assert r.returncode != 0, "a poisoned lincoln tag must not receive lincoln traffic"
    assert "poisoned" in r.stderr.lower()
    assert "optimus" in r.stderr          # names the pane's real identity
    time.sleep(0.5)
    pane = _capture(tmux_socket, "w")
    assert "POISON-SHOULD-NOT-LAND" not in pane


def test_agreeing_identity_still_delivers(tmux_socket):
    # The cross-check must not break legitimate sends: a pane whose process
    # identity AGREES with its role tag (both 'optimus') delivers normally.
    _start_pane_with_identity(tmux_socket, identity="optimus", role_tag="optimus")
    r = _run_msg(tmux_socket, "optimus",
                 "echo IDENTITY-AGREES-DELIVERED", "--from", "lincoln")
    assert r.returncode == 0, r.stderr
    time.sleep(0.8)
    pane = _capture(tmux_socket, "w")
    assert "⟦SABLE-MSG⟧ from=lincoln to=optimus" in pane
    assert "IDENTITY-AGREES-DELIVERED" in pane


def test_untagged_process_identity_falls_open_to_tag(tmux_socket):
    # A pane SABLE did not spawn (no CLAUDE_AGENT_NAME in its process env) has no
    # authority to contradict its tag, so the pre-authority tag-only behavior is
    # preserved: a tarzan-tagged bare shell still receives tarzan traffic.
    _start_pane(tmux_socket)  # bash, no -e identity
    _tmux(tmux_socket, "set-option", "-p", "-t", "w", "@sable_role", "tarzan")
    r = _run_msg(tmux_socket, "tarzan",
                 "echo NO-IDENTITY-FALLS-OPEN", "--from", "lincoln")
    assert r.returncode == 0, r.stderr
    time.sleep(0.8)
    assert "NO-IDENTITY-FALLS-OPEN" in _capture(tmux_socket, "w")


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
