#!/usr/bin/env python3
"""Integration test for bin/sable-recycle-cockpit against a REAL tmux server
and a REAL sandbox bd store (SABLE-twn37).

Two isolation disciplines, combined:
  - real bd: a throwaway HOME + `bd init --non-interactive` in a scratch dir,
    matching test_sable_bd_remember_integration.py / test_sable_reconcile_
    handoffs_integration.py — never the developer's own beads DB.
  - real tmux: an isolated `-L` socket (test_sable_msg_integration.py's
    pattern) plus the SABLE-j3bi hermeticity rule — identity env
    (CLAUDE_AGENT_NAME / SABLE_WORKER_PANE / CLAUDE_AGENT_ROLE / SABLE_BEAD /
    TMUX_PANE / TMUX) is scrubbed BEFORE the first pane-spawning tmux call,
    since this suite may itself run inside a live SABLE agent pane and a
    later scrub cannot retroactively clean an already-spawned pane's
    inherited environ.

Self-skips when bd/dolt or tmux are absent (ci-verify's clean room has
neither by design).

The scratch pane runs a bash stand-in for the Claude Code TUI: composer
posture toggles idle/busy on $BUSY_FLAG (marker-file driven, not a wall-clock
window — the SABLE-l7uv/wcbj class of flake this repo's other tmux suites
document). Receiving a literal `/clear` line while idle simulates a real
reboot: it erases tmux's OWN scrollback (ESC[3J) — matching the real TUI's
observed behavior in this bead's notes (a genuine boot leaves nothing above
the banner in full scrollback) — then renders the boot banner plus this
repo's actual SessionStart marker ('[bd prime]') persistently, so
boot-observed polling can find it on any subsequent capture.

Single test drives both required assertions from the dispatch's test spec in
one flow: keys land ONLY at idle (busy first, refused, no keys recorded) and
boot is observed (idle second, keys land, marker appears).
"""
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent / "sable-recycle-cockpit"

HAVE_BD = shutil.which("bd") is not None
HAVE_TMUX = shutil.which("tmux") is not None
pytestmark = pytest.mark.skipif(
    not (HAVE_BD and HAVE_TMUX),
    reason="ci-verify clean-room has no bd/dolt/tmux by design; real integration self-skips",
)

_ENV_LEAKS = ("CLAUDE_AGENT_NAME", "TMUX_PANE", "TMUX", "SABLE_WORKER_PANE",
              "CLAUDE_AGENT_ROLE", "SABLE_BEAD", "SABLE_TMUX_SOCKET")


def _env(home, extra=None):
    # SABLE-j3bi: identity env stripped for every subprocess this suite
    # launches, not only the tmux server — a leaked CLAUDE_AGENT_NAME on the
    # `python3 sable-recycle-cockpit` call itself would be equally wrong,
    # even though that call doesn't spawn a pane.
    env = {k: v for k, v in os.environ.items() if k not in _ENV_LEAKS}
    env["HOME"] = str(home)
    env["BD_NON_INTERACTIVE"] = "1"
    env["CI"] = "true"
    return {**env, **(extra or {})}


def _run(argv, cwd, home, extra_env=None, check=True):
    env = _env(home, extra_env)
    cp = subprocess.run(argv, cwd=str(cwd), env=env, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    if check and cp.returncode != 0:
        raise AssertionError(f"{argv} failed: {cp.stdout}")
    return cp


def _robust_bd_init(work, home):
    """bd init on the embedded-Dolt backend can leave a partial DB on a
    first-run race (rc 0 but no .beads/config.yaml) — gate success on that
    artifact and wipe+retry (mirrors test_sable_bd_remember_integration.py)."""
    beads = work / ".beads"
    last = None
    for _ in range(4):
        if beads.exists():
            shutil.rmtree(beads)
        last = _run(["bd", "init", "--non-interactive"], work, home, check=False)
        if last.returncode == 0 and (beads / "config.yaml").is_file():
            return last
    raise AssertionError(f"bd init never produced a clean DB: {last.stdout if last else '<none>'}")


@pytest.fixture()
def bd_sandbox(tmp_path):
    work = tmp_path / "work"
    home = tmp_path / "home"
    work.mkdir()
    home.mkdir()
    _robust_bd_init(work, home)
    return work, home


@pytest.fixture()
def tmux_socket():
    sock = f"sable-twn37-it-{uuid.uuid4().hex[:8]}"
    yield sock
    subprocess.run(["tmux", "-L", sock, "kill-server"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _tmux_server_env():
    # Positional per SABLE-j3bi: scrubbed BEFORE the first pane-spawning call.
    env = dict(os.environ)
    for var in _ENV_LEAKS:
        env.pop(var, None)
    return env


def _tmux(sock, *args, check=True):
    return subprocess.run(["tmux", "-L", sock, *args],
                          capture_output=True, text=True, check=check,
                          env=_tmux_server_env())


def _capture(sock, target):
    return _tmux(sock, "capture-pane", "-t", target, "-p").stdout


# The Claude Code TUI stand-in. BUSY_FLAG toggles the composer between idle
# and mid-turn; REC_FILE records every submitted line (proof of what actually
# landed); BOOTED_FLAG flips once a real /clear-while-idle is processed (or is
# pre-set by the test to model a pane that was already recycled before this
# script ever ran).
#
# render() repositions the cursor to FIXED absolute rows (CSI row;1H) and
# erases only that single line (CSI K) on every tick, rather than re-clearing
# the whole screen — this matters empirically, not cosmetically: a real
# terminal's CSI 2J (erase display) does NOT discard the erased content, it
# scrolls it into history, so a naive full-screen redraw on every poll tick
# was observed (debugging this suite) to duplicate the banner into scrollback
# once per tick, breaking the "exactly one occurrence" already-recycled
# signature. In-place single-line updates never scroll, so nothing
# accumulates — matching how a real TUI's live composer/status region behaves.
#
# boot_now() renders the ACTUAL one-time reboot transition: CSI 2J (erase
# display, pushing whatever was on screen — e.g. an echoed, not-yet-processed
# "/clear" — into history) immediately followed by CSI 3J (erase that
# scrollback too) then home + the boot banner. Order matters: 3J before 2J
# would erase history that 2J then immediately repopulates with the just-
# erased screen. This reproduces the bead notes' verified real-TUI signature:
# after a genuine reboot, full scrollback shows the banner exactly once with
# nothing above it.
_COCKPIT_STUB = r'''#!/usr/bin/env bash
STATUS_ROW=1
PROMPT_ROW=2
boot_now() {
  printf '\033[2J\033[3J\033[H'
  printf 'Claude Code v9.9.9 (stand-in)\n[bd prime] fresh session booted\n'
  STATUS_ROW=4
  PROMPT_ROW=5
}
render() {
  if [ -e "$BUSY_FLAG" ]; then
    printf '\033[%d;1H\033[K  Working (esc to interrupt)' "$STATUS_ROW"
  else
    printf '\033[%d;1H\033[K' "$STATUS_ROW"
  fi
  printf '\033[%d;1H\033[K\xe2\x9d\xaf ' "$PROMPT_ROW"
}
if [ -e "$BOOTED_FLAG" ]; then
  boot_now
fi
render
while true; do
  if IFS= read -r -t 0.2 line; then
    printf '%s\n' "$line" >> "$REC_FILE"
    if [ "$line" = "/clear" ] && [ ! -e "$BUSY_FLAG" ] && [ ! -e "$BOOTED_FLAG" ]; then
      boot_now
      : > "$BOOTED_FLAG"
    fi
  fi
  render
done
'''


def _start_cockpit_pane(sock, tmp_path, start_busy, start_booted=False):
    rec = tmp_path / "rec.txt"
    busy_flag = tmp_path / "busy_flag"
    booted_flag = tmp_path / "booted_flag"
    script = tmp_path / "cockpit_stub.sh"
    script.write_text(_COCKPIT_STUB)
    script.chmod(0o755)
    # Markers present BEFORE the pane's first read/startup check — the stub
    # only evaluates BOOTED_FLAG once at process start, so setting it after
    # spawn is a real race (SABLE-wcbj/l7uv class), not a cosmetic ordering
    # nit: the stub would already be past its one-shot check.
    if start_busy:
        busy_flag.touch()
    if start_booted:
        booted_flag.touch()
    _tmux(sock, "new-session", "-d", "-s", "w", "-x", "200", "-y", "50",
          f"REC_FILE={rec} BUSY_FLAG={busy_flag} BOOTED_FLAG={booted_flag} bash {script}")
    time.sleep(0.5)
    return rec, busy_flag, booted_flag


def _wait_until(pred, timeout=10.0, interval=0.15):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


def _plant_shift_report(work, home):
    cp = _run(["bd", "create", "--title=Cockpit shift report (SABLE-twn37 integration test)",
              "--type=task", "--priority=2"], work, home)
    # `bd create` prints "✓ Created issue: <id> — <title>" — the id is the
    # token right after "issue:".
    for line in cp.stdout.splitlines():
        if "Created issue:" in line:
            return line.split("Created issue:", 1)[1].split()[0]
    raise AssertionError(f"could not find created issue id in: {cp.stdout!r}")


def test_full_recycle_against_scratch_tmux_session(bd_sandbox, tmux_socket, tmp_path):
    work, home = bd_sandbox
    bead_id = _plant_shift_report(work, home)
    rec, busy_flag, booted_flag = _start_cockpit_pane(tmux_socket, tmp_path, start_busy=True)

    common_args = ["python3", str(BIN), bead_id, "--pane", "w",
                   "--socket", tmux_socket, "--max-age-seconds", "3600",
                   "--idle-timeout", "3", "--poll-interval", "0.3",
                   "--boot-timeout", "6"]

    # --- leg 1: busy pane -> refused, no keys land ---
    r_busy = _run(common_args, work, home, check=False)
    assert r_busy.returncode == 3, r_busy.stdout
    assert "busy" in r_busy.stdout.lower()
    assert not booted_flag.exists()
    rec_text = rec.read_text() if rec.exists() else ""
    assert "/clear" not in rec_text, "keys must never land on a busy pane"

    # --- leg 2: pane goes idle -> keys land, boot is observed ---
    busy_flag.unlink()
    r_idle = _run(common_args, work, home, check=False)
    assert r_idle.returncode == 0, r_idle.stdout
    # bare bead id printed for the initiator to relay (last line of stdout)
    assert r_idle.stdout.strip().splitlines()[-1] == bead_id

    assert _wait_until(lambda: "/clear" in (rec.read_text() if rec.exists() else "")), \
        "the /clear keystroke must have actually landed in the pane"
    assert _wait_until(booted_flag.exists), "the stand-in never observed a real reboot"
    assert _wait_until(lambda: "[bd prime]" in _capture(tmux_socket, "w")), \
        "the SessionStart marker must be observable in the pane after boot"


def test_already_recycled_pane_is_a_noop_and_still_relays(bd_sandbox, tmux_socket, tmp_path):
    work, home = bd_sandbox
    bead_id = _plant_shift_report(work, home)
    rec, busy_flag, booted_flag = _start_cockpit_pane(
        tmux_socket, tmp_path, start_busy=False, start_booted=True)

    r = _run(["python3", str(BIN), bead_id, "--pane", "w", "--socket", tmux_socket,
             "--max-age-seconds", "3600", "--idle-timeout", "3", "--poll-interval", "0.3",
             "--boot-timeout", "6"], work, home, check=False)
    assert r.returncode == 0, r.stdout
    assert "already" in r.stdout.lower()
    assert r.stdout.strip().splitlines()[-1] == bead_id
    rec_text = rec.read_text() if rec.exists() else ""
    assert "/clear" not in rec_text, "an already-recycled pane must never receive /clear"


def test_stale_shift_report_refuses_without_touching_the_pane(bd_sandbox, tmux_socket, tmp_path):
    work, home = bd_sandbox
    bead_id = _plant_shift_report(work, home)
    rec, _busy_flag, _booted_flag = _start_cockpit_pane(tmux_socket, tmp_path, start_busy=False)

    r = _run(["python3", str(BIN), bead_id, "--pane", "w", "--socket", tmux_socket,
             "--max-age-seconds", "0", "--idle-timeout", "3", "--poll-interval", "0.3",
             "--boot-timeout", "6"], work, home, check=False)
    assert r.returncode == 2, r.stdout
    assert "stale" in r.stdout.lower()
    time.sleep(0.5)
    rec_text = rec.read_text() if rec.exists() else ""
    assert "/clear" not in rec_text
