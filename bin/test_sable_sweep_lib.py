#!/usr/bin/env python3
"""Unit tests for bin/sable_sweep_lib.py (SABLE-tz7h.4, matrix S1 cases 20-27).

Covers the slicer's disjoint-union property and its N<K / K=1 / empty-export
boundaries, the 10-cap enforced in code (shard_count), completion_check's
three failure shapes, and merge's finding-preservation + shape-match
guarantees. All dependency-injected (`run=`) — no real `bd` or filesystem
beyond pytest's own tmp_path.

Also covers merge()'s count-reconciliation cross-check (SABLE-5s97): a
correct set of shard reports merges clean, and a shard that silently drops
its first or last bead (the exact live failure mode — 3 of 10 shards each
omitted exactly one boundary bead despite a correctly-sized slice file)
raises ShardUnderReportError rather than merging short.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sable_sweep_lib as lib  # noqa: E402


def _beads(n):
    return [{"id": f"B-{i}"} for i in range(n)]


# --- export_snapshot -----------------------------------------------------

def test_export_snapshot_parses_json_list():
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        return json.dumps([{"id": "A"}, {"id": "B"}])

    result = lib.export_snapshot(run=fake_run)
    assert result == [{"id": "A"}, {"id": "B"}]
    assert calls == [["bd", "list", "--json"]]


def test_export_snapshot_appends_scope_args():
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        return "[]"

    lib.export_snapshot(scope_args=["--status=open", "--not-claimed"], run=fake_run)
    assert calls == [["bd", "list", "--json", "--status=open", "--not-claimed"]]


def test_export_snapshot_empty_output_is_empty_list():
    assert lib.export_snapshot(run=lambda cmd: "") == []
    assert lib.export_snapshot(run=lambda cmd: "   ") == []


def test_export_snapshot_wraps_single_object():
    assert lib.export_snapshot(run=lambda cmd: json.dumps({"id": "solo"})) == [{"id": "solo"}]


# --- shard_count: the 10-cap enforced IN CODE (case 24) -------------------

def test_shard_count_caps_at_ten():
    assert lib.shard_count(50) == 10
    assert lib.shard_count(11) == 10
    assert lib.shard_count(10) == 10


def test_shard_count_floors_at_one():
    assert lib.shard_count(0) == 1
    assert lib.shard_count(-5) == 1


def test_shard_count_passes_through_midrange():
    assert lib.shard_count(3) == 3
    assert lib.shard_count(1) == 1


# --- slice: disjoint-union property (case 20) -----------------------------

def test_slice_disjoint_union_property_various_n_and_k():
    for n in (0, 1, 2, 3, 7, 10, 23, 37):
        for k in (1, 2, 3, 5, 10, 15, 100):
            beads = _beads(n)
            slices = lib.slice(beads, k)
            # disjoint: every bead's id appears in exactly one slice
            seen = []
            for s in slices:
                seen.extend(b["id"] for b in s)
            assert sorted(seen) == sorted(b["id"] for b in beads), (n, k)
            assert len(seen) == len(set(seen)), f"duplication at n={n} k={k}"
            # union reconstructs the original set (order-independent)
            assert set(seen) == {b["id"] for b in beads}


def test_slice_preserves_relative_order_within_a_slice():
    beads = _beads(9)
    slices = lib.slice(beads, 3)
    for s in slices:
        ids = [int(b["id"].split("-")[1]) for b in s]
        assert ids == sorted(ids)


# --- slice: N<K boundary (case 21) ----------------------------------------

def test_slice_fewer_beads_than_shards_leaves_empty_slices():
    beads = _beads(3)
    slices = lib.slice(beads, 10)
    assert len(slices) == 10
    non_empty = [s for s in slices if s]
    assert len(non_empty) == 3
    assert sum(len(s) for s in slices) == 3


# --- slice: K=1 boundary (case 22) -----------------------------------------

def test_slice_k_equals_one_returns_single_slice_with_everything():
    beads = _beads(5)
    slices = lib.slice(beads, 1)
    assert len(slices) == 1
    assert slices[0] == beads


# --- slice: empty-export boundary (case 23) --------------------------------

def test_slice_empty_export_returns_k_empty_slices():
    slices = lib.slice([], 4)
    assert slices == [[], [], [], []]


def test_slice_empty_export_k_one():
    assert lib.slice([], 1) == [[]]


# --- slice: shard_count clamping applied inside slice ----------------------

def test_slice_clamps_k_via_shard_count():
    beads = _beads(2)
    slices = lib.slice(beads, 999)
    assert len(slices) == lib.MAX_SHARDS == 10


# --- completion_check: three failure shapes (case 25) ----------------------

def test_completion_check_false_when_missing(tmp_path):
    assert lib.completion_check(tmp_path / "does-not-exist.json") is False


def test_completion_check_false_when_empty(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text("")
    assert lib.completion_check(p) is False


def test_completion_check_false_when_malformed(tmp_path):
    p = tmp_path / "malformed.json"
    p.write_text("{not valid json")
    assert lib.completion_check(p) is False


def test_completion_check_true_when_valid_json(tmp_path):
    p = tmp_path / "good.json"
    p.write_text(json.dumps({"findings": []}))
    assert lib.completion_check(p) is True


def test_completion_check_true_for_valid_json_array(tmp_path):
    p = tmp_path / "arr.json"
    p.write_text("[]")
    assert lib.completion_check(p) is True


# --- merge: preserves every shard finding (case 26) ------------------------

def test_merge_preserves_every_shard_finding():
    shard1 = {"findings": [
        {"bead_id": "B-1", "classification": "valid"},
        {"bead_id": "B-2", "classification": "stale-fixed"},
    ]}
    shard2 = {"findings": [
        {"bead_id": "B-3", "classification": "ambiguous"},
    ]}
    merged = lib.merge([shard1, shard2])
    ids = {f["bead_id"] for f in merged["findings"]}
    assert ids == {"B-1", "B-2", "B-3"}
    assert len(merged["findings"]) == 3


def test_merge_stats_count_each_classification():
    shard = {"findings": [
        {"bead_id": "B-1", "classification": "valid"},
        {"bead_id": "B-2", "classification": "valid"},
        {"bead_id": "B-3", "classification": "stale-fixed", "model_stale": True},
    ]}
    merged = lib.merge([shard])
    assert merged["stats"]["candidates"] == 3
    assert merged["stats"]["valid"] == 2
    assert merged["stats"]["stale-fixed"] == 1
    assert merged["stats"]["model_stale"] == 1
    assert merged["stats"]["ambiguous"] == 0


def test_merge_tolerates_shard_with_no_findings_key():
    merged = lib.merge([{}, {"findings": [{"bead_id": "B-1", "classification": "valid"}]}])
    assert len(merged["findings"]) == 1


def test_merge_empty_shard_list():
    merged = lib.merge([])
    assert merged == {"findings": [], "stats": {
        "candidates": 0, "valid": 0, "reference": 0, "stale-fixed": 0,
        "stale-moved": 0, "description-rotted": 0, "ambiguous": 0,
        "needs-verification-spec": 0, "model_stale": 0,
    }}


# --- merge: shape match regardless of shard count (case 27) ---------------

def test_merge_shape_identical_single_shard_vs_many_shards():
    all_findings = [
        {"bead_id": f"B-{i}", "classification": "valid"} for i in range(6)
    ]
    single_agent = lib.merge([{"findings": all_findings}])

    split = lib.slice(all_findings, 3)
    sharded = lib.merge([{"findings": s} for s in split])

    assert set(single_agent.keys()) == set(sharded.keys())
    assert set(single_agent["stats"].keys()) == set(sharded["stats"].keys())
    assert single_agent["stats"] == sharded["stats"]
    assert {f["bead_id"] for f in single_agent["findings"]} == \
        {f["bead_id"] for f in sharded["findings"]}


# --- merge: count-reconciliation cross-check (SABLE-5s97) ------------------

def test_merge_reconciles_clean_when_every_shard_covers_its_slice():
    beads = _beads(7)
    slices = lib.slice(beads, 3)
    shard_reports = [
        {"findings": [{"bead_id": b["id"], "classification": "valid"} for b in s]}
        for s in slices
    ]
    merged = lib.merge(shard_reports, slices=slices)
    assert merged["stats"]["candidates"] == 7
    assert {f["bead_id"] for f in merged["findings"]} == {b["id"] for b in beads}


def test_merge_raises_when_shard_drops_first_element_of_its_slice():
    beads = _beads(4)
    slices = lib.slice(beads, 2)
    shard0_slice = slices[0]
    assert len(shard0_slice) >= 2, "fixture needs >=2 beads in shard 0 to drop the first"
    short_findings = [
        {"bead_id": b["id"], "classification": "valid"} for b in shard0_slice[1:]
    ]
    shard_reports = [
        {"findings": short_findings},
        {"findings": [{"bead_id": b["id"], "classification": "valid"} for b in slices[1]]},
    ]
    with pytest.raises(lib.ShardUnderReportError) as excinfo:
        lib.merge(shard_reports, slices=slices)
    assert excinfo.value.shard_index == 0
    assert shard0_slice[0]["id"] in excinfo.value.missing_ids


def test_merge_raises_when_shard_drops_last_element_of_its_slice():
    beads = _beads(4)
    slices = lib.slice(beads, 2)
    shard0_slice = slices[0]
    assert len(shard0_slice) >= 2, "fixture needs >=2 beads in shard 0 to drop the last"
    short_findings = [
        {"bead_id": b["id"], "classification": "valid"} for b in shard0_slice[:-1]
    ]
    shard_reports = [
        {"findings": short_findings},
        {"findings": [{"bead_id": b["id"], "classification": "valid"} for b in slices[1]]},
    ]
    with pytest.raises(lib.ShardUnderReportError) as excinfo:
        lib.merge(shard_reports, slices=slices)
    assert excinfo.value.shard_index == 0
    assert shard0_slice[-1]["id"] in excinfo.value.missing_ids


def test_merge_raises_when_a_shard_report_is_entirely_missing():
    beads = _beads(3)
    slices = lib.slice(beads, 2)
    shard_reports = [{"findings": [{"bead_id": b["id"], "classification": "valid"} for b in slices[0]]}]
    with pytest.raises(lib.ShardUnderReportError) as excinfo:
        lib.merge(shard_reports, slices=slices)
    assert excinfo.value.shard_index == 1


def test_merge_skips_reconciliation_when_slices_not_passed():
    # backward-compat: existing callers that don't pass slices are unaffected,
    # even for a report that would otherwise fail reconciliation.
    merged = lib.merge([{"findings": [{"bead_id": "B-1", "classification": "valid"}]}])
    assert merged["stats"]["candidates"] == 1


def test_merge_reconciliation_skips_empty_slices():
    # an empty slice (fewer beads than shards) has nothing to reconcile
    slices = lib.slice(_beads(1), 3)
    shard_reports = [{"findings": [{"bead_id": "B-0", "classification": "valid"}]}, {}, {}]
    merged = lib.merge(shard_reports, slices=slices)
    assert merged["stats"]["candidates"] == 1


def test_reconcile_shard_counts_raises_directly_as_pre_merge_validate_step():
    beads = _beads(2)
    slices = lib.slice(beads, 2)
    shard_reports = [{"findings": []}, {"findings": [{"bead_id": beads[1]["id"], "classification": "valid"}]}]
    with pytest.raises(lib.ShardUnderReportError):
        lib.reconcile_shard_counts(shard_reports, slices)


# --- write_plan: append-notes-only, skips findings with no bead_id --------

def test_write_plan_emits_append_notes_only():
    merged = lib.merge([{"findings": [
        {"bead_id": "B-1", "classification": "valid"},
        {"bead_id": "B-2", "classification": "stale-fixed", "evidence": "fixed in a1b2c3"},
    ]}])
    plan = lib.write_plan(merged)
    assert len(plan) == 2
    for cmd in plan:
        assert cmd[:2] == ["bd", "update"]
        assert "--append-notes" in cmd
        assert "--close" not in cmd
        assert "--add-label" not in cmd
        assert "--set-labels" not in cmd
        assert "--description" not in cmd
    assert "fixed in a1b2c3" in plan[1][-1]


def test_write_plan_skips_findings_without_bead_id():
    merged = {"findings": [{"classification": "valid"}]}
    assert lib.write_plan(merged) == []


def test_write_plan_flags_model_stale_in_note():
    merged = {"findings": [
        {"bead_id": "B-9", "classification": "valid", "model_stale": True},
    ]}
    plan = lib.write_plan(merged)
    assert "[model-stale]" in plan[0][-1]
