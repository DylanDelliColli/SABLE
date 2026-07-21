#!/usr/bin/env python3
"""Unit tests for the preview-kick poll leg in bin/sable-reconcile-handoffs
(SABLE-jd5fj.2 / S5).

The push-based post-push-merge-notify hook (SABLE-jd5fj.1) fires
`sable-merge-gate preview` on a confirmed worker push so CI is already
computing by the time Chuck wakes. If that kick is missed (hook unwired, races
the ref update, or dies mid-flight), this poll leg re-kicks it on the next
reconcile sweep. These are the UNIT tests — pure predicates plus the
`kick_preview` seam injected via monkeypatch (the same pattern
test_sable_reconcile_handoffs.py uses for `_git`/`_bd`). Real end-to-end
composition (hook + poll racing, hook killed mid-push) lives in
hooks/test/test-event-pair.sh.

Matrix:
  U1-U4  preview_kick_eligible — reuses P1 (unmerged) + P4 (settle window)
         ONLY; never consults P2/P3 (work-bead status / for-chuck corpus).
  U5-U8  attempt_preview_kick — delegates to sable-merge-gate's kick_preview
         with the right args; interprets rc 0 / nonzero / an exception.
  U9-U10 IDEMPOTENCY OVERLAP (both directions) — from the caller's side, a
         hook-already-kicked ref and a freshly-kicked ref are BOTH rc==0 and
         BOTH treated identically (no retry, no distinguishing action) —
         that symmetry is what lets a hook-kicked preview suppress the poll's
         kick and vice versa, entirely inside kick_preview's own shared
         preview_kick_ref key.
"""
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_LOADER = SourceFileLoader(
    "sable_reconcile_handoffs", str(Path(__file__).resolve().parent / "sable-reconcile-handoffs")
)
_SPEC = importlib.util.spec_from_loader("sable_reconcile_handoffs", _LOADER)
smrh = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(smrh)


# ===========================================================================
# preview_kick_eligible — P1 + P4 only, P2/P3 never consulted
# ===========================================================================

def test_U1_unmerged_and_aged_is_eligible():
    eligible, reason = smrh.preview_kick_eligible(rc=1, age_seconds=601.0, threshold_minutes=10.0)
    assert eligible is True
    assert reason == "eligible-for-preview-kick"


def test_U2_merged_branch_not_eligible():
    # P1: branch tip IS an ancestor of the integration branch -> already merged
    eligible, reason = smrh.preview_kick_eligible(rc=0, age_seconds=601.0, threshold_minutes=10.0)
    assert eligible is False
    assert "merged-or-unresolvable" in reason


@pytest.mark.parametrize("rc", [128, 2])
def test_U2b_unresolvable_ancestry_not_eligible(rc):
    # a git error on ancestry is UNRESOLVED, never treated as unmerged
    eligible, reason = smrh.preview_kick_eligible(rc=rc, age_seconds=601.0, threshold_minutes=10.0)
    assert eligible is False
    assert "merged-or-unresolvable" in reason


def test_U3_age_unresolvable_not_eligible():
    eligible, reason = smrh.preview_kick_eligible(rc=1, age_seconds=None, threshold_minutes=10.0)
    assert eligible is False
    assert reason == "age-unresolvable"


@pytest.mark.parametrize("age_s,expected", [
    (601.0, True),   # just over 10min -> eligible
    (600.0, False),  # exactly at threshold -> still settling (strict >, mirrors P4)
    (100.0, False),  # freshly pushed -> too fresh
])
def test_U4_settle_window_boundary(age_s, expected):
    eligible, reason = smrh.preview_kick_eligible(rc=1, age_seconds=age_s, threshold_minutes=10.0)
    assert eligible is expected
    if not expected:
        assert "too-fresh" in reason


def test_U4b_eligibility_ignores_work_bead_and_for_chuck_state():
    # Unlike classify_branch, preview_kick_eligible takes no open_beads / bead
    # status args at all -- P2/P3 are structurally unreachable here, which IS
    # the point: a preview kick fires even with no work bead or an open one.
    import inspect
    params = list(inspect.signature(smrh.preview_kick_eligible).parameters)
    assert params == ["rc", "age_seconds", "threshold_minutes"]


# ===========================================================================
# attempt_preview_kick — delegates to sable-merge-gate's kick_preview
# ===========================================================================

def test_U5_delegates_with_correct_args(monkeypatch):
    calls = []

    def fake_kick(branch, base, repo, remote):
        calls.append((branch, base, repo, remote))
        return 0

    monkeypatch.setattr(smrh, "kick_preview", fake_kick)
    ok, detail = smrh.attempt_preview_kick("/repo", "origin", "wk-x", "trunk")
    assert ok is True
    assert calls == [("wk-x", "trunk", "/repo", "origin")]
    assert "kick-ok" in detail


def test_U6_nonzero_exit_is_not_ok(monkeypatch):
    monkeypatch.setattr(smrh, "kick_preview", lambda *a, **k: 22)
    ok, detail = smrh.attempt_preview_kick("/repo", "origin", "wk-x", "trunk")
    assert ok is False
    assert "kick-exit-22" in detail


def test_U7_exception_is_caught_not_raised(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(smrh, "kick_preview", boom)
    ok, detail = smrh.attempt_preview_kick("/repo", "origin", "wk-x", "trunk")
    assert ok is False
    assert "kick-error" in detail
    assert "network down" in detail


# ===========================================================================
# IDEMPOTENCY OVERLAP (both directions) — U8-U10
# ===========================================================================

def test_U8_hook_already_kicked_reads_as_ok_no_op(monkeypatch):
    # kick_preview's OWN contract (SABLE-jd5fj.1): rc==0 for BOTH "kicked fresh"
    # and "ref already existed" (hook got there first). attempt_preview_kick
    # must not distinguish -> the poll takes no corrective/duplicate action
    # when the hook already won the race.
    monkeypatch.setattr(smrh, "kick_preview", lambda *a, **k: 0)
    ok_hook_first, _ = smrh.attempt_preview_kick("/repo", "origin", "wk-x", "trunk")
    assert ok_hook_first is True


def test_U9_poll_kicked_first_is_symmetric(monkeypatch):
    # The reverse order: the poll wins the race and kicks fresh. Same rc==0,
    # same caller-side handling -- proving the suppression is symmetric and
    # lives entirely inside kick_preview's shared key, not in the caller.
    monkeypatch.setattr(smrh, "kick_preview", lambda *a, **k: 0)
    ok_poll_first, _ = smrh.attempt_preview_kick("/repo", "origin", "wk-x", "trunk")
    assert ok_poll_first is True


def test_U10_repeat_calls_stay_ok_every_time(monkeypatch):
    # A branch polled across several reconcile sweeps before it merges: every
    # call after the first kick must keep reading as a harmless no-op, never
    # an escalating retry/error.
    monkeypatch.setattr(smrh, "kick_preview", lambda *a, **k: 0)
    results = [smrh.attempt_preview_kick("/repo", "origin", "wk-x", "trunk") for _ in range(3)]
    assert all(ok for ok, _ in results)


# ===========================================================================
# reconcile() orchestration — the preview-kick leg is additive, dry-run-safe
# ===========================================================================

def _cp(args, rc, out):
    import subprocess
    return subprocess.CompletedProcess(args, rc, stdout=out, stderr="")


def _drive_reconcile(monkeypatch, *, dry_run, branch="wk-x", rc=1, age=9999.0):
    monkeypatch.setattr(smrh, "resolve_integration_branch", lambda repo: "trunk")
    monkeypatch.setattr(smrh, "list_origin_wk_branches", lambda repo, remote: [branch])
    monkeypatch.setattr(smrh, "open_for_chuck_beads", lambda repo: [])
    monkeypatch.setattr(smrh, "branch_ancestor_rc", lambda *a, **k: rc)
    monkeypatch.setattr(smrh, "branch_tip_age_seconds", lambda *a, **k: age)
    monkeypatch.setattr(smrh, "find_work_bead_status", lambda repo, branch: None)

    kick_calls = []

    def fake_kick(branch_, base_, repo_, remote_):
        kick_calls.append((branch_, base_, repo_, remote_))
        return 0

    monkeypatch.setattr(smrh, "kick_preview", fake_kick)
    monkeypatch.setattr(smrh, "_git", lambda repo, *a, check=False: _cp(a, 0, ""))
    monkeypatch.setattr(smrh, "_bd", lambda repo, *a, check=False: _cp(a, 0, ""))

    rc_out = smrh.reconcile("/repo", "origin", 10.0, dry_run)
    return rc_out, kick_calls


def test_U11_reconcile_kicks_eligible_branch(monkeypatch):
    rc_out, kick_calls = _drive_reconcile(monkeypatch, dry_run=False)
    assert rc_out == 0
    assert kick_calls == [("wk-x", "trunk", "/repo", "origin")]


def test_U12_dry_run_kicks_nothing(monkeypatch):
    rc_out, kick_calls = _drive_reconcile(monkeypatch, dry_run=True)
    assert rc_out == 0
    assert kick_calls == [], "dry-run must attempt zero preview kicks (no side effects)"


def test_U13_merged_branch_not_kicked(monkeypatch):
    rc_out, kick_calls = _drive_reconcile(monkeypatch, dry_run=False, rc=0)
    assert rc_out == 0
    assert kick_calls == []


def test_U14_too_fresh_branch_not_kicked(monkeypatch):
    rc_out, kick_calls = _drive_reconcile(monkeypatch, dry_run=False, age=100.0)
    assert rc_out == 0
    assert kick_calls == []


def test_U15_kick_fires_even_with_no_qualifying_work_bead(monkeypatch):
    # REGRESSION-adjacent: find_work_bead_status is stubbed to None (P2 fails)
    # in _drive_reconcile above -- the stranded predicate would reject this
    # branch (no-qualifying-work-bead), yet the preview-kick leg still fires.
    # This is the "additional leg, not a rewrite" contract made concrete.
    rc_out, kick_calls = _drive_reconcile(monkeypatch, dry_run=False)
    assert rc_out == 0
    assert len(kick_calls) == 1
