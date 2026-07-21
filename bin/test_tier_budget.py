#!/usr/bin/env python3
"""Unit tests for sable_gate_budget_lib (SABLE-cmar4.4, columbo cmar4 S2 cases).

Everything here is unit-level: no repo, no real bd, no real test-tiers.sh
subprocess — bd is stubbed by monkeypatching the module's own bd calls
(find_open_budget_bead / file_budget_bead_once directly, the same seam
sable_snapshot_lib's classifier tests stub at). Real composition (real
sandboxed bd, real .github/ci/test-tiers.sh subprocess reads) lives in
hooks/test/test-tier-budget-bead.sh.

Three case groups, matching the columbo test spec:
  1. BREACH DETECTION AT THE BOUNDARY — breach() is strict-'>', mirroring the
     is_orphan/age_exceeds_threshold convention elsewhere in this fleet.
  2. SPLIT-INVARIANCE — the check only ever sees a per-tier-TOTAL duration; it
     is mechanically incapable of caring how that total was decomposed, which
     is the property the locked design (never per-test) is supposed to buy.
  3. UNDER-BUDGET NEGATIVE CASE — no breach means no WARN and no bd call at
     all (not "a bd call that happens to find nothing to file").

Plus the idempotency contract (key derivation, and check_and_file's
file-once-per-key orchestration against a stubbed bd layer) and the
never-raises contract (a budget check must not fail a green promotion).
"""
from __future__ import annotations

import sable_gate_budget_lib as budget


# ---------------------------------------------------------------------------
# 1. Breach detection at the boundary
# ---------------------------------------------------------------------------

def test_breach_false_exactly_at_budget():
    assert budget.breach(900.0, 900.0) is False


def test_breach_true_one_second_over_budget():
    assert budget.breach(901.0, 900.0) is True


def test_breach_false_well_under_budget():
    assert budget.breach(0.0, 900.0) is False


# ---------------------------------------------------------------------------
# 2. Split-invariance: only the TOTAL is ever inspected, never a
#    decomposition of it. Splitting a slow tier into N faster-looking pieces
#    cannot evade a breach, because breach() never sees the pieces at all.
# ---------------------------------------------------------------------------

def test_split_invariant_many_fast_pieces_summing_over_budget_still_breach():
    budget_sec = 900.0
    # Ten sub-durations, each individually well under a naive "budget / N"
    # per-piece allowance, that sum to something over the TIER's total budget.
    pieces = [95.0] * 10  # sum = 950 > 900
    total = sum(pieces)
    assert budget.breach(total, budget_sec) is True
    # A per-test check dividing the same budget across the same N pieces would
    # have missed this: each piece (95s) is under budget/N (90s)... no, it
    # would also have flagged 95 > 90. Use pieces that are each under even a
    # generous per-piece share to make the contrast concrete.
    generous_per_piece = budget_sec  # a (wrong) per-test check re-using the
                                     # WHOLE tier budget as each test's own
                                     # ceiling would never flag any one piece.
    assert all(p <= generous_per_piece for p in pieces), (
        "fixture invariant broken: every piece must individually clear a "
        "per-test check for this case to demonstrate anything")
    # The TOTAL-based check catches what a per-test check structurally cannot.


def test_split_invariant_same_total_breaches_regardless_of_decomposition():
    budget_sec = 900.0
    one_test_total = 1000.0
    ten_test_total = sum([100.0] * 10)
    assert one_test_total == ten_test_total
    assert budget.breach(one_test_total, budget_sec) == budget.breach(ten_test_total, budget_sec) is True


# ---------------------------------------------------------------------------
# 3. Under-budget negative case: no WARN, no bd call, nothing filed.
# ---------------------------------------------------------------------------

def test_under_budget_check_and_file_does_not_touch_bd(monkeypatch, capsys):
    monkeypatch.setattr(budget, "tier_budget_sec", lambda repo, tier: 900.0)

    def _boom(*a, **kw):
        raise AssertionError("check_and_file touched bd on an under-budget run")
    monkeypatch.setattr(budget, "file_budget_bead_once", _boom)

    result = budget.check_and_file("/repo", "merge_preview", 500.0)
    assert result == {"checked": True, "breached": False, "budget_sec": 900.0}
    assert "WARN" not in capsys.readouterr().err


def test_unresolvable_budget_skips_the_check_entirely(monkeypatch):
    monkeypatch.setattr(budget, "tier_budget_sec", lambda repo, tier: None)

    def _boom(*a, **kw):
        raise AssertionError("check_and_file touched bd with no resolvable budget")
    monkeypatch.setattr(budget, "file_budget_bead_once", _boom)

    result = budget.check_and_file("/repo", "merge_preview", 99999.0)
    assert result == {"checked": False, "reason": "budget-unresolvable"}


# ---------------------------------------------------------------------------
# Idempotency key: the budget VALUE is its own version.
# ---------------------------------------------------------------------------

def test_budget_key_is_tier_and_budget_value():
    assert budget.budget_key("merge_preview", 900.0) == "merge_preview:900"


def test_budget_key_changes_when_the_ssot_budget_is_bumped():
    v1 = budget.budget_key("merge_preview", 900.0)
    v2 = budget.budget_key("merge_preview", 1200.0)
    assert v1 != v2, "bumping the SSOT budget must mint a new idempotency key"


def test_budget_key_distinguishes_tiers_at_the_same_budget():
    assert (budget.budget_key("merge_preview", 900.0)
            != budget.budget_key("full_snapshot", 900.0))


# ---------------------------------------------------------------------------
# check_and_file's breach path: exactly one file_budget_bead_once call, with
# the derived key, title, and description — against a fully stubbed bd layer.
# ---------------------------------------------------------------------------

def test_breach_calls_file_once_with_the_derived_key(monkeypatch, capsys):
    monkeypatch.setattr(budget, "tier_budget_sec", lambda repo, tier: 900.0)
    calls = []

    def _fake_file_once(repo, *, key, title, description):
        calls.append((repo, key, title, description))
        return ("SABLE-fake1", True)

    monkeypatch.setattr(budget, "file_budget_bead_once", _fake_file_once)

    result = budget.check_and_file("/repo", "merge_preview", 1000.0,
                                   context="bead=SABLE-x branch=wk-x ref=ci-verify/wk-x")
    assert len(calls) == 1
    repo, key, title, description = calls[0]
    assert key == "merge_preview:900"
    assert "merge_preview" in title
    assert "SABLE-x" in description
    assert result == {"checked": True, "breached": True, "budget_sec": 900.0,
                      "key": "merge_preview:900", "bead_id": "SABLE-fake1", "filed": True}
    err = capsys.readouterr().err
    assert "WARN" in err and "merge_preview" in err


def test_second_breach_same_key_finds_existing_and_files_nothing(monkeypatch):
    monkeypatch.setattr(budget, "tier_budget_sec", lambda repo, tier: 900.0)
    creates = []

    def _fake_find(repo, key):
        return "SABLE-already-open"

    def _fake_create_should_not_be_called(*a, **kw):
        creates.append((a, kw))
        raise AssertionError("a bead was created despite an existing open one")

    monkeypatch.setattr(budget, "find_open_budget_bead", _fake_find)
    # file_budget_bead_once itself is real here — it must consult
    # find_open_budget_bead (stubbed above) and return without creating.
    import sable_gate_git_lib as git_lib
    monkeypatch.setattr(git_lib, "_run", _fake_create_should_not_be_called)

    bead_id, created = budget.file_budget_bead_once(
        "/repo", key="merge_preview:900", title="t", description="d")
    assert (bead_id, created) == ("SABLE-already-open", False)
    assert creates == []


# ---------------------------------------------------------------------------
# Never-raises contract: an exception anywhere in the pipeline must not
# escape check_and_file (a budget-check fault must never fail a promotion).
# ---------------------------------------------------------------------------

def test_check_and_file_never_raises_on_an_internal_fault(monkeypatch):
    def _boom(repo, tier):
        raise RuntimeError("simulated SSOT read failure")
    monkeypatch.setattr(budget, "tier_budget_sec", _boom)

    result = budget.check_and_file("/repo", "merge_preview", 1000.0)
    assert result["checked"] is False
    assert "error" in result["reason"]
