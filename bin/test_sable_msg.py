#!/usr/bin/env python3
"""Unit tests for bin/sable-msg (loaded by path — the CLI has no .py extension).

Covers the Fresh-Agent-Test spec items for SABLE-bldh.1 (header formatting, arg
parsing, registry lookup), SABLE-bq93 (verified delivery: --interrupt waits for
pane readiness and submission is retried until the framed header is confirmed
in the pane, not just assumed from a zero exit code), and SABLE-6izz
(bead-addressed worker delivery via --bead, and the pinned guarantee that
manager-name lookups never fall through to a worker pane's bead tag).
"""
import importlib.util
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

# Load the no-extension CLI as a module (needs an explicit source loader since
# there is no .py suffix for importlib to infer one from).
_LOADER = SourceFileLoader("sable_msg", str(Path(__file__).resolve().parent / "sable-msg"))
_SPEC = importlib.util.spec_from_loader("sable_msg", _LOADER)
sable_msg = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(sable_msg)

# sable-msg inserts its own dir on sys.path at import, so the shared helper
# module it imports from is now importable directly for predicate-level tests.
import sable_pane_lib  # noqa: E402


# --- header / message formatting -------------------------------------------

def test_format_message_basic():
    msg = sable_msg.format_message("optimus", "lincoln", "API epic is urgent")
    assert msg == "⟦SABLE-MSG⟧ from=optimus to=lincoln :: API epic is urgent"


def test_format_message_collapses_newlines_and_runs():
    msg = sable_msg.format_message("lincoln", "optimus", "drop auth\n\n  do API   now")
    # newlines/extra spaces collapse to single spaces -> single-line, single turn
    assert msg == "⟦SABLE-MSG⟧ from=lincoln to=optimus :: drop auth do API now"
    assert "\n" not in msg


def test_header_glyph_present():
    assert sable_msg.HEADER == "⟦SABLE-MSG⟧"


# --- registry parsing (tmux list-panes output) ------------------------------

def test_parse_panes_basic():
    out = "%1 lincoln\n%2 optimus\n%3 tarzan\n"
    assert sable_msg.parse_panes(out) == {
        "lincoln": "%1",
        "optimus": "%2",
        "tarzan": "%3",
    }


def test_parse_panes_skips_roleless_and_blank():
    # panes with no @sable_role set emit just the pane id (no second field)
    out = "%1 lincoln\n%2 \n%3\n\n%4 optimus\n"
    assert sable_msg.parse_panes(out) == {"lincoln": "%1", "optimus": "%4"}


def test_parse_panes_first_wins_on_duplicate_role():
    out = "%1 optimus\n%2 optimus\n"
    assert sable_msg.parse_panes(out)["optimus"] == "%1"


@pytest.fixture(autouse=True)
def _pin_session(monkeypatch):
    """Keep every test hermetic: main() resolves the target session per-repo
    (SABLE-e1e3.3), which would consult the real tmux server — the env
    override short-circuits that."""
    monkeypatch.setenv("SABLE_TMUX_SESSION", "s")


def test_lookup_pane_found_and_missing():
    fake_out = "%1 lincoln\n%2 optimus\n"
    runner = lambda args: fake_out
    assert sable_msg.lookup_pane("optimus", runner) == "%2"
    assert sable_msg.lookup_pane("ghost", runner) is None


def test_lookup_pane_scopes_to_session_when_given():
    seen = []
    runner = lambda args: seen.append(args) or "%1 optimus\n"
    assert sable_msg.lookup_pane("optimus", runner, session="sable-alpha") == "%1"
    cmd = seen[0]
    assert ["-s", "-t", "sable-alpha"] == cmd[cmd.index("-s"):cmd.index("-s") + 3]
    assert "-a" not in cmd


def test_lookup_worker_by_bead_scopes_to_session_when_given():
    seen = []
    runner = lambda args: seen.append(args) or "%7 worker SABLE-x1\n"
    assert sable_msg.lookup_worker_by_bead("SABLE-x1", runner,
                                           session="sable-alpha") == "%7"
    cmd = seen[0]
    assert "-a" not in cmd and "sable-alpha" in cmd


def test_lookup_pane_missing_session_returns_none():
    def runner(args):
        raise subprocess.CalledProcessError(1, args)
    assert sable_msg.lookup_pane("optimus", runner, session="sable-gone") is None


# --- tmux base / socket isolation -------------------------------------------

def test_tmux_base_default():
    assert sable_msg.tmux_base(None) == ["tmux"]


def test_tmux_base_socket():
    assert sable_msg.tmux_base("sable-test") == ["tmux", "-L", "sable-test"]


# --- arg parsing ------------------------------------------------------------

def test_parse_args_requires_role_and_body():
    with pytest.raises(SystemExit):
        sable_msg.parse_args([])
    with pytest.raises(SystemExit):
        sable_msg.parse_args(["optimus"])  # body missing


def test_parse_args_from_default_and_interrupt():
    ns = sable_msg.parse_args(["optimus", "hi there", "--from", "lincoln"])
    assert ns.to_role == "optimus"
    assert ns.body == "hi there"
    assert ns.frm == "lincoln"
    assert ns.interrupt is False
    assert ns.bead is False
    ns2 = sable_msg.parse_args(["lincoln", "stop", "--interrupt"])
    assert ns2.interrupt is True


def test_parse_args_bead_flag():
    ns = sable_msg.parse_args(["market-brief-package-73t4", "hold the tree claim", "--bead"])
    assert ns.to_role == "market-brief-package-73t4"
    assert ns.bead is True


# --- main: missing role is a hard error -------------------------------------

def test_main_missing_role_errors(monkeypatch, capsys):
    monkeypatch.setattr(sable_msg, "lookup_pane", lambda role, run=None, socket=None, session=None: None)
    rc = sable_msg.main(["ghost", "hello", "--from", "lincoln"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "ghost" in err


# --- main: delivery is verified, not assumed (SABLE-bq93) -------------------

def test_main_happy_path_reports_delivered(monkeypatch, capsys):
    monkeypatch.setattr(sable_msg, "lookup_pane", lambda role, run=None, socket=None, session=None: "%2")
    monkeypatch.setattr(sable_msg, "deliver_message", lambda *a, **k: True)
    rc = sable_msg.main(["optimus", "ship it", "--from", "lincoln"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "delivered" in err
    assert "optimus" in err


def test_main_reports_undelivered_and_exits_nonzero(monkeypatch, capsys):
    # This is the exact SABLE-bq93 false-positive: send-keys "succeeding" must
    # no longer be enough to print `delivered` — verification failing must
    # surface as a hard, non-zero-exit failure with a durable-fallback hint
    # (bd unavailable here, so the manual hint is the fallback's fallback).
    monkeypatch.setattr(sable_msg, "lookup_pane", lambda role, run=None, socket=None, session=None: "%2")
    monkeypatch.setattr(sable_msg, "deliver_message", lambda *a, **k: False)
    monkeypatch.setattr(sable_msg, "file_fallback_bead", lambda *a, **k: None)
    rc = sable_msg.main(["optimus", "cap in force", "--from", "lincoln", "--interrupt"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "undelivered" in err
    assert "optimus" in err
    assert "for-optimus" in err  # durable inbox-bead fallback hint


def test_main_undelivered_auto_files_durable_fallback_bead(monkeypatch, capsys):
    # SABLE-1umr acceptance: failed verification FILES the durable inbox bead
    # (not just advice) and reports its id — delivery degrades to the bead
    # substrate instead of silently degrading to nothing.
    monkeypatch.setattr(sable_msg, "lookup_pane", lambda role, run=None, socket=None, session=None: "%2")
    monkeypatch.setattr(sable_msg, "deliver_message", lambda *a, **k: False)
    calls = []
    monkeypatch.setattr(sable_msg, "file_fallback_bead",
                        lambda frm, to, msg, runner=None: calls.append((frm, to, msg)) or "SABLE-fb42")
    rc = sable_msg.main(["optimus", "cap in force", "--from", "lincoln"])
    assert rc != 0
    assert calls and calls[0][0] == "lincoln" and calls[0][1] == "optimus"
    err = capsys.readouterr().err
    assert "SABLE-fb42" in err


def test_main_undelivered_bead_addressed_does_not_auto_file(monkeypatch, capsys):
    # Worker lanes are owned by their dispatching manager (who sees the nonzero
    # exit live); a for-<bead-id> inbox label would be meaningless. No auto-file.
    monkeypatch.setattr(sable_msg, "lookup_worker_by_bead",
                        lambda bead, run=None, socket=None, session=None: "%9")
    monkeypatch.setattr(sable_msg, "deliver_message", lambda *a, **k: False)
    monkeypatch.setattr(sable_msg, "file_fallback_bead",
                        lambda *a, **k: pytest.fail("must not auto-file for --bead"))
    rc = sable_msg.main(["market-brief-package-73t4", "hold", "--from", "optimus", "--bead"])
    assert rc != 0


def test_file_fallback_bead_creates_for_role_inbox_bead():
    seen = []

    class R:
        returncode = 0
        stdout = "Created issue: SABLE-ab12\n"
        stderr = ""

    def runner(args):
        seen.append(args)
        return R()

    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    bead_id = sable_msg.file_fallback_bead("lincoln", "optimus", message, runner=runner)
    assert bead_id == "SABLE-ab12"
    argv = seen[0]
    assert argv[:2] == ["bd", "create"]
    joined = " ".join(argv)
    assert "for-optimus" in joined
    assert message in joined


def test_file_fallback_bead_returns_none_when_bd_unavailable():
    class R:
        returncode = 1
        stdout = ""
        stderr = "bd: not a beads workspace"

    assert sable_msg.file_fallback_bead("lincoln", "optimus", "msg",
                                        runner=lambda a: R()) is None


# --- deliver_message: stub-tmux retry + verification (SABLE-bq93) -----------

def test_deliver_message_retries_until_header_lands_outside_input_box():
    # Simulates the exact failure mode: the pane is still booting for the first
    # two readiness polls (no empty prompt line -> wait_for_ready keeps waiting),
    # then becomes ready but the typed message sits unsubmitted in the input box
    # for two more checks (the dropped-Enter race) before it finally lands.
    screens = iter([
        "╭─ Claude Code ─╮\n│ booting… │\n╰──────────╯",             # not ready
        "╭─ Claude Code ─╮\n│ booting… │\n╰──────────╯",             # not ready
        "❯ \n  ddc@host:~/wt",                                       # ready, empty box
        "❯ ⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force\n  ddc@host:~/wt",  # still in box
        "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force\n"
        "● thinking…\n❯ \n  ddc@host:~/wt",                          # landed
    ])
    sent = []

    def run(cmd):
        sent.append(cmd)
        return True

    def capture():
        return next(screens)

    sleeps = []
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    landed = sable_msg.deliver_message(
        "%2", message, interrupt=True, run=run, capture=capture,
        sleep=sleeps.append, ready_timeout=10, interval=0.01, tries=5,
    )
    assert landed is True
    assert sent[0] == ["tmux", "send-keys", "-t", "%2", "Escape"]
    assert any(c[-3:] == ["send-keys", "-t", "%2"] or c[-2:] == ["-l", message] for c in sent
              if "-l" in c)
    # it genuinely retried (multiple polls/resends), not a single blind send
    assert len(sleeps) >= 2


def test_deliver_message_gives_up_when_never_confirmed_landed():
    # The message NEVER leaves the input box (e.g. the recipient pane died
    # mid-turn) -> deliver_message must report failure, not delivery.
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    landed = sable_msg.deliver_message(
        "%2", message, interrupt=False,
        run=lambda cmd: True,
        capture=lambda: f"❯ {message}\n  ddc@host:~/wt",  # always still in the box
        sleep=lambda s: None, tries=3, interval=0.01,
    )
    assert landed is False


# --- visible-versus-submitted: no composer box => not landed (SABLE-wvk9) ----

def test_dispatch_landed_false_when_no_composer_box_even_if_text_visible():
    # The silent-swallow signature: the typed text is VISIBLE in the capture but
    # no composer glyph (❯/>) is locatable — a busy pane whose prompt line was
    # obscured by a spinner/reflow, or a booting/gated pane. We cannot prove the
    # text left the input box, so it must NOT count as a submitted turn. The old
    # `box_start is None -> return True` reported these as delivered while the
    # message sat unsubmitted as pending input (two stranded handoffs).
    snippet = "⟦SABLE-MSG⟧ from=optimus to=chuck :: PR ready from optimus"
    # Uses the two REAL swallow fixtures from the bead design field as the
    # visible-but-boxless payloads.
    for boxless in (
        f"● merging PR…\n✻ Thinking… (8s · esc to interrupt)\n{snippet}",
        "some transcript\n⟦SABLE-MSG⟧ from=lincoln to=worker :: fix SABLE-poka now",
        "prior output\n⟦SABLE-MSG⟧ from=lincoln to=worker :: stand down",
    ):
        # snippet for the fixture rows is the tail after '::'
        want = boxless.split("::", 1)[-1].strip() if "::" in boxless else snippet
        assert sable_pane_lib.dispatch_landed(boxless, want) is False


def test_deliver_message_boxless_visible_text_degrades_to_failure():
    # End-to-end through deliver_message: the pane only ever shows the text with
    # no composer box (never a submitted turn) -> report non-delivery so the
    # caller routes to the durable fallback bead, not a phantom 'delivered'.
    message = "⟦SABLE-MSG⟧ from=optimus to=chuck :: PR ready from optimus"
    landed = sable_msg.deliver_message(
        "%2", message, interrupt=False,
        run=lambda cmd: True,
        # visible in a busy pane, but no ❯/> composer line anywhere
        capture=lambda: f"✻ Thinking… (esc to interrupt)\n{message}",
        sleep=lambda s: None, tries=3, interval=0.01,
    )
    assert landed is False


# --- interrupt idle-wait state machine (SABLE-m6is) -------------------------
# A busy Claude turn STILL shows the empty composer prompt at the bottom, so
# pane_ready fired mid-turn and --interrupt typed into a pane still redrawing the
# interrupted turn — the message was swallowed (two consecutive live sends failed
# all 8 submit attempts). The fix: send Escape ONCE, then defer injection until
# the pane is genuinely IDLE (empty prompt AND no 'esc to interrupt' status).

# A pane mid-turn: composer prompt present (pane_ready True) AND the running
# turn's interrupt affordance visible.
_BUSY_SCREEN = ("● Running the auth refactor…\n"
                "✻ Thinking… (12s · ↓ 1.2k tokens · esc to interrupt)\n"
                "❯ \n  ddc@host:~/wt")
# Same pane after the turn settles: prompt present, no busy status line.
_IDLE_SCREEN = "● earlier turn output\n● done\n❯ \n  ddc@host:~/wt"


def test_pane_busy_true_only_while_turn_running():
    assert sable_msg.pane_busy(_BUSY_SCREEN) is True
    assert sable_msg.pane_busy(_IDLE_SCREEN) is False
    # whitespace/padding in the status line must not defeat the match
    assert sable_msg.pane_busy("│   esc   to   interrupt   │") is True


def test_pane_idle_requires_ready_and_not_busy():
    # the crux: a busy pane is READY (has the empty prompt) but NOT idle
    assert sable_msg.pane_ready(_BUSY_SCREEN) is True
    assert sable_msg.pane_idle(_BUSY_SCREEN) is False
    assert sable_msg.pane_idle(_IDLE_SCREEN) is True
    # a booting pane (no prompt yet) is neither ready nor idle
    assert sable_msg.pane_idle("╭─ Claude Code ─╮\n│ booting… │") is False


def test_interrupt_sends_escape_once_and_defers_injection_until_idle():
    # The pane is busy for the first two readiness polls, then settles to idle,
    # then the typed message lands. --interrupt must (a) send Escape exactly
    # once, (b) NOT type the message while the pane is still busy — injection is
    # deferred until the idle poll, three captures in (2 busy + 1 idle). Under
    # the old pane_ready wait it would have typed at the FIRST capture (busy
    # panes are 'ready'), so `typed_at_capture[0] > 1` is the regression guard.
    landed_screen = ("⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force\n"
                     "● thinking…\n❯ \n  ddc@host:~/wt")
    screens = iter([_BUSY_SCREEN, _BUSY_SCREEN, _IDLE_SCREEN, landed_screen])
    captures = {"n": 0}
    sent = []
    typed_at_capture = []

    def run(cmd):
        sent.append(cmd)
        if "-l" in cmd:
            typed_at_capture.append(captures["n"])
        return True

    def capture():
        captures["n"] += 1
        return next(screens)

    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    landed = sable_msg.deliver_message(
        "%2", message, interrupt=True, run=run, capture=capture,
        sleep=lambda s: None, ready_timeout=10, interval=0.01, tries=5,
    )
    assert landed is True
    escapes = [c for c in sent if c[-1] == "Escape"]
    assert len(escapes) == 1                       # Escape sent exactly ONCE
    assert typed_at_capture == [3]                 # typed only after the idle poll
    assert typed_at_capture[0] > 1                 # NOT at the first (busy) poll
    # ordering: Escape precedes the first keystroke injection
    assert sent.index(escapes[0]) < next(i for i, c in enumerate(sent) if "-l" in c)


def test_interrupt_never_types_while_pane_stays_busy_then_degrades():
    # A pane that never leaves the busy state (Escape did not settle it in time):
    # wait_for_idle times out, delivery is ATTEMPTED anyway (never worse than the
    # pre-idle-wait behavior) but the message is never confirmed out of the box,
    # so it degrades to a verified-delivery failure. Escape is still sent once and
    # the message text never appears, so no phantom "landed".
    sent = []

    def run(cmd):
        sent.append(cmd)
        return True

    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: urgent"
    landed = sable_msg.deliver_message(
        "%2", message, interrupt=True, run=run,
        capture=lambda: _BUSY_SCREEN,                # never goes idle
        sleep=lambda s: None, ready_timeout=0.05, interval=0.01, tries=3,
    )
    assert landed is False
    assert len([c for c in sent if c[-1] == "Escape"]) == 1


# --- wrapped-composer delivery (SABLE-1umr) ---------------------------------

def test_deliver_message_wrapped_composer_requires_a_real_enter():
    # SABLE-1umr: the wrapped-composer false positive meant deliver_message
    # could report delivered WITHOUT EVER SENDING ENTER (the first Enter used
    # to be sent only after a failed landed-check). Stateful fake: the message
    # sits wrapped in the composer until an Enter arrives, then shows as a
    # submitted turn.
    message = ("⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap all lanes at 4 "
               "workers and hold pushes until chuck drains the merge queue")
    state = {"entered": False}

    def run(cmd):
        if cmd[-1] == "Enter":
            state["entered"] = True
        return True

    def capture():
        if state["entered"]:
            return f"{message}\n● thinking…\n❯ \n  ddc@host:~/wt"
        return ("❯ ⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap all lanes at 4\n"
                "workers and hold pushes until chuck drains the merge queue\n"
                "  ddc@host:~/wt")

    landed = sable_msg.deliver_message(
        "%2", message, interrupt=False, run=run, capture=capture,
        sleep=lambda s: None, tries=4, interval=0.01,
    )
    assert landed is True
    assert state["entered"] is True  # delivered must imply a submitted turn


def test_deliver_message_sends_enter_immediately_not_only_after_failed_poll():
    # Submission must not depend on the verifier failing once: the Enter is
    # part of typing the message, the retry loop only covers dropped Enters.
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: status?"
    sent = []

    def run(cmd):
        sent.append(cmd)
        return True

    landed = sable_msg.deliver_message(
        "%2", message, interrupt=False, run=run,
        capture=lambda: f"{message}\n● thinking…\n❯ \n  ddc@host:~/wt",
        sleep=lambda s: None, tries=3, interval=0.01,
    )
    assert landed is True
    li = next(i for i, c in enumerate(sent) if "-l" in c)
    assert li + 1 < len(sent), "no keystroke followed the typed text"
    assert sent[li + 1][-1] == "Enter"


# --- bead-addressed worker delivery (SABLE-6izz) ----------------------------

def test_parse_worker_bead_tags_matches_only_worker_role():
    out = ("%1 worker market-brief-package-73t4\n"
           "%2 optimus \n"
           "%3 worker market-brief-package-6izz\n")
    assert sable_msg.parse_worker_bead_tags(out) == {
        "market-brief-package-73t4": "%1",
        "market-brief-package-6izz": "%3",
    }


def test_parse_worker_bead_tags_skips_non_worker_roles():
    # a manager pane happening to carry a stray @sable_bead-shaped 3rd field
    # must never be treated as a bead-addressable pane.
    out = "%1 optimus market-brief-package-73t4\n"
    assert sable_msg.parse_worker_bead_tags(out) == {}


def test_lookup_worker_by_bead_found_and_missing():
    fake_out = "%1 worker market-brief-package-73t4\n%2 optimus \n"
    runner = lambda args: fake_out
    assert sable_msg.lookup_worker_by_bead("market-brief-package-73t4", runner) == "%1"
    assert sable_msg.lookup_worker_by_bead("ghost-bead", runner) is None


def test_manager_name_lookup_never_resolves_via_worker_bead_tag_even_when_stale():
    # SABLE-6izz reassigned regression (originally market-brief-package-0h8k):
    # a worker pane's @sable_bead happens to collide with a manager name
    # ("optimus"), and the real optimus manager pane's role tag is gone/stale
    # (e.g. a race during respawn). Manager-name-addressed delivery (the
    # default, no --bead) must NEVER fall through and land on that worker pane.
    stale_listing = "%1 worker optimus\n"  # worker pane whose bead tag == "optimus"
    runner = lambda args: stale_listing
    assert sable_msg.lookup_pane("optimus", runner) is None
    # bead-addressed lookup is a strictly separate path/flag, opted into explicitly
    assert sable_msg.lookup_worker_by_bead("optimus", runner) == "%1"


def test_main_bead_addressed_delivery(monkeypatch, capsys):
    monkeypatch.setattr(sable_msg, "lookup_worker_by_bead",
                        lambda bead, run=None, socket=None, session=None: "%9")
    monkeypatch.setattr(sable_msg, "deliver_message", lambda *a, **k: True)
    rc = sable_msg.main(["market-brief-package-73t4", "hold the tree claim",
                        "--from", "optimus", "--bead"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "delivered" in err


def test_main_bead_addressed_unknown_bead_errors_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(sable_msg, "lookup_worker_by_bead",
                        lambda bead, run=None, socket=None, session=None: None)
    rc = sable_msg.main(["ghost-bead", "hello", "--from", "optimus", "--bead"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "ghost-bead" in err


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
