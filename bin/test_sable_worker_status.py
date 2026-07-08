#!/usr/bin/env python3
"""Unit tests for bin/sable-worker-status (SABLE-bldh.4).

Done-signal detection + reaping-decision logic. A worker pane carries three
tmux user-options set at spawn / completion: @sable_role=worker, @sable_bead=<id>,
@sable_status=running|done. Reaping is driven by the pane's own done-flag (pure
tmux); the manager separately watches the bead pool for the actual result.
"""
import importlib.util
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_LOADER = SourceFileLoader(
    "sable_worker_status", str(Path(__file__).resolve().parent / "sable-worker-status")
)
_SPEC = importlib.util.spec_from_loader("sable_worker_status", _LOADER)
sws = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(sws)


def test_parse_worker_panes_basic():
    out = "%1 worker abc-1 running\n%2 worker abc-2 done\n"
    panes = sws.parse_worker_panes(out)
    assert panes == [
        {"pane": "%1", "bead": "abc-1", "status": "running"},
        {"pane": "%2", "bead": "abc-2", "status": "done"},
    ]


def test_parse_worker_panes_filters_non_workers():
    # lincoln/optimus panes (role != worker) and a role-less pane are skipped
    out = "%1 lincoln  \n%2 optimus  \n%3 worker abc-1 running\n%4\n"
    panes = sws.parse_worker_panes(out)
    assert [p["pane"] for p in panes] == ["%3"]


def test_parse_worker_panes_missing_status_defaults_running():
    # a freshly spawned worker may not have set @sable_status yet
    out = "%5 worker abc-9 \n"
    panes = sws.parse_worker_panes(out)
    assert panes == [{"pane": "%5", "bead": "abc-9", "status": "running"}]


def test_is_done():
    assert sws.is_done("done") is True
    assert sws.is_done("running") is False
    assert sws.is_done("") is False


def test_reaping_decision_only_done_panes():
    workers = [
        {"pane": "%1", "bead": "a", "status": "running"},
        {"pane": "%2", "bead": "b", "status": "done"},
        {"pane": "%3", "bead": "c", "status": "done"},
    ]
    assert sws.reaping_decision(workers) == ["%2", "%3"]


def test_reaping_decision_empty_when_none_done():
    workers = [{"pane": "%1", "bead": "a", "status": "running"}]
    assert sws.reaping_decision(workers) == []


def test_tmux_base_socket():
    assert sws.tmux_base("sk") == ["tmux", "-L", "sk"]
    assert sws.tmux_base(None) == ["tmux"]


# --- market-brief-package-c0k5: grouped-session duplicate rows crash --reap ---

def test_dedupe_panes_collapses_duplicate_rows():
    """A grouped-session topology (`tmux new-session -t <sess> -s <alias>`)
    makes `tmux list-panes -a` enumerate the SAME physical pane once per
    session alias sharing its window. Dedupe by pane id before reaping."""
    workers = [
        {"pane": "%2", "bead": "b", "status": "done"},
        {"pane": "%3", "bead": "c", "status": "running"},
        {"pane": "%2", "bead": "b", "status": "done"},
        {"pane": "%3", "bead": "c", "status": "running"},
    ]
    assert sws.dedupe_panes(workers) == [
        {"pane": "%2", "bead": "b", "status": "done"},
        {"pane": "%3", "bead": "c", "status": "running"},
    ]


def test_dedupe_panes_empty():
    assert sws.dedupe_panes([]) == []


def test_reap_tolerates_already_dead_pane():
    """The second kill-pane on a duplicate-listed (now-dead) pane must not
    abort the whole reap sweep — it should be skipped, not raised."""
    calls = []

    def fake_run(args):
        calls.append(args)
        if len(calls) == 2:  # second kill-pane call: pane already dead
            raise subprocess.CalledProcessError(1, args)
        return ""

    # explicit no-op capture: this test is about kill-pane tolerance, not
    # pending-input handling, and must not fall through to a REAL tmux
    # capture-pane call against the default socket.
    sws.reap(["%2", "%2", "%3"], None, run=fake_run, capture=lambda pane: "")
    assert len(calls) == 3  # all three attempted despite the middle failure


# --- market-brief-package-0h8k: don't silently kill a done pane holding
# unsubmitted composer input (a misrouted/queued instruction) ---

def test_has_pending_input_true_when_box_nonempty():
    cap = "some scrollback\n❯ check the pool for next work"
    assert sws.has_pending_input(cap) is True


def test_has_pending_input_false_when_box_empty():
    assert sws.has_pending_input("some scrollback\n❯") is False
    assert sws.has_pending_input("some scrollback\n>") is False


def test_has_pending_input_false_when_no_prompt_box_found():
    assert sws.has_pending_input("just scrollback\nno prompt line here") is False


def test_reap_clears_and_flags_pending_input_before_killing():
    calls = []

    def fake_run(args):
        calls.append(args)
        return ""

    def fake_capture(pane):
        return "❯ check the pool for next work" if pane == "%2" else "❯"

    flagged = sws.reap(["%2", "%3"], None, run=fake_run, capture=fake_capture)
    assert flagged == ["%2"]
    assert calls == [
        ["tmux", "send-keys", "-t", "%2", "C-u"],
        ["tmux", "kill-pane", "-t", "%2"],
        ["tmux", "kill-pane", "-t", "%3"],
    ]


# --- market-brief-package-b5ow: the reap flag message must carry the actual
# composer text, not just the pane id — the text IS the evidence for the
# 0h8k misrouted-instruction mystery, and reap() previously destroyed it
# unrecorded via the C-u clear before this fix ---

def test_pending_input_text_returns_box_content():
    assert sws.pending_input_text(
        "scrollback\n❯ check the pool for next work"
    ) == "check the pool for next work"
    assert sws.pending_input_text("scrollback\n> queued instruction") == "queued instruction"


def test_pending_input_text_none_when_empty_or_absent():
    assert sws.pending_input_text("scrollback\n❯") is None
    assert sws.pending_input_text("scrollback\n>") is None
    assert sws.pending_input_text("just scrollback\nno prompt line here") is None


def test_reap_flag_message_includes_pending_text(capsys):
    def fake_run(args):
        return ""

    def fake_capture(pane):
        return "❯ check the pool for next work"

    sws.reap(["%2"], None, run=fake_run, capture=fake_capture)
    err = capsys.readouterr().err
    assert "check the pool for next work" in err


def test_reap_flag_message_truncates_long_pending_text(capsys):
    long_text = "x" * 600

    def fake_run(args):
        return ""

    def fake_capture(pane):
        return f"❯ {long_text}"

    sws.reap(["%2"], None, run=fake_run, capture=fake_capture)
    err = capsys.readouterr().err
    assert "x" * 500 in err
    assert "x" * 501 not in err
    assert "…" in err


def test_list_workers_scopes_to_session_when_given():
    # SABLE-e1e3.3: discovery is per-repo — a session target replaces the
    # server-wide -a listing, so another repo's fleet is never enumerated.
    seen = []
    runner = lambda args: seen.append(args) or "%1 worker bead-a running\n"
    out = sws.list_workers(None, run=runner, session="sable-alpha")
    assert out and out[0]["bead"] == "bead-a"
    cmd = seen[0]
    assert ["-s", "-t", "sable-alpha"] == cmd[cmd.index("-s"):cmd.index("-s") + 3]
    assert "-a" not in cmd


def test_list_workers_missing_session_is_empty():
    def runner(args):
        raise subprocess.CalledProcessError(1, args)
    assert sws.list_workers(None, run=runner, session="sable-gone") == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
