#!/usr/bin/env python3
"""Integration tests for bin/sable-msg against a REAL tmux server.

Uses an isolated tmux socket (-L) so it never touches the operator's session.
Default sends prove queue-first publication and zero arbitrary-body pane writes
across idle, busy, queued-footer, and stuck-composer postures. Explicit
``--interrupt`` cases retain real-keystroke authority for the sole direct-body
override, including role/bead/session routing and verified submission.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

import sable_inbox_lib as inbox_lib
import sable_pane_lib as pane_lib

BIN = Path(__file__).resolve().parent / "sable-msg"
SEAT_GATE_HOOK = Path(__file__).resolve().parent.parent / "hooks" / "multi-manager" / "seat-sighting-gate.sh"
HAVE_TMUX = shutil.which("tmux") is not None
pytestmark = pytest.mark.skipif(not HAVE_TMUX, reason="tmux not installed")


@pytest.fixture()
def tmux_socket():
    sock = f"sable-it-{uuid.uuid4().hex[:8]}"
    yield sock
    subprocess.run(["tmux", "-L", sock, "kill-server"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture(autouse=True)
def isolated_inbox(monkeypatch, tmp_path):
    """Every real sable-msg subprocess uses a per-test queue/heartbeat root."""

    monkeypatch.setenv("SABLE_TEST", "1")
    monkeypatch.setenv("SABLE_TEST_INBOX_ROOT", str(tmp_path / "inbox"))


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


def _wait_until(pred, timeout=10.0, interval=0.1):
    """Return when a real observable boundary is satisfied, or at timeout."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


def _require_until(pred, *, timeout=3.0, interval=0.02, description="condition"):
    if not _wait_until(pred, timeout=timeout, interval=interval):
        raise AssertionError(f"timed out waiting for {description}")


def _tmux(sock, *args, check=True):
    # tmux's structural commands are synchronous. The test that causes
    # asynchronous pane output waits on its own semantic marker.
    return subprocess.run(["tmux", "-L", sock, *args],
                          capture_output=True, text=True, check=check,
                          env=_server_env())


def _capture(sock, target):
    return _tmux(sock, "capture-pane", "-t", target, "-p").stdout


def _prepare_bash_interrupt(sock, target):
    """Make a bash stand-in model the TUI's non-destructive bare Escape."""

    marker = f"SABLE-ESCAPE-READY-{uuid.uuid4().hex[:8]}"
    command = (
        '''bind '"\\e": redraw-current-line'; '''
        f'''bind 'set keyseq-timeout 1'; echo {marker}'''
    )
    _tmux(sock, "send-keys", "-t", target, command, "Enter")
    _require_until(
        lambda: marker in _capture(sock, target),
        description=f"bash interrupt binding in {target}",
    )


def _read_recording(path):
    """Defensive read for REC_FILE/ARRIVALS_FILE fixtures. Some stand-ins
    (_BUSY_TUI, _STUCK_BOX_TUI) read the pty a character at a time, which can
    split the multi-byte \\xe2\\x9f\\xa6 (⟦) SABLE-MSG framing glyph under host
    load and leave bare continuation bytes behind (SABLE-wcbj; root-cause
    conversion of those stand-ins tracked separately as SABLE-qby7). These
    tests assert on message content, not encoding, so a strict-UTF-8 read
    must not be able to crash them (SABLE-9qzfg)."""
    return path.read_text(errors="replace")


def _start_pane(sock):
    # A bash REPL stand-in for an agent pane; tag it with @sable_role=optimus.
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "200", "-y", "50",
          "PS1='> ' bash --noprofile --norc")
    _tmux(sock, "set-option", "-p", "-t", "w", "@sable_role", "optimus")
    _prepare_bash_interrupt(sock, "w")
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
    env = dict(os.environ)
    # These subprocess-driven sends must exercise CWD-derivation deterministically,
    # not whichever real pane happens to be running pytest (SABLE-ssd8: pytest's
    # ambient $TMUX_PANE could coincidentally collide with a pane id on the
    # freshly created isolated socket below, since ids restart from %0 per server).
    env.pop("TMUX_PANE", None)
    env.pop("TMUX", None)
    # SABLE-qqcd: this suite may itself run inside a real SABLE worker pane
    # (SABLE_WORKER_PANE=1, CLAUDE_AGENT_NAME=<lane>) -- exactly the ambient
    # state resolve_from()'s default now keys on. Leaking it in here would make
    # every --from-less send in this file silently resolve as a worker instead
    # of hermetically testing what each fixture sets up.
    env.pop("SABLE_WORKER_PANE", None)
    env.pop("SABLE_BEAD", None)
    # No integration failure may manufacture coordination work in the live
    # tracker. Tests that exercise the fallback do so in an explicit scratch
    # BEADS_DB in the shell suite.
    env["SABLE_MSG_AUTO_FALLBACK"] = "0"
    # Ordinary routing/identity fixtures use already-idle deterministic
    # stand-ins. The dedicated busy-interrupt cases override this with a real
    # wait budget; skipping it here avoids paying 25 seconds per direct test.
    env["SABLE_MSG_READY_TIMEOUT"] = "0"
    return env


def test_file_sighting_priorities_reach_bd_and_remain_provisional(tmp_path):
    """P1 and default P2 survive the real CLI event, bd, ordering, and hook.

    This is deliberately integration-tier coverage: initializing Dolt and
    passing the CLI's actual top-level command/stdout/stderr to the real
    PostToolUse hook prevents a fabricated child `bd create` event from
    masking a wrapper/hook contract gap. The hook's follow-up write sampled
    above the ordinary 10-second test budget (SABLE-kdn3y).
    """
    if not SEAT_GATE_HOOK.is_file():
        pytest.skip(f"seat-sighting-gate.sh not found at {SEAT_GATE_HOOK}")
    if shutil.which("bd") is None:
        pytest.skip("bd not on PATH")
    beads_root = tmp_path / "beads"
    beads_root.mkdir()
    init = subprocess.run(
        ["bd", "init", "--prefix=sga"],
        cwd=str(beads_root),
        env={**os.environ, "BD_NON_INTERACTIVE": "1"},
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert init.returncode == 0, init.stdout + init.stderr
    beads_db = str(beads_root / ".beads")

    bd_env = {
        **os.environ,
        "BEADS_DB": beads_db,
        "BD_NON_INTERACTIVE": "1",
    }

    def file_and_annotate(body, priority=None):
        expected_priority = 2 if priority is None else priority
        cli = [
            "python3",
            str(BIN),
            "--file-sighting",
            "--from",
            "chuck",
        ]
        if priority is not None:
            cli.append(f"--priority={priority}")
        cli.append(body)
        created = subprocess.run(
            cli,
            env=bd_env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert created.returncode == 0, created.stdout + created.stderr
        assert f"priority P{expected_priority}" in created.stderr
        bead_id = re.search(r"sable-msg: filed\s+(\S+)", created.stderr).group(1)

        hook_input = json.dumps(
            {
                "tool_input": {
                    "command": shlex.join(cli)
                },
                "tool_response": {
                    "stdout": created.stdout,
                    "stderr": created.stderr,
                },
            }
        )
        annotated = subprocess.run(
            ["bash", str(SEAT_GATE_HOOK)],
            input=hook_input,
            text=True,
            capture_output=True,
            env={
                **bd_env,
                "CLAUDE_AGENT_NAME": "chuck",
                "CLAUDE_AGENT_ROLE": "manager",
            },
            timeout=10,
        )
        assert annotated.returncode == 0
        return bead_id

    # Create P2 first so chronological ordering cannot accidentally satisfy
    # the later priority-order assertion.
    p2_id = file_and_annotate("default seat observation")
    p1_id = file_and_annotate("urgent seat observation", priority=1)

    listed = subprocess.run(
        ["bd", "list", "--all", "--limit=0", "--sort=priority", "--json"],
        env=bd_env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert listed.returncode == 0, listed.stdout + listed.stderr
    records = json.loads(listed.stdout)
    by_id = {record["id"]: record for record in records}
    assert by_id[p1_id]["priority"] == 1
    assert by_id[p2_id]["priority"] == 2
    ranked_ids = [record["id"] for record in records]
    assert ranked_ids.index(p1_id) < ranked_ids.index(p2_id)

    for bead_id in (p1_id, p2_id):
        show = subprocess.run(
            ["bd", "show", bead_id, "--json"],
            env=bd_env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert show.returncode == 0, show.stdout + show.stderr
        data = json.loads(show.stdout)
        data = data[0] if isinstance(data, list) else data
        assert "seat-filed" in (data.get("labels") or [])
        assert (data.get("metadata") or {}).get("priority_provisional") in (
            True,
            "true",
            "True",
        )


def test_read_recording_survives_non_utf8_delimiter_bytes(tmp_path):
    # SABLE-9qzfg: reproduces the observed corruption directly, no tmux needed.
    # A char-at-a-time stand-in read can drop the leading 0xe2 of the
    # \xe2\x9f\xa6 (⟦) SABLE-MSG framing glyph, leaving bare continuation
    # bytes 0x9f 0xa6 that strict-UTF-8 read_text() cannot decode. The tests
    # in this file only assert on message content, so that must not raise.
    path = tmp_path / "rec.txt"
    path.write_bytes(b"\x9f\xa6 from=lincoln to=optimus :: cap in force\n")
    assert "cap in force" in _read_recording(path)


def test_message_delivered_to_registered_pane(tmux_socket):
    _start_pane(tmux_socket)
    r = _run_msg(tmux_socket, "optimus",
                 "echo SABLE-MSG-DELIVERED", "--from", "lincoln", "--interrupt")
    assert r.returncode == 0, r.stderr
    _require_until(
        lambda: "SABLE-MSG-DELIVERED" in _capture(tmux_socket, "w"),
        description="registered-pane delivery",
    )
    pane = _capture(tmux_socket, "w")
    # the header is injected verbatim and the body executed by the REPL
    assert "⟦SABLE-MSG⟧ from=lincoln to=optimus" in pane
    assert "SABLE-MSG-DELIVERED" in pane


def test_submit_gap_is_present_against_real_tmux(tmux_socket):
    """Measure the paste/Enter gap across real tmux invocations.

    The recipient is intentionally the suite's bash stand-in, which has no
    paste-burst detector and therefore CANNOT reproduce Codex swallowing the
    Enter. A green result proves only that deliver_text preserves the measured
    gap while driving a real tmux server. Faithful symptom coverage would need
    a real Codex pane, which this repository's test suite does not spawn.
    """
    _start_pane(tmux_socket)
    _require_until(
        lambda: ">" in _capture(tmux_socket, "w"),
        description="bash stand-in prompt",
    )
    base = ["tmux", "-L", tmux_socket]
    text = "echo SUBMIT-GAP-LANDED"
    send_times = []

    def timed_run(cmd):
        if "paste-buffer" in cmd or "send-keys" in cmd:
            send_times.append(time.monotonic())
        return subprocess.run(cmd, capture_output=True, text=True).returncode == 0

    assert pane_lib.deliver_text(
        base,
        "w",
        text,
        text,
        tries=2,
        interval=0.01,
        run=timed_run,
        capture=lambda: pane_lib.capture_pane(base, "w"),
    )
    assert len(send_times) == 2
    assert send_times[1] - send_times[0] >= pane_lib.SUBMIT_GAP_SECONDS


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
    _tmux(tmux_socket, "set-option", "-p", "-t", "w", "@sable_role", "optimus")
    _prepare_bash_interrupt(tmux_socket, "w")
    body = "; echo WRAP-$((40+2))-VERIFIED end of a long directive body padding"
    r = _run_msg(
        tmux_socket, "optimus", body, "--from", "lincoln", "--interrupt"
    )
    assert r.returncode == 0, r.stderr
    _require_until(
        lambda: "WRAP-42-VERIFIED" in _capture(tmux_socket, "w"),
        description="wrapped narrow-pane delivery",
    )
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
    inputrc.write_text(
        '"\\e": redraw-current-line\n'
        "set keyseq-timeout 1\n"
    )
    _tmux(tmux_socket, "new-session", "-d", "-s", "w", "-x", "200", "-y", "50",
          f"INPUTRC={inputrc} PS1='> ' bash --noprofile --norc")
    _tmux(tmux_socket, "set-option", "-p", "-t", "w", "@sable_role", "optimus")

    r = subprocess.run(
        ["python3", str(BIN), "optimus", "; echo INTERRUPT-LANDED",
         "--from", "lincoln", "--interrupt"],
        capture_output=True, text=True,
        env={**_env(), "SABLE_TMUX_SOCKET": tmux_socket, "SABLE_TMUX_SESSION": "w",
             "SABLE_MSG_SUBMIT_TRIES": "1", "SABLE_MSG_AUTO_FALLBACK": "0",
             "SABLE_MSG_POLL_INTERVAL": "0.2"},
    )
    assert r.returncode == 0, r.stderr          # delivered, verified, first attempt
    _require_until(
        lambda: "INTERRUPT-LANDED" in _capture(tmux_socket, "w"),
        description="interrupt delivery",
    )
    pane = _capture(tmux_socket, "w")
    assert "⟦SABLE-MSG⟧ from=lincoln to=optimus" in pane   # framed turn landed intact
    assert "INTERRUPT-LANDED" in pane                        # and executed (turn submitted)


def test_default_send_to_busy_pane_queues_without_typing_body(tmux_socket):
    # SABLE-1el7e: the default path publishes the original body before a wake
    # attempt and never lets arbitrary text enter a busy composer. With no live
    # drainer heartbeat this reports deferred surfacing, but the enqueue itself
    # is the successful durable delivery state.
    _start_pane(tmux_socket)
    _tmux(tmux_socket, "send-keys", "-t", "w",
          "echo BUSY-START; sleep 3; echo BUSY-END", "Enter")
    _require_until(
        lambda: "BUSY-START" in _capture(tmux_socket, "w"),
        description="busy-pane turn start",
    )
    r = subprocess.run(
        ["python3", str(BIN), "optimus", "echo QUEUED-RAN", "--from", "lincoln"],
        capture_output=True, text=True,
        env={**_env(), "SABLE_TMUX_SOCKET": tmux_socket, "SABLE_TMUX_SESSION": "w",
             "SABLE_MSG_AUTO_FALLBACK": "0", "SABLE_MSG_SUBMIT_TRIES": "2",
             "SABLE_MSG_POLL_INTERVAL": "0.2"},
    )
    assert r.returncode == 0
    assert "QUEUED-WAKE-DEFERRED" in r.stderr
    assert [message.body for message in inbox_lib.read("optimus")] == [
        "echo QUEUED-RAN"
    ]
    assert "echo QUEUED-RAN" not in _capture(tmux_socket, "w")


# --- mid-turn busy pane: interrupt lands, default queues without typing -----
# The live failure: --interrupt into a manager pane actively mid-turn (xhigh
# thinking, tools running) dropped the message on all 8 submit attempts, because
# the pane STILL shows the empty composer prompt during a turn — pane_ready fired
# early and the message was typed into a pane still redrawing the interrupted
# turn. The stand-in below models exactly that shape.

# A Claude-TUI-mid-turn stand-in. A long-running redraw loop paints a composer
# prompt line (so pane_ready is True — the early-fire trap) AND an
# "esc to interrupt" status line (so pane_busy is True, pane_idle False). A bare
# Escape INTERRUPTS (records INTERRUPTED, settles to an idle REPL); without one,
# the turn ends on its own after BUSY_SECS (records NATURAL). Non-Escape input
# is held and replayed as one submitted turn once the turn ends. Default sends
# must never exercise that input path; the explicit --interrupt control does.
# Every submitted turn is appended to REC_FILE.
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
    pane = _capture(tmux_socket, "w")
    # composed=<ts> (SABLE-xwy0b) trails the body in a bracket, so the
    # pre-xwy0b literal header+body text is still a straight substring.
    assert "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force" in pane  # landed intact
    assert "cap in force" in _read_recording(rec)    # and was SUBMITTED as a turn


# A marker-driven variant of _BUSY_TUI, identical except the busy->idle
# transition is TEST-CONTROLLED via markers instead of a wall-clock BUSY_SECS
# window (SABLE-um6i: the wall-clock window raced under host load exactly like
# the l7uv/msxj/wcbj class — stand-in startup + tmux setup could eat the whole
# window before sable-msg's first probe, making the pane read IDLE and the
# send verify as landed, when the test's premise requires it be BUSY at t0).
# Signals busy-entry (BUSY_READY, before the first read) so the test can gate
# t0 capture to the busy phase, and stays busy until the test releases it
# (GO_IDLE), recording NATURAL exactly as the wall-clock timeout did. The
# per-character read loop (and its bare-Escape interrupt detection) is left
# untouched — this stand-in is a separate script from _BUSY_TUI, so L211's
# loop in _BUSY_TUI itself still stands, unmodified, for SABLE-qby7 to convert.
_BUSY_MARKER_TUI = r'''#!/usr/bin/env bash
queued=""
busy=1
: > "$BUSY_READY"                                  # signal busy-entry BEFORE first read
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


def _start_busy_pane_markers(sock, tmp_path):
    """Marker-driven variant of _start_busy_pane (SABLE-um6i). Returns
    (rec, end, busy_ready, go_idle): rec/end as before; busy_ready signals
    busy-entry before the first read (so a caller can gate t0 capture to the
    busy phase); go_idle is the test->stand-in release that ends the busy
    phase, replacing the wall-clock BUSY_SECS window that raced under load."""
    rec = tmp_path / "rec.txt"
    end = tmp_path / "end.txt"
    busy_ready = tmp_path / "busy_ready"
    go_idle = tmp_path / "go_idle"
    script = tmp_path / "busy_marker_tui.sh"
    script.write_text(_BUSY_MARKER_TUI)
    script.chmod(0o755)
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "200", "-y", "50",
          f"REC_FILE={rec} END_FILE={end} BUSY_READY={busy_ready} "
          f"GO_IDLE={go_idle} bash {script}")
    _tmux(sock, "set-option", "-p", "-t", "w", "@sable_role", "optimus")
    return rec, end, busy_ready, go_idle


def test_default_send_to_busy_turn_does_not_interrupt_or_type_payload(
    tmux_socket, tmp_path,
):
    # The marker-driven companion proves the absence against real tmux: no
    # Escape, no payload bytes, and the original turn reaches its natural end.
    rec, end, busy_ready, go_idle = _start_busy_pane_markers(tmux_socket, tmp_path)
    assert _wait_until(busy_ready.exists, timeout=10), \
        "stand-in never signalled busy-entry"
    r = subprocess.run(
        ["python3", str(BIN), "optimus", "queued directive", "--from", "lincoln"],
        capture_output=True, text=True,
        env={**_env(), "SABLE_TMUX_SOCKET": tmux_socket, "SABLE_TMUX_SESSION": "w",
             "SABLE_MSG_SUBMIT_TRIES": "2", "SABLE_MSG_AUTO_FALLBACK": "0",
             "SABLE_MSG_POLL_INTERVAL": "0.2"},
    )
    assert r.returncode == 0
    assert "QUEUED-WAKE-DEFERRED" in r.stderr
    assert not rec.exists(), "default send must not type into the busy pane"
    assert [message.body for message in inbox_lib.read("optimus")] == [
        "queued directive"
    ]
    go_idle.touch()
    assert _wait_until(lambda: end.exists() and end.read_text().strip() == "NATURAL",
                       timeout=10), \
        "busy turn never reached its (test-controlled) natural end"
    assert not rec.exists(), "queued payload must stay out of the pane after it idles"


def test_fresh_drainer_makes_busy_default_queue_a_success_without_body_injection(
    tmux_socket, tmp_path,
):
    # Heartbeat semantics stay unchanged under queue-first: a complete recent
    # host sweep is sufficient evidence for rc0 even when this immediate wake
    # defers. It is never permission to type the payload into the pane.
    rec, end, busy_ready, go_idle = _start_busy_pane_markers(
        tmux_socket, tmp_path
    )
    assert _wait_until(busy_ready.exists, timeout=10)
    inbox_lib.record_drain_heartbeat(interval_seconds=60)
    r = subprocess.run(
        ["python3", str(BIN), "optimus", "cap in force", "--from", "lincoln"],
        capture_output=True, text=True,
        env={**_env(), "SABLE_TMUX_SOCKET": tmux_socket, "SABLE_TMUX_SESSION": "w",
             "SABLE_MSG_AUTO_FALLBACK": "0"},
    )
    assert r.returncode == 0, r.stderr
    assert "queued" in r.stderr
    assert "drainer heartbeat is fresh" in r.stderr
    assert not rec.exists()
    assert [message.body for message in inbox_lib.read("optimus")] == [
        "cap in force"
    ]
    go_idle.touch()
    assert _wait_until(
        lambda: end.exists() and end.read_text().strip() == "NATURAL",
        timeout=10,
    )
    assert not rec.exists()


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
    _tmux(sock, "set-option", "-p", "-t", "w", "@sable_role", "optimus")
    return rec, end, arrivals, busy_ready, go_idle


def test_default_send_never_appends_payload_to_existing_queued_composer(
    tmux_socket, tmp_path,
):
    # The exact old msxj posture is now a negative transport control: even a
    # composer already carrying a queued line must receive no payload bytes.
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
    assert r.returncode == 0
    assert "QUEUED-WAKE-DEFERRED" in r.stderr
    assert not end.exists(), "the turn must still be running (never reached NATURAL end)"
    assert not arrivals.exists()
    assert [message.body for message in inbox_lib.read("optimus")] == [
        "cap in force"
    ]


def test_two_default_sends_are_fifo_payloads_and_zero_composer_writes(
    tmux_socket, tmp_path,
):
    # Poke idempotency does not mean payload deduplication: two explicit sends
    # are two FIFO records. Neither record may be typed into the busy pane.
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
    assert r1.returncode == 0
    r2 = subprocess.run(
        ["python3", str(BIN), "optimus", "cap in force", "--from", "lincoln"], **kwargs)
    assert r2.returncode == 0
    assert [message.body for message in inbox_lib.read("optimus")] == [
        "cap in force",
        "cap in force",
    ]
    assert not arrivals.exists()

    go_idle.touch()  # release the busy turn now that both sends are confirmed queued
    assert _wait_until(lambda: end.exists() and end.read_text().strip() == "NATURAL",
                       timeout=10), \
        "busy turn never reached its (test-controlled) natural end"
    assert not rec.exists()


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
while [ -z "$line" ]; do
  IFS= read -rsN1 first || exit 0
  if [ "$first" = $'\033' ]; then
    continue                           # --interrupt Escape clears nothing here
  fi
  IFS= read -r rest
  line="${first}${rest}"
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
    _tmux(tmux_socket, "set-option", "-p", "-t", "w", "@sable_role", "optimus")

    r = subprocess.run(
        ["python3", str(BIN), "optimus", "GO push your worktree branch now",
         "--from", "lincoln", "--interrupt"],
        capture_output=True, text=True,
        env={**_env(), "SABLE_TMUX_SOCKET": tmux_socket, "SABLE_TMUX_SESSION": "w",
             "SABLE_MSG_AUTO_FALLBACK": "0", "SABLE_MSG_SUBMIT_TRIES": "5",
             "SABLE_MSG_POLL_INTERVAL": "0.2"},
    )
    assert "GO push your worktree branch now" in _read_recording(rec), \
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
    _tmux(sock, "set-option", "-p", "-t", "w", "@sable_role", "optimus")
    return rec, busy_ready, stuck_read, go_idle


def test_default_queue_cannot_create_the_l7uv_stuck_body_posture(
    tmux_socket, tmp_path,
):
    # The legacy self-heal remains covered at the pane-library layer, but the
    # default sender must now make this posture unreachable: it never types the
    # arbitrary body, so there is nothing for a redraw to strand in the editor.
    rec, busy_ready, stuck_read, go_idle = _start_stuck_box_pane(tmux_socket, tmp_path)
    assert _wait_until(busy_ready.exists, timeout=10), \
        "stand-in never signalled busy-entry"
    result = subprocess.run(
        ["python3", str(BIN), "optimus", "cap in force", "--from", "lincoln"],
        capture_output=True,
        text=True,
        env={**_env(), "SABLE_TMUX_SOCKET": tmux_socket, "SABLE_TMUX_SESSION": "w",
             "SABLE_MSG_AUTO_FALLBACK": "0"},
    )
    assert result.returncode == 0
    assert "QUEUED-WAKE-DEFERRED" in result.stderr
    assert not stuck_read.exists()
    assert [message.body for message in inbox_lib.read("optimus")] == [
        "cap in force"
    ]
    go_idle.touch()
    time.sleep(0.1)
    assert not rec.exists()


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
    _tmux(sock, "set-option", "-t", sess, "@sable_repo", root)
    _tmux(sock, "set-option", "-p", "-t", sess, "@sable_role", role)
    _tmux(sock, "set-option", "-p", "-t", sess, "@sable_repo", root)
    _prepare_bash_interrupt(sock, sess)
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
                      "echo ALPHA-ONLY", "--from", "lincoln", "--interrupt")
    assert r.returncode == 0, r.stderr
    _require_until(
        lambda: "ALPHA-ONLY" in _capture(tmux_socket, sess_a),
        description="repo-scoped delivery",
    )
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
    all_panes = _tmux(tmux_socket, "list-panes", "-t", sess_a,
                      "-F", "#{pane_id}").stdout.split()
    worker_pane = next(p for p in all_panes if p != tarzan_pane)
    _tmux(tmux_socket, "set-option", "-p", "-t", worker_pane, "@sable_role", "worker")

    # send FROM the worker pane itself via send-keys, so $TMUX_PANE is real
    # (set by tmux for that pane's own bash, not injected by the test) even
    # though the shell's CWD is beta, not alpha.
    body = "; echo CROSS-REPO-DELIVERED"
    cmd = (f'unset SABLE_TMUX_SESSION; SABLE_TMUX_SOCKET={tmux_socket} '
          f'python3 {BIN} tarzan "{body}" --from worker --interrupt')
    _tmux(tmux_socket, "send-keys", "-t", worker_pane, cmd, "Enter")
    _require_until(
        lambda: "CROSS-REPO-DELIVERED" in _capture(tmux_socket, sess_a),
        timeout=5,
        description="cross-repo pane-session delivery",
    )
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
    target = f"{session}:{window_name}"
    pane_id = _tmux(sock, "list-panes", "-t", target, "-F", "#{pane_id}").stdout.strip()
    _tmux(sock, "set-option", "-p", "-t", pane_id, "@sable_role", "worker")
    _tmux(sock, "set-option", "-p", "-t", pane_id, "@sable_bead", bead)
    _tmux(sock, "set-option", "-p", "-t", pane_id, "@sable_status", status)
    _prepare_bash_interrupt(sock, pane_id)
    return pane_id


def test_bead_message_routes_to_running_pane_not_stale_done_duplicate(tmux_socket):
    # THE bead repro, live: old done-but-unreaped pane and fresh running pane
    # both tagged SABLE-pi5m. --bead delivery must land in the running one.
    _tmux(tmux_socket, "new-session", "-d", "-s", "w", "-x", "200", "-y", "50",
          "PS1='> ' bash --noprofile --norc")
    done_pane = _start_worker_pane(tmux_socket, "w", "old", "SABLE-pi5m", "done")
    running_pane = _start_worker_pane(tmux_socket, "w", "new", "SABLE-pi5m", "running")

    r = _run_msg(tmux_socket, "SABLE-pi5m", "echo BEAD-MSG-LANDED",
                 "--from", "optimus", "--bead", "--interrupt")
    assert r.returncode == 0, r.stderr
    _require_until(
        lambda: "BEAD-MSG-LANDED" in _capture(tmux_socket, running_pane),
        description="running duplicate-bead pane delivery",
    )
    assert "BEAD-MSG-LANDED" in _capture(tmux_socket, running_pane)
    assert "BEAD-MSG-LANDED" not in _capture(tmux_socket, done_pane)


def test_bead_message_only_done_pane_reports_undelivered_with_reap_hint(tmux_socket):
    # Only a done-but-unreaped pane matches — must fail loudly with a reap
    # hint, never silently deliver into the dead composer and report success.
    _tmux(tmux_socket, "new-session", "-d", "-s", "w", "-x", "200", "-y", "50",
          "PS1='> ' bash --noprofile --norc")
    done_pane = _start_worker_pane(tmux_socket, "w", "old", "SABLE-ghost", "done")

    r = _run_msg(tmux_socket, "SABLE-ghost", "echo SHOULD-NOT-LAND",
                 "--from", "optimus", "--bead")
    assert r.returncode != 0
    assert "done" in r.stderr
    assert "reap" in r.stderr.lower()
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
    _tmux(sock, "set-option", "-p", "-t", "w", "@sable_role", role_tag)
    _prepare_bash_interrupt(sock, "w")
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
    pane = _capture(tmux_socket, "w")
    assert "POISON-SHOULD-NOT-LAND" not in pane


def test_agreeing_identity_still_delivers(tmux_socket):
    # The cross-check must not break legitimate sends: a pane whose process
    # identity AGREES with its role tag (both 'optimus') delivers normally.
    _start_pane_with_identity(tmux_socket, identity="optimus", role_tag="optimus")
    r = _run_msg(tmux_socket, "optimus",
                 "echo IDENTITY-AGREES-DELIVERED", "--from", "lincoln", "--interrupt")
    assert r.returncode == 0, r.stderr
    _require_until(
        lambda: "IDENTITY-AGREES-DELIVERED" in _capture(tmux_socket, "w"),
        description="identity-confirmed delivery",
    )
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
                 "echo NO-IDENTITY-FALLS-OPEN", "--from", "lincoln", "--interrupt")
    assert r.returncode == 0, r.stderr
    _require_until(
        lambda: "NO-IDENTITY-FALLS-OPEN" in _capture(tmux_socket, "w"),
        description="untagged-identity delivery",
    )
    assert "NO-IDENTITY-FALLS-OPEN" in _capture(tmux_socket, "w")


# --- worker sends must not wear the lane manager's identity (SABLE-qqcd) ----
# sable-spawn-worker:696-697 stamps EVERY worker pane with CLAUDE_AGENT_NAME=
# <lane manager> (deliberate, for push-attribution, SABLE-bldh.13) AND
# SABLE_WORKER_PANE=1 (always, the SABLE-38zi disambiguator). Before this fix,
# sable-msg's --from default consulted only CLAUDE_AGENT_NAME, so a worker's own
# report was framed 'from=<manager>' -- indistinguishable from a real directive
# from that manager. This drives the REAL resolve_from() path end-to-end
# against a real tmux pane carrying the real @sable_bead tag (not a
# reimplementation, per SABLE-f00o).

def test_worker_pane_send_frames_as_worker_not_manager_lane_qqcd(tmux_socket):
    # Manager pane (recipient), tagged @sable_role=tarzan -- a bash REPL
    # stand-in with no CLAUDE_AGENT_NAME of its own, so the SABLE-to8m identity
    # cross-check falls open and does not interfere with this test.
    _tmux(tmux_socket, "new-session", "-d", "-s", "w", "-x", "200", "-y", "50",
          "PS1='> ' bash --noprofile --norc")
    _tmux(tmux_socket, "set-option", "-p", "-t", "w", "@sable_role", "tarzan")
    _prepare_bash_interrupt(tmux_socket, "w")
    # Pin the manager pane's own id NOW -- _start_worker_pane below creates a
    # second window that becomes the session's active one, so capturing by the
    # bare session name "w" afterwards would capture the WRONG (worker) pane.
    tarzan_pane = _tmux(tmux_socket, "list-panes", "-t", "w", "-F", "#{pane_id}").stdout.strip()

    # A worker pane exactly as sable-spawn-worker tags it: @sable_role=worker,
    # @sable_bead=<the dispatched bead>.
    worker_pane = _start_worker_pane(tmux_socket, "w", "worker1", "SABLE-i8kv", "running")

    # Simulate the worker's OWN sable-msg send: it runs inside worker_pane, so
    # in real life TMUX_PANE is that pane's id in its process environment
    # (ambient tmux behavior) alongside the CLAUDE_AGENT_NAME=tarzan +
    # SABLE_WORKER_PANE=1 sable-spawn-worker:696-697 actually stamps there.
    r = subprocess.run(
        ["python3", str(BIN), "tarzan", "status: pushed and green", "--interrupt"],
        capture_output=True, text=True,
        env={**_env(), "SABLE_TMUX_SOCKET": tmux_socket, "SABLE_TMUX_SESSION": "w",
             "CLAUDE_AGENT_NAME": "tarzan", "SABLE_WORKER_PANE": "1",
             "TMUX_PANE": worker_pane},
    )
    assert r.returncode == 0, r.stderr
    _require_until(
        lambda: "from=worker:SABLE-i8kv to=tarzan" in _capture(
            tmux_socket, tarzan_pane
        ),
        description="worker-framed delivery",
    )
    pane = _capture(tmux_socket, tarzan_pane)
    assert "⟦SABLE-MSG⟧ from=worker:SABLE-i8kv to=tarzan" in pane
    assert "from=tarzan to=tarzan" not in pane

    # Same run, a REAL manager pane's own send (CLAUDE_AGENT_NAME=tarzan, no
    # SABLE_WORKER_PANE) must still read from=<manager> -- the regression guard.
    r2 = subprocess.run(
        ["python3", str(BIN), "tarzan", "manager directive: hold pushes", "--interrupt"],
        capture_output=True, text=True,
        env={**_env(), "SABLE_TMUX_SOCKET": tmux_socket, "SABLE_TMUX_SESSION": "w",
             "CLAUDE_AGENT_NAME": "tarzan"},
    )
    assert r2.returncode == 0, r2.stderr
    _require_until(
        lambda: "from=tarzan to=tarzan" in _capture(tmux_socket, tarzan_pane),
        description="manager-framed delivery",
    )
    pane2 = _capture(tmux_socket, tarzan_pane)
    assert "⟦SABLE-MSG⟧ from=tarzan to=tarzan" in pane2


# --- SABLE-tmbx1: caller-shell command-substitution hazard ------------------
# HIT LIVE 2026-07-21: a message body composed inside a double-quoted shell
# argument had its backticks/$(...) command-substituted by the CALLING shell
# before sable-msg's argv was ever populated -- the substituted command
# actually RAN, and the delivered text silently lost those passages while
# sable-msg still reported success. sable-msg cannot detect this: by the time
# main() sees the body, the shell has already parsed it. --body-file sidesteps
# the hazard by reading the body from a file, which no shell re-parses.
#
# A passive echo REPL (not the bash REPLs used above) is the recipient here
# deliberately: bash would itself re-interpret backticks/$() a SECOND time on
# receipt, confounding whether corruption happened at send time or receipt
# time. This stand-in prints the same `> ` composer glyph the bash REPLs use
# (so dispatch_landed's box-scan heuristic still recognizes it) but only ever
# echoes the submitted line back VERBATIM -- it never re-parses it as a shell
# command -- so these tests isolate the SENDING side, which is where
# SABLE-tmbx1 actually lives.

def _start_echo_pane(sock, tmp_path):
    script = tmp_path / "echo_repl.py"
    script.write_text(
        "import sys\n"
        "while True:\n"
        "    sys.stdout.write('> ')\n"
        "    sys.stdout.flush()\n"
        "    line = sys.stdin.readline()\n"
        "    if not line:\n"
        "        break\n"
        "    sys.stdout.write(line)\n"
        "    sys.stdout.flush()\n",
        encoding="utf-8",
    )
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "200", "-y", "50",
          f"python3 -u {shlex.quote(str(script))}")
    _tmux(sock, "set-option", "-p", "-t", "w", "@sable_role", "optimus")
    return "w"


def test_body_file_delivers_backticks_and_dollar_paren_unexecuted(tmux_socket, tmp_path):
    _start_echo_pane(tmux_socket, tmp_path)
    marker = tmp_path / "would-be-pwned"
    hazardous = f"see `hostname` and $(touch {marker}) now"
    body_path = tmp_path / "body.txt"
    body_path.write_text(hazardous, encoding="utf-8")

    r = _run_msg(
        tmux_socket,
        "optimus",
        "--body-file",
        str(body_path),
        "--from",
        "lincoln",
        "--interrupt",
    )
    assert r.returncode == 0, r.stderr
    _require_until(
        lambda: f"$(touch {marker})" in _capture(tmux_socket, "w"),
        description="literal body-file delivery",
    )
    pane = _capture(tmux_socket, "w")
    assert "`hostname`" in pane
    assert f"$(touch {marker})" in pane
    assert not marker.exists(), "the file-based path must never let $(...) execute"


def test_negative_control_inline_body_through_a_real_shell_is_corrupted_before_sable_msg_runs(
    tmux_socket, tmp_path,
):
    """Plant-and-fail (non-negotiable per SABLE-tmbx1's dispatch notes): proves
    the OLD/vulnerable invocation shape -- a body embedded inline inside a
    double-quoted shell argument, exactly how an agent's Bash tool naively
    composes a send -- actually corrupts the message and actually executes
    the embedded command. This is the reproduction the --body-file fix above
    is defending against; if this test ever stopped failing on the inline
    path, the round-trip test above would no longer be evidence of anything."""
    _start_echo_pane(tmux_socket, tmp_path)
    marker = tmp_path / "executed-marker"
    shell_cmd = (
        f'python3 {shlex.quote(str(BIN))} optimus '
        f'"see `hostname` and $(touch {marker}) now" --from lincoln --interrupt'
    )
    env = {**_env(), "SABLE_TMUX_SOCKET": tmux_socket, "SABLE_TMUX_SESSION": "w"}
    r = subprocess.run(shell_cmd, shell=True, capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    _require_until(
        lambda: "see " in _capture(tmux_socket, "w"),
        description="negative-control inline delivery",
    )

    # HARM 1 -- EXECUTION: $(...) actually ran, with the caller's privileges,
    # before sable-msg's argv was even populated.
    assert marker.exists(), (
        "expected the pre-fix vulnerable invocation to execute the command "
        "inside $(...) -- if this fails, the reproduction is no longer "
        "faithful to the reported bug and the contrast above proves nothing"
    )
    # HARM 2 -- CORRUPTION: the delivered text is missing the substituted
    # commands, silently replaced by their stdout, not the literal source.
    pane = _capture(tmux_socket, "w")
    assert "$(touch" not in pane
    assert "`hostname`" not in pane


# --- SABLE-o8uti: inline versus body-file on the durable default path -------
#
# The original transport experiment asked whether inline and --body-file
# payloads landed differently in a live composer. SABLE-1el7e removes that
# variable from default delivery: both input forms must converge to the same
# byte-exact queue representation before any pane write. Direct transport
# behavior remains separately exercised through the explicit --interrupt arm.

def test_default_inline_and_body_file_enqueue_identical_bytes_o8uti(
    tmux_socket, tmp_path,
):
    """Default input paths converge before pane transport (SABLE-1el7e).

    The former six-cell busy/idle experiment answered a transport question the
    default path no longer asks. Inline and file bodies must now publish the
    same original bytes, and a fresh drainer makes both successful without
    either body appearing in the composer.
    """

    _start_pane(tmux_socket)
    body = (
        "o8uti controlled body: identical length and content across both "
        "default input paths"
    )
    body_path = tmp_path / "cell_body.txt"
    body_path.write_text(body, encoding="utf-8")
    inbox_lib.record_drain_heartbeat(interval_seconds=60)

    inline = _run_msg(
        tmux_socket, "optimus", body, "--from", "lincoln"
    )
    file_backed = _run_msg(
        tmux_socket,
        "optimus",
        "--body-file",
        str(body_path),
        "--from",
        "lincoln",
    )

    assert inline.returncode == 0, inline.stderr
    assert file_backed.returncode == 0, file_backed.stderr
    assert [message.body for message in inbox_lib.read("optimus")] == [body, body]
    assert body not in _capture(tmux_socket, "w")


# --- freshness: supersession (SABLE-xwy0b) ----------------------------------
#
# The observed defect this bead is about: message A queues behind a busy
# pane and enters its own verified-delivery retry loop; before it confirms, a
# corrected message B (same sender, same recipient) is composed and forced
# in with --interrupt. Every participant behaves correctly — the manager sent
# a stronger instruction, the recipient's TUI would obey whichever text it
# last accepted — yet A's retry loop is a mechanism by which the recipient
# could end up acting on A anyway, later, with nothing marking it stale.
#
# _BUSY_MARKER_DISCARD_TUI is a one-line variant of _BUSY_MARKER_TUI above:
# Escape DISCARDS any not-yet-submitted queued text instead of flushing it to
# REC_FILE. This models the real Claude-TUI's --interrupt clearing the
# composer NON-DESTRUCTIVELY (sable-msg's own module docstring: "--interrupt's
# Escape CLEARS the composer non-destructively") — the physical mechanism
# that keeps a still-queued, not-yet-submitted stale message from ever
# becoming a real turn. Reusing the flush-on-interrupt _BUSY_MARKER_TUI here
# would leak A's text into REC_FILE regardless of whether sable-msg's own
# supersession bookkeeping works at all, making even a fully correct fix look
# broken — so this test needs the discard variant specifically.
_BUSY_MARKER_DISCARD_TUI = r'''#!/usr/bin/env bash
queued=""
busy=1
: > "$BUSY_READY"                                  # signal busy-entry BEFORE first read
while [ "$busy" = 1 ]; do
  printf '\033[H\033[2J  Running the turn (esc to interrupt)\n'
  printf '\xe2\x9d\xaf %s\n' "$queued"
  if IFS= read -rsN1 -t 0.2 ch; then
    case "$ch" in
      $'\x1b') printf 'INTERRUPTED' > "$END_FILE"; queued=""; busy=0 ;;
      $'\n'|$'\r'|'') : ;;
      *) IFS= read -r rest; queued="$ch$rest" ;;
    esac
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


def _start_busy_pane_markers_discard(sock, tmp_path):
    """Like _start_busy_pane_markers, but running _BUSY_MARKER_DISCARD_TUI
    (see above) so Escape discards rather than flushes a queued line."""
    rec = tmp_path / "rec.txt"
    end = tmp_path / "end.txt"
    busy_ready = tmp_path / "busy_ready"
    go_idle = tmp_path / "go_idle"
    script = tmp_path / "busy_marker_discard_tui.sh"
    script.write_text(_BUSY_MARKER_DISCARD_TUI)
    script.chmod(0o755)
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "200", "-y", "50",
          f"REC_FILE={rec} END_FILE={end} BUSY_READY={busy_ready} "
          f"GO_IDLE={go_idle} bash {script}")
    _tmux(sock, "set-option", "-p", "-t", "w", "@sable_role", "optimus")
    return rec, end, busy_ready, go_idle


def test_interrupt_bypasses_without_mutating_existing_queued_payload(
    tmux_socket, tmp_path,
):
    """A deliberate override may overtake, but never corrupt, the file queue.

    Direct-retry supersession remains pinned in the unit suite. Once a default
    payload is durably published it is no longer a retrying pane injection: the
    later interrupt lands directly while the older FIFO record remains exact
    and was never exposed to the composer.
    """
    rec, end, busy_ready, go_idle = _start_busy_pane_markers_discard(tmux_socket, tmp_path)
    assert _wait_until(busy_ready.exists, timeout=10), \
        "stand-in never signalled busy-entry"

    a_body = "HOLD-A push-only, superseded"
    b_body = "HOLD-B two-part corrected hold"
    env_common = {
        **_env(),
        "SABLE_TMUX_SOCKET": tmux_socket,
        "SABLE_TMUX_SESSION": "w",
    }

    result_a = subprocess.run(
        ["python3", str(BIN), "optimus", a_body, "--from", "lincoln"],
        capture_output=True,
        text=True,
        env=env_common,
    )
    assert result_a.returncode == 0
    assert "QUEUED-WAKE-DEFERRED" in result_a.stderr
    assert a_body not in _capture(tmux_socket, "w")

    result_b = subprocess.run(
        ["python3", str(BIN), "optimus", b_body, "--from", "lincoln", "--interrupt"],
        capture_output=True,
        text=True,
        env={
            **env_common,
            "SABLE_MSG_SUBMIT_TRIES": "8",
            "SABLE_MSG_POLL_INTERVAL": "0.2",
            "SABLE_MSG_READY_TIMEOUT": "5",
        },
    )
    go_idle.touch()
    assert result_b.returncode == 0, result_b.stderr
    assert "delivered" in result_b.stderr

    assert _wait_until(lambda: rec.exists() and b_body in _read_recording(rec), timeout=10), \
        "message B must have actually landed as a submitted turn"
    rec_text = _read_recording(rec)
    assert a_body not in rec_text
    assert [message.body for message in inbox_lib.read("optimus")] == [a_body]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
