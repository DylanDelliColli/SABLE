#!/usr/bin/env python3
"""Unit tests for bin/sable-worker-status (SABLE-bldh.4).

Done-signal detection + reaping-decision logic. A worker pane carries three
tmux user-options set at spawn / completion: @sable_role=worker, @sable_bead=<id>,
@sable_status=running|done. Reaping is driven by the pane's own done-flag (pure
tmux); the manager separately watches the bead pool for the actual result.
"""
import importlib.util
import json
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
    out = "%1\tworker\tabc-1\trunning\n%2\tworker\tabc-2\tdone\n"
    panes = sws.parse_worker_panes(out)
    assert panes == [
        {"pane": "%1", "bead": "abc-1", "status": "running"},
        {"pane": "%2", "bead": "abc-2", "status": "done"},
    ]


def test_parse_worker_panes_filters_non_workers():
    # lincoln/optimus panes (role != worker) and a role-less pane are skipped
    out = "%1\tlincoln\n%2\toptimus\n%3\tworker\tabc-1\trunning\n%4\n"
    panes = sws.parse_worker_panes(out)
    assert [p["pane"] for p in panes] == ["%3"]


def test_parse_worker_panes_missing_status_defaults_running():
    # a freshly spawned worker may not have set @sable_status yet -- tmux
    # renders the unset placeholder as an empty field, not an absent one
    out = "%5\tworker\tabc-9\t\n"
    panes = sws.parse_worker_panes(out)
    assert panes == [{"pane": "%5", "bead": "abc-9", "status": "running"}]


# --- SABLE-exab: whitespace-split collapsed an EMPTY @sable_bead field,
# shifting every later column left by one -- reproduced live by a producer
# pane (victor) spawned WITHOUT a bead tag, whose status/class/deliverable
# then landed one slot early and made is_done() see the wrong value ---

def test_parse_worker_panes_preserves_empty_bead_field():
    # tmux renders an unset @sable_bead placeholder as nothing between two
    # tabs -- the tab delimiter must not collapse it into its neighbors
    out = "%45\tvictor\t\tdone\tproducer\t/tmp/deliverable.json\n"
    panes = sws.parse_worker_panes(out)
    assert panes == [{
        "pane": "%45", "bead": "", "status": "done",
        "class": "producer", "deliverable": "/tmp/deliverable.json",
    }]


def test_beadless_done_producer_with_valid_deliverable_is_reaped(tmp_path):
    # end-to-end acceptance: a beadless done producer with a valid
    # deliverable must reach reaping_decision with status/class/deliverable
    # intact, not shifted into the bead's empty slot
    deliverable = tmp_path / "d.json"
    deliverable.write_text('{"ok": true}')
    out = f"%45\tvictor\t\tdone\tproducer\t{deliverable}\n"
    panes = sws.parse_worker_panes(out)
    assert panes == [{
        "pane": "%45", "bead": "", "status": "done",
        "class": "producer", "deliverable": str(deliverable),
    }]
    assert sws.reaping_decision(panes) == ["%45"]


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
    runner = lambda args: seen.append(args) or "%1\tworker\tbead-a\trunning\n"
    out = sws.list_workers(None, run=runner, session="sable-alpha")
    assert out and out[0]["bead"] == "bead-a"
    cmd = seen[0]
    assert ["-s", "-t", "sable-alpha"] == cmd[cmd.index("-s"):cmd.index("-s") + 3]
    assert "-a" not in cmd


def test_list_workers_missing_session_is_empty():
    def runner(args):
        raise subprocess.CalledProcessError(1, args)
    assert sws.list_workers(None, run=runner, session="sable-gone") == []


# --- SABLE-tz7h.2: @sable_class filter — worker-name-prefix classification
# replaced by the pane's own @sable_class tag (architecture.json decision 2),
# with a legacy fallback so already-running untagged worker panes (e.g. from
# a not-yet-updated sable-spawn-worker) are never silently dropped ---

def test_parse_worker_panes_includes_producer_tagged_panes():
    out = "%7\tvictor\tbead-v\tdone\tproducer\t/tmp/deliverable.json\n"
    panes = sws.parse_worker_panes(out)
    assert panes == [{
        "pane": "%7", "bead": "bead-v", "status": "done",
        "class": "producer", "deliverable": "/tmp/deliverable.json",
    }]


def test_parse_worker_panes_still_includes_explicit_worker_class():
    out = "%9\tworker\tbead-w\trunning\tworker\n"
    panes = sws.parse_worker_panes(out)
    assert panes == [{"pane": "%9", "bead": "bead-w", "status": "running"}]


def test_parse_worker_panes_excludes_manager_class():
    # a manager (e.g. optimus) always-on loop, explicitly tagged -- must
    # never be surfaced by this tool, regardless of its @sable_status
    out = "%8\toptimus\t-\tdone\tmanager\n"
    panes = sws.parse_worker_panes(out)
    assert panes == []


def test_parse_worker_panes_missing_class_falls_back_to_legacy_worker_rule():
    # @sable_class unset (empty placeholder) -- back-compat: still classified
    # as a worker via the WORKER_ROLE_PREFIX role-name check
    out = "%10\tworker\tbead-legacy\trunning\n"
    panes = sws.parse_worker_panes(out)
    assert panes == [{"pane": "%10", "bead": "bead-legacy", "status": "running"}]


def test_parse_worker_panes_missing_class_non_worker_role_still_skipped():
    # the legacy fallback only rescues worker-prefixed roles; a non-worker
    # role with no class tag is skipped exactly as before this bead
    out = "%11\tsherlock\tbead-s\tdone\n"
    panes = sws.parse_worker_panes(out)
    assert panes == []


def test_parse_worker_panes_lists_unrecognized_class_but_flags_it():
    out = "%12\tsherlock\tbead-x\tdone\tgremlin\n"
    panes = sws.parse_worker_panes(out)
    assert panes == [{
        "pane": "%12", "bead": "bead-x", "status": "done",
        "class": "gremlin", "deliverable": "",
    }]


def test_reaping_decision_never_reaps_unrecognized_class():
    workers = [{"pane": "%12", "bead": "bead-x", "status": "done", "class": "gremlin"}]
    assert sws.reaping_decision(workers) == []


# --- reap decision + detection surface for the SABLE-e6m6 done-no-deliverable
# fail-safe: a done producer with a written, valid-JSON deliverable is reaped
# normally; one without is flagged and left alive ---

def test_deliverable_ok_true_for_valid_json(tmp_path):
    path = tmp_path / "d.json"
    path.write_text('{"ok": true}')
    assert sws.deliverable_ok(str(path)) is True


def test_deliverable_ok_false_for_missing_file(tmp_path):
    assert sws.deliverable_ok(str(tmp_path / "missing.json")) is False


def test_deliverable_ok_false_for_malformed_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json{{{")
    assert sws.deliverable_ok(str(path)) is False


def test_deliverable_ok_false_for_empty_path():
    assert sws.deliverable_ok("") is False


def test_reaping_decision_reaps_done_producer_with_valid_deliverable(tmp_path):
    deliverable = tmp_path / "d.json"
    deliverable.write_text('{"ok": true}')
    workers = [{"pane": "%1", "bead": "b", "status": "done",
                "class": "producer", "deliverable": str(deliverable)}]
    assert sws.reaping_decision(workers) == ["%1"]


def test_reaping_decision_excludes_done_producer_missing_deliverable(tmp_path):
    workers = [{"pane": "%1", "bead": "b", "status": "done", "class": "producer",
                "deliverable": str(tmp_path / "missing.json")}]
    assert sws.reaping_decision(workers) == []


def test_reaping_decision_excludes_done_producer_malformed_deliverable(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    workers = [{"pane": "%1", "bead": "b", "status": "done", "class": "producer",
                "deliverable": str(bad)}]
    assert sws.reaping_decision(workers) == []


def test_reaping_decision_still_reaps_plain_worker_panes():
    # regression guard: bare worker dicts (no "class" key at all) must keep
    # reaping exactly as before this bead
    workers = [{"pane": "%1", "bead": "a", "status": "done"}]
    assert sws.reaping_decision(workers) == ["%1"]


def test_done_no_deliverable_flags_missing_and_malformed(tmp_path):
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("not json")
    workers = [
        {"pane": "%1", "bead": "a", "status": "done", "class": "producer",
         "deliverable": str(tmp_path / "missing.json")},
        {"pane": "%2", "bead": "b", "status": "done", "class": "producer",
         "deliverable": str(bad_json)},
    ]
    flagged = {w["pane"] for w in sws.done_no_deliverable(workers)}
    assert flagged == {"%1", "%2"}


def test_done_no_deliverable_empty_when_deliverable_valid(tmp_path):
    good = tmp_path / "good.json"
    good.write_text("{}")
    workers = [{"pane": "%1", "bead": "a", "status": "done", "class": "producer",
                "deliverable": str(good)}]
    assert sws.done_no_deliverable(workers) == []


def test_done_no_deliverable_ignores_non_producer_and_not_done():
    workers = [
        {"pane": "%1", "bead": "a", "status": "done"},  # plain worker, no class
        {"pane": "%2", "bead": "b", "status": "running", "class": "producer",
         "deliverable": "/nonexistent.json"},  # not done yet
    ]
    assert sws.done_no_deliverable(workers) == []


# --- SABLE-1kbo: is_done() was a bare tag check with no real-completion
# crosscheck and no sampling window -- a worker could be labeled done off
# ONE stale tag (mid-turn-labeled-done), or a live pane could vanish from a
# single listing race (omitted-live-pane). bead_closed / reconcile_samples /
# confirm_done_status / list_workers_confirmed fix both without touching
# reaping_decision's already-correct contract (SABLE-exab) or a producer's
# own deliverable-file completion signal (SABLE-e6m6). ---

def test_bead_closed_true_when_bd_reports_closed():
    def fake_run(args):
        assert args[:2] == ["bd", "show"]
        return json.dumps([{"status": "closed"}])
    assert sws.bead_closed("SABLE-x", run=fake_run) is True


def test_bead_closed_false_when_bd_reports_open():
    def fake_run(args):
        return json.dumps([{"status": "in_progress"}])
    assert sws.bead_closed("SABLE-x", run=fake_run) is False


def test_bead_closed_none_when_bd_unavailable_or_bead_missing():
    # fails OPEN: a lookup that resolves nothing either way must not punish
    # a real done pane (also what keeps every synthetic bead-id fixture
    # elsewhere in this suite/the integration suites from regressing)
    def fake_run(args):
        raise subprocess.CalledProcessError(1, args)
    assert sws.bead_closed("SABLE-x", run=fake_run) is None


def test_bead_closed_none_for_empty_bead():
    assert sws.bead_closed("", run=lambda args: "[]") is None


def test_confirm_done_status_downgrades_done_tag_with_open_bead():
    # the done-crosscheck: tag says done, but bd says the bead is still
    # open -- must not be reported/reaped as done
    def fake_run(args):
        return json.dumps([{"status": "in_progress"}])
    worker = {"pane": "%1", "bead": "SABLE-x", "status": "done"}
    result = sws.confirm_done_status(worker, run=fake_run)
    assert result["status"] == "done-unconfirmed"
    assert sws.is_done(result["status"]) is False


def test_confirm_done_status_trusts_tag_when_bead_confirmed_closed():
    def fake_run(args):
        return json.dumps([{"status": "closed"}])
    worker = {"pane": "%1", "bead": "SABLE-x", "status": "done"}
    assert sws.confirm_done_status(worker, run=fake_run)["status"] == "done"


def test_confirm_done_status_fails_open_when_bd_cannot_resolve_bead():
    def fake_run(args):
        raise subprocess.CalledProcessError(1, args)
    worker = {"pane": "%1", "bead": "bead-fixture", "status": "done"}
    assert sws.confirm_done_status(worker, run=fake_run)["status"] == "done"


def test_confirm_done_status_exempts_producer_class():
    # producers signal completion via their deliverable file (SABLE-e6m6),
    # not a bead -- SABLE-exab established a producer may have NO bead at
    # all, so the bd crosscheck must never apply to them
    def fake_run(args):
        raise AssertionError("bd must never be called for a producer pane")
    worker = {"pane": "%1", "bead": "", "status": "done", "class": "producer",
              "deliverable": "/tmp/x.json"}
    assert sws.confirm_done_status(worker, run=fake_run) == worker


def test_confirm_done_status_ignores_non_done_panes():
    def fake_run(args):
        raise AssertionError("bd must never be called for a non-done pane")
    worker = {"pane": "%1", "bead": "SABLE-x", "status": "running"}
    assert sws.confirm_done_status(worker, run=fake_run) == worker


def test_reconcile_samples_downgrades_stale_first_sample_done():
    # mid-turn-labeled-done: a pane read done in the FIRST sample only (a
    # stale tag from a reused pane's prior occupant) -- the SECOND, fresher
    # sample shows it's actually still running
    first = [{"pane": "%1", "bead": "a", "status": "done"}]
    second = [{"pane": "%1", "bead": "a", "status": "running"}]
    assert sws.reconcile_samples(first, second) == [
        {"pane": "%1", "bead": "a", "status": "running"}
    ]


def test_reconcile_samples_confirms_done_when_both_samples_agree():
    first = [{"pane": "%1", "bead": "a", "status": "done"}]
    second = [{"pane": "%1", "bead": "a", "status": "done"}]
    assert sws.reconcile_samples(first, second) == [
        {"pane": "%1", "bead": "a", "status": "done"}
    ]


def test_reconcile_samples_surfaces_pane_missing_from_second_sample():
    # omitted-live-pane regression: a pane present in the first sample but
    # missing from the second (a listing race, not an actual lifecycle
    # change) must still be surfaced, not silently dropped
    first = [{"pane": "%1", "bead": "a", "status": "running"}]
    second = []
    assert sws.reconcile_samples(first, second) == [
        {"pane": "%1", "bead": "a", "status": "running"}
    ]


def test_reconcile_samples_surfaces_pane_missing_from_first_sample():
    first = []
    second = [{"pane": "%1", "bead": "a", "status": "running"}]
    assert sws.reconcile_samples(first, second) == [
        {"pane": "%1", "bead": "a", "status": "running"}
    ]


def test_list_workers_confirmed_samples_twice_and_sleeps_between():
    calls = []
    sleeps = []

    def fake_run(args):
        calls.append(args)
        return "%1\tworker\tbead-a\tdone\n"

    def fake_sleep(seconds):
        sleeps.append(seconds)

    result = sws.list_workers_confirmed(None, run=fake_run, sleep=fake_sleep, interval=2.5)
    assert sleeps == [2.5]
    list_panes_calls = [c for c in calls if "list-panes" in c]
    assert len(list_panes_calls) == 2  # the two windowed samples
    assert result[0]["bead"] == "bead-a"
    assert result[0]["status"] == "done"  # bd show returns non-JSON here -> fails open


# --- SABLE-yfdn: reap() killed every done pane unconditionally, with no
# awareness of an attached client currently viewing that pane's window --
# kill-pane on a single-pane worker window forcibly yanks the client's view
# mid-observation. protected_windows / pane_location / filter_protected add
# that awareness as a pre-reap filter without touching reaping_decision or
# reap() themselves. ---

def test_parse_client_sessions_strips_and_drops_blank_lines():
    assert sws.parse_client_sessions("w\n\nw2\n") == ["w", "w2"]


def test_parse_client_sessions_empty():
    assert sws.parse_client_sessions("") == []


def test_protected_windows_pairs_session_with_current_window_index():
    def fake_run(args):
        if "list-clients" in args:
            return "w\n"
        if "display-message" in args:
            return "2\n"
        raise AssertionError(f"unexpected call {args}")
    assert sws.protected_windows(None, run=fake_run) == {("w", "2")}


def test_protected_windows_empty_when_no_clients_attached():
    def fake_run(args):
        if "list-clients" in args:
            return ""
        raise AssertionError("should not query a window index with no clients")
    assert sws.protected_windows(None, run=fake_run) == set()


def test_protected_windows_empty_when_list_clients_fails():
    def fake_run(args):
        raise subprocess.CalledProcessError(1, args)
    assert sws.protected_windows(None, run=fake_run) == set()


def test_protected_windows_skips_client_whose_window_cant_be_resolved():
    def fake_run(args):
        if "list-clients" in args:
            return "w\n"
        raise subprocess.CalledProcessError(1, args)
    assert sws.protected_windows(None, run=fake_run) == set()


def test_pane_location_parses_session_and_window_index():
    def fake_run(args):
        return "w\t3\n"
    assert sws.pane_location("%1", None, run=fake_run) == ("w", "3")


def test_pane_location_none_when_pane_gone():
    def fake_run(args):
        raise subprocess.CalledProcessError(1, args)
    assert sws.pane_location("%1", None, run=fake_run) is None


def test_filter_protected_returns_all_when_no_clients_attached():
    def fake_run(args):
        if "list-clients" in args:
            return ""
        raise AssertionError("pane location must not be queried when nothing is protected")
    assert sws.filter_protected(["%1", "%2"], None, run=fake_run) == ["%1", "%2"]


def test_filter_protected_excludes_pane_in_attached_clients_window():
    # a client is attached to session "w", currently viewing window 0. Pane
    # %1 lives in w's window 0 (protected); pane %2 lives in window 1 (not).
    def fake_run(args):
        if args[-3:] == ["list-clients", "-F", "#{client_session}"]:
            return "w\n"
        if "display-message" in args:
            target = args[args.index("-t") + 1]
            if target == "w":
                return "0\n"
            if target == "%1":
                return "w\t0\n"
            if target == "%2":
                return "w\t1\n"
        raise AssertionError(f"unexpected call {args}")

    assert sws.filter_protected(["%1", "%2"], None, run=fake_run) == ["%2"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
