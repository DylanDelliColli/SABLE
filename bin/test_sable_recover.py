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


def test_diverged_branch_is_not_classified_ahead():
    """SABLE-su3j3. The live instance: wk-hotswap-runners measured 22 ahead / 1
    behind, so a plain push was guaranteed non-fast-forward — yet the tool called
    it `ahead`, which is the name for "a plain push fast-forwards".

    The whole matrix is re-asserted here, not just the new row. The failure mode
    this test exists to catch is not "DIVERGED is missing" (that shows up
    immediately); it is a later simplification that reaches DIVERGED by breaking
    AHEAD — and a lone diverged assertion cannot tell those apart."""
    # the new row: commits on BOTH sides -> DIVERGED, never AHEAD
    assert rec.classify_push_state(has_origin=True, ahead=22, behind=1) == rec.DIVERGED
    assert rec.classify_push_state(has_origin=True, ahead=1, behind=1) == rec.DIVERGED

    # NEGATIVE CONTROL: origin a strict ancestor is still AHEAD, still pushable
    assert rec.classify_push_state(has_origin=True, ahead=22, behind=0) == rec.AHEAD
    assert rec.classify_push_state(has_origin=True, ahead=3) == rec.AHEAD  # behind defaults 0

    # the rest of the pre-existing matrix is unchanged
    assert rec.classify_push_state(has_origin=True, ahead=0, behind=0) == rec.PUSHED
    assert rec.classify_push_state(has_origin=False, ahead=0, behind=0) == rec.UNPUSHED
    assert rec.classify_push_state(has_origin=False, ahead=5, behind=5) == rec.UNPUSHED
    assert rec.classify_push_state(has_origin=True, ahead=9, behind=9,
                                   detached=True) == rec.DETACHED

    # strictly behind: nothing to push, so not a push candidate
    assert rec.classify_push_state(has_origin=True, ahead=0, behind=4) == rec.PUSHED


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

def _wt(path, branch, dirty=False, has_origin=False, ahead=0, behind=0, detached=False):
    return {"path": path, "branch": branch, "dirty": dirty,
            "has_origin": has_origin, "ahead": ahead, "behind": behind,
            "detached": detached}


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


# --- SABLE-su3j3: a DIVERGED branch is never a push step ---------------------

def test_diverged_branch_never_becomes_a_push_step():
    """The hazard is the PLAN TEXT, not the push. A rejected push fails loudly;
    a plan line that says "push this" is what sends an operator to
    --force-with-lease on published history. So the assertion is about what the
    plan says, and it must also say the counts — a review step that reports
    divergence without reporting how far apart the sides are just sends the
    reader back to the shell."""
    worktrees = [
        _wt("/wt/div", "wk-div", has_origin=True, ahead=22, behind=1),
        # NEGATIVE CONTROL in the same fixture: strict-ancestor origin.
        # Without this row, "never emit a push step" passes trivially.
        _wt("/wt/ahead", "wk-ahead", has_origin=True, ahead=3, behind=0),
    ]
    report = rec.build_report(worktrees, [], [], [], [])

    assert report["worktrees"][0]["push_state"] == rec.DIVERGED
    assert report["worktrees"][1]["push_state"] == rec.AHEAD

    div_steps = [s for s in report["plan"] if s["target"] == "wk-div"]
    assert [s["action"] for s in div_steps] == ["review"]
    detail = div_steps[0]["detail"]
    assert "22" in detail and "1" in detail          # both counts stated
    assert "REJECTED" in detail                       # says what a push would do
    assert "force-with-lease" in detail               # names the trap explicitly

    ahead_steps = [s for s in report["plan"] if s["target"] == "wk-ahead"]
    assert [s["action"] for s in ahead_steps] == ["push"]


def test_apply_fix_refuses_a_diverged_branch_even_if_a_push_step_reaches_it():
    """Defence in depth at the line that actually mutates origin. The plan no
    longer produces this step; the guard is for the edit that reintroduces it."""
    row = {"path": "/wt/div", "branch": "wk-div", "push_state": rec.DIVERGED,
           "dirty": False, "frozen": False}
    report = {"worktrees": [row],
              "plan": [{"order": 1, "action": "push", "target": "wk-div", "detail": "x"}]}
    ok, failed, refused = rec.apply_fix(report)
    assert (ok, failed, refused) == ([], [], ["wk-div"])


# --- SABLE-xgb29: a FROZEN branch is never a push or merge step --------------

def test_frozen_branch_is_excluded_from_push_steps():
    frozen, warnings = rec.parse_frozen_branches(
        "wk-frozen  SABLE-wvxb4  do-not-merge, never force-push\n")
    assert warnings == []

    worktrees = [
        # frozen AND a perfectly ordinary push candidate — clean, unpushed.
        # Freeze has to outrank the git state, not merely agree with it.
        _wt("/wt/frozen", "wk-frozen", has_origin=False, ahead=0),
        # NEGATIVE CONTROL: an identical non-frozen sibling still gets its push.
        _wt("/wt/free", "wk-free", has_origin=False, ahead=0),
    ]
    report = rec.build_report(worktrees, [], [],
                              ["origin/wk-frozen", "origin/wk-free"], [],
                              frozen=frozen)

    assert [s["target"] for s in report["plan"] if s["action"] == "push"] == ["wk-free"]
    # not quietly downgraded to a review either — a frozen branch is not a to-do
    assert [s for s in report["plan"] if s["target"] == "wk-frozen"] == []

    # visible, and citing the ruling bead
    section = {f["branch"]: f for f in report["frozen_branches"]}
    assert section["wk-frozen"]["bead"] == "SABLE-wvxb4"
    assert "do-not-merge" in section["wk-frozen"]["reason"]
    assert report["worktrees"][0]["frozen"] is True
    assert report["worktrees"][1]["frozen"] is False


def test_frozen_branch_is_withheld_from_chucks_merge_list():
    """A branch excluded from the push plan but still queued for a merge has
    only moved the hazard one line down — the live ruling is do-not-MERGE."""
    frozen, _ = rec.parse_frozen_branches("wk-frozen SABLE-wvxb4 do-not-merge\n")
    report = rec.build_report([], [], [],
                              ["origin/wk-frozen", "origin/wk-free"], [],
                              frozen=frozen)
    assert report["unmerged_branches"] == ["origin/wk-free"]
    merge_steps = [s for s in report["plan"] if s["action"] == "merge"]
    assert merge_steps and "wk-frozen" not in merge_steps[0]["detail"]
    # withheld, not vanished
    section = {f["branch"]: f for f in report["frozen_branches"]}
    assert section["wk-frozen"]["held_from_merge_list"] is True


def test_frozen_section_reports_a_freeze_whose_branch_this_sweep_never_saw():
    """The ruling outlives the branch. If the section were built from worktree
    rows it would go silent exactly when the branch stops being visible
    elsewhere, and the freeze would decay back into human memory."""
    frozen, _ = rec.parse_frozen_branches("wk-reaped SABLE-wvxb4 retires unmerged\n")
    report = rec.build_report([], [], [], [], [], frozen=frozen)
    entry = report["frozen_branches"][0]
    assert entry["branch"] == "wk-reaped"
    assert entry["seen"] is False
    assert entry["worktree"] is None


def test_frozen_list_parse_tolerates_absent_and_malformed_file(tmp_path):
    """*** THE FAIL DIRECTION HERE IS THE OPPOSITE OF EVERY OTHER TEST IN THIS
    FILE. *** Elsewhere the tool must refuse to recommend. Here it must refuse to
    RELEASE: an absent or damaged freeze list is not the fact "nothing is
    frozen", and the two must never render identically."""
    # absent -> empty set, no exception, but a LOUD warning
    frozen, warnings = rec.load_frozen_branches(str(tmp_path / "nope.txt"))
    assert frozen == {}
    assert len(warnings) == 1
    assert "NOT FOUND" in warnings[0]
    assert "nope.txt" in warnings[0]          # names the path it looked at

    # blank lines and comments are ignored SILENTLY — they are not damage
    path = tmp_path / "frozen.txt"
    path.write_text(
        "# a comment\n"
        "\n"
        "   \n"
        "   # an indented comment\n"
        "wk-good  SABLE-aaaa  a stated reason\n"
    )
    frozen, warnings = rec.load_frozen_branches(str(path))
    assert list(frozen) == ["wk-good"]
    assert frozen["wk-good"] == {"bead": "SABLE-aaaa", "reason": "a stated reason"}
    assert warnings == []

    # malformed (no ruling bead) -> STILL FROZEN, and warned about. Dropping the
    # line would release a branch a human deliberately wrote down.
    path.write_text("wk-nobead\nwk-good SABLE-aaaa fine\n")
    frozen, warnings = rec.load_frozen_branches(str(path))
    assert set(frozen) == {"wk-nobead", "wk-good"}
    assert frozen["wk-nobead"]["bead"] is None
    assert len(warnings) == 1
    assert "wk-nobead" in warnings[0]

    # and the malformed entry still suppresses the push step
    report = rec.build_report([_wt("/wt/n", "wk-nobead")], [], [], [], [],
                              frozen=frozen)
    assert [s for s in report["plan"] if s["action"] == "push"] == []


def test_warnings_reach_the_report_and_the_rendered_text():
    """A warning nothing surfaces is the silence it was written to prevent."""
    report = rec.build_report([], [], [], [], [], frozen={},
                              warnings=["FROZEN-BRANCH LIST NOT FOUND at /x"])
    assert report["warnings"] == ["FROZEN-BRANCH LIST NOT FOUND at /x"]
    out = rec.render_text(report)
    assert "WARNINGS" in out
    assert "NOT FOUND" in out


def test_apply_fix_refuses_a_frozen_branch_even_if_a_push_step_reaches_it():
    row = {"path": "/wt/f", "branch": "wk-frozen", "push_state": rec.UNPUSHED,
           "dirty": False, "frozen": True}
    report = {"worktrees": [row],
              "plan": [{"order": 1, "action": "push", "target": "wk-frozen", "detail": "x"}]}
    ok, failed, refused = rec.apply_fix(report)
    assert (ok, failed, refused) == ([], [], ["wk-frozen"])


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
