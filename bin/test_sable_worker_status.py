#!/usr/bin/env python3
"""Unit tests for bin/sable-worker-status (SABLE-bldh.4).

Done-signal detection + reaping-decision logic. A worker pane carries three
tmux user-options set at spawn / completion: @sable_role=worker, @sable_bead=<id>,
@sable_status=running|done. Reaping is driven by the pane's own done-flag (pure
tmux); the manager separately watches the bead pool for the actual result.
"""
import argparse
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


# --- SABLE-c008: pane %4 (i8kv v1) stayed alive done-unconfirmed after its
# revise-successor %5 was spawned into the SAME worktree -- tarzan had to
# manually kill it to prevent a stray wake into the tree %5 was actively
# editing (the SABLE-nhrb cross-worktree isolation class). A live successor
# pane tagged for the same bead proves the done-unconfirmed pane is stale,
# so reaping_decision must kill it too -- but a LONE done-unconfirmed pane
# (no live sibling) must stay untouched, since its work may still matter. ---

def test_reaping_decision_reaps_done_unconfirmed_superseded_by_live_successor():
    workers = [
        {"pane": "%4", "bead": "SABLE-x", "status": "done-unconfirmed"},
        {"pane": "%5", "bead": "SABLE-x", "status": "running"},
    ]
    assert sws.reaping_decision(workers) == ["%4"]


def test_reaping_decision_spares_lone_done_unconfirmed_pane():
    # regression: no live sibling for the bead -- must NOT be reaped
    workers = [{"pane": "%4", "bead": "SABLE-x", "status": "done-unconfirmed"}]
    assert sws.reaping_decision(workers) == []


def test_reaping_decision_spares_done_unconfirmed_with_unrelated_live_pane():
    # a live pane for a DIFFERENT bead is not a successor -- must not supersede
    workers = [
        {"pane": "%4", "bead": "SABLE-x", "status": "done-unconfirmed"},
        {"pane": "%5", "bead": "SABLE-y", "status": "running"},
    ]
    assert sws.reaping_decision(workers) == []


def test_reaping_decision_still_reaps_confirmed_done_pane():
    # regression: status == "done" (confirmed) reaps exactly as before,
    # independent of the new superseded path
    workers = [{"pane": "%2", "bead": "SABLE-x", "status": "done"}]
    assert sws.reaping_decision(workers) == ["%2"]


def test_reaping_decision_superseded_kill_runs_the_real_path():
    # SABLE-f00o bite-proof: neutering the successor-detection (no live
    # sibling recognized) must turn this RED, not silently pass
    workers = [
        {"pane": "%4", "bead": "SABLE-x", "status": "done-unconfirmed"},
        {"pane": "%5", "bead": "SABLE-x", "status": "running"},
    ]
    live = sws.live_beads(workers)
    assert "SABLE-x" in live
    assert sws.is_superseded(workers[0], live) is True
    assert sws.reaping_decision(workers) == ["%4"]


def test_reaping_decision_never_reaps_superseded_pane_with_unrecognized_class():
    # fail-safe applies to the superseded path too -- an unrecognized class
    # tag must never be reaped, even when superseded
    workers = [
        {"pane": "%4", "bead": "SABLE-x", "status": "done-unconfirmed",
         "class": "mystery"},
        {"pane": "%5", "bead": "SABLE-x", "status": "running"},
    ]
    assert sws.reaping_decision(workers) == []


def test_live_beads_excludes_done_and_done_unconfirmed():
    workers = [
        {"pane": "%1", "bead": "a", "status": "done"},
        {"pane": "%2", "bead": "b", "status": "done-unconfirmed"},
        {"pane": "%3", "bead": "c", "status": "running"},
        {"pane": "%4", "bead": "", "status": "running"},  # beadless: excluded
    ]
    assert sws.live_beads(workers) == {"c"}


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


# --- SABLE-jb3o: the pending-input reap flag previously cited a cross-tracker
# provenance id (market-brief-package-0h8k) in OPERATOR-FACING stderr. `bd show
# market-brief-package-0h8k` cannot resolve it against the SABLE beads DB, so an
# agent reading the reap line chases a dangling ref (optimus did exactly this,
# 2026-07-17). Provenance cites belong in comments (the ones in this module's
# docstrings stay); runtime output must never contain a bare non-SABLE
# bead-id-shaped token a reader would try to `bd show`. ---

def _dangling_bead_ids(text: str) -> list[str]:
    """Bead-id-shaped tokens (>=2 hyphens, final segment mixing letters+digits
    like a real bead suffix -- '0h8k', 'b5ow') that are NOT a SABLE-* id. A
    plain English hyphenated phrase (e.g. this tool's own 'sable-worker-status'
    name) never has a digit-bearing final segment, so it is never mistaken for
    a dangling cross-tracker reference."""
    hits = []
    for token in text.replace("'", " ").replace('"', " ").split():
        token = token.strip(".,:;()[]{}")
        parts = token.split("-")
        if len(parts) < 3:
            continue
        last = parts[-1]
        if any(c.isdigit() for c in last) and any(c.isalpha() for c in last):
            if token.upper() != token or not token.startswith("SABLE-"):
                hits.append(token)
    return hits


def test_reap_pending_input_message_has_no_dangling_cross_tracker_id(capsys):
    def fake_run(args):
        return ""

    def fake_capture(pane):
        return "❯ check the pool for next work"

    sws.reap(["%2"], None, run=fake_run, capture=fake_capture)
    err = capsys.readouterr().err
    assert _dangling_bead_ids(err) == [], (
        f"reap stderr contains a dangling cross-tracker bead id: {err!r}")


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


# --- SABLE-dcw2: panes carried no owner attribution, so any manager's sweep
# saw (and --reap'd) every manager's workers. parse_worker_panes now surfaces
# the @sable_lane tag as "lane", and filter_by_lane scopes a listing to the
# caller's own lane. The lane key is added ONLY when stamped, so every bare-shape
# assertion above (and the reap/sampling logic) is untouched. ---

def test_parse_worker_panes_surfaces_lane_when_stamped():
    # a worker pane spawned by the updated sable-spawn-worker carries a 7th
    # field, @sable_lane -- surfaced as "lane" on the record
    out = "%1\tworker\tbead-a\trunning\t\t\toptimus\n"
    panes = sws.parse_worker_panes(out)
    assert panes == [{"pane": "%1", "bead": "bead-a", "status": "running",
                      "lane": "optimus"}]


def test_parse_worker_panes_omits_lane_key_when_unstamped():
    # a pane spawned before the @sable_lane column (only 4 fields) keeps the
    # exact bare {pane, bead, status} shape -- no "lane" key -- so it stays
    # unattributed and is surfaced only under --all, never silently swept
    out = "%2\tworker\tbead-b\trunning\n"
    panes = sws.parse_worker_panes(out)
    assert panes == [{"pane": "%2", "bead": "bead-b", "status": "running"}]
    assert "lane" not in panes[0]


def test_parse_worker_panes_producer_carries_lane():
    # the lane rides alongside a producer's class/deliverable, added last
    out = "%3\tvictor\tbead-v\tdone\tproducer\t/tmp/d.json\ttarzan\n"
    panes = sws.parse_worker_panes(out)
    assert panes == [{"pane": "%3", "bead": "bead-v", "status": "done",
                      "class": "producer", "deliverable": "/tmp/d.json",
                      "lane": "tarzan"}]


def test_filter_by_lane_keeps_only_matching_lane():
    workers = [
        {"pane": "%1", "bead": "a", "status": "running", "lane": "optimus"},
        {"pane": "%2", "bead": "b", "status": "done", "lane": "tarzan"},
        {"pane": "%3", "bead": "c", "status": "running", "lane": "optimus"},
    ]
    assert sws.filter_by_lane(workers, "optimus") == [workers[0], workers[2]]
    assert sws.filter_by_lane(workers, "tarzan") == [workers[1]]


def test_filter_by_lane_excludes_unattributed_pane():
    # an ownerless pane (no "lane" key) matches NO manager -- it belongs to
    # --all only, never to a lane that can't positively claim it
    workers = [
        {"pane": "%1", "bead": "a", "status": "done"},              # unstamped
        {"pane": "%2", "bead": "b", "status": "done", "lane": "optimus"},
    ]
    assert sws.filter_by_lane(workers, "optimus") == [workers[1]]
    assert sws.filter_by_lane(workers, "tarzan") == []


def test_resolve_view_lane_all_returns_none():
    ns = argparse.Namespace(all=True, mine=False, lane=None)
    assert sws.resolve_view_lane(ns) is None


def test_resolve_view_lane_explicit_lane_wins():
    ns = argparse.Namespace(all=False, mine=False, lane="tarzan")
    assert sws.resolve_view_lane(ns) == "tarzan"


def test_resolve_view_lane_defaults_to_caller_agent_name(monkeypatch):
    monkeypatch.setenv("CLAUDE_AGENT_NAME", "optimus")
    ns = argparse.Namespace(all=False, mine=False, lane=None)
    assert sws.resolve_view_lane(ns) == "optimus"
    # --mine is the explicit spelling of that same default
    ns_mine = argparse.Namespace(all=False, mine=True, lane=None)
    assert sws.resolve_view_lane(ns_mine) == "optimus"


def test_resolve_view_lane_unlaned_caller_falls_back_to_global(monkeypatch):
    # no CLAUDE_AGENT_NAME (the operator by hand) -> global view, not an empty
    # own-lane one
    monkeypatch.delenv("CLAUDE_AGENT_NAME", raising=False)
    ns = argparse.Namespace(all=False, mine=False, lane=None)
    assert sws.resolve_view_lane(ns) is None


# --- SABLE-ita7: a worker pane whose turn was cut off by the Claude Code
# session-rate-limit banner ("hit your session limit ... resets ...") reads
# identically to a busy pane by tag alone (@sable_status=running never
# changes) -- the SABLE-tz7h.4 worker pane sat "running" for ~5 hours after
# hitting the limit, invisible to sable-worker-status the whole time.
# rate_limit_stall/flag_rate_limit_stalls close that gap: capture-pane every
# running pane and flag one showing the banner AND an idle composer
# (pane_ready) as "stalled-rate-limit" -- a banner still sitting in
# scrollback while the composer is NOT ready means the turn is still
# processing, so it stays "running" (detection surface only -- never reaped,
# never auto-nudged). ---

def test_session_limit_reset_extracts_reset_time():
    assert sws.session_limit_reset("You have hit your session limit - resets 2pm") == "2pm"


def test_session_limit_reset_none_when_absent():
    assert sws.session_limit_reset("just ordinary scrollback") is None


def test_rate_limit_stall_returns_reset_when_banner_present_and_composer_ready():
    cap = "turn output\nYou have hit your session limit - resets 2pm\n\n❯"
    assert sws.rate_limit_stall(cap) == "2pm"


def test_rate_limit_stall_none_when_no_banner():
    # busy capture, no banner at all -- stays running
    cap = "turn output\n⠋ Thinking… (esc to interrupt)"
    assert sws.rate_limit_stall(cap) is None


def test_rate_limit_stall_none_when_banner_present_but_composer_not_ready():
    # the banner sits mid-scrollback (an earlier hit that has since resumed),
    # but the composer isn't idle -- a turn is still processing, not stalled
    cap = ("You have hit your session limit - resets 2pm\n"
           "more scrollback since then\n"
           "⠋ Thinking… (esc to interrupt)")
    assert sws.rate_limit_stall(cap) is None


def test_flag_rate_limit_stalls_flags_running_pane_on_banner_and_ready_composer():
    workers = [{"pane": "%1", "bead": "a", "status": "running"}]

    def fake_capture(pane):
        return "turn output\nYou have hit your session limit - resets 2pm\n\n❯"

    result = sws.flag_rate_limit_stalls(workers, None, capture=fake_capture)
    assert result == [{"pane": "%1", "bead": "a", "status": "stalled-rate-limit", "reset": "2pm"}]


def test_flag_rate_limit_stalls_leaves_busy_no_banner_pane_running():
    workers = [{"pane": "%1", "bead": "a", "status": "running"}]

    def fake_capture(pane):
        return "turn output\n⠋ Thinking… (esc to interrupt)"

    assert sws.flag_rate_limit_stalls(workers, None, capture=fake_capture) == workers


def test_flag_rate_limit_stalls_leaves_still_processing_pane_running():
    workers = [{"pane": "%1", "bead": "a", "status": "running"}]

    def fake_capture(pane):
        return ("You have hit your session limit - resets 2pm\n"
                "more scrollback since then\n"
                "⠋ Thinking… (esc to interrupt)")

    assert sws.flag_rate_limit_stalls(workers, None, capture=fake_capture) == workers


def test_flag_rate_limit_stalls_never_captures_done_panes():
    def fake_capture(pane):
        raise AssertionError("must never capture-pane a done pane")

    workers = [{"pane": "%1", "bead": "a", "status": "done"}]
    assert sws.flag_rate_limit_stalls(workers, None, capture=fake_capture) == workers


# --- SABLE-axp0: fleet-wide dialog/overlay liveness probe. A pane parked on an
# interactive dialog or a modal overlay (a numbered selector, a startup gate, or
# a /usage-style panel dismissed with Esc) silently swallows every message sent
# to it — the live incident was chuck's warm MANAGER pane parked ~30min on a
# /usage overlay, absorbing 3 sable-msg attempts, detected only by manual
# capture-pane. The probe reuses the shared overlay_posture classifier
# (dialog_posture + the modal-overlay case), gates it with not-busy so a working
# pane is never flagged, and covers MANAGER panes too (parse_fleet_panes keeps
# them where parse_worker_panes drops them). Detection surface only in v1 —
# reported "dialog-stalled" and alerted loudly, never auto-dismissed or reaped. ---

DIALOG_MENU = (
    "  ? Which option?\n"
    "  > 1. alpha\n"
    "    2. beta\n"
    "  (Use arrow keys, Enter to select)")
USAGE_OVERLAY = (
    "  Current usage\n"
    "  Session:  45%\n"
    "  Weekly:   12%\n"
    "\n"
    "  Esc to close")
EMPTY_COMPOSER = "some prior output\n\n❯"
DIALOG_WHILE_BUSY = (
    # a menu-looking pair of lines WHILE a turn is actively running — the
    # not-busy guard must keep this from flagging as a stall
    "  > 1. alpha\n"
    "    2. beta\n"
    "⠋ Thinking… (esc to interrupt)")


def test_overlay_posture_true_for_numbered_menu():
    assert sws.overlay_posture(DIALOG_MENU) is True


def test_overlay_posture_true_for_usage_style_esc_to_close_overlay():
    # the case dialog_posture MISSES: no numbered menu, no Enter-to-select —
    # only an 'Esc to close' dismiss hint (the chuck /usage repro).
    assert sws.overlay_posture(USAGE_OVERLAY) is True


def test_overlay_posture_false_on_empty_composer():
    assert sws.overlay_posture(EMPTY_COMPOSER) is False


def test_overlay_posture_false_for_busy_esc_to_interrupt_hint():
    # 'esc to interrupt' is the busy-turn marker, NOT an overlay dismiss verb —
    # it must never be mistaken for an overlay.
    assert sws.overlay_posture("⠋ Thinking… (esc to interrupt)\n\n❯") is False


def test_dialog_stall_true_for_idle_dialog():
    assert sws.dialog_stall(DIALOG_MENU) is True


def test_dialog_stall_true_for_idle_usage_overlay():
    assert sws.dialog_stall(USAGE_OVERLAY) is True


def test_dialog_stall_false_on_empty_composer():
    assert sws.dialog_stall(EMPTY_COMPOSER) is False


def test_dialog_stall_false_when_dialog_line_but_pane_is_busy():
    # the not-busy guard: a running turn momentarily painting a dialog-like line
    # is not a stall.
    assert sws.dialog_stall(DIALOG_WHILE_BUSY) is False


def test_parse_fleet_panes_keeps_manager_pane():
    # tab-delimited _FORMAT: pane, role, bead, status, class, deliverable, lane.
    # parse_worker_panes DROPS this row (class=manager); the fleet probe keeps it.
    line = "%3\toptimus\t\t\tmanager\t\toptimus"
    assert sws.parse_fleet_panes(line) == [
        {"pane": "%3", "role": "optimus", "bead": "", "status": "running",
         "class": "manager", "lane": "optimus"}]


def test_parse_fleet_panes_keeps_worker_and_producer():
    lines = ("%1\tworker\tbead-a\trunning\tworker\t\toptimus\n"
             "%2\tvictor\t\tdone\tproducer\t/tmp/d.json\ttarzan")
    panes = sws.parse_fleet_panes(lines)
    assert {p["pane"] for p in panes} == {"%1", "%2"}
    assert panes[1]["class"] == "producer" and panes[1]["role"] == "victor"


def test_parse_fleet_panes_skips_untagged_shell():
    # no @sable_role AND no @sable_class -> a plain terminal, not a fleet pane
    assert sws.parse_fleet_panes("%7\t\t\t\t\t\t") == []


def test_flag_dialog_stalls_flags_stalled_manager_pane():
    # the key fleet-wide extension: a MANAGER pane on an overlay is flagged
    # (parse_worker_panes would never have surfaced it).
    panes = [{"pane": "%9", "role": "chuck", "bead": "", "status": "running",
              "class": "manager", "lane": "chuck"}]
    result = sws.flag_dialog_stalls(panes, None, capture=lambda pane: USAGE_OVERLAY)
    # SABLE-ccxc: the record now carries the matched evidence line so the alert
    # can surface it (the /usage overlay's 'Esc to close' dismiss hint).
    assert result == [{"pane": "%9", "role": "chuck", "bead": "",
                       "status": "dialog-stalled", "class": "manager",
                       "lane": "chuck", "evidence": "Esc to close"}]


def test_flag_dialog_stalls_leaves_normal_composer_pane():
    panes = [{"pane": "%1", "role": "worker", "bead": "a", "status": "running",
              "class": "worker", "lane": "optimus"}]
    assert sws.flag_dialog_stalls(panes, None, capture=lambda pane: EMPTY_COMPOSER) == []


def test_flag_dialog_stalls_leaves_busy_pane_unflagged():
    panes = [{"pane": "%1", "role": "worker", "bead": "a", "status": "running",
              "class": "worker", "lane": "optimus"}]
    assert sws.flag_dialog_stalls(panes, None, capture=lambda pane: DIALOG_WHILE_BUSY) == []


def test_flag_dialog_stalls_never_captures_done_panes():
    def fake_capture(pane):
        raise AssertionError("must never capture-pane a done pane")

    panes = [{"pane": "%1", "role": "worker", "bead": "a", "status": "done",
              "class": "worker", "lane": "optimus"}]
    assert sws.flag_dialog_stalls(panes, None, capture=fake_capture) == []


# --- reaper liveness guard: never reap a live agent we didn't spawn as a worker
# (SABLE-to8m, generalized by SABLE-k8o5) -------------------------------------
# filter_live_agents drops any reap candidate whose LIVE process is an interactive
# claude the reaper did not spawn as a worker — the operator cockpit ('lincoln')
# OR a resumed manager (optimus/tarzan/chuck) — even though its @sable_status=done
# tag (a stale leftover from the worker that previously owned the window) makes it
# look reap-eligible. A genuine done worker carries SABLE_WORKER_PANE=1 and its
# CLAUDE_AGENT_NAME is its owning-manager lane, so the name alone can't tell a done
# worker from a resumed manager — the worker marker is the disambiguator. Driven
# with an injected tmux runner + fake /proc root.

class _CP:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _fake_pid_runner(pane_to_pid):
    """A run(cmd)->_CP that answers #{pane_pid} per pane from `pane_to_pid`."""
    def run(cmd):
        if "#{pane_pid}" in cmd:
            pane = cmd[cmd.index("-t") + 1]
            pid = pane_to_pid.get(pane)
            return _CP(stdout=f"{pid}\n" if pid is not None else "", returncode=0)
        return _CP(returncode=1)
    return run


def _proc_root(tmp_path, pid_to_env):
    """Build a fake /proc. Each value is either an env dict, a bare string
    (shorthand for CLAUDE_AGENT_NAME=<that> with NO worker marker — i.e. a
    resumed cockpit/manager), or None (no agent env at all — a bare shell)."""
    for pid, spec in pid_to_env.items():
        d = tmp_path / str(pid)
        d.mkdir()
        env = {"PATH": "/usr/bin"}
        if isinstance(spec, str):
            env["CLAUDE_AGENT_NAME"] = spec
        elif isinstance(spec, dict):
            env.update(spec)
        entries = [f"{k}={v}".encode() for k, v in env.items()]
        (d / "environ").write_bytes(b"\x00".join(entries) + b"\x00")
    return str(tmp_path)


def _worker(lane):
    """A genuinely-spawned worker's env: its OWNING MANAGER's lane as identity
    plus the SABLE_WORKER_PANE=1 spawn marker (sable-spawn-worker worker_env_args)."""
    return {"CLAUDE_AGENT_NAME": lane, "SABLE_WORKER_PANE": "1"}


def test_filter_live_agents_drops_resumed_cockpit_pane(tmp_path):
    # %1 is a genuinely-done worker owned by optimus (lane identity + worker
    # marker); %2 is a finished worker window the cockpit was resumed into
    # (identity 'lincoln', NO worker marker) still carrying a stale
    # @sable_status=done. Only %1 survives as reap-eligible; the cockpit is spared.
    run = _fake_pid_runner({"%1": 100, "%2": 200})
    proc = _proc_root(tmp_path, {100: _worker("optimus"), 200: "lincoln"})
    kept = sws.filter_live_agents(["%1", "%2"], None, run=run, proc_root=proc)
    assert kept == ["%1"]


def test_filter_live_agents_drops_resumed_manager_pane(tmp_path):
    # SABLE-k8o5: %1 is a genuine done worker OWNED by optimus (CLAUDE_AGENT_NAME
    # 'optimus' + worker marker); %2 is the optimus MANAGER resumed into a sibling
    # finished worker window — the SAME CLAUDE_AGENT_NAME 'optimus', but NO worker
    # marker. Identity alone can't separate them; the worker marker does. The
    # manager must be spared while the real worker stays reap-eligible.
    run = _fake_pid_runner({"%1": 100, "%2": 200})
    proc = _proc_root(tmp_path, {100: _worker("optimus"), 200: "optimus"})
    kept = sws.filter_live_agents(["%1", "%2"], None, run=run, proc_root=proc)
    assert kept == ["%1"]


def test_filter_live_agents_keeps_normal_workers(tmp_path):
    # All candidates are genuine done workers (lane identity + worker marker) ->
    # nothing filtered; a legitimate reap is never blocked.
    run = _fake_pid_runner({"%1": 100, "%2": 101})
    proc = _proc_root(tmp_path, {100: _worker("optimus"), 101: _worker("tarzan")})
    assert sws.filter_live_agents(["%1", "%2"], None, run=run, proc_root=proc) == ["%1", "%2"]


def test_filter_live_agents_keeps_pane_with_no_process_identity(tmp_path):
    # A pane whose process carries no CLAUDE_AGENT_NAME (a bare shell / a pane
    # SABLE did not spawn) has no authority to be spared -> reap proceeds as
    # before (fail-open).
    run = _fake_pid_runner({"%1": 100})
    proc = _proc_root(tmp_path, {100: None})
    assert sws.filter_live_agents(["%1"], None, run=run, proc_root=proc) == ["%1"]


# --- SABLE-tz9f: the SABLE-axp0 dialog/overlay probe false-positived HEALTHY
# manager/worker panes as DIALOG-STALLED — a busy mid-turn pane and idle
# composer panes — because overlay_posture was a SUPERSET classifier that read
# any 2+ numbered lines (ordinary composer chrome / a queued ⟦SABLE-MSG⟧ block)
# as a dialog box, and the not-busy guard missed a working pane whose 'esc to
# interrupt' hint had scrolled out of frame. The remedy text told the operator
# to Esc the pane — which interrupts a live turn (a real near-miss: optimus was
# 4m36s into a turn). The fix requires a POSITIVE selector/dismiss affordance and
# an authoritative busy guard (pane_working). Only a REAL dialog flags. ---

# (a) an idle composer whose transcript/queued area holds a numbered list AND a
# horizontal separator row — the exact chrome the superset misread as a dialog.
IDLE_COMPOSER_WITH_NUMBERED_BLOCK = (
    "⟦SABLE-MSG⟧ from optimus: here is the plan\n"
    "  1. rebase onto tmux-only\n"
    "  2. run the unit + integration tests\n"
    "  3. push your own branch\n"
    "────────────────────────────────────────\n"
    "  ddc@host:~/wk-foo\n"
    "❯")
# (b) a BUSY pane mid-turn ('Crafting… 4m 36s') with a queued numbered SABLE-MSG
# block AND no 'esc to interrupt' in frame — the authoritative busy guard
# (spinner + elapsed timer) must keep it from ever flagging.
BUSY_CRAFTING_WITH_QUEUED_BLOCK = (
    "  ⟦SABLE-MSG⟧ queued: do these steps\n"
    "  1. first\n"
    "  2. second\n"
    "✳ Crafting… (4m 36s · ↑ 2.1k tokens)\n"
    "❯")
# (c) a genuine permission/trust dialog: a bordered selector with a caret option
# row AND an explicit 'Enter to confirm' affordance — the ONE case that flags.
REAL_PERMISSION_DIALOG = (
    "╭─────────────────────────────────────────────╮\n"
    "│ Allow this tool call?                        │\n"
    "│                                              │\n"
    "│ ❯ 1. Yes                                     │\n"
    "│   2. No, and tell Claude what to do          │\n"
    "│                                              │\n"
    "│ Enter to confirm · Esc to reject             │\n"
    "╰─────────────────────────────────────────────╯")


def test_dialog_stall_false_on_idle_composer_with_numbered_block():
    # regression: the superset classifier flagged this (2+ numbered lines).
    assert sws.overlay_posture(IDLE_COMPOSER_WITH_NUMBERED_BLOCK) is False
    assert sws.dialog_stall(IDLE_COMPOSER_WITH_NUMBERED_BLOCK) is False


def test_dialog_stall_false_on_busy_crafting_pane_with_queued_block():
    # the busy-guard near-miss: 'esc to interrupt' is NOT in frame, but the
    # spinner+elapsed status row proves the pane is working — never flag it.
    assert sws.pane_working(BUSY_CRAFTING_WITH_QUEUED_BLOCK) is True
    assert sws.dialog_stall(BUSY_CRAFTING_WITH_QUEUED_BLOCK) is False


def test_dialog_stall_true_only_on_real_permission_dialog():
    assert sws.overlay_posture(REAL_PERMISSION_DIALOG) is True
    assert sws.dialog_stall(REAL_PERMISSION_DIALOG) is True


def test_only_the_real_dialog_flags_across_all_three_fixtures():
    # the bead's core assertion: of (a) idle-composer, (b) busy-crafting,
    # (c) real-dialog, ONLY (c) is a DIALOG-STALL.
    flags = {
        "a": sws.dialog_stall(IDLE_COMPOSER_WITH_NUMBERED_BLOCK),
        "b": sws.dialog_stall(BUSY_CRAFTING_WITH_QUEUED_BLOCK),
        "c": sws.dialog_stall(REAL_PERMISSION_DIALOG),
    }
    assert flags == {"a": False, "b": False, "c": True}


def test_overlay_evidence_returns_matched_snippet():
    # ccxc: the alert must surface the matched line so an operator judges
    # true-vs-false without a manual capture-pane.
    ev = sws.overlay_evidence(REAL_PERMISSION_DIALOG)
    assert ev is not None and "Enter to confirm" in ev
    assert sws.overlay_evidence(IDLE_COMPOSER_WITH_NUMBERED_BLOCK) is None


def test_flag_dialog_stalls_carries_evidence_snippet():
    panes = [{"pane": "%1", "role": "chuck", "bead": "", "status": "running",
              "class": "manager", "lane": "chuck"}]
    result = sws.flag_dialog_stalls(
        panes, None, capture=lambda pane: REAL_PERMISSION_DIALOG)
    assert len(result) == 1
    assert result[0]["status"] == "dialog-stalled"
    assert "Enter to confirm" in result[0]["evidence"]


def test_flag_dialog_stalls_ignores_busy_and_idle_false_positives():
    # a fleet of the two healthy false-positive panes yields ZERO stalls.
    panes = [
        {"pane": "%1", "role": "optimus", "bead": "", "status": "running",
         "class": "manager", "lane": "optimus"},
        {"pane": "%2", "role": "chuck", "bead": "", "status": "running",
         "class": "manager", "lane": "chuck"},
    ]
    caps = {"%1": BUSY_CRAFTING_WITH_QUEUED_BLOCK,
            "%2": IDLE_COMPOSER_WITH_NUMBERED_BLOCK}
    assert sws.flag_dialog_stalls(panes, None, capture=lambda p: caps[p]) == []


def test_pane_working_still_true_for_plain_esc_to_interrupt():
    # pane_working is a SUPERSET of pane_busy: the classic interrupt hint still
    # marks a working pane even without a visible elapsed timer.
    assert sws.pane_working("● doing work\n✻ Thinking… (esc to interrupt)\n❯") is True


def test_pane_working_false_for_idle_dialog():
    # a real idle dialog has neither a spinner nor a running timer.
    assert sws.pane_working(REAL_PERMISSION_DIALOG) is False


# --- SABLE-1g8i: sable-worker-status printed 'no worker panes' (a false-empty)
# while sable-view simultaneously listed a running + a done worker. Root cause
# (reproduced): the SABLE-dcw2 own-lane filter — a manager (tarzan) default view
# whose OWN lane has no panes but ANOTHER lane's are busy. parse_worker_panes
# keeps both worker rows; the divergence is purely the lane scope. Rather than
# undo dcw2 (own-lane scoping is intentional and reap-safe), make the empty-view
# message HONEST: report that the fleet holds worker panes in other lanes and
# point at --all, instead of implying the fleet is idle. ---

def test_parse_worker_panes_keeps_running_and_done_worker():
    # 1g8i unit spec: the parse path is NOT the culprit — a running + a done
    # worker both survive parse (proving the false-empty is downstream, in the
    # lane filter, not here).
    out = "%10\tworker\tSABLE-jfg6.3\trunning\n%9\tworker\tSABLE-done9\tdone\n"
    panes = sws.parse_worker_panes(out)
    assert panes == [
        {"pane": "%10", "bead": "SABLE-jfg6.3", "status": "running"},
        {"pane": "%9", "bead": "SABLE-done9", "status": "done"},
    ]


def test_empty_worker_message_bare_for_global_view():
    # the un-laned/--all view (view_lane is None) keeps the plain message.
    assert sws.empty_worker_message(None, []) == "no worker panes"


def test_empty_worker_message_bare_when_fleet_truly_empty():
    assert sws.empty_worker_message("tarzan", []) == "no worker panes"


# --- SABLE-6xtx: tmux held 5 windows (worker-market-brief-package-{129o,268o,
# wfab0,m7xs9,pjfll}) whose claude process had exited back to zsh; yet
# sable-worker-status printed 'no worker panes' and --reap could not clean
# them. parse_worker_panes classified purely by @sable_role/@sable_class pane
# OPTIONS, so a pane whose tags were never stamped (or were lost) vanished
# from every listing even though the window itself still carried its
# worker-<bead> name (sable-spawn-worker's window_name(), stamped at creation,
# independent of the mutable @sable_role/@sable_class options). The fallback
# below recognizes such a pane as a worker from its window name alone.
#
# An earlier version of this fix ALSO forced status="done" whenever the
# pane's live #{pane_current_command} was a bare shell (reasoning:
# with_lifecycle_flags/SABLE-5v9n has no tag-writer for an untagged pane).
# That broke this suite's own integration fixtures, which simulate a
# "running" worker with a plain `bash --noprofile --norc` pane and never
# launch a real claude process — pane_current_command reads "bash" for the
# pane's entire life, indistinguishable from "claude exited" by that
# heuristic, and the override reaped live-simulated workers wholesale. So
# @sable_status remains the SOLE source of truth for a worker's status; only
# the CLASSIFICATION (is this pane a worker at all) gets the window-name
# fallback. ---

def test_parse_worker_panes_window_name_fallback_when_tags_absent():
    # no @sable_role, no @sable_class -- only the window-name prefix identifies
    # this as a worker pane; its @sable_status tag still drives the status
    out = "%20\t\t\trunning\t\t\t\tworker-market-brief-package-129o\n"
    panes = sws.parse_worker_panes(out)
    assert panes == [{"pane": "%20", "bead": "", "status": "running"}]


def test_parse_worker_panes_window_name_fallback_recognizes_done_zombie():
    # the core incident repro: tags absent (or lost), but the window name
    # alone is enough to surface the pane -- and once its @sable_status tag
    # reads "done" (set by with_lifecycle_flags before the pane returned to
    # its idle shell), --reap can finally collect it
    out = "%21\t\t\tdone\t\t\t\tworker-market-brief-package-268o\n"
    panes = sws.parse_worker_panes(out)
    assert panes == [{"pane": "%21", "bead": "", "status": "done"}]
    assert sws.reaping_decision(panes) == ["%21"]


def test_parse_worker_panes_window_name_fallback_ignores_non_worker_window():
    # empty tags AND a non-worker-prefixed window name -- still skipped, exactly
    # as the pre-existing legacy rule already required
    out = "%22\t\t\trunning\t\t\t\tlincoln\n"
    panes = sws.parse_worker_panes(out)
    assert panes == []


def test_parse_worker_panes_window_name_fallback_defaults_missing_status_running():
    # a just-spawned fallback-classified pane with no @sable_status tag yet
    # still defaults to "running", exactly like the tag-classified path
    out = "%23\t\t\t\t\t\t\tworker-sable-x\n"
    panes = sws.parse_worker_panes(out)
    assert panes == [{"pane": "%23", "bead": "", "status": "running"}]


def test_empty_worker_message_names_other_lanes_when_fleet_nonempty():
    # the false-empty fix: own lane empty, but other lanes hold worker panes.
    other = [
        {"pane": "%10", "bead": "SABLE-jfg6.3", "status": "running", "lane": "optimus"},
        {"pane": "%9", "bead": "SABLE-done9", "status": "done", "lane": "optimus"},
    ]
    msg = sws.empty_worker_message("tarzan", other)
    assert "no worker panes" != msg
    assert "tarzan" in msg
    assert "--all" in msg
    assert "2" in msg  # count of panes in other lanes


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
