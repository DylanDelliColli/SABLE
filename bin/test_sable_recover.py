#!/usr/bin/env python3
"""Unit tests for bin/sable-recover (SABLE-wwxd).

A classification matrix over synthetic fixture repo states — NO git/tmux/bd.
Every row is a hand-built primitive record (branch/dirty/has_origin/ahead + a
pane dump + a bead snapshot), fed straight into the pure classification layer so
the push-state / owning-bead / stranded / unmerged / plan logic is pinned down
independent of any live environment. The real-composition proof (an actual temp
repo with real worktrees) is the integration variant.
"""
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

_LOADER = SourceFileLoader(
    "sable_recover", str(Path(__file__).resolve().parent / "sable-recover")
)
_SPEC = importlib.util.spec_from_loader("sable_recover", _LOADER)
rec = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(rec)


# --- push-state classification matrix ----------------------------------------

def test_push_state_unpushed_when_no_origin():
    assert rec.classify_push_state(has_origin=False, ahead=0) == rec.UNPUSHED
    # ahead is meaningless without an origin counterpart -> still UNPUSHED
    assert rec.classify_push_state(has_origin=False, ahead=5) == rec.UNPUSHED


def test_push_state_ahead_when_local_has_extra_commits():
    assert rec.classify_push_state(has_origin=True, ahead=3) == rec.AHEAD


def test_push_state_pushed_when_in_sync():
    assert rec.classify_push_state(has_origin=True, ahead=0) == rec.PUSHED


def test_push_state_detached_short_circuits():
    assert rec.classify_push_state(has_origin=True, ahead=9, detached=True) == rec.DETACHED
    assert rec.classify_push_state(has_origin=False, ahead=0, detached=True) == rec.DETACHED


# --- owning-bead resolution --------------------------------------------------

def test_owning_bead_pane_tag_wins():
    # scoped branch: the bead id is NOT in the branch name; only the pane knows
    bead, src = rec.owning_bead("wk-sable-recover", "SABLE-wwxd", ["SABLE-wwxd"])
    assert (bead, src) == ("SABLE-wwxd", "pane")


def test_owning_bead_branch_fallback_recases_id():
    # no pane; sable-spawn-worker lowercased the id into the branch name
    bead, src = rec.owning_bead("wk-sable-wwxd", None, ["SABLE-wwxd", "SABLE-abcd"])
    assert (bead, src) == ("SABLE-wwxd", "branch")


def test_owning_bead_unknown_when_scoped_branch_and_no_pane():
    bead, src = rec.owning_bead("wk-sable-recover", None, ["SABLE-wwxd"])
    assert (bead, src) == (None, None)


def test_owning_bead_non_wk_branch_never_resolves():
    bead, src = rec.owning_bead("tmux-only", None, ["SABLE-wwxd"])
    assert (bead, src) == (None, None)


# --- live pane / stranded claim detection ------------------------------------

def _pane(path, bead, status):
    return {"path": path, "bead": bead, "status": status}


def test_live_pane_beads_only_running_with_bead():
    panes = [
        _pane("/wt/a", "SABLE-aaaa", "running"),
        _pane("/wt/b", "SABLE-bbbb", "done"),      # done -> released, not live
        _pane("/wt/c", "", "running"),              # untagged shell -> ignored
    ]
    assert rec.live_pane_beads(panes) == {"SABLE-aaaa"}


def test_stranded_claim_is_in_progress_with_no_live_pane():
    beads = [
        {"id": "SABLE-live", "title": "has a pane"},
        {"id": "SABLE-dead", "title": "crashed, no pane"},
    ]
    panes = [_pane("/wt/live", "SABLE-live", "running")]
    stranded = rec.stranded_claims(beads, panes)
    assert [b["id"] for b in stranded] == ["SABLE-dead"]


def test_done_pane_does_not_shield_a_claim():
    beads = [{"id": "SABLE-x", "title": "t"}]
    panes = [_pane("/wt/x", "SABLE-x", "done")]  # done pane -> claim IS stranded
    assert [b["id"] for b in rec.stranded_claims(beads, panes)] == ["SABLE-x"]


def test_pane_bead_for_path_matches_root_and_subdir():
    panes = [_pane("/wt/foo/src", "SABLE-foo", "running")]
    assert rec.pane_bead_for_path(panes, "/wt/foo") == "SABLE-foo"
    assert rec.pane_bead_for_path(panes, "/wt/foo/") == "SABLE-foo"
    assert rec.pane_bead_for_path(panes, "/wt/bar") is None


# --- unmerged branch detection -----------------------------------------------

def test_unmerged_is_origin_minus_merged_order_preserving():
    origin = ["origin/wk-a", "origin/wk-b", "origin/wk-c"]
    merged = ["origin/wk-b"]
    assert rec.unmerged_branches(origin, merged) == ["origin/wk-a", "origin/wk-c"]


# --- parsers -----------------------------------------------------------------

def test_parse_worktrees_branch_detached_and_main():
    porcelain = (
        "worktree /home/ddc/dev-env/SABLE\n"
        "HEAD 1111111\n"
        "branch refs/heads/tmux-only\n"
        "\n"
        "worktree /home/ddc/dev-env/wk-foo\n"
        "HEAD 2222222\n"
        "branch refs/heads/wk-foo\n"
        "\n"
        "worktree /home/ddc/dev-env/detached\n"
        "HEAD 3333333\n"
        "detached\n"
    )
    trees = rec.parse_worktrees(porcelain)
    assert [t["branch"] for t in trees] == ["tmux-only", "wk-foo", None]
    assert trees[2]["detached"] is True
    assert trees[0]["path"] == "/home/ddc/dev-env/SABLE"


def test_parse_panes_tab_delimited_empty_bead_keeps_column():
    text = (
        "/wt/a\tSABLE-aaaa\trunning\n"
        "/wt/b\t\trunning\n"          # empty bead field must not shift status
    )
    panes = rec.parse_panes(text)
    assert panes[0] == {"path": "/wt/a", "bead": "SABLE-aaaa", "status": "running"}
    assert panes[1] == {"path": "/wt/b", "bead": "", "status": "running"}


# --- full report assembly + ordered plan -------------------------------------

def _wt(path, branch, dirty=False, has_origin=False, ahead=0, detached=False):
    return {"path": path, "branch": branch, "dirty": dirty,
            "has_origin": has_origin, "ahead": ahead, "detached": detached}


def test_build_report_classifies_three_canonical_states():
    """The exact post-crash trio the integration test builds for real: one
    pushed worktree, one unpushed+dirty worktree, and one stranded claim."""
    worktrees = [
        _wt("/wt/pushed", "wk-pushed", has_origin=True, ahead=0),
        _wt("/wt/dirty", "wk-dirty", dirty=True, has_origin=False, ahead=0),
    ]
    in_progress = [
        {"id": "SABLE-dirty", "title": "the dirty worker"},
        {"id": "SABLE-gone", "title": "stranded — no pane"},
    ]
    panes = [_pane("/wt/dirty", "SABLE-dirty", "running")]
    origin_wk = ["origin/wk-pushed", "origin/wk-dirty"]
    merged_wk = ["origin/wk-dirty"]

    report = rec.build_report(worktrees, in_progress, panes, origin_wk, merged_wk,
                              known_ids=["SABLE-dirty", "SABLE-gone"],
                              base_branch="tmux-only")

    rows = {r["branch"]: r for r in report["worktrees"]}
    assert rows["wk-pushed"]["push_state"] == rec.PUSHED
    assert rows["wk-dirty"]["push_state"] == rec.UNPUSHED
    assert rows["wk-dirty"]["dirty"] is True
    assert rows["wk-dirty"]["bead"] == "SABLE-dirty"
    assert [b["id"] for b in report["stranded_claims"]] == ["SABLE-gone"]
    assert report["unmerged_branches"] == ["origin/wk-pushed"]


def test_plan_is_ordered_push_then_redispatch_then_merge():
    worktrees = [
        _wt("/wt/clean", "wk-clean", has_origin=False, ahead=0),   # clean unpushed -> push
        _wt("/wt/dirty", "wk-dirty", dirty=True, has_origin=True, ahead=2),  # dirty ahead -> review
    ]
    in_progress = [{"id": "SABLE-gone", "title": "stranded"}]
    panes = []  # no panes at all -> the stranded claim survives
    report = rec.build_report(worktrees, in_progress, panes,
                              ["origin/wk-old"], [], base_branch="tmux-only")

    actions = [s["action"] for s in report["plan"]]
    # push/review are order-group 1, redispatch is 2, merge is 3
    assert actions == ["push", "review", "redispatch", "merge"]
    orders = [s["order"] for s in report["plan"]]
    assert orders == sorted(orders)  # non-decreasing group order


def test_clean_state_yields_empty_plan():
    worktrees = [_wt("/wt/ok", "wk-ok", has_origin=True, ahead=0)]
    report = rec.build_report(worktrees, [], [], ["origin/wk-ok"], ["origin/wk-ok"])
    assert report["plan"] == []


def test_dirty_worktree_is_review_not_push():
    worktrees = [_wt("/wt/d", "wk-d", dirty=True, has_origin=False, ahead=0)]
    report = rec.build_report(worktrees, [], [], [], [])
    assert [s["action"] for s in report["plan"]] == ["review"]


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
