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
    override short-circuits that. The recipient identity cross-check (SABLE-to8m)
    would likewise shell to the real tmux server + /proc, so it is stubbed to
    None (no poisoning) by default; the cross-check's own test overrides it."""
    monkeypatch.setenv("SABLE_TMUX_SESSION", "s")
    monkeypatch.setattr(sable_msg, "recipient_identity", lambda pane, socket=None: None)


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


def test_main_refuses_poisoned_role_tag(monkeypatch, capsys):
    # SABLE-to8m: the pane resolved for role 'lincoln' has an authoritative
    # process identity of 'optimus' — a poisoned/stale @sable_role tag. Delivery
    # must be REFUSED (never routed into the wrong pane), before deliver_message
    # is ever called.
    monkeypatch.setattr(sable_msg, "lookup_pane", lambda role, run=None, socket=None, session=None: "%9")
    monkeypatch.setattr(sable_msg, "recipient_identity", lambda pane, socket=None: "optimus")
    monkeypatch.setattr(sable_msg, "deliver_message",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not deliver")))
    rc = sable_msg.main(["lincoln", "escalation", "--from", "optimus"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "poisoned" in err.lower()
    assert "optimus" in err          # names the real identity
    assert "sable-relink" in err     # points at the recovery path


def test_main_delivers_when_identity_agrees(monkeypatch, capsys):
    # The cross-check must not block a legitimate send: identity == requested role.
    monkeypatch.setattr(sable_msg, "lookup_pane", lambda role, run=None, socket=None, session=None: "%2")
    monkeypatch.setattr(sable_msg, "recipient_identity", lambda pane, socket=None: "optimus")
    monkeypatch.setattr(sable_msg, "deliver_message", lambda *a, **k: True)
    assert sable_msg.main(["optimus", "ship it", "--from", "lincoln"]) == 0
    assert "delivered" in capsys.readouterr().err


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
        "❯ \n  ddc@host:~/wt",                                       # ready+idle (wait_for_idle returns)
        "❯ \n  ddc@host:~/wt",                                       # idle at send (deliver_text t0, SABLE-d21h)
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
    # Idle at send time, but the message NEVER leaves the input box (the
    # dropped-Enter race that never wins, or the pane dies mid-turn) ->
    # deliver_message must report failure, not delivery. Stateful fake: an empty
    # idle composer BEFORE we type (so idle_at_send is True and it is NOT the
    # SABLE-d21h guard that fails this), then the message stuck in the box
    # forever thereafter.
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    state = {"typed": False}

    def run(cmd):
        if "-l" in cmd:
            state["typed"] = True
        return True

    def capture():
        if not state["typed"]:
            return "❯ \n  ddc@host:~/wt"             # idle at t0
        return f"❯ {message}\n  ddc@host:~/wt"        # stuck in the box thereafter

    landed = sable_msg.deliver_message(
        "%2", message, interrupt=False, run=run, capture=capture,
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


# --- submitted-echo redraw race: report-NOT-landed-when-it-DID (SABLE-uh4b) ---
# The INVERSE of wvk9. Once a message SUBMITS, Claude Code echoes it into the
# transcript as its own prompt-glyph line ("❯ <msg>", glyph + REGULAR space) and
# starts the turn; in the brief redraw window right after Enter the empty
# composer has not repainted yet, so that echo is momentarily the LAST glyph
# line while the turn already runs beneath it (busy marker below). box_start
# alone mistook the echo for the still-unsubmitted composer and false-negatived
# the landing — filing a duplicate durable fallback bead for a message that
# actually submitted, which blocked a P0 worker release.

def test_dispatch_landed_true_for_submitted_echo_above_busy_marker_uh4b():
    msg = ("⟦SABLE-MSG⟧ from=lincoln to=SABLE-z776 :: GO push your worktree "
           "branch now recovery landed and chuck drained the merge queue")
    # redraw window: submitted echo (regular space) is the last glyph line, the
    # turn is already running BELOW it, composer not yet repainted.
    redraw = ("● prior turn output\n"
              f"❯ {msg}\n"
              "✻ Thinking… (1s · ↓ 8 tokens · esc to interrupt)")
    assert sable_pane_lib.dispatch_landed(redraw, msg) is True
    # steady state a moment later (empty composer repainted at the bottom of the
    # box-drawing frame) must stay landed too — the busy marker now sits ABOVE
    # the composer, so the normal box_start path returns True.
    border = "─" * 120
    steady = ("● prior turn output\n"
              f"❯ {msg}\n✻ Thinking… (esc to interrupt)\n"
              f"{border}\n❯\xa0\n{border}\n  ddc@host:~/wk-idle")
    assert sable_pane_lib.dispatch_landed(steady, msg) is True


def test_dispatch_landed_false_for_idle_message_in_box_frame_uh4b_guard():
    # The false-POSITIVE guard on the uh4b allowance: a message sitting UNSENT in
    # the box-drawing composer while the pane is IDLE (no busy marker anywhere
    # below the prompt) must still count as NOT landed. The redraw-race allowance
    # keys strictly on a running turn beneath the echo, so it can never resurrect
    # the wvk9/d21h silent-swallow (report-landed-when-it-did-not).
    msg = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    border = "─" * 120
    stuck = f"● prior turn output\n{border}\n❯\xa0{msg}\n{border}\n  ddc@host:~/wk-idle"
    assert sable_pane_lib.dispatch_landed(stuck, msg) is False


def test_deliver_message_idle_box_frame_lands_via_redraw_race_uh4b():
    # SABLE-uh4b end-to-end at deliver_message, against a REAL-shaped pane: an
    # empty composer inside a box-drawing frame (── borders, "❯\xa0" prompt), a
    # WIDE message, IDLE at send. After type+Enter the message submits and every
    # post-send capture lands in the redraw window (echo "❯ <msg>" + running turn,
    # composer not repainted). deliver_message must report LANDED. Before the fix
    # this false-negatived on every one of the 8 polls (the exact z776 symptom:
    # 'undelivered after 8 attempts' while the message had actually submitted).
    border = "─" * 128
    nbsp = "\xa0"
    cwd = "  ddc@KW-LPT-050:~/dev-environment/wk-idle-pane-landed"
    mode = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
    msg = ("⟦SABLE-MSG⟧ from=lincoln to=SABLE-z776 :: GO push your worktree "
           "branch now that recovery has landed and chuck has drained the merge "
           "queue so your self-push applies cleanly")
    state = {"typed": False}

    def run(cmd):
        if "-l" in cmd:
            state["typed"] = True
        return True

    def capture():
        if not state["typed"]:
            # idle: empty composer inside the box-drawing frame
            return "\n".join(["● prior turn output", "● done", border,
                              "❯" + nbsp + " " * 40, border, cwd, mode])
        # after submit: redraw window persists — transcript echo (regular space)
        # + running turn, the empty composer has NOT repainted yet.
        return "\n".join(["● prior turn output",
                          "❯ " + msg,
                          "✢ Sautéing… (3s · ↓ 40 tokens · esc to interrupt)"])

    landed = sable_msg.deliver_message(
        "%2", msg, interrupt=True, run=run, capture=capture,
        sleep=lambda s: None, ready_timeout=5, interval=0.001, tries=8,
    )
    assert landed is True


def test_main_landed_box_frame_send_does_not_double_file_fallback_bead_uh4b(monkeypatch):
    # SABLE-uh4b second half: a send the pane ACCEPTS must NOT be double-counted
    # into a durable fallback bead (message delivered + a redundant for-<role>
    # bead — the mirror of the not-delivered-but-reported-success loss). Drive the
    # REAL main -> deliver_message -> deliver_text -> dispatch_landed composition
    # against a box-drawing-frame fake that lands via the redraw race; assert rc 0,
    # "delivered", and that file_fallback_bead was NEVER called.
    monkeypatch.setenv("SABLE_MSG_POLL_INTERVAL", "0")
    monkeypatch.setenv("SABLE_MSG_READY_TIMEOUT", "1")
    monkeypatch.setenv("SABLE_MSG_SUBMIT_TRIES", "4")
    monkeypatch.setattr(sable_msg, "lookup_pane",
                        lambda role, run=None, socket=None, session=None: "%2")

    border = "─" * 128
    cwd = "  ddc@KW-LPT-050:~/dev-environment/wk-idle-pane-landed"
    mode = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
    framed = sable_msg.format_message("lincoln", "optimus",
                                      "GO push your worktree branch now recovery landed")
    state = {"typed": False}

    class FakeProc:
        returncode = 0

    def fake_run(cmd, **kw):
        if "-l" in cmd:
            state["typed"] = True
        return FakeProc()

    def fake_capture(base, pane):
        if not state["typed"]:
            return "\n".join(["● prior", border, "❯\xa0" + " " * 20, border, cwd, mode])
        return "\n".join(["● prior", "❯ " + framed,
                          "✻ Thinking… (0s · esc to interrupt)"])

    monkeypatch.setattr(sable_msg.subprocess, "run", fake_run)
    monkeypatch.setattr(sable_msg, "_capture_pane", fake_capture)
    filed = []
    monkeypatch.setattr(sable_msg, "file_fallback_bead",
                        lambda *a, **k: filed.append(a) or "SABLE-should-not-file")

    rc = sable_msg.main(["optimus", "GO push your worktree branch now recovery landed",
                         "--from", "lincoln"])
    assert rc == 0
    assert filed == [], "a landed send must not also file a durable fallback bead"


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


# --- queued-while-busy: pre-send idle tracking (SABLE-d21h) ------------------
# dispatch_landed alone (visible AND not-in-box) cannot tell "our message
# started this turn" from "our message QUEUED behind a DIFFERENT running turn":
# Claude Code hoists a queued line ABOVE the composer and clears the input box,
# so both look identical in a single capture. A queued line can then be dropped
# on the running turn's compaction/redraw or a pane reap — the swallow that
# stranded two handoffs. The fix captures pane_idle at t0 (before the send) and
# only counts an idle->our-turn transition; busy-at-t0 fails closed so the
# caller routes to the durable fallback bead.

def test_deliver_message_queued_while_busy_is_not_landed():
    # THE bead repro: the pane is BUSY running a DIFFERENT turn at send time
    # (its status line shows 'esc to interrupt'). After we type+Enter, our line
    # is hoisted ABOVE an empty composer while the OTHER turn keeps running —
    # visible AND not-in-box — yet it was only QUEUED, never accepted as its own
    # submitted turn. Pre-send state was busy, so deliver_message must report NOT
    # landed (sable-msg then files the durable fallback).
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    other_turn = "● Running the auth refactor…\n✻ Thinking… (12s · esc to interrupt)"
    state = {"typed": False}

    def run(cmd):
        if "-l" in cmd:
            state["typed"] = True
        return True

    def capture():
        if not state["typed"]:
            # t0: someone else's turn is running (busy) — no empty composer.
            return f"{other_turn}\n❯ \n  ddc@host:~/wt"
        # after we type+Enter: our line hoisted above the (still-empty) composer
        # while the OTHER turn keeps running -> visible + not-in-box, but queued.
        return f"{message}\n{other_turn}\n❯ \n  ddc@host:~/wt"

    landed = sable_msg.deliver_message(
        "%2", message, interrupt=False, run=run, capture=capture,
        sleep=lambda s: None, tries=4, interval=0.01,
    )
    assert landed is False

    # Proof the pre-send idle guard is load-bearing: on the hoisted capture, the
    # old visible-vs-submitted signal (dispatch_landed) would have FALSE-POSITIVED.
    hoisted = f"{message}\n{other_turn}\n❯ \n  ddc@host:~/wt"
    assert sable_pane_lib.dispatch_landed(hoisted, message) is True


def test_deliver_message_idle_recipient_transitions_to_our_turn_and_lands():
    # The PRESERVED happy path: the recipient is IDLE at send time (empty
    # composer, no running turn). After we type+Enter it goes busy processing
    # OUR message, which is visible above the composer -> a genuine idle->our-turn
    # transition -> landed. The guard must not regress this normal case.
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: status?"
    state = {"typed": False}

    def run(cmd):
        if "-l" in cmd:
            state["typed"] = True
        return True

    def capture():
        if not state["typed"]:
            return "● earlier turn output\n● done\n❯ \n  ddc@host:~/wt"  # idle at t0
        # now busy processing OUR message, which sits above the composer:
        return f"{message}\n✻ Thinking… (2s · esc to interrupt)\n❯ \n  ddc@host:~/wt"

    landed = sable_msg.deliver_message(
        "%2", message, interrupt=False, run=run, capture=capture,
        sleep=lambda s: None, tries=4, interval=0.01,
    )
    assert landed is True


def test_deliver_text_fresh_pane_dispatch_still_lands():
    # The sable-spawn-worker path, at the shared helper it actually calls
    # (deliver_text). A FRESH worker pane is idle at the empty composer
    # (wait_for_ready already confirmed the prompt), then LEGITIMATELY goes busy
    # after we submit the dispatch. Idle at t0 -> the dispatch must still land;
    # SABLE-d21h must NOT false-negative worker dispatch.
    dispatch = "Read /home/ddc/.claude/sable/dispatch/SABLE-xyz.md in full"
    snippet = "SABLE-xyz"
    state = {"typed": False}

    def run(cmd):
        if "-l" in cmd:
            state["typed"] = True
        return True

    def capture():
        if not state["typed"]:
            return "❯ \n  ddc@host:~/wk-xyz"           # fresh + idle, empty composer
        return f"{dispatch}\n✻ Working… (esc to interrupt)\n❯ \n  ddc@host:~/wk-xyz"

    landed = sable_pane_lib.deliver_text(
        ["tmux"], "%9", dispatch, snippet,
        tries=4, interval=0.01, run=run, capture=capture, sleep=lambda s: None,
    )
    assert landed is True


# --- delayed confirmation of a busy-at-t0 queued send (SABLE-h0jw) -----------
# SABLE-d21h fixed the queued-while-busy phantom-confirm by failing CLOSED the
# instant the pane was busy at t0 — but that filed a durable noise bead EVEN
# WHEN the queued line genuinely submitted+landed once the running turn ended
# (LINCOLN evidence 2026-07-14: chuck's pane, two instances). h0jw replaces
# fail-close-at-t0 with DELAYED confirmation: keep watching the queued line and
# only confirm once it PROVABLY became its own submitted turn (a signal a still-
# queued capture can never present), failing closed only on timeout.


def test_submitted_own_turn_rejects_queued_accepts_submitted_h0jw():
    # The load-bearing predicate. A line QUEUED behind a DIFFERENT running turn
    # (hoisted above the empty composer, the other turn's busy marker ABOVE that
    # composer) must be REJECTED — even though plain dispatch_landed false-
    # positives it (the d21h trap). The same line, once it SUBMITS as its own
    # turn (echoed on a prompt-glyph line with OUR running turn's busy marker
    # BELOW it, or the pane fallen idle with the line in the transcript), must be
    # ACCEPTED.
    msg = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    other = "● Running the auth refactor…\n✻ Thinking… (12s · esc to interrupt)"

    queued = f"{msg}\n{other}\n❯ \n  ddc@host:~/wt"
    # dispatch_landed alone is fooled (this is exactly why d21h needed the t0 guard)
    assert sable_pane_lib.dispatch_landed(queued, msg) is True
    # but submitted_own_turn is NOT — the busy marker sits above the composer
    assert sable_pane_lib.submitted_own_turn(queued, msg) is False

    # submitted as OUR turn: echo + running-turn busy marker directly below it
    running = f"● auth refactor done\n❯ {msg}\n✻ Thinking… (2s · esc to interrupt)"
    assert sable_pane_lib.submitted_own_turn(running, msg) is True

    # or the pane fell fully idle with the line persisted in the transcript
    border = "─" * 80
    idle_done = f"❯ {msg}\n● reply…\n{border}\n❯\xa0\n{border}\n  ddc@host:~/wt"
    assert sable_pane_lib.submitted_own_turn(idle_done, msg) is True

    # a line NEVER present is never landed
    assert sable_pane_lib.submitted_own_turn(f"{other}\n❯ \n  ddc@host:~/wt", msg) is False


def test_deliver_message_busy_at_t0_then_submits_lands_via_delayed_confirmation_h0jw():
    # THE bead repro's happy path: the pane is BUSY running a DIFFERENT turn at
    # send time; our line queues for a few polls (hoisted above the composer,
    # visible-and-not-in-box — the d21h false-positive shape), then the running
    # turn ENDS and our queued line SUBMITS as its own turn. deliver_message must
    # report LANDED via delayed confirmation — NOT fail closed at t0 and file a
    # redundant noise bead.
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    other_turn = "● Running the auth refactor…\n✻ Thinking… (12s · esc to interrupt)"
    state = {"typed": False, "polls": 0}

    def run(cmd):
        if "-l" in cmd:
            state["typed"] = True
        return True

    def capture():
        if not state["typed"]:
            # t0: someone else's turn is running (busy) — no empty composer.
            return f"{other_turn}\n❯ \n  ddc@host:~/wt"
        state["polls"] += 1
        if state["polls"] < 3:
            # queued behind the other turn: hoisted above the empty composer,
            # the OTHER turn's busy marker still ABOVE it. Looks landed to plain
            # dispatch_landed (d21h) but is only QUEUED.
            return f"{message}\n{other_turn}\n❯ \n  ddc@host:~/wt"
        # the other turn ENDED and our queued line SUBMITTED as its own turn:
        # echoed as a prompt-glyph line with OUR running turn's busy marker below.
        return f"● auth refactor done\n❯ {message}\n✻ Thinking… (2s · esc to interrupt)"

    landed = sable_msg.deliver_message(
        "%2", message, interrupt=False, run=run, capture=capture,
        sleep=lambda s: None, tries=8, interval=0.01,
    )
    assert landed is True


def test_deliver_message_busy_at_t0_turn_never_ends_times_out_and_fails_h0jw():
    # The other half of the bead spec: a busy pane whose running turn NEVER ends
    # within the poll budget (and our line never becomes its own submitted turn)
    # must still report NOT landed, so sable-msg files the durable fallback. The
    # delayed confirmation degrades to fail-closed on timeout — never worse than
    # d21h.
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: urgent"
    other_turn = "● Running the auth refactor…\n✻ Thinking… (99s · esc to interrupt)"
    state = {"typed": False}

    def run(cmd):
        if "-l" in cmd:
            state["typed"] = True
        return True

    def capture():
        if not state["typed"]:
            return f"{other_turn}\n❯ \n  ddc@host:~/wt"
        # our line stays queued behind the never-ending turn, forever.
        return f"{message}\n{other_turn}\n❯ \n  ddc@host:~/wt"

    landed = sable_msg.deliver_message(
        "%2", message, interrupt=False, run=run, capture=capture,
        sleep=lambda s: None, tries=4, interval=0.01,
    )
    assert landed is False


def test_main_busy_delayed_land_files_no_fallback_bead_h0jw(monkeypatch):
    # End-to-end through the REAL main -> deliver_message -> deliver_text ->
    # submitted_own_turn composition: a busy-at-t0 send whose queued line later
    # submits+lands must report rc 0 AND must NOT file a durable fallback bead.
    # This is the exact regression the bead is about — every busy-pane send under
    # d21h permanently cost a noise bead even when the message landed.
    monkeypatch.setenv("SABLE_MSG_POLL_INTERVAL", "0")
    monkeypatch.setenv("SABLE_MSG_SUBMIT_TRIES", "8")
    monkeypatch.setenv("SABLE_MSG_READY_TIMEOUT", "1")
    monkeypatch.setattr(sable_msg, "lookup_pane",
                        lambda role, run=None, socket=None, session=None: "%2")

    framed = sable_msg.format_message("lincoln", "optimus", "cap in force")
    other_turn = "● Running the auth refactor…\n✻ Thinking… (12s · esc to interrupt)"
    state = {"typed": False, "polls": 0}

    class FakeProc:
        returncode = 0

    def fake_run(cmd, **kw):
        if "-l" in cmd:
            state["typed"] = True
        return FakeProc()

    def fake_capture(base, pane):
        if not state["typed"]:
            return f"{other_turn}\n❯ \n  ddc@host:~/wt"       # busy at t0
        state["polls"] += 1
        if state["polls"] < 3:
            return f"{framed}\n{other_turn}\n❯ \n  ddc@host:~/wt"  # queued
        return f"● done\n❯ {framed}\n✻ Thinking… (2s · esc to interrupt)"  # submitted

    monkeypatch.setattr(sable_msg.subprocess, "run", fake_run)
    monkeypatch.setattr(sable_msg, "_capture_pane", fake_capture)
    filed = []
    monkeypatch.setattr(sable_msg, "file_fallback_bead",
                        lambda *a, **k: filed.append(a) or "SABLE-should-not-file")

    rc = sable_msg.main(["optimus", "cap in force", "--from", "lincoln"])
    assert rc == 0
    assert filed == [], "a busy-at-t0 send that eventually lands must not file a noise bead"


# --- queued-composer footer + idempotent retry (SABLE-msxj) -----------------
# Recurrence of the h0jw class AFTER h0jw merged: a busy-at-t0 send that
# ACTUALLY queued (visible in the composer with the real Claude-TUI's 'Press up
# to edit queued messages' footer) was still scored as failure. h0jw's signals
# (submitted_own_turn branches 1-2) assumed a queued line gets hoisted ABOVE the
# composer with the box cleared — this TUI posture instead leaves the line IN
# the composer/box, so neither branch fires and the poll budget timed out on a
# send that had, in fact, already succeeded (SABLE-l8a5: closed false-fail,
# evidence the queued line was live in the pane the whole time). Worse, the
# caller then retried the call, and retyping into a still-busy pane whose
# earlier attempt's line was STILL queued produced a literal duplicate turn.


def test_pane_has_queued_message_true_with_footer_false_without():
    msg = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    with_footer = f"❯ {msg}\n  Press up to edit queued messages\n  ddc@host:~/wt"
    assert sable_pane_lib.pane_has_queued_message(with_footer, msg) is True
    # same text, no footer -> not proof of a queued-delivered send
    no_footer = f"❯ {msg}\n  ddc@host:~/wt"
    assert sable_pane_lib.pane_has_queued_message(no_footer, msg) is False
    # footer present but OUR text absent -> some OTHER queued line, not ours
    other_queued = "❯ some other line\n  Press up to edit queued messages\n  ddc@host:~/wt"
    assert sable_pane_lib.pane_has_queued_message(other_queued, msg) is False


def test_submitted_own_turn_accepts_queued_footer_even_when_box_scan_fails_closed_msxj():
    # THE bead's exact posture: the message sits IN the composer/box (never
    # "leaves" it, so dispatch_landed's box-based branches inside
    # submitted_own_turn fail closed here) but the queued-messages footer is
    # independent proof that it landed as a delivered-queued send.
    msg = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    queued_in_box = f"❯ {msg}\n  Press up to edit queued messages\n  ddc@host:~/wt"
    # proof the box-scan alone would fail closed here (the exact SABLE-l8a5 trap)
    assert sable_pane_lib.dispatch_landed(queued_in_box, msg) is False
    assert sable_pane_lib.submitted_own_turn(queued_in_box, msg) is True


def test_deliver_message_busy_at_t0_queued_footer_confirms_without_waiting_out_budget_msxj():
    # End-to-end through deliver_message: busy at t0, and the FIRST poll after
    # typing already shows the queued footer -> must confirm right away, not
    # exhaust the tries budget the way SABLE-l8a5 did (the running turn here
    # never ends, so ANY confirmation must come from the footer signal alone).
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    other_turn = "● Running the auth refactor…\n✻ Thinking… (12s · esc to interrupt)"
    state = {"type_calls": 0}

    def run(cmd):
        if "-l" in cmd:
            state["type_calls"] += 1
        return True

    def capture():
        if state["type_calls"] == 0:
            return f"{other_turn}\n❯ \n  ddc@host:~/wt"          # busy at t0
        # queued in the composer, footer shown, the OTHER turn never ends
        return (f"❯ {message}\n  Press up to edit queued messages\n"
                f"{other_turn}\n  ddc@host:~/wt")

    landed = sable_msg.deliver_message(
        "%47", message, interrupt=False, run=run, capture=capture,
        sleep=lambda s: None, tries=8, interval=0.01,
    )
    assert landed is True
    assert state["type_calls"] == 1, "must type exactly once, not double-queue"


def test_deliver_text_busy_at_t0_skips_retype_when_message_already_pending_msxj():
    # THE double-queue repro: deliver_text is invoked while the pane is BUSY and
    # a PRIOR attempt's line is already sitting queued in the composer (e.g. a
    # caller retrying after an earlier call reported false failure). The retry
    # must recognize the pre-existing text and skip typing it again -- a single
    # queued copy, not two.
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    other_turn = "● Running the auth refactor…\n✻ Thinking… (12s · esc to interrupt)"
    # t0: busy, AND our line from a prior attempt is already visible, queued.
    pending = f"{message}\n{other_turn}\n❯ \n  ddc@host:~/wt"
    state = {"type_calls": 0, "polls": 0}

    def run(cmd):
        if "-l" in cmd:
            state["type_calls"] += 1
        return True

    def capture():
        state["polls"] += 1
        if state["polls"] < 3:
            return pending
        # the running turn ends and our (already-queued) line submits as its own
        return f"● done\n❯ {message}\n✻ Thinking… (2s · esc to interrupt)"

    landed = sable_pane_lib.deliver_text(
        ["tmux"], "%47", message, message,
        tries=8, interval=0.01, run=run, capture=capture, sleep=lambda s: None,
    )
    assert landed is True
    assert state["type_calls"] == 0, "prior attempt's text already queued -- must not retype"


def test_deliver_text_idle_at_t0_always_types_even_if_snippet_coincidentally_visible_msxj():
    # The idempotent-retry guard is scoped to the BUSY-at-t0 path only (the
    # scenario the bead actually reports). An IDLE pane must always type
    # normally -- this is the common send path and must not regress.
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: status?"
    state = {"type_calls": 0}

    def run(cmd):
        if "-l" in cmd:
            state["type_calls"] += 1
        return True

    def capture():
        if state["type_calls"] == 0:
            return "● earlier turn output\n● done\n❯ \n  ddc@host:~/wt"  # idle at t0
        return f"{message}\n✻ Thinking… (2s · esc to interrupt)\n❯ \n  ddc@host:~/wt"

    landed = sable_pane_lib.deliver_text(
        ["tmux"], "%47", message, message,
        tries=4, interval=0.01, run=run, capture=capture, sleep=lambda s: None,
    )
    assert landed is True
    assert state["type_calls"] == 1


# --- busy-at-t0 submit-race self-heal (SABLE-l7uv) --------------------------
# The false-undelivered class the msxj footer path did NOT retire. The original
# repro (SABLE-mgyh) is explicitly "NOT the queued-behind-a-turn state": our line
# sits UN-submitted in the recipient's EDITABLE composer (prompt-glyph line, NO
# 'Press up to edit queued messages' footer). The mechanism: the pane was BUSY at
# t0 (finishing the PRIOR turn — 'Baked for 8s' rendering), so deliver_text took
# the busy leg and sent Enter exactly ONCE; that Enter was absorbed in the
# busy->idle redraw, the prior turn then ended, and our text was left sitting in
# the now-EDITABLE composer. Because the busy leg never resent Enter, the line
# would NEVER auto-submit (it was never a real queued line) and submitted_own_turn
# could never confirm it -> the poll budget timed out -> false 'undelivered' +
# durable fallback bead, while the message sat visibly stuck. The fix: on the busy
# leg, once the pane has fallen IDLE with our snippet still un-submitted in the
# editable box, (re)send Enter to submit it as its own turn.


def test_deliver_text_busy_at_t0_then_idle_with_text_stuck_in_box_resends_enter_l7uv():
    # THE SABLE-l7uv repro, at the helper it lives in. Busy at t0; our Enter is
    # absorbed in the redraw so the first poll shows the pane fallen IDLE with the
    # snippet sitting UN-submitted in the editable composer (no footer, no busy
    # line). submitted_own_turn cannot confirm this (dispatch_landed False on an
    # idle pane == text still in box; no queued-footer). The busy leg must
    # self-heal by RE-SENDING Enter; the stand-in then submits it, and the next
    # poll confirms LANDED. Pre-fix: no Enter is ever resent -> times out -> False.
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    other_turn = "● Baking the prior turn…\n✻ Thinking… (8s · esc to interrupt)"
    state = {"typed": False, "submitted": False, "enter_after_type": 0}

    def run(cmd):
        if "-l" in cmd:
            state["typed"] = True
        elif cmd[-1] == "Enter" and state["typed"]:
            state["enter_after_type"] += 1
            # The FIRST post-type Enter is the absorbed one (busy->idle redraw);
            # the SECOND (the self-heal resend on the idle editable box) submits.
            if state["enter_after_type"] >= 2:
                state["submitted"] = True
        return True

    def capture():
        if not state["typed"]:
            return f"{other_turn}\n❯ \n  ddc@host:~/wt"          # busy at t0
        if not state["submitted"]:
            # prior turn ended; our line sits in the EDITABLE composer, no footer,
            # no busy status -> pane_idle True, dispatch_landed False (still in box)
            return f"❯ {message}\n  ddc@host:~/wt"
        # the self-heal Enter submitted it as its own turn
        return f"● prior done\n❯ {message}\n✻ Thinking… (1s · esc to interrupt)"

    landed = sable_pane_lib.deliver_text(
        ["tmux"], "%5", message, message,
        tries=8, interval=0.01, run=run, capture=capture, sleep=lambda s: None,
    )
    assert landed is True
    assert state["enter_after_type"] >= 2, "the busy leg must resend Enter to submit the stuck line"


def test_deliver_text_busy_at_t0_genuinely_queued_no_selfheal_double_submit_l7uv():
    # The guard against the d21h phantom-confirm regression: a line GENUINELY
    # queued behind a still-running turn (pane stays BUSY, line hoisted above the
    # composer) must NOT trigger the self-heal Enter — pane_idle is False the whole
    # time, so no stray Enter is sent, and when the turn ends the line auto-submits
    # and is confirmed by submitted_own_turn. Exactly one submission, never two.
    message = "⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force"
    other_turn = "● Running the merge gate…\n✻ Thinking… (12s · esc to interrupt)"
    state = {"typed": False, "polls": 0, "enter_after_type": 0}

    def run(cmd):
        if "-l" in cmd:
            state["typed"] = True
        elif cmd[-1] == "Enter" and state["typed"]:
            state["enter_after_type"] += 1
        return True

    def capture():
        if not state["typed"]:
            return f"{other_turn}\n❯ \n  ddc@host:~/wt"          # busy at t0
        state["polls"] += 1
        if state["polls"] < 3:
            # genuinely queued: hoisted above the composer, the OTHER turn still
            # running (busy) -> pane_idle False, self-heal must NOT fire.
            return f"{message}\n{other_turn}\n❯ \n  ddc@host:~/wt"
        # turn ended, our queued line auto-submitted as its own turn
        return f"● gate done\n❯ {message}\n✻ Thinking… (1s · esc to interrupt)"

    landed = sable_pane_lib.deliver_text(
        ["tmux"], "%21", message, message,
        tries=8, interval=0.01, run=run, capture=capture, sleep=lambda s: None,
    )
    assert landed is True
    assert state["enter_after_type"] == 1, "a genuinely-queued busy line must get NO self-heal Enter"


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
    # deferred until the pane is idle. wait_for_idle consumes 2 busy + 1 idle
    # capture, then deliver_text takes its OWN pre-send idle capture (the
    # SABLE-d21h t0 check) before typing, so injection lands at the 4th capture.
    # Under the old pane_ready wait it would have typed at the FIRST capture
    # (busy panes are 'ready'), so `typed_at_capture[0] > 1` is the regression
    # guard.
    landed_screen = ("⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force\n"
                     "● thinking…\n❯ \n  ddc@host:~/wt")
    screens = iter([_BUSY_SCREEN, _BUSY_SCREEN, _IDLE_SCREEN, _IDLE_SCREEN, landed_screen])
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
    assert typed_at_capture == [4]                 # typed only after the idle polls (t0 check is #4)
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
    # to be sent only after a failed landed-check). Stateful fake: an empty idle
    # composer BEFORE we type (idle_at_send True, SABLE-d21h), then the message
    # sits wrapped in the composer until an Enter arrives, then shows as a
    # submitted turn.
    message = ("⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap all lanes at 4 "
               "workers and hold pushes until chuck drains the merge queue")
    state = {"typed": False, "entered": False}

    def run(cmd):
        if "-l" in cmd:
            state["typed"] = True
        if cmd[-1] == "Enter":
            state["entered"] = True
        return True

    def capture():
        if state["entered"]:
            return f"{message}\n● thinking…\n❯ \n  ddc@host:~/wt"
        if state["typed"]:
            return ("❯ ⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap all lanes at 4\n"
                    "workers and hold pushes until chuck drains the merge queue\n"
                    "  ddc@host:~/wt")
        return "❯ \n  ddc@host:~/wt"                  # idle at t0, empty composer

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
    out = ("%1 worker market-brief-package-73t4 running\n"
           "%2 optimus \n"
           "%3 worker market-brief-package-6izz running\n")
    assert sable_msg.parse_worker_bead_tags(out) == {
        "market-brief-package-73t4": [("%1", "running")],
        "market-brief-package-6izz": [("%3", "running")],
    }


def test_parse_worker_bead_tags_skips_non_worker_roles():
    # a manager pane happening to carry a stray @sable_bead-shaped 3rd field
    # must never be treated as a bead-addressable pane.
    out = "%1 optimus market-brief-package-73t4 running\n"
    assert sable_msg.parse_worker_bead_tags(out) == {}


def test_parse_worker_bead_tags_preserves_duplicate_bead_tags_qq6r():
    # SABLE-qq6r: a REVISE re-spawn into the same worktree creates a fresh
    # pane before the old one is reaped, so two panes legitimately share one
    # @sable_bead tag. Both must survive parsing (not last-wins) so
    # lookup_worker_by_bead can filter by status.
    out = "%26 worker SABLE-pi5m done\n%37 worker SABLE-pi5m running\n"
    assert sable_msg.parse_worker_bead_tags(out) == {
        "SABLE-pi5m": [("%26", "done"), ("%37", "running")],
    }


def test_lookup_worker_by_bead_found_and_missing():
    fake_out = "%1 worker market-brief-package-73t4\n%2 optimus \n"
    runner = lambda args: fake_out
    assert sable_msg.lookup_worker_by_bead("market-brief-package-73t4", runner) == "%1"
    assert sable_msg.lookup_worker_by_bead("ghost-bead", runner) is None


def test_lookup_worker_by_bead_prefers_running_pane_over_done_duplicate_qq6r():
    # THE bead repro: old done-but-unreaped pane %26 and fresh running pane
    # %37 both tagged SABLE-pi5m. Resolution must pick the LIVE one, not
    # whichever the map happened to keep last.
    fake_out = "%26 worker SABLE-pi5m done\n%37 worker SABLE-pi5m running\n"
    runner = lambda args: fake_out
    assert sable_msg.lookup_worker_by_bead("SABLE-pi5m", runner) == "%37"
    # order-independence: the done pane listed second must not win either.
    fake_out_reordered = "%37 worker SABLE-pi5m running\n%26 worker SABLE-pi5m done\n"
    runner2 = lambda args: fake_out_reordered
    assert sable_msg.lookup_worker_by_bead("SABLE-pi5m", runner2) == "%37"


def test_lookup_worker_by_bead_only_done_pane_raises_hint_qq6r():
    # If ONLY a done pane matches, resolving to it would deliver into a dead
    # composer and (from the caller's perspective) silently report success.
    # Fail loudly instead with a reap hint.
    fake_out = "%26 worker SABLE-pi5m done\n"
    runner = lambda args: fake_out
    with pytest.raises(sable_msg.OnlyDonePane) as exc_info:
        sable_msg.lookup_worker_by_bead("SABLE-pi5m", runner)
    assert exc_info.value.bead_id == "SABLE-pi5m"
    assert exc_info.value.pane_id == "%26"


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


def test_main_bead_addressed_only_done_pane_errors_with_reap_hint_qq6r(monkeypatch, capsys):
    # SABLE-qq6r: resolving to a done-but-unreaped duplicate must never report
    # "delivered" — it must fail loudly with a reap hint instead.
    def raise_only_done(bead, run=None, socket=None, session=None):
        raise sable_msg.OnlyDonePane(bead, "%26")

    monkeypatch.setattr(sable_msg, "lookup_worker_by_bead", raise_only_done)
    rc = sable_msg.main(["SABLE-pi5m", "hold", "--from", "optimus", "--bead"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "SABLE-pi5m" in err
    assert "done" in err
    assert "reap" in err.lower()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
