#!/usr/bin/env python3
"""Unit tests for bin/sable-recycle-cockpit (SABLE-twn37, loaded by path — the
CLI has no .py extension).

Covers the dispatch's Fresh-Agent-Test spec: refuse a busy pane, refuse a
missing/stale shift-report bead, and four distinct-and-non-empty loud failure
messages (the silent-instrument contract). Also covers the same-day notes'
required additions found running the manual procedure three times: a fifth
loud outcome (ALREADY-RECYCLED, detected before the idle poll AND re-asserted
immediately before send-keys, since idle alone races a concurrent recycle),
and the composer-already-contains-/clear edge case.

All orchestration tests drive run_recycle() with injected IO fakes — no real
tmux or bd process runs here (that's the integration suite's job).
"""
import importlib.util
from datetime import datetime, timedelta, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

_LOADER = SourceFileLoader(
    "sable_recycle_cockpit", str(Path(__file__).resolve().parent / "sable-recycle-cockpit")
)
_SPEC = importlib.util.spec_from_loader("sable_recycle_cockpit", _LOADER)
src = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(src)


NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)


def _fresh_bead(bead_id="SABLE-shift1", age_seconds=60):
    created = (NOW - timedelta(seconds=age_seconds)).isoformat().replace("+00:00", "Z")
    return {"id": bead_id, "created_at": created}


def _never_called(*_a, **_k):
    raise AssertionError("must not be called on this path")


def _recorder():
    calls = []

    def send(visible):
        calls.append(visible)
    send.calls = calls
    return send


# --- test_refuses_busy_pane --------------------------------------------------

def test_refuses_busy_pane():
    sent = _recorder()
    code, message, sent_flag = src.run_recycle(
        "SABLE-shift1", "%0",
        now=NOW, max_age_seconds=3600, boot_timeout=20,
        bd_show_fn=lambda bid: _fresh_bead(bid),
        capture_history_fn=lambda: "no banner here, ordinary pane content",
        capture_visible_fn=_never_called,
        wait_idle_fn=lambda: False,   # never went idle within the budget
        send_fn=sent,
        poll_boot_fn=_never_called,
    )
    assert code == 3
    assert "busy" in message.lower()
    assert message  # loud, never empty
    assert sent.calls == [], "a busy pane must never receive send-keys"
    assert sent_flag is False


def test_refuses_busy_pane_reasserted_immediately_before_send():
    # Idle poll passes, but the pane is busy again by the time we re-check
    # right before send-keys (SABLE-twn37 notes: idle alone races a fresh
    # turn starting in the gap). Must still refuse, never send.
    sent = _recorder()
    code, message, sent_flag = src.run_recycle(
        "SABLE-shift1", "%0",
        now=NOW, max_age_seconds=3600, boot_timeout=20,
        bd_show_fn=lambda bid: _fresh_bead(bid),
        capture_history_fn=lambda: "no banner here",
        capture_visible_fn=lambda: "  esc to interrupt\n❯ ",
        wait_idle_fn=lambda: True,
        send_fn=sent,
        poll_boot_fn=_never_called,
    )
    assert code == 3
    assert "busy" in message.lower()
    assert sent.calls == []
    assert sent_flag is False


# --- test_refuses_missing_or_stale_shift_report ------------------------------

def test_refuses_missing_or_stale_shift_report():
    sent = _recorder()

    # missing bead
    code, message, sent_flag = src.run_recycle(
        "SABLE-ghost", "%0",
        now=NOW, max_age_seconds=3600, boot_timeout=20,
        bd_show_fn=lambda bid: None,
        capture_history_fn=_never_called,
        capture_visible_fn=_never_called,
        wait_idle_fn=_never_called,
        send_fn=sent,
        poll_boot_fn=_never_called,
    )
    assert code == 1
    assert "SABLE-ghost" in message
    assert sent.calls == []
    assert sent_flag is False

    # stale bead (created well outside the window)
    stale = _fresh_bead("SABLE-old1", age_seconds=7200)
    code, message, sent_flag = src.run_recycle(
        "SABLE-old1", "%0",
        now=NOW, max_age_seconds=3600, boot_timeout=20,
        bd_show_fn=lambda bid: stale,
        capture_history_fn=_never_called,
        capture_visible_fn=_never_called,
        wait_idle_fn=_never_called,
        send_fn=sent,
        poll_boot_fn=_never_called,
    )
    assert code == 2
    assert "SABLE-old1" in message
    assert sent.calls == []
    assert sent_flag is False


def test_refuses_bead_with_no_created_at():
    code, message, _ = src.run_recycle(
        "SABLE-noage", "%0",
        now=NOW, max_age_seconds=3600, boot_timeout=20,
        bd_show_fn=lambda bid: {"id": bid},  # no created_at field at all
        capture_history_fn=_never_called,
        capture_visible_fn=_never_called,
        wait_idle_fn=_never_called,
        send_fn=_never_called,
        poll_boot_fn=_never_called,
    )
    assert code == 2
    assert "unknown" in message.lower()


# --- test_distinct_loud_outcomes_per_failure_path ----------------------------

def test_distinct_loud_outcomes_per_failure_path():
    sent = _recorder()
    no_bead = src.run_recycle(
        "SABLE-a", "%0", now=NOW, max_age_seconds=3600, boot_timeout=20,
        bd_show_fn=lambda bid: None, capture_history_fn=_never_called,
        capture_visible_fn=_never_called, wait_idle_fn=_never_called,
        send_fn=sent, poll_boot_fn=_never_called,
    )
    stale_bead = src.run_recycle(
        "SABLE-b", "%0", now=NOW, max_age_seconds=3600, boot_timeout=20,
        bd_show_fn=lambda bid: _fresh_bead(bid, age_seconds=99999),
        capture_history_fn=_never_called, capture_visible_fn=_never_called,
        wait_idle_fn=_never_called, send_fn=sent, poll_boot_fn=_never_called,
    )
    busy_pane = src.run_recycle(
        "SABLE-c", "%0", now=NOW, max_age_seconds=3600, boot_timeout=20,
        bd_show_fn=lambda bid: _fresh_bead(bid), capture_history_fn=lambda: "plain",
        capture_visible_fn=_never_called, wait_idle_fn=lambda: False,
        send_fn=sent, poll_boot_fn=_never_called,
    )
    boot_not_observed = src.run_recycle(
        "SABLE-d", "%0", now=NOW, max_age_seconds=3600, boot_timeout=20,
        bd_show_fn=lambda bid: _fresh_bead(bid), capture_history_fn=lambda: "plain",
        capture_visible_fn=lambda: "❯ ", wait_idle_fn=lambda: True,
        send_fn=sent, poll_boot_fn=lambda: False,
    )

    outcomes = [no_bead, stale_bead, busy_pane, boot_not_observed]
    codes = [o[0] for o in outcomes]
    messages = [o[1] for o in outcomes]

    assert codes == [1, 2, 3, 4]
    for m in messages:
        assert m, "every failure path must produce a non-empty message"
    assert len(set(messages)) == 4, "all four failure messages must be distinct"
    # only the boot-not-observed path actually sent keys
    assert sent.calls == ["❯ "]


# --- already-recycled: the fifth loud outcome (SABLE-twn37 same-day notes) --

def test_already_recycled_detected_before_polling_and_no_keys_sent():
    sent = _recorder()
    code, message, sent_flag = src.run_recycle(
        "SABLE-shift1", "%0",
        now=NOW, max_age_seconds=3600, boot_timeout=20,
        bd_show_fn=lambda bid: _fresh_bead(bid),
        capture_history_fn=lambda: "Claude Code v2.1.214\n\n❯ ",
        capture_visible_fn=_never_called,
        wait_idle_fn=_never_called,
        send_fn=sent,
        poll_boot_fn=_never_called,
    )
    assert code == 0
    assert "already" in message.lower()
    assert sent.calls == []
    assert sent_flag is False


def test_already_recycled_message_is_distinct_and_nonempty_relative_to_failures():
    msg = src.already_recycled_message("SABLE-shift1", "%0")
    failures = {
        src.no_bead_message("SABLE-x"),
        src.stale_bead_message("SABLE-x", 999999, 3600),
        src.busy_pane_message("%0"),
        src.boot_not_observed_message("%0", 20),
    }
    assert msg
    assert msg not in failures


def test_already_recycled_reasserted_right_before_send_when_poll_races_a_concurrent_clear():
    # The exact near-miss: idle-poll passes, but a concurrent human /clear
    # completed DURING the poll. The re-check immediately before send-keys
    # must catch it and refuse to send, even though the pre-poll capture
    # (captured before the race) looked ordinary.
    sent = _recorder()
    captures = iter([
        "ordinary pane content, no banner",   # pre-poll already-recycled check
        "Claude Code v2.1.214\n\n❯ ",         # re-check right before send: now fresh
    ])
    code, message, sent_flag = src.run_recycle(
        "SABLE-shift1", "%0",
        now=NOW, max_age_seconds=3600, boot_timeout=20,
        bd_show_fn=lambda bid: _fresh_bead(bid),
        capture_history_fn=lambda: next(captures),
        capture_visible_fn=_never_called,
        wait_idle_fn=lambda: True,
        send_fn=sent,
        poll_boot_fn=_never_called,
    )
    assert code == 0
    assert "already" in message.lower()
    assert sent.calls == []
    assert sent_flag is False


# --- pure predicate coverage --------------------------------------------------

def test_already_recycled_true_only_for_lone_banner_with_nothing_above():
    assert src.already_recycled("Claude Code v2.1.214\n\n❯ ") is True
    assert src.already_recycled("  \n\nClaude Code v2.1.214\n❯ ") is True  # blank lines ok


def test_already_recycled_false_when_content_precedes_banner():
    # a banner MENTIONED deep in a transcript, not a real boot
    text = "some earlier turn output\nmore transcript\nClaude Code v2.1.214 was announced\n❯ "
    assert src.already_recycled(text) is False


def test_already_recycled_false_with_no_banner_at_all():
    assert src.already_recycled("just an ordinary idle composer\n❯ ") is False


def test_boot_observed_keys_on_sessionstart_marker():
    assert src.boot_observed("[bd prime] some output\n❯ ") is True
    assert src.boot_observed("ordinary content\n❯ ") is False


def test_composer_has_pending_clear_true_when_unsubmitted():
    cap = "some transcript\n❯ /clear"
    assert src.composer_has_pending_clear(cap) is True


def test_composer_has_pending_clear_false_when_composer_empty():
    cap = "some transcript\n❯ "
    assert src.composer_has_pending_clear(cap) is False


def test_send_clear_sends_enter_only_when_clear_already_pending():
    calls = []
    run = lambda cmd: calls.append(cmd)  # noqa: E731
    base = ["tmux"]
    src._send_clear(base, "%0", run, "some transcript\n❯ /clear")
    assert calls == [["tmux", "send-keys", "-t", "%0", "Enter"]]


def test_send_clear_types_clear_then_enter_when_composer_empty():
    calls = []
    run = lambda cmd: calls.append(cmd)  # noqa: E731
    base = ["tmux"]
    src._send_clear(base, "%0", run, "some transcript\n❯ ")
    assert calls == [
        ["tmux", "send-keys", "-t", "%0", "-l", "/clear"],
        ["tmux", "send-keys", "-t", "%0", "Enter"],
    ]


def test_bead_is_fresh_boundary():
    bead = _fresh_bead(age_seconds=3600)
    assert src.bead_is_fresh(bead, NOW, 3600) is True
    bead_over = _fresh_bead(age_seconds=3601)
    assert src.bead_is_fresh(bead_over, NOW, 3600) is False


def test_success_path_prints_bare_bead_id_for_relay():
    code, message, sent_flag = src.run_recycle(
        "SABLE-shift1", "%0",
        now=NOW, max_age_seconds=3600, boot_timeout=20,
        bd_show_fn=lambda bid: _fresh_bead(bid),
        capture_history_fn=lambda: "plain, not recycled",
        capture_visible_fn=lambda: "❯ ",
        wait_idle_fn=lambda: True,
        send_fn=lambda visible: None,
        poll_boot_fn=lambda: True,
    )
    assert code == 0
    assert message == "SABLE-shift1"
    assert sent_flag is True
