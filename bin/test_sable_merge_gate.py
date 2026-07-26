#!/usr/bin/env python3
"""Unit tests for bin/sable-merge-gate (SABLE-o9aa).

The merge-preview ci-verify gate: build a merge-preview commit, gate it on an
Actions run keyed to the preview SHA, and PROMOTE the byte-identical object on
green (never re-merge). These are the UNIT tests — pure logic + subprocess seams
injected via the SABLE_MG_* env vars / monkeypatched _git. Real composition
against a scratch remote (the three o9aa rehearsals) lives in the integration
variant.
"""
import importlib.util
import re
import subprocess
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_LOADER = SourceFileLoader(
    "sable_merge_gate", str(Path(__file__).resolve().parent / "sable-merge-gate")
)
_SPEC = importlib.util.spec_from_loader("sable_merge_gate", _LOADER)
smg = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(smg)

# SABLE-jd5fj.3 split: the gate now lives in three modules beside the CLI, and
# each seam is patched on the module that DEFINES it (every caller invokes them
# module-qualified, so one patch reaches the whole gate). These aliases are the
# only plumbing change the split required here — no assertion below moved.
classify = smg.classify
git_lib = smg.git_lib
preview_lib = smg.preview_lib
promote_lib = smg.promote_lib


def _no_stored_verdict(repo, ref, preview_sha):
    """No verdict is stored yet, so promote falls through to wait_for_ci — the
    pre-split path these promote cases were written against. Stubbed explicitly
    rather than left to a failing gh call, so the case states which of the two
    verdict sources it is exercising (SABLE-jd5fj.3)."""
    return classify.Verdict("pending", "", preview_sha, ref,
                            source="precomputed", complete=False)


# --- preview_ref_name --------------------------------------------------------

def test_preview_ref_name_basic():
    assert smg.preview_ref_name("market-brief-package-nev0", "abcdef1234") == "ci-verify/market-brief-package-nev0-abcdef1"


def test_preview_ref_name_sanitizes_slash():
    # a stray slash in a bead id must not create a nested ref the flat sweep misses
    assert "/" not in smg.preview_ref_name("weird/bead", "abcdef1234").split("ci-verify/", 1)[1]


def test_preview_ref_name_rejects_short_sha():
    with pytest.raises(ValueError):
        smg.preview_ref_name("bead", "abc")


def test_preview_ref_name_rejects_empty_bead():
    with pytest.raises(ValueError):
        smg.preview_ref_name("///", "abcdef1234")


# --- is_orphan ---------------------------------------------------------------

@pytest.mark.parametrize("age_s,hours,expected", [
    (3600 * 5, 6, False),      # 5h < 6h -> keep
    (3600 * 7, 6, True),       # 7h > 6h -> sweep
    (3600 * 6, 6, False),      # exactly at threshold -> keep (strict >)
])
def test_is_orphan(age_s, hours, expected):
    assert smg.is_orphan(age_s, hours) is expected


# --- qualify_remote_ref: caution-1 regression guard --------------------------
# A stray local branch named `origin` makes bare `origin/<branch>` ambiguous and
# broke a real rev-parse. The gate must ONLY resolve fully-qualified refs.

def test_qualify_remote_ref_basic():
    assert smg.qualify_remote_ref("origin", "tmux-only") == "refs/remotes/origin/tmux-only"


def test_qualify_remote_ref_refuses_already_qualified_branch():
    with pytest.raises(ValueError):
        smg.qualify_remote_ref("origin", "origin/tmux-only")


def test_qualify_remote_ref_refuses_refs_prefix():
    with pytest.raises(ValueError):
        smg.qualify_remote_ref("origin", "refs/heads/tmux-only")


def test_qualify_remote_ref_refuses_slashed_remote():
    with pytest.raises(ValueError):
        smg.qualify_remote_ref("origin/x", "tmux-only")


# --- build_preview: conflict -> exit 22 --------------------------------------

def _fake_git_factory(monkeypatch, *, merge_tree_rc, merge_tree_out="TREEOID\n", commit_out="PREVIEWSHA\n"):
    def fake_git(repo, *args, check=True):
        if args and args[0] == "merge-tree":
            return subprocess.CompletedProcess(args, merge_tree_rc, stdout=merge_tree_out, stderr="")
        if args and args[0] == "commit-tree":
            return subprocess.CompletedProcess(args, 0, stdout=commit_out, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    monkeypatch.setattr(git_lib, "_git", fake_git)


def test_build_preview_conflict_raises_22(monkeypatch):
    _fake_git_factory(monkeypatch, merge_tree_rc=1)
    with pytest.raises(smg.GateError) as ei:
        smg.build_preview("/repo", "BASESHA", "BRANCHSHA", "msg")
    assert ei.value.code == 22


def test_build_preview_clean_returns_commit(monkeypatch):
    _fake_git_factory(monkeypatch, merge_tree_rc=0)
    assert smg.build_preview("/repo", "BASESHA", "BRANCHSHA", "msg") == "PREVIEWSHA"


# --- wait_for_ci: actions-down when no run ever appears ----------------------

def test_wait_for_ci_actions_down(monkeypatch):
    # gh returns an empty run list; with grace=0 the first poll reports actions_down
    def fake_run(argv, cwd=None, check=True, timeout=None):
        return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
    monkeypatch.setattr(git_lib, "_run", fake_run)
    monkeypatch.setenv("SABLE_MG_GRACE", "0")
    monkeypatch.setenv("SABLE_MG_POLL", "0")
    monkeypatch.setenv("SABLE_MG_TIMEOUT", "0")
    conclusion, url = smg.wait_for_ci("/repo", "ci-verify/bead-abcdef1", "PREVIEWSHA")
    assert conclusion == "actions_down"


def test_wait_for_ci_survives_gh_hang(monkeypatch):
    # SABLE-7wyl: sustained-503-class outage where the `gh` subprocess itself
    # hangs (never returns) rather than erroring fast. Must not propagate
    # subprocess.TimeoutExpired and must still converge to a clean park.
    def fake_run(argv, cwd=None, check=True, timeout=None):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
    monkeypatch.setattr(git_lib, "_run", fake_run)
    monkeypatch.setenv("SABLE_MG_GRACE", "0")
    monkeypatch.setenv("SABLE_MG_TIMEOUT", "0")
    monkeypatch.setenv("SABLE_MG_GH_TIMEOUT", "5")
    conclusion, url = smg.wait_for_ci("/repo", "ci-verify/bead-abcdef1", "PREVIEWSHA")
    assert conclusion == "actions_down"


def test_wait_for_ci_mid_flight_hang_times_out_not_hangs(monkeypatch):
    # A run is seen once (mid-verify), then every subsequent gh call hangs —
    # the muw0 incident shape. Must resolve to 'timeout' (not crash, not spin
    # past SABLE_MG_TIMEOUT) using only the per-call SABLE_MG_GH_TIMEOUT budget,
    # never the real wall clock.
    import json
    calls = {"n": 0}

    def fake_run(argv, cwd=None, check=True, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            payload = json.dumps([{"headSha": "PREVIEWSHA", "status": "in_progress",
                                   "conclusion": None, "url": ""}])
            return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    monkeypatch.setattr(git_lib, "_run", fake_run)
    monkeypatch.setenv("SABLE_MG_POLL", "0")
    monkeypatch.setenv("SABLE_MG_GRACE", "0")
    monkeypatch.setenv("SABLE_MG_TIMEOUT", "1")
    monkeypatch.setenv("SABLE_MG_GH_TIMEOUT", "5")
    conclusion, url = smg.wait_for_ci("/repo", "ci-verify/bead-abcdef1", "PREVIEWSHA")
    assert conclusion == "timeout"
    assert calls["n"] <= 3, "should converge in a couple of hung calls, not spin"


def test_wait_for_ci_success(monkeypatch):
    import json
    payload = json.dumps([{"headSha": "PREVIEWSHA", "status": "completed",
                           "conclusion": "success", "url": "http://run/1"}])

    def fake_run(argv, cwd=None, check=True, timeout=None):
        return subprocess.CompletedProcess(argv, 0, stdout=payload, stderr="")
    monkeypatch.setattr(git_lib, "_run", fake_run)
    monkeypatch.setenv("SABLE_MG_POLL", "0")
    conclusion, url = smg.wait_for_ci("/repo", "ci-verify/bead-abcdef1", "PREVIEWSHA")
    assert conclusion == "success"
    assert url == "http://run/1"


# --- F1: tip moved during gate -> non-ff promote -> retryable exit 23 ---------
# post-flip this is a COMMON case (serial merges advance the tip during the CI
# wait), so the non-ff promote push must map to a clean retryable exit, never an
# uncaught CalledProcessError traceback, and cleanup must still run.

def test_promote_tip_moved_non_ff_is_retryable_23(monkeypatch):
    seen = []

    def fake_git(repo, *args, check=True):
        seen.append(args)
        head = args[0] if args else ""
        if head == "merge-tree":
            return subprocess.CompletedProcess(args, 0, stdout="TREEOID\n", stderr="")
        if head == "commit-tree":
            return subprocess.CompletedProcess(args, 0, stdout="PREVIEWSHA\n", stderr="")
        if head == "rev-parse":
            return subprocess.CompletedProcess(args, 0, stdout="SOMESHA\n", stderr="")
        if head == "push" and len(args) >= 3 and args[2].endswith(":refs/heads/trunk"):
            # the promote push: simulate the base having advanced (non-ff reject)
            return subprocess.CompletedProcess(args, 1, stdout="! [rejected] (non-fast-forward)", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(git_lib, "_git", fake_git)
    monkeypatch.setattr(preview_lib, "read_verdict", _no_stored_verdict)
    monkeypatch.setattr(preview_lib, "wait_for_ci", lambda *a, **k: ("success", "http://run/1"))
    monkeypatch.setattr(promote_lib, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(promote_lib, "_append_evidence", lambda *a, **k: None)

    with pytest.raises(smg.GateError) as ei:
        smg.promote("BEAD", "wk-x", "trunk", "/repo", "origin", "mgr", None)
    assert ei.value.code == 23
    # cleanup still ran despite the non-ff exit: a push --delete of the ci-verify ref
    assert any("--delete" in a for a in seen), "ci-verify ref not cleaned up on tip-moved exit"


# --- SABLE-sc24: a CANCELLED run is retryable, never RED ----------------------
# A ci-verify run cancelled mid-flight (concurrent sweep deleting the ref, manual
# cancel, or concurrency-group pre-emption) is not a content defect. promote()
# must map it to the retryable exit 24 (rebuild preview + re-gate), NOT the red
# path's exit 20 with a "fix + re-push" message — there is nothing to fix.

def test_cancelled_conclusion_is_retryable(monkeypatch):
    seen = []

    def fake_git(repo, *args, check=True):
        seen.append(args)
        head = args[0] if args else ""
        if head == "merge-tree":
            return subprocess.CompletedProcess(args, 0, stdout="TREEOID\n", stderr="")
        if head == "commit-tree":
            return subprocess.CompletedProcess(args, 0, stdout="PREVIEWSHA\n", stderr="")
        if head == "rev-parse":
            return subprocess.CompletedProcess(args, 0, stdout="SOMESHA\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(git_lib, "_git", fake_git)
    monkeypatch.setattr(preview_lib, "read_verdict", _no_stored_verdict)
    monkeypatch.setattr(preview_lib, "wait_for_ci", lambda *a, **k: ("cancelled", "http://run/cancelled"))
    notes = []
    monkeypatch.setattr(promote_lib, "_notify", lambda *a, **k: None)
    monkeypatch.setattr(promote_lib, "_append_evidence", lambda repo, bead, note: notes.append(note))

    with pytest.raises(smg.GateError) as ei:
        smg.promote("BEAD", "wk-x", "trunk", "/repo", "origin", "mgr", None)
    # retryable (24), NOT the red path (20)
    assert ei.value.code == 24
    # cleanup still ran: the (possibly already-gone) ci-verify ref is deleted
    assert any("--delete" in a for a in seen), "ci-verify ref not cleaned up on cancelled exit"
    # evidence records a retryable cancellation, not a red-with-fix instruction
    assert notes and "CANCELLED" in notes[0] and "retryable" in notes[0]
    assert not any("RED" in n for n in notes), "cancelled must not record a RED verdict"


# --- SABLE-sc24: sweep must not reap a ref whose Actions run is still in-flight -
# Deleting a live ci-verify ref cancels its GitHub run — the very failure this
# bead fixes. Age alone does not make a ref an orphan.

def _sweep_fake_git(monkeypatch, listing):
    calls = []

    def fake_git(repo, *args, check=True):
        calls.append(args)
        if args and args[0] == "for-each-ref":
            return subprocess.CompletedProcess(args, 0, stdout=listing, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(git_lib, "_git", fake_git)
    return calls


def _sweep_deletes(calls):
    return [a for a in calls if a and a[0] == "push" and "--delete" in a]


def test_sweep_spares_ref_with_inflight_run(monkeypatch):
    # an aged ref (committerdate 0 = 1970) whose run is still in-flight is NOT reaped
    calls = _sweep_fake_git(monkeypatch, "origin/ci-verify/bead-abcdef1 0\n")
    monkeypatch.setattr(preview_lib, "ref_has_inflight_run", lambda repo, ref: True)
    assert smg.sweep("/repo", "origin", 6) == 0
    assert _sweep_deletes(calls) == [], "in-flight ref was wrongly reaped"


def test_sweep_reaps_aged_ref_without_inflight_run(monkeypatch):
    calls = _sweep_fake_git(monkeypatch, "origin/ci-verify/bead-abcdef1 0\n")
    monkeypatch.setattr(preview_lib, "ref_has_inflight_run", lambda repo, ref: False)
    assert smg.sweep("/repo", "origin", 6) == 0
    assert _sweep_deletes(calls) == [("push", "origin", "--delete", "ci-verify/bead-abcdef1")]


def test_sweep_keeps_fresh_ref_regardless_of_run(monkeypatch):
    # a ref younger than the threshold is never even queried for its run status
    import time as _time
    fresh_ts = str(int(_time.time()))
    calls = _sweep_fake_git(monkeypatch, f"origin/ci-verify/bead-abcdef1 {fresh_ts}\n")
    probed = []
    monkeypatch.setattr(preview_lib, "ref_has_inflight_run", lambda repo, ref: probed.append(ref) or False)
    assert smg.sweep("/repo", "origin", 6) == 0
    assert _sweep_deletes(calls) == [], "fresh ref wrongly reaped"
    assert probed == [], "fresh ref should not incur a gh run-status probe"


# --- SABLE-o9b8u: sweep --dry-run reports the blast radius, deletes nothing ---
# The default's safety is a function of how recently the sweep last ran, and the
# command has no memory of that: the identical --max-age-hours can mean 3 refs
# or 232, and the operator has no way to see which before it acts on the shared
# remote. --dry-run is the look-before-you-leap.

def _dry_run_reported(out):
    return set(re.findall(r"would delete (\S+) \(age", out))


def test_dry_run_lists_candidates_and_deletes_nothing(monkeypatch, capsys):
    listing = "origin/ci-verify/old-abcdef1 0\norigin/ci-verify/older-bbbbbb1 100\n"
    calls = _sweep_fake_git(monkeypatch, listing)
    monkeypatch.setattr(preview_lib, "ref_has_inflight_run", lambda repo, ref: False)

    assert smg.sweep("/repo", "origin", 6, dry_run=True) == 0

    assert _sweep_deletes(calls) == [], "dry run must never invoke the delete path"
    out = capsys.readouterr().out
    assert _dry_run_reported(out) == {"ci-verify/old-abcdef1", "ci-verify/older-bbbbbb1"}, (
        "dry run must print exactly the over-threshold refs"
    )
    assert "would be deleted" in out


def test_dry_run_excludes_fresh_and_inflight_refs(monkeypatch, capsys):
    # a dry run that over-reports (lists a ref the real run would spare) is
    # worse than no dry run at all — it manufactures false confidence.
    fresh_ts = str(int(time.time()))
    listing = (
        "origin/ci-verify/old-abcdef1 0\n"
        "origin/ci-verify/inflight-cc1 0\n"
        f"origin/ci-verify/fresh-dddddd1 {fresh_ts}\n"
    )
    _sweep_fake_git(monkeypatch, listing)
    monkeypatch.setattr(preview_lib, "ref_has_inflight_run",
                        lambda repo, ref: ref == "ci-verify/inflight-cc1")

    assert smg.sweep("/repo", "origin", 6, dry_run=True) == 0

    out = capsys.readouterr().out
    assert _dry_run_reported(out) == {"ci-verify/old-abcdef1"}


def test_real_run_deletes_exactly_the_dry_run_set(monkeypatch, capsys):
    # opposite polarity of the two tests above: whatever dry-run reported, a
    # real run against the SAME fixture must delete exactly that set — no
    # more, no less.
    fresh_ts = str(int(time.time()))
    listing = (
        "origin/ci-verify/old-abcdef1 0\n"
        "origin/ci-verify/older-bbbbbb1 100\n"
        f"origin/ci-verify/fresh-dddddd1 {fresh_ts}\n"
    )
    _sweep_fake_git(monkeypatch, listing)
    monkeypatch.setattr(preview_lib, "ref_has_inflight_run", lambda repo, ref: False)
    assert smg.sweep("/repo", "origin", 6, dry_run=True) == 0
    dry_reported = _dry_run_reported(capsys.readouterr().out)

    calls = _sweep_fake_git(monkeypatch, listing)
    monkeypatch.setattr(preview_lib, "ref_has_inflight_run", lambda repo, ref: False)
    assert smg.sweep("/repo", "origin", 6, dry_run=False) == 0
    real_deleted = {a[3] for a in _sweep_deletes(calls)}

    assert dry_reported == real_deleted == {"ci-verify/old-abcdef1", "ci-verify/older-bbbbbb1"}


def test_sweep_reports_count_before_acting_above_threshold(monkeypatch, capsys):
    # a large over-threshold set: the count and age span must be visible
    # BEFORE any deletion is attempted, not just after the fact.
    listing = "\n".join(
        f"origin/ci-verify/bead{i}-abcdef1 {i}" for i in range(50)
    ) + "\n"
    events = []

    def fake_git(repo, *args, check=True):
        if args and args[0] == "for-each-ref":
            return subprocess.CompletedProcess(args, 0, stdout=listing, stderr="")
        if args and args[0] == "push" and "--delete" in args:
            events.append(("delete", args))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(git_lib, "_git", fake_git)
    monkeypatch.setattr(preview_lib, "ref_has_inflight_run", lambda repo, ref: False)

    real_print = print

    def recording_print(*a, **kw):
        events.append(("print", " ".join(str(x) for x in a)))
        real_print(*a, **kw)

    monkeypatch.setattr(preview_lib, "print", recording_print, raising=False)

    assert smg.sweep("/repo", "origin", 6, dry_run=False) == 0

    kinds = [k for k, _ in events]
    first_delete = kinds.index("delete")
    report_lines = [v for k, v in events if k == "print" and "ref(s) match the" in v]
    assert report_lines, "no count/age-span report line was printed"
    # the report line specifically (not just any print) must precede the first delete
    report_index = next(i for i, (k, v) in enumerate(events)
                        if k == "print" and "ref(s) match the" in v)
    assert report_index < first_delete, "count/age-span report must be emitted before any deletion"
    assert "50 ref(s) match" in report_lines[0]
    assert "oldest" in report_lines[0] and "newest" in report_lines[0]


# --- SABLE-sc24: ref_has_inflight_run — the sweep's live-run probe -------------

def _ref_status_run(monkeypatch, *, rc, stdout):
    monkeypatch.setattr(git_lib, "_run",
                        lambda argv, cwd=None, check=True, timeout=None: subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr=""))


def test_ref_has_inflight_run_true_when_not_completed(monkeypatch):
    import json
    _ref_status_run(monkeypatch, rc=0, stdout=json.dumps([{"status": "in_progress"}]))
    assert smg.ref_has_inflight_run("/repo", "ci-verify/bead-abcdef1") is True


def test_ref_has_inflight_run_false_when_completed(monkeypatch):
    import json
    _ref_status_run(monkeypatch, rc=0, stdout=json.dumps([{"status": "completed"}]))
    assert smg.ref_has_inflight_run("/repo", "ci-verify/bead-abcdef1") is False


def test_ref_has_inflight_run_false_when_no_runs(monkeypatch):
    _ref_status_run(monkeypatch, rc=0, stdout="[]")
    assert smg.ref_has_inflight_run("/repo", "ci-verify/bead-abcdef1") is False


def test_ref_has_inflight_run_fail_open_on_gh_error(monkeypatch):
    # gh error -> False (fail-open): an undiscoverable run cannot wedge the sweep
    _ref_status_run(monkeypatch, rc=1, stdout="gh: could not connect")
    assert smg.ref_has_inflight_run("/repo", "ci-verify/bead-abcdef1") is False


def test_ref_has_inflight_run_fail_open_on_gh_hang(monkeypatch):
    # SABLE-7wyl: a hung gh call (same class as wait_for_ci's) must fail open,
    # not propagate subprocess.TimeoutExpired and wedge the sweep.
    def fake_run(argv, cwd=None, check=True, timeout=None):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
    monkeypatch.setattr(git_lib, "_run", fake_run)
    assert smg.ref_has_inflight_run("/repo", "ci-verify/bead-abcdef1") is False


def test_ref_has_inflight_run_fail_open_on_bad_json(monkeypatch):
    _ref_status_run(monkeypatch, rc=0, stdout="not json")
    assert smg.ref_has_inflight_run("/repo", "ci-verify/bead-abcdef1") is False


# --- SABLE-dn7r: post-merge worktree/branch cleanup (GREEN path only) ----------
# At fleet pace worker worktrees + branches re-accumulate (58 in one day) unless
# a green promote reaps them. These drive cleanup_after_merge as a unit via a
# recording fake _git; the destructive-call ORDER is load-bearing (worktree must
# come off before the branch, or git refuses to delete a checked-out branch).

def _cleanup_fake_git(monkeypatch, *, has_worktree=True, dirty=False, branch_exists=True,
                      worktree_remove_rc=0, branch_d_rc=0, cherry_out="", cherry_rc=0,
                      branch_big_d_rc=0, push_delete_rc=0):
    calls = []

    def fake_git(repo, *args, check=True):
        calls.append(args)
        head = args[0] if args else ""
        if head == "worktree" and len(args) >= 2 and args[1] == "list":
            block = ("worktree /main\nHEAD aaa\nbranch refs/heads/other\n\n")
            if has_worktree:
                block += "worktree /wt/wk-x\nHEAD bbb\nbranch refs/heads/wk-x\n"
            return subprocess.CompletedProcess(args, 0, stdout=block, stderr="")
        if head == "status":
            return subprocess.CompletedProcess(args, 0, stdout=("M f.py\n" if dirty else ""), stderr="")
        if head == "worktree" and len(args) >= 2 and args[1] == "remove":
            return subprocess.CompletedProcess(args, worktree_remove_rc, stdout="", stderr="")
        if head == "show-ref":
            return subprocess.CompletedProcess(args, 0 if branch_exists else 1, stdout="", stderr="")
        if head == "branch" and len(args) >= 2 and args[1] == "-d":
            return subprocess.CompletedProcess(args, branch_d_rc,
                                               stdout=("not fully merged" if branch_d_rc else ""), stderr="")
        if head == "cherry":
            return subprocess.CompletedProcess(args, cherry_rc, stdout=cherry_out, stderr="")
        if head == "branch" and len(args) >= 2 and args[1] == "-D":
            return subprocess.CompletedProcess(args, branch_big_d_rc, stdout="", stderr="")
        if head == "push":
            return subprocess.CompletedProcess(args, push_delete_rc, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(git_lib, "_git", fake_git)
    return calls


def _destructive(calls):
    """The gate-mutating verbs, in call order, ignoring read-only probes."""
    out = []
    for a in calls:
        if a[:2] == ("worktree", "remove"):
            out.append("worktree-remove")
        elif a[:2] == ("branch", "-d"):
            out.append("branch-d")
        elif a[:2] == ("branch", "-D"):
            out.append("branch-D")
        elif a and a[0] == "push" and "--delete" in a:
            out.append("push-delete")
    return out


def test_cleanup_removes_worktree_and_branches(monkeypatch):
    # happy path: registered clean worktree, branch -d succeeds -> worktree
    # removed, local branch deleted, remote branch deleted, in that order.
    calls = _cleanup_fake_git(monkeypatch)
    smg.cleanup_after_merge("/repo", "origin", "refs/remotes/origin/trunk", "wk-x")
    assert _destructive(calls) == ["worktree-remove", "branch-d", "push-delete"]


def test_cleanup_refuses_dirty_worktree(monkeypatch, capsys):
    # a dirty worktree aborts the WHOLE cleanup: zero destructive calls, warning,
    # and (in promote) exit stays 0 — uncommitted work is never destroyed.
    calls = _cleanup_fake_git(monkeypatch, dirty=True)
    smg.cleanup_after_merge("/repo", "origin", "refs/remotes/origin/trunk", "wk-x")
    assert _destructive(calls) == []
    assert "DIRTY" in capsys.readouterr().err


def test_cleanup_refuses_unmerged_branch(monkeypatch):
    # branch -d fails AND git cherry shows a genuinely absent commit ('+') -> no
    # -D escalation, and neither the local nor the remote branch is deleted.
    calls = _cleanup_fake_git(monkeypatch, branch_d_rc=1, cherry_out="+ deadbeef\n")
    smg.cleanup_after_merge("/repo", "origin", "refs/remotes/origin/trunk", "wk-x")
    assert _destructive(calls) == ["worktree-remove", "branch-d"]


def test_cleanup_patch_equivalent_branch_deleted(monkeypatch):
    # branch -d fails ancestry but every unique commit is patch-equivalent ('-')
    # -> guarded -D proceeds, then the remote is deleted (wk-git-autopush-hunt).
    calls = _cleanup_fake_git(monkeypatch, branch_d_rc=1, cherry_out="- cca59c2\n")
    smg.cleanup_after_merge("/repo", "origin", "refs/remotes/origin/trunk", "wk-x")
    # the refused -d is a real attempt before the guarded -D escalation
    assert _destructive(calls) == ["worktree-remove", "branch-d", "branch-D", "push-delete"]


# --- SABLE-x9vby: identifier-decay sweep at the branch-name retirement seam ----
# Deleting the remote branch RETIRES the branch name, so any open instruction
# keyed to that name (a hold reading "do not merge wk-foo") goes stale silently
# at exactly that moment. The sweep is advisory: it reports and never gates.

def _fake_sweeper(monkeypatch, *, rc=0, out=""):
    seen = []

    def fake_run(argv, *, cwd, check=True, timeout=None):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")

    monkeypatch.setattr(git_lib, "_run", fake_run)
    return seen


def test_cleanup_sweeps_for_branch_name_decay_before_deleting_the_remote(monkeypatch, capsys):
    calls = _cleanup_fake_git(monkeypatch)
    seen = _fake_sweeper(monkeypatch, out="⚠ identifier-decay: SABLE-hold still names wk-x\n")
    smg.cleanup_after_merge("/repo", "origin", "refs/remotes/origin/trunk", "wk-x")
    assert _destructive(calls) == ["worktree-remove", "branch-d", "push-delete"], \
        "the sweep must not change what cleanup deletes"
    assert seen and "wk-x" in seen[0] and "--branch" in seen[0]
    assert "SABLE-hold" in capsys.readouterr().err


def test_cleanup_never_aborts_when_the_sweeper_is_broken(monkeypatch, capsys):
    """Fail-open on the DECISION, loud on the REPORT: an unrunnable sweep must
    leave the cleanup untouched and must NOT look like a clean sweep."""
    calls = _cleanup_fake_git(monkeypatch)

    def boom(argv, *, cwd, check=True, timeout=None):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(git_lib, "_run", boom)
    smg.cleanup_after_merge("/repo", "origin", "refs/remotes/origin/trunk", "wk-x")
    assert _destructive(calls) == ["worktree-remove", "branch-d", "push-delete"]
    assert "COULD NOT ASSESS" in capsys.readouterr().err


def test_cleanup_sweep_is_silent_when_nothing_references_the_branch(monkeypatch, capsys):
    """Positive control: the seam can stay quiet, so its noise is real signal."""
    _cleanup_fake_git(monkeypatch)
    _fake_sweeper(monkeypatch, out="")
    smg.cleanup_after_merge("/repo", "origin", "refs/remotes/origin/trunk", "wk-x")
    assert capsys.readouterr().err == ""


def test_cleanup_missing_worktree_is_noop(monkeypatch):
    # no registered worktree -> skip (a) entirely, still delete both branches.
    calls = _cleanup_fake_git(monkeypatch, has_worktree=False)
    smg.cleanup_after_merge("/repo", "origin", "refs/remotes/origin/trunk", "wk-x")
    assert _destructive(calls) == ["branch-d", "push-delete"]


# --- SABLE-dtp1: resolve_integration_branch / resolve_base --------------------
# The pre-push hook resolves a repo's integration branch via git config >
# .sable file > env > "main" (hooks/multi-manager/lib-identity.sh's
# sable_resolve_integration_branch). promote() must agree, instead of
# defaulting to the literal 'llm-integration'.

def _fake_git_config(monkeypatch, *, config_val=None, config_rc=1):
    """Fake _git that answers `config --get sable.integrationBranch` and is a
    no-op for anything else (resolve_integration_branch only calls config)."""
    def fake_git(repo, *args, check=True):
        if args[:2] == ("config", "--get"):
            rc = 0 if config_val is not None else config_rc
            return subprocess.CompletedProcess(args, rc, stdout=(config_val or "") + ("\n" if config_val else ""), stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    monkeypatch.setattr(git_lib, "_git", fake_git)


def test_resolve_integration_branch_git_config_wins(monkeypatch, tmp_path):
    _fake_git_config(monkeypatch, config_val="release-line")
    (tmp_path / ".sable").write_text("integrationBranch=sable-file-branch\n")
    assert smg.resolve_integration_branch(str(tmp_path)) == "release-line"


def test_resolve_integration_branch_sable_file_when_no_git_config(monkeypatch, tmp_path):
    _fake_git_config(monkeypatch, config_val=None)
    (tmp_path / ".sable").write_text("integrationBranch=sable-file-branch\n")
    assert smg.resolve_integration_branch(str(tmp_path)) == "sable-file-branch"


def test_resolve_integration_branch_env_when_no_config_or_file(monkeypatch, tmp_path):
    _fake_git_config(monkeypatch, config_val=None)
    monkeypatch.setenv("SABLE_INTEGRATION_BRANCH", "env-branch")
    assert smg.resolve_integration_branch(str(tmp_path)) == "env-branch"


def test_resolve_integration_branch_strips_origin_prefix_from_base_branch_env(monkeypatch, tmp_path):
    _fake_git_config(monkeypatch, config_val=None)
    monkeypatch.delenv("SABLE_INTEGRATION_BRANCH", raising=False)
    monkeypatch.setenv("SABLE_BASE_BRANCH", "origin/legacy-main")
    assert smg.resolve_integration_branch(str(tmp_path)) == "legacy-main"


def test_resolve_integration_branch_defaults_to_main(monkeypatch, tmp_path):
    _fake_git_config(monkeypatch, config_val=None)
    monkeypatch.delenv("SABLE_INTEGRATION_BRANCH", raising=False)
    monkeypatch.delenv("SABLE_BASE_BRANCH", raising=False)
    assert smg.resolve_integration_branch(str(tmp_path)) == "main"


def test_resolve_base_explicit_flag_wins_over_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("SABLE_MG_BASE", "env-base")
    monkeypatch.setattr(git_lib, "resolve_integration_branch", lambda repo: "resolved-base")
    assert smg.resolve_base("flag-base", str(tmp_path)) == "flag-base"


def test_resolve_base_env_wins_when_flag_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("SABLE_MG_BASE", "env-base")
    monkeypatch.setattr(git_lib, "resolve_integration_branch", lambda repo: "resolved-base")
    assert smg.resolve_base(None, str(tmp_path)) == "env-base"


def test_resolve_base_falls_back_to_resolved_integration_branch(monkeypatch, tmp_path):
    monkeypatch.delenv("SABLE_MG_BASE", raising=False)
    monkeypatch.setattr(git_lib, "resolve_integration_branch", lambda repo: "resolved-base")
    assert smg.resolve_base(None, str(tmp_path)) == "resolved-base"

