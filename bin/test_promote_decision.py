#!/usr/bin/env python3
"""The optimistic-disjoint-promotion decision tree (SABLE-jd5fj.4).

This bead is the epic's one deliberate REDUCTION in safety: before it, the exact
object CI tested was the exact object that landed, so a semantically broken merge
was structurally impossible rather than merely rare. That structural guarantee is
what the optimistic path gives up, which is why the acceptance criterion here is
not a set of examples but two properties proven by ENUMERATION over the whole
input space of decide_promotion:

  I1  NO REACHABLE PROMOTE PATH where the base moved and the footprints are not
      proven disjoint and no re-verification ran. Argued exhaustively, not by
      example — a single unreachable-looking path that turns out reachable is a
      silent bad merge, and it does not announce itself.

  I2  EVERY promotion pushes exactly the object some verifier attested: the
      CI-green preview when the base held still, the impact-tier-green COMBINED
      commit when it did not. Byte-identical promotion survives jd5fj.4; only
      the identity of the attesting verifier changes.

The enumeration deliberately includes combinations the callers cannot currently
produce (impact results without disjointness, green tiers with no combined
object). Those rows are the point: they are what makes the table safe against a
future caller that reaches them.

The second half wires the table to promote() itself, because a correct decision
table consulted in the wrong place proves nothing. Real-git composition —
including the impact tier actually running against a checked-out combined tree —
lives in hooks/test/test-optimistic-promotion.sh.
"""
import ast
import collections
import importlib.util
import itertools
import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent
_LOADER = SourceFileLoader("sable_merge_gate", str(_BIN / "sable-merge-gate"))
_SPEC = importlib.util.spec_from_loader("sable_merge_gate", _LOADER)
smg = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(smg)

classify = smg.classify
git_lib = smg.git_lib
preview_lib = smg.preview_lib
promote_lib = smg.promote_lib
footprint_lib = smg.footprint_lib
_REAL_SHUTIL_WHICH = shutil.which


@pytest.fixture(autouse=True)
def _do_not_build_a_real_bd_store_for_unrelated_unit_tests(monkeypatch):
    """Keep ambient bd installation from adding seconds to every tier fixture.

    One explicit test below restores the real binary and proves the isolated
    BEADS_DB contract end to end. The other tests exercise worktree, locking,
    selection, and reporting behavior; repeatedly initializing a real store
    in each of them adds no coverage.
    """
    monkeypatch.setattr(
        promote_lib.shutil,
        "which",
        lambda command: None if command == "bd" else _REAL_SHUTIL_WHICH(command),
    )

decide = promote_lib.decide_promotion

REPO, REMOTE, BASE, BRANCH, MANAGER = "/repo", "origin", "trunk", "wk-x", "chuck"
BASE_SHA = "a" * 40
BRANCH_SHA = "b" * 40
PREVIEW_SHA = "c" * 40
NEW_BASE_SHA = "d" * 40
COMBINED_SHA = "e" * 40
REF = "ci-verify/wk-x-abcdef1"

OUTCOMES = (classify.GREEN, classify.RED, classify.BLOCKED, classify.RETRY)
DISJOINT_VALUES = (True, False, None)
IMPACT_VALUES = (None, promote_lib.IMPACT_GREEN, promote_lib.IMPACT_RED,
                 promote_lib.IMPACT_ERROR, "some-unrecognized-answer")
COMBINED_VALUES = ("", COMBINED_SHA)

ALL_INPUTS = [
    dict(outcome=o, base_moved=m, disjoint=d, impact=i, preview_sha=PREVIEW_SHA, combined_sha=c)
    for o, m, d, i, c in itertools.product(
        OUTCOMES, (True, False), DISJOINT_VALUES, IMPACT_VALUES, COMBINED_VALUES)
]


def test_the_enumeration_is_the_whole_space():
    """Guard on the guard: if an input dimension gains a value and this list is
    not regenerated, the 'exhaustive' proofs below quietly stop being
    exhaustive."""
    assert len(ALL_INPUTS) == 4 * 2 * 3 * 5 * 2 == 240


# --------------------------------------------------------------------------
# REGRESSION (SABLE-be4lo.7, priority 1): the single-branch decision tree is
# BYTE-IDENTICAL after the batch-landing path is added alongside it.
# --------------------------------------------------------------------------

# A HARDCODED fingerprint of decide_promotion over the ENTIRE 240-row input
# space, captured the moment before SABLE-be4lo.7 added the batch-landing path
# beside it. Hardcoded (not recomputed from the current source, which would be
# circular): the literal below is the pre-change value, so if the batch work
# alters ANY cell of the single-branch table — a reordered branch, a changed
# reason string, a different exit code — the recomputed hash diverges from this
# literal and reds here, rather than surfacing as a silent bad merge. To
# legitimately change the single-branch table, a future edit must recompute and
# update this literal deliberately, which is exactly the review gate intended.
_DECISION_TABLE_GOLDEN_SHA256 = (
    "2d72b3a4a46007f2eaf2cb3277627c7e9e54122db66a03cf17cdc969c64d2480")


def test_REGRESSION_single_branch_decision_tree_is_byte_identical():
    """SABLE-be4lo.7 regression (priority 1): every field of every decision
    over this module's 240-row enumeration hashes byte-identical to the
    pre-batch-path golden. decide_promotion is the sole authority on the
    single-branch promote; adding the batch path must leave it untouched."""
    import hashlib
    rows = []
    for kw in ALL_INPUTS:
        d = decide(**kw)
        rows.append(repr((kw["outcome"], kw["base_moved"], kw["disjoint"],
                          kw["impact"], kw["combined_sha"], d.action, d.exit_code,
                          d.verified_sha, d.reverified, d.reason)))
    assert len(rows) == 240
    got = hashlib.sha256("\n".join(rows).encode()).hexdigest()
    assert got == _DECISION_TABLE_GOLDEN_SHA256, (
        "the single-branch decision tree changed — batch-path work must not alter "
        f"it. Recomputed {got}, golden {_DECISION_TABLE_GOLDEN_SHA256}. If this "
        "change is intentional, update the golden deliberately.")


# --------------------------------------------------------------------------
# PROPERTY INVARIANT I1
# --------------------------------------------------------------------------

def test_I1_no_promote_on_a_moved_base_without_a_re_verification():
    """THE property this bead is accepted or rejected on. Over every point in
    the input space: if the decision promotes and the base moved, a
    re-verification must have run — and it must have been a GREEN impact tier on
    a proven-disjoint pair, not merely a flag someone set."""
    offenders = []
    for kw in ALL_INPUTS:
        d = decide(**kw)
        if d.action != promote_lib.ACTION_PROMOTE or not kw["base_moved"]:
            continue
        if not (d.reverified
                and kw["disjoint"] is True
                and kw["impact"] == promote_lib.IMPACT_GREEN):
            offenders.append((kw, d))
    assert offenders == [], f"promote on a moved base without re-verification: {offenders}"


def test_I1_non_disjoint_or_undetermined_moves_never_reach_a_promote_or_a_tier_run():
    """The narrower statement of I1 the contract words: base-moved AND
    non-disjoint (or undetermined) must end in a full re-preview, whatever an
    impact result claims. Undetermined is checked alongside False because the
    tri-state must not create a third behaviour."""
    for kw in ALL_INPUTS:
        if not (kw["outcome"] == classify.GREEN and kw["base_moved"]
                and kw["disjoint"] is not True):
            continue
        d = decide(**kw)
        assert d.action == promote_lib.ACTION_REPREVIEW, (kw, d)
        assert d.exit_code == classify.EXIT_BASE_MOVED, (kw, d)
        assert d.reverified is False


def test_I1_holds_even_for_input_combinations_no_caller_produces_today():
    """A green impact tier paired with a non-disjoint footprint is unreachable
    from _stale_base as written. It must still refuse — that is what keeps the
    table safe under a refactor of the caller."""
    d = decide(classify.GREEN, base_moved=True, disjoint=False,
               impact=promote_lib.IMPACT_GREEN, combined_sha=COMBINED_SHA)
    assert d.action == promote_lib.ACTION_REPREVIEW
    d = decide(classify.GREEN, base_moved=True, disjoint=None,
               impact=promote_lib.IMPACT_GREEN, combined_sha=COMBINED_SHA)
    assert d.action == promote_lib.ACTION_REPREVIEW


# --------------------------------------------------------------------------
# PROPERTY INVARIANT I2
# --------------------------------------------------------------------------

def test_I2_every_promotion_names_the_object_that_was_verified():
    for kw in ALL_INPUTS:
        d = decide(**kw)
        if d.action != promote_lib.ACTION_PROMOTE:
            continue
        assert d.verified_sha, (kw, d)
        expected = kw["combined_sha"] if kw["base_moved"] else kw["preview_sha"]
        assert d.verified_sha == expected, (
            f"promotion would push an object no verifier attested: {kw} -> {d}")


def test_I2_a_green_tier_with_no_combined_object_promotes_nothing():
    """There is no object to be byte-identical TO, so there is no promotion to
    make. Refuse rather than fall back to the stale preview."""
    d = decide(classify.GREEN, base_moved=True, disjoint=True,
               impact=promote_lib.IMPACT_GREEN, preview_sha=PREVIEW_SHA, combined_sha="")
    assert d.action == promote_lib.ACTION_REPREVIEW
    assert d.verified_sha is None


# --------------------------------------------------------------------------
# The rest of the table: taxonomy preservation and total-ness
# --------------------------------------------------------------------------

def test_a_non_green_verdict_never_promotes_and_keeps_its_taxonomy_code():
    for kw in ALL_INPUTS:
        if kw["outcome"] == classify.GREEN:
            continue
        d = decide(**kw)
        assert d.action == promote_lib.ACTION_REFUSE, (kw, d)
        assert d.exit_code == classify.OUTCOME_EXIT[kw["outcome"]], (kw, d)


def test_every_decision_carries_a_taxonomy_exit_code_except_the_non_terminal_one():
    """ACTION_REVERIFY is the only state with no exit code, because it is not an
    outcome — it is an instruction to go and learn something."""
    legal = {classify.EXIT_OK, classify.EXIT_RED, classify.EXIT_BLOCKED,
             classify.EXIT_BASE_MOVED, classify.EXIT_CANCELLED}
    for kw in ALL_INPUTS:
        d = decide(**kw)
        if d.action == promote_lib.ACTION_REVERIFY:
            assert d.exit_code is None, (kw, d)
        else:
            assert d.exit_code in legal, (kw, d)


def test_reverify_is_reached_exactly_when_the_optimistic_path_opens():
    expected = {(classify.GREEN, True, True, None)}
    actual = {(kw["outcome"], kw["base_moved"], kw["disjoint"], kw["impact"])
              for kw in ALL_INPUTS
              if decide(**kw).action == promote_lib.ACTION_REVERIFY}
    assert actual == expected


def test_an_unmoved_base_still_promotes_the_ci_verified_preview_untouched():
    d = decide(classify.GREEN, base_moved=False, disjoint=None, impact=None,
               preview_sha=PREVIEW_SHA)
    assert (d.action, d.exit_code, d.verified_sha, d.reverified) == (
        promote_lib.ACTION_PROMOTE, classify.EXIT_OK, PREVIEW_SHA, False)


def test_an_impact_red_ejects_on_the_existing_exit_20_path():
    d = decide(classify.GREEN, base_moved=True, disjoint=True,
               impact=promote_lib.IMPACT_RED, combined_sha=COMBINED_SHA)
    assert (d.action, d.exit_code) == (promote_lib.ACTION_REFUSE, classify.EXIT_RED)


def test_an_unanswerable_impact_tier_falls_back_to_a_full_re_preview():
    for impact in (promote_lib.IMPACT_ERROR, "some-unrecognized-answer"):
        d = decide(classify.GREEN, base_moved=True, disjoint=True, impact=impact,
                   combined_sha=COMBINED_SHA)
        assert (d.action, d.exit_code) == (promote_lib.ACTION_REPREVIEW,
                                           classify.EXIT_BASE_MOVED), impact


# --------------------------------------------------------------------------
# Wiring: promote() consults the table, at the right moment, with real effects
# --------------------------------------------------------------------------

@pytest.fixture
def gate(monkeypatch):
    """promote() with every seam stubbed, returning a record of what it DID.

    The base is stale by construction (resolve_commit reports NEW_BASE_SHA until
    the combined object is pushed), so every case below runs the stale-base path.
    """
    state = {"pushes": [], "impact_calls": [], "built": [], "notices": [], "evidence": [],
             "base_reads": 0}

    def fake_git(repo, *args, check=True):
        if args and args[0] == "push":
            state["pushes"].append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="")

    def fake_resolve(repo, ref):
        """The base a promote resolves at the top (the preview's base) and the
        base it re-reads before pushing are DIFFERENT commits — that is what
        'the base moved during the CI wait' is. After the combined object lands,
        the read reports that object, so the integrity assertion can pass."""
        if not ref.endswith(BASE):
            return BRANCH_SHA
        if _promoted_to_base(state):
            return COMBINED_SHA
        state["base_reads"] += 1
        return BASE_SHA if state["base_reads"] == 1 else NEW_BASE_SHA

    monkeypatch.setattr(git_lib, "_git", fake_git)
    monkeypatch.setattr(git_lib, "resolve_commit", fake_resolve)
    monkeypatch.setattr(preview_lib, "materialize_preview",
                        lambda *a, **kw: (PREVIEW_SHA, REF, True))
    monkeypatch.setattr(preview_lib, "acquire_verdict",
                        lambda *a, **kw: classify.Verdict("success", "u", PREVIEW_SHA, REF,
                                                          source="precomputed"))
    monkeypatch.setattr(preview_lib, "build_preview",
                        lambda *a, **kw: state["built"].append(a) or COMBINED_SHA)
    monkeypatch.setattr(promote_lib, "cleanup_after_merge", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib, "_notify",
                        lambda target, msg: state["notices"].append(msg))
    monkeypatch.setattr(promote_lib, "_append_evidence",
                        lambda repo, bead, note: state["evidence"].append(note))
    state["_monkeypatch"] = monkeypatch
    return state


def _arm(state, *, disjoint, impact, paths=("left.py", "right.py")):
    mp = state["_monkeypatch"]
    mp.setattr(footprint_lib, "assess", lambda *a, **kw: footprint_lib.Assessment(
        disjoint=disjoint, reason="stubbed footprint assessment", paths=tuple(paths)))
    mp.setattr(promote_lib, "run_impact_tier",
               lambda repo, sha, p: state["impact_calls"].append((sha, tuple(p)))
               or (impact, "stubbed impact tier"))


def _promoted_to_base(state):
    return [p[-1] for p in state["pushes"] if p[-1].endswith(f"refs/heads/{BASE}")]


def _run():
    return promote_lib.promote("SABLE-x", BRANCH, BASE, REPO, REMOTE, MANAGER, None)


def test_promote_reverifies_and_promotes_the_combined_object_on_a_disjoint_stale_base(gate):
    _arm(gate, disjoint=True, impact=promote_lib.IMPACT_GREEN)
    assert _run() == 0
    assert gate["impact_calls"] == [(COMBINED_SHA, ("left.py", "right.py"))], \
        "the impact tier must run on the combined object, scoped to the union footprint"
    assert _promoted_to_base(gate) == [f"{COMBINED_SHA}:refs/heads/{BASE}"], \
        "the stale CI-green preview must never be the object that lands"
    assert any("OPTIMISTIC DISJOINT PROMOTION" in e for e in gate["evidence"])


def test_promote_refuses_a_non_disjoint_stale_base_and_never_runs_the_tier(gate):
    _arm(gate, disjoint=False, impact=promote_lib.IMPACT_GREEN)
    with pytest.raises(classify.GateError) as exc:
        _run()
    assert exc.value.code == classify.EXIT_BASE_MOVED
    assert gate["impact_calls"] == []
    assert _promoted_to_base(gate) == []


def test_promote_refuses_an_undetermined_footprint_exactly_like_an_overlapping_one(gate):
    _arm(gate, disjoint=None, impact=promote_lib.IMPACT_GREEN)
    with pytest.raises(classify.GateError) as exc:
        _run()
    assert exc.value.code == classify.EXIT_BASE_MOVED
    assert gate["impact_calls"] == []
    assert _promoted_to_base(gate) == []


def test_promote_ejects_on_exit_20_when_the_combined_tree_is_red(gate):
    _arm(gate, disjoint=True, impact=promote_lib.IMPACT_RED)
    assert _run() == classify.EXIT_RED
    assert _promoted_to_base(gate) == []
    assert any("COMBINED TREE" in n for n in gate["notices"])


def test_promote_falls_back_to_a_full_re_preview_when_the_tier_cannot_answer(gate):
    _arm(gate, disjoint=True, impact=promote_lib.IMPACT_ERROR)
    with pytest.raises(classify.GateError) as exc:
        _run()
    assert exc.value.code == classify.EXIT_BASE_MOVED
    assert _promoted_to_base(gate) == []


def test_the_kill_switch_restores_the_pre_jd5fj4_behaviour(gate, monkeypatch):
    """SABLE_MG_OPTIMISTIC=0: a disjoint stale base still costs a full
    re-preview, and no footprint or tier work happens at all."""
    monkeypatch.setenv("SABLE_MG_OPTIMISTIC", "0")
    _arm(gate, disjoint=True, impact=promote_lib.IMPACT_GREEN)
    monkeypatch.setattr(footprint_lib, "assess", lambda *a, **kw: pytest.fail(
        "the kill switch must short-circuit before any footprint work"))
    with pytest.raises(classify.GateError) as exc:
        _run()
    assert exc.value.code == classify.EXIT_BASE_MOVED
    assert gate["impact_calls"] == []
    assert _promoted_to_base(gate) == []


def test_a_second_base_move_during_re_verification_is_retryable_not_promoted(gate, monkeypatch):
    """The optimistic window is opened exactly once per promote: if the base
    moves again while the tier runs, the push is rejected and the gate exits 23
    rather than looping into a new window."""
    _arm(gate, disjoint=True, impact=promote_lib.IMPACT_GREEN)

    def rejecting_git(repo, *args, check=True):
        if args and args[0] == "push" and args[-1].endswith(f"refs/heads/{BASE}"):
            gate["pushes"].append(list(args))
            return subprocess.CompletedProcess(args, 1, stdout="! [rejected] non-fast-forward")
        if args and args[0] == "push":
            gate["pushes"].append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="")

    monkeypatch.setattr(git_lib, "_git", rejecting_git)
    with pytest.raises(classify.GateError) as exc:
        _run()
    assert exc.value.code == classify.EXIT_BASE_MOVED
    assert any("second time" in n for n in gate["notices"])


def test_the_integrity_assertion_still_fires_on_the_optimistic_path(gate, monkeypatch):
    """IRON RULE, on the new path too: if the base tip after the push is not the
    object the tier verified, abort loudly (exit 4). Provoked by making the
    post-push read report something else entirely."""
    _arm(gate, disjoint=True, impact=promote_lib.IMPACT_GREEN)
    reads = {"n": 0}

    def never_lands(repo, ref):
        if not ref.endswith(BASE):
            return BRANCH_SHA
        reads["n"] += 1
        return BASE_SHA if reads["n"] == 1 else NEW_BASE_SHA

    monkeypatch.setattr(git_lib, "resolve_commit", never_lands)
    with pytest.raises(classify.GateError) as exc:
        _run()
    assert exc.value.code == classify.EXIT_INTEGRITY


def test_an_unmoved_base_never_touches_the_footprint_machinery(monkeypatch):
    """Non-vacuity for the whole file: the ordinary promote path is untouched by
    jd5fj.4 — no footprint is computed, no tier runs, and the CI-green preview
    itself is what lands."""
    pushes = []

    def fake_git(repo, *args, check=True):
        if args and args[0] == "push":
            pushes.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="")

    monkeypatch.setattr(git_lib, "_git", fake_git)
    monkeypatch.setattr(git_lib, "resolve_commit", lambda repo, ref: (
        (PREVIEW_SHA if pushes else BASE_SHA) if ref.endswith(BASE) else BRANCH_SHA))
    monkeypatch.setattr(preview_lib, "materialize_preview",
                        lambda *a, **kw: (PREVIEW_SHA, REF, True))
    monkeypatch.setattr(preview_lib, "acquire_verdict",
                        lambda *a, **kw: classify.Verdict("success", "u", PREVIEW_SHA, REF))
    monkeypatch.setattr(promote_lib, "cleanup_after_merge", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib, "_notify", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib, "_append_evidence", lambda *a, **kw: None)
    monkeypatch.setattr(footprint_lib, "assess",
                        lambda *a, **kw: pytest.fail("footprint work on an unmoved base"))
    monkeypatch.setattr(promote_lib, "run_impact_tier",
                        lambda *a, **kw: pytest.fail("impact tier on an unmoved base"))

    assert promote_lib.promote("SABLE-x", BRANCH, BASE, REPO, REMOTE, MANAGER, None) == 0
    assert [p[-1] for p in pushes if p[-1].endswith(f"refs/heads/{BASE}")] == \
        [f"{PREVIEW_SHA}:refs/heads/{BASE}"]


# --------------------------------------------------------------------------
# The WIDENED entry: an ADOPTION MISS reaches the same decision (SABLE-kzi1a)
# --------------------------------------------------------------------------
#
# jd5fj.4 wired the table to ONE caller: the base moved during the gate's own CI
# wait. Under chuck's SERIAL merge lane that caller is unreachable by
# construction — chuck is the only writer to the integration branch, so while he
# is inside a promote nothing can move the base under it. Measured: 157
# promotions, 0 optimistic, and 0 across a 15-worker burst that queued 11
# branches. Meanwhile the branches WAITING in that queue are in the identical
# situation the optimistic path was built for — a green verdict for a preview
# whose base has since moved — and each was paying a full fresh CI run for it.
#
# So the entry widens to that case and the decision does not change: same table,
# same footprint computation, same mandatory impact tier on the REAL COMBINED
# TREE. What these cases pin is that the widening reaches the table WITHOUT
# reaching the promote rows the table refuses, and that the overlapping case
# still costs exactly what it cost before.

STALE_BASE_SHA = "9" * 40      # the base the queued branch's preview was built on
FRESH_PREVIEW_SHA = "7" * 40   # what the pre-kzi1a flow would build instead
STALE_REF = "ci-verify/wk-x-9999999"


@pytest.fixture
def queued(monkeypatch):
    """promote() for a branch that sat in the serial merge queue: its push-time
    preview is GREEN but was built on a base a previous merge has since moved
    past, so adoption MISSES. The base does NOT move during this promote — that
    is the whole point, and it is why the jd5fj.4 entry cannot fire here."""
    state = {"pushes": [], "impact_calls": [], "built": [], "notices": [], "evidence": [],
             "materialized": 0, "deleted": [], "waited": 0}

    def fake_git(repo, *args, check=True):
        if args and args[0] == "push":
            state["pushes"].append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="")

    def fake_resolve(repo, ref):
        if not ref.endswith(BASE):
            return BRANCH_SHA
        # The current base, steady throughout this promote — nothing else writes
        # to the integration branch while chuck is inside one, which is exactly
        # why the jd5fj.4 entry cannot fire — until whatever promote pushes lands.
        landed = _promoted_to_base(state)
        return landed[-1].split(":", 1)[0] if landed else NEW_BASE_SHA

    def fake_materialize(*a, **kw):
        state["materialized"] += 1
        return (FRESH_PREVIEW_SHA, "ci-verify/wk-x-7777777", False)

    def fake_acquire(*a, **kw):
        state["waited"] += 1
        return classify.Verdict("success", "u", FRESH_PREVIEW_SHA,
                                "ci-verify/wk-x-7777777", source="waited")

    monkeypatch.setattr(git_lib, "_git", fake_git)
    monkeypatch.setattr(git_lib, "resolve_commit", fake_resolve)
    monkeypatch.setattr(preview_lib, "find_stale_green_preview",
                        lambda *a, **kw: preview_lib.StalePreview(
                            PREVIEW_SHA, STALE_REF, STALE_BASE_SHA, "http://run/9"))
    monkeypatch.setattr(preview_lib, "materialize_preview", fake_materialize)
    monkeypatch.setattr(preview_lib, "acquire_verdict", fake_acquire)
    monkeypatch.setattr(preview_lib, "build_preview",
                        lambda *a, **kw: state["built"].append(a) or COMBINED_SHA)
    monkeypatch.setattr(preview_lib, "delete_ci_ref",
                        lambda repo, remote, ref: state["deleted"].append(ref))
    monkeypatch.setattr(promote_lib, "cleanup_after_merge", lambda *a, **kw: None)
    monkeypatch.setattr(promote_lib, "_notify",
                        lambda target, msg: state["notices"].append(msg))
    monkeypatch.setattr(promote_lib, "_append_evidence",
                        lambda repo, bead, note: state["evidence"].append(note))
    state["_monkeypatch"] = monkeypatch
    return state


def test_adoption_miss_reaches_the_disjoint_decision(queued):
    """THE bead. A green verdict, a preview whose base_sha != the current base,
    and disjoint footprints must reach the optimistic re-verify — not a blind
    from-scratch preview and a second full CI run for a merge that already has a
    green one."""
    _arm(queued, disjoint=True, impact=promote_lib.IMPACT_GREEN)
    assert _run() == 0

    assert queued["materialized"] == 0, \
        "a green push-time preview was discarded and a fresh one built anyway"
    assert queued["waited"] == 0, "the widened path paid for a second CI run"
    assert queued["impact_calls"] == [(COMBINED_SHA, ("left.py", "right.py"))], \
        "the impact tier must re-verify the REAL combined tree, scoped to the union footprint"
    assert _promoted_to_base(queued) == [f"{COMBINED_SHA}:refs/heads/{BASE}"], \
        "the object that lands must be the re-verified combined tree, never the stale preview"
    assert any("OPTIMISTIC DISJOINT PROMOTION" in e for e in queued["evidence"])
    assert STALE_REF in queued["deleted"], "the consumed ci-verify ref was left behind"


def test_the_widened_entry_re_verifies_the_combined_tree_against_the_CURRENT_base(queued):
    """I2 on the widened path, stated where it can fail: the tree handed to the
    impact tier is built from the CURRENT base and the branch — not from the base
    the stale preview was built on, which is what made it stale."""
    _arm(queued, disjoint=True, impact=promote_lib.IMPACT_GREEN)
    assert _run() == 0
    assert queued["built"], "no combined tree was built"
    built_base, built_branch = queued["built"][0][1], queued["built"][0][2]
    assert (built_base, built_branch) == (NEW_BASE_SHA, BRANCH_SHA)


def test_adoption_miss_with_overlapping_footprints_still_full_re_previews(queued):
    """THE NEGATIVE DIRECTION, and the reason the widening is allowed at all: a
    non-disjoint pair reaches the SAME table row it always did (ACTION_REPREVIEW
    / exit 23), the impact tier never runs, and the promote falls back to the
    pre-kzi1a flow — build a fresh preview and gate it on a fresh CI run."""
    assert decide(classify.GREEN, base_moved=True, disjoint=False, impact=None,
                  preview_sha=PREVIEW_SHA).action == promote_lib.ACTION_REPREVIEW
    assert decide(classify.GREEN, base_moved=True, disjoint=False, impact=None,
                  preview_sha=PREVIEW_SHA).exit_code == classify.EXIT_BASE_MOVED

    _arm(queued, disjoint=False, impact=promote_lib.IMPACT_GREEN)
    assert _run() == 0
    assert queued["impact_calls"] == [], "an overlapping pair must never reach the tier"
    assert queued["materialized"] == 1, "the overlapping case must pay the full re-preview"
    assert _promoted_to_base(queued) == [f"{FRESH_PREVIEW_SHA}:refs/heads/{BASE}"], \
        "the overlapping case must promote the freshly CI-verified preview, nothing else"


def test_adoption_miss_with_an_undetermined_footprint_behaves_exactly_like_overlap(queued):
    _arm(queued, disjoint=None, impact=promote_lib.IMPACT_GREEN)
    assert _run() == 0
    assert queued["impact_calls"] == []
    assert queued["materialized"] == 1
    assert _promoted_to_base(queued) == [f"{FRESH_PREVIEW_SHA}:refs/heads/{BASE}"]


def test_the_widened_entry_falls_back_when_the_impact_tier_cannot_answer(queued):
    """An unanswerable tier funds no optimism. Because nothing is in flight yet
    on this path, the fallback is the ordinary flow rather than exit 23 — exiting
    would strand the branch, since every retry finds the same stale preview."""
    _arm(queued, disjoint=True, impact=promote_lib.IMPACT_ERROR)
    assert _run() == 0
    assert queued["materialized"] == 1
    assert _promoted_to_base(queued) == [f"{FRESH_PREVIEW_SHA}:refs/heads/{BASE}"]


def test_the_widened_entry_ejects_on_exit_20_when_the_combined_tree_is_red(queued):
    """Two changes that were each green alone break together. That is a real
    defect with a named author, so it takes the SAME exit-20 eject a red run
    takes — it is not a reason to go and buy a second opinion from a full
    re-preview."""
    _arm(queued, disjoint=True, impact=promote_lib.IMPACT_RED)
    assert _run() == classify.EXIT_RED
    assert _promoted_to_base(queued) == []
    assert queued["materialized"] == 0
    assert any("COMBINED TREE" in n for n in queued["notices"])


def test_the_kill_switch_closes_the_widened_entry_too(queued, monkeypatch):
    """SABLE_MG_OPTIMISTIC=0 must restore the pre-jd5fj.4 behaviour EXACTLY, and
    that now includes never going looking for a stale preview in the first
    place."""
    monkeypatch.setenv("SABLE_MG_OPTIMISTIC", "0")
    _arm(queued, disjoint=True, impact=promote_lib.IMPACT_GREEN)
    monkeypatch.setattr(preview_lib, "find_stale_green_preview", lambda *a, **kw: pytest.fail(
        "the kill switch must close the widened entry before any discovery"))
    assert _run() == 0
    assert queued["materialized"] == 1
    assert queued["impact_calls"] == []


def test_no_stale_preview_leaves_the_ordinary_flow_untouched(queued, monkeypatch):
    """Non-vacuity: with nothing queued to find, promote does exactly what it did
    before this bead — build, gate, promote the CI-verified preview."""
    monkeypatch.setattr(preview_lib, "find_stale_green_preview", lambda *a, **kw: None)
    monkeypatch.setattr(footprint_lib, "assess",
                        lambda *a, **kw: pytest.fail("footprint work with nothing stale to assess"))
    assert _run() == 0
    assert queued["materialized"] == 1
    assert _promoted_to_base(queued) == [f"{FRESH_PREVIEW_SHA}:refs/heads/{BASE}"]


def test_a_human_override_does_not_take_the_widened_entry(queued, monkeypatch):
    """--override is an actions-down bypass that consults no run at all. The
    widened entry exists to consume a STORED verdict, so the two must not meet:
    an operator bypassing CI gets the documented bypass, not an optimistic path
    keyed on a verdict they were overriding."""
    monkeypatch.setattr(preview_lib, "find_stale_green_preview", lambda *a, **kw: pytest.fail(
        "an override promote went looking for a stored verdict"))
    assert promote_lib.promote("SABLE-x", BRANCH, BASE, REPO, REMOTE, MANAGER,
                               "http://human/approval") == 0
    assert queued["materialized"] == 1


# --------------------------------------------------------------------------
# THE NO-UNGUARDED-WRITER PROOF (SABLE-67qxt) — derive it, do not spell it
# --------------------------------------------------------------------------
#
# THE PROPERTY THE MACHINERY BELOW STANDS IN FOR: *no path in
# sable_gate_promote_lib writes a ref in a remote repository except the writers
# pinned in test_the_module_has_exactly_three_writers_to_the_integration_branch.*
# That property is the bridge between "the decision table is safe" and "no path
# bypasses the table" — the I1 enumeration and the 240-row golden table above
# are both CONDITIONAL on it, so its blind spots are the blind spots of the
# whole promote-safety argument.
#
# It used to be checked by regexing the module source for
# f"\{(\w+)\}:refs/heads/\{base\}". That matched ONE SPELLING OF A PUSH, not the
# property: to be counted, a writer had to be ALL of a literal f-string, with
# exactly one {var} before the colon, the literal text refs/heads/, and a branch
# variable named literally `base`. A writer that was any of these instead — a
# refspec built in a variable, a branch variable named `target`, %/format/+
# concatenation, the bare-ref push form, a push in a helper, a push through a
# different runner — was INVISIBLE, and invisible meant the suite went MORE
# green, not less. (The evading form was never hypothetical: line ~126 of the
# module under analysis already builds a ref as f"refs/heads/{branch}".)
#
# So this DERIVES the writers in three steps, each of which FAILS LOUD rather
# than silently not counting something:
#
#   1. CALL SITES — every call in the module whose argument tokens contain a git
#      verb that can write a remote ref, through whatever runner it goes, in
#      every kind of scope Python has: functions, nested closures, lambdas,
#      class bodies, decorators, default arguments and module scope.
#   2. VERBS — every verb the module hands to git must be classified below as
#      remote-ref-writing or not. An UNCLASSIFIED verb fails the check: a verb
#      nobody has reasoned about may well move a ref.
#   3. DESTINATIONS — each refspec of each such call is resolved to a SHAPE
#      through f-strings, %, str.format, str.join, + concatenation and local
#      single-assignment variables. A refspec, flag or argv layout the resolver
#      cannot decide is reported UNDECIDABLE, never skipped: a writer the
#      analysis cannot classify is exactly the case this check exists to stop.
#
# What it deliberately does NOT do is decide, from a name, whether a given
# destination IS the integration branch — a refspec built from a parameter
# called `target` writes the base exactly as much as one built from `base`, and
# no static rule tells them apart. So it pins the whole INVENTORY of remote-ref
# writes instead, in source order, with each destination's resolved shape. A new
# writer in ANY spelling, aimed anywhere, changes the inventory and fails.
# Judging a new write harmless is a human review step, recorded by editing the
# expected list — which is fail-CLOSED, the opposite polarity to the regex it
# replaces.
#
# SCOPE, stated so its edge is visible rather than assumed away: this is a
# static analysis of THIS module's source. A push inside a helper in ANOTHER
# module that promote() calls is beyond its reach (SABLE-taz3q).

_GIT_VERBS_THAT_CAN_WRITE_A_REMOTE_REF = frozenset({"push", "send-pack"})
"""Verbs that can move a ref in a repository other than the one they run in.
Every call site invoking one of these is parsed for its destination refspecs
below. A verb in NEITHER this set nor the reviewed-benign set below is
UNCLASSIFIED and fails: `git replay`, `git subtree push`, `git svn dcommit` and
friends are exactly the kind of arrival this check must not sleep through."""

_GIT_VERBS_REVIEWED_AS_NOT_WRITING_A_REMOTE_REF = frozenset({
    # read-only
    "status", "show", "show-ref", "rev-parse", "rev-list", "log", "diff", "cherry",
    "cat-file", "ls-files", "ls-tree", "ls-remote", "for-each-ref", "merge-base",
    "name-rev", "describe", "blame", "grep", "count-objects", "fsck", "config",
    # writes the working tree, the object store, or a LOCAL ref only — out of
    # scope: the integration branch this gate protects is the ref on the REMOTE,
    # which nothing here can move without a push.
    "init", "add", "commit", "checkout", "switch", "restore", "reset", "clean",
    "branch", "tag", "update-ref", "symbolic-ref", "notes", "stash", "worktree",
    "fetch", "merge", "rebase", "cherry-pick", "revert", "apply", "am", "remote",
    "gc", "prune", "hash-object", "write-tree", "commit-tree", "read-tree",
})

_PUSH_FLAGS_WITH_NO_EFFECT_ON_THE_DESTINATION = frozenset({
    "--force", "-f", "--force-with-lease", "--quiet", "-q", "--verbose", "-v",
    "--porcelain", "--atomic", "--no-verify", "--verify", "--set-upstream", "-u",
    "--follow-tags", "--thin", "--no-thin", "--progress", "--no-progress",
    "--dry-run", "-n",
})
"""Flags that change HOW a push happens, never WHICH refs it lands on. Anything
else — --all, --mirror, --tags, or a flag added to git after this was written —
makes the destination set something this parser has not reasoned about, so it is
reported UNDECIDABLE rather than quietly ignored."""

_RefWrite = collections.namedtuple("_RefWrite", "function mode src dest")
_Undecidable = collections.namedtuple("_Undecidable", "function reason")
_WriterReport = collections.namedtuple(
    "_WriterReport", "writes undecidable unclassified_verbs")


def _dotted(node):
    """`git_lib._git` for an Attribute chain, `_git` for a Name, else a marker."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return "expression"


def _marker(text):
    """An opaque `<...>` stand-in for something the resolver could not decide.
    Colons are stripped HERE, once, rather than trusted not to occur: a marker
    carrying one would split like a refspec and hand back a `dest` the parser
    never resolved — an undecidable write reading as a decided one, which is
    the exact failure direction this whole section exists to remove."""
    return "<" + str(text).replace(":", " ") + ">"


def _is_string_building(node):
    """Whether a bound value is worth inlining into a shape: only expressions
    that BUILD A STRING. A name bound to a call keeps its {name} placeholder —
    inlining would trade a readable placeholder for an opaque marker."""
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return True
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("format", "join"))


def _inline(name, assigns, seen):
    """The shape of `name` when this scope binds it EXACTLY ONCE to a
    string-building expression — i.e. the `spec = f"{sha}:refs/heads/{base}"`
    then `_git(..., spec)` evasion, which the old regex could not see. `seen`
    breaks a self-referential binding rather than recursing forever."""
    if name in seen:
        return None
    value = assigns.get(name)
    if value is None or not _is_string_building(value):
        return None
    return _shape(value, assigns, seen + (name,))


def _interpolated(node, assigns, seen):
    """The text an f-string's `{...}` contributes to a shape: `{name}` for a
    plain variable, the inlined shape when that variable is itself a built
    string, and an opaque marker for anything undecidable."""
    if isinstance(node, ast.Name):
        inlined = _inline(node.id, assigns, seen)
        return inlined if inlined is not None else "{" + node.id + "}"
    if isinstance(node, ast.Attribute):
        return "{" + _dotted(node) + "}"
    if isinstance(node, (ast.Constant, ast.JoinedStr, ast.BinOp, ast.Call)):
        return _shape(node, assigns, seen)
    return _marker("unresolved expression")


def _percent_shape(node, assigns, seen):
    """`"%s:refs/heads/%s" % (sha, base)` -> `{sha}:refs/heads/{base}`."""
    fmt = _shape(node.left, assigns, seen)
    values = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
    parts = fmt.split("%s")
    if len(parts) != len(values) + 1 or "%" in "".join(parts):
        return _marker("unresolved %-format")
    out = [parts[0]]
    for value, tail in zip(values, parts[1:]):
        out.append(_interpolated(value, assigns, seen))
        out.append(tail)
    return "".join(out)


def _call_shape(node, assigns, seen):
    """str.format and str.join resolve to a shape; every other call is opaque
    (`<call name>`), which makes it UNDECIDABLE in a refspec position."""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "format":
        parts = _shape(func.value, assigns, seen).split("{}")
        if len(parts) == len(node.args) + 1 and "{" not in "".join(parts):
            out = [parts[0]]
            for value, tail in zip(node.args, parts[1:]):
                out.append(_interpolated(value, assigns, seen))
                out.append(tail)
            return "".join(out)
        return _marker("unresolved str.format")
    if isinstance(func, ast.Attribute) and func.attr == "join":
        if len(node.args) == 1 and isinstance(node.args[0], (ast.List, ast.Tuple)):
            sep = _shape(func.value, assigns, seen)
            return sep.join(_shape(e, assigns, seen) for e in node.args[0].elts)
        return _marker("unresolved str.join")
    return _marker("call " + _dotted(func))


def _shape(node, assigns, seen=()):
    """An expression rendered as the STRING IT BUILDS, with each interpolated
    variable as `{name}` and everything undecidable as an opaque `<...>` marker.
    No marker contains a colon, so an unresolved argument can never be mistaken
    for the `src:dest` half of a refspec."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else _marker(f"constant {node.value!r}")
    if isinstance(node, ast.JoinedStr):
        return "".join(
            _interpolated(part.value, assigns, seen)
            if isinstance(part, ast.FormattedValue) else _shape(part, assigns, seen)
            for part in node.values)
    if isinstance(node, ast.Name):
        inlined = _inline(node.id, assigns, seen)
        return inlined if inlined is not None else "{" + node.id + "}"
    if isinstance(node, ast.Attribute):
        return "{" + _dotted(node) + "}"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _shape(node.left, assigns, seen) + _shape(node.right, assigns, seen)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _percent_shape(node, assigns, seen)
    if isinstance(node, ast.Call):
        return _call_shape(node, assigns, seen)
    if isinstance(node, ast.Starred):
        return _marker("starred argument")
    return _marker("unresolved expression")


def _tokens(node, assigns):
    """One call argument flattened into argv tokens: a list/tuple literal (or a
    variable bound to one, or a `_tool(...) + [...]` concatenation) contributes
    its elements in place, everything else contributes its own shape. This is
    what makes the runner irrelevant — `_git(repo, "push", ...)`,
    `subprocess.run(["git", "push", ...])` and `_run(_tool(...) + ["push", ...])`
    all flatten to a token sequence containing "push"."""
    if isinstance(node, (ast.List, ast.Tuple)):
        return [t for e in node.elts for t in _tokens(e, assigns)]
    if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)
            and (isinstance(node.left, (ast.List, ast.Tuple))
                 or isinstance(node.right, (ast.List, ast.Tuple)))):
        return _tokens(node.left, assigns) + _tokens(node.right, assigns)
    if isinstance(node, ast.Name) and isinstance(assigns.get(node.id), (ast.List, ast.Tuple)):
        return _tokens(assigns[node.id], assigns)
    return [_shape(node, assigns)]


def _own_nodes(scope):
    """Every node under `scope` WITHOUT descending into a nested scope, so each
    call is attributed to the innermost scope containing it and resolves its
    variables against that scope's own bindings. Seeded from the node's CHILDREN
    rather than from its body, so a call hiding in a decorator, a default
    argument or an annotation still belongs to somebody — an unattributed call is
    an uncounted one."""
    out, stack = [], list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.Lambda)):
            continue
        out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return out


def _assignments(nodes):
    """name -> the single expression bound to it in this scope. A name bound
    more than once maps to None: two bindings mean the value at the call site is
    not decidable from one of them, and guessing is how a check starts lying."""
    bound = {}
    for node in nodes:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [(t, node.value) for t in node.targets]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [(node.target, node.value)]
        elif isinstance(node, ast.AugAssign):
            targets = [(node.target, None)]
        elif isinstance(node, ast.NamedExpr):
            targets = [(node.target, node.value)]
        for target, value in targets:
            if isinstance(target, ast.Name):
                bound[target.id] = None if target.id in bound else value
    return bound


def _scopes(tree):
    """(name, own nodes) for module scope and EVERY nested scope — functions,
    async functions, methods, class bodies and lambdas. A push hidden in any of
    them is still a push this module can perform, and the enumeration is over
    Python's scope kinds rather than over the places writers happen to live
    today."""
    yield "<module>", _own_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name, _own_nodes(node)
        elif isinstance(node, ast.Lambda):
            yield "<lambda>", _own_nodes(node)


def _git_argv(call, tokens):
    """The git arguments of a call that demonstrably runs git — this module's
    `_git(repo, *args)` runner, or any runner handed an argv starting with
    "git". Used for the VERB inventory; the refspec scan below does not depend
    on recognising the runner."""
    if _dotted(call.func).split(".")[-1] == "_git":
        return tokens[1:]
    if tokens[:1] == ["git"]:
        return tokens[1:]
    return []


def _parse_push(function, tokens):
    """Destinations of one push, from its verb onwards. Returns (writes,
    problems); a problem is a destination this parser could NOT decide, which
    fails the check rather than dropping the site."""
    remote, deleting, specs, problems = None, False, [], []
    for token in tokens[1:]:
        if token in ("--delete", "-d"):
            deleting = True
        elif token.startswith("-"):
            if token not in _PUSH_FLAGS_WITH_NO_EFFECT_ON_THE_DESTINATION:
                problems.append(f"push flag {token} this parser has not reasoned about")
        elif remote is None:
            remote = token
        else:
            specs.append(token)
    if not specs:
        problems.append("push with no explicit refspec — the destination is whatever "
                        "push.default resolves to at runtime")
    writes = []
    for spec in specs:
        spec = spec.lstrip("+")
        # A bare ref pushes to the ref of the SAME NAME on the remote, which is
        # why `_git(repo, "push", remote, sha, base)` writes the base too.
        src, _, dest = spec.partition(":") if ":" in spec else (spec, "", spec)
        if deleting:
            src, dest = None, spec
        if "<" in dest:
            problems.append(f"refspec destination this parser could not resolve: {dest}")
        writes.append(_RefWrite(function, "delete" if deleting else "update", src, dest))
    return writes, problems


def _writer_report(tree):
    """THE CHECK. Every remote-ref write `tree`'s module can perform, in source
    order, with everything the analysis could not decide reported alongside
    rather than dropped.

    Takes an already-parsed tree — like promote_lib.unbudgeted_promote_timeouts,
    and for the same reason — so the tests below can hand it a planted writer
    and prove the mechanism FIRES, instead of only observing that today's real
    source happens to be clean."""
    writes, undecidable, verbs = [], [], []
    for function, nodes in _scopes(tree):
        assigns = _assignments(nodes)
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            tokens = [t for arg in node.args for t in _tokens(arg, assigns)]
            argv = _git_argv(node, tokens)
            if argv:
                verbs.append(argv[0])
            for index, token in enumerate(tokens):
                if token in _GIT_VERBS_THAT_CAN_WRITE_A_REMOTE_REF:
                    found, problems = _parse_push(function, tokens[index:])
                    writes.extend((node.lineno, w) for w in found)
                    undecidable.extend((node.lineno, _Undecidable(function, p))
                                       for p in problems)
                    break
    known = (_GIT_VERBS_THAT_CAN_WRITE_A_REMOTE_REF
             | _GIT_VERBS_REVIEWED_AS_NOT_WRITING_A_REMOTE_REF)
    return _WriterReport(
        writes=[w for _, w in sorted(writes, key=lambda pair: pair[0])],
        undecidable=[u for _, u in sorted(undecidable, key=lambda pair: pair[0])],
        unclassified_verbs=sorted({v for v in verbs if v not in known}))


def _planted(source):
    """The report for a synthetic module — the plant half of plant-and-fail."""
    return _writer_report(ast.parse(textwrap.dedent(source)))


# The three guarded writers, as the derived check reports them. Source order
# (_stale_base precedes promote precedes land_batch), so this pins WHERE each
# writer lives, not just how many there are.
_GUARDED_INTEGRATION_WRITERS = [
    _RefWrite("_stale_base", "update", "{combined_sha}", "refs/heads/{base}"),
    _RefWrite("promote", "update", "{preview_sha}", "refs/heads/{base}"),
    _RefWrite("land_batch", "update", "{fold_tip}", "refs/heads/{base}"),
]

# Every OTHER remote-ref write the module performs, reviewed and pinned so that
# a fourth writer cannot hide among them. Today there is one: chuck's cleanup
# retiring a merged worker branch. It is a DELETE of `branch`, never an update
# of `base` — and it is pinned by shape, so a `branch` that ever became `base`
# would have to change this line to pass.
_REVIEWED_NON_INTEGRATION_WRITES = [
    _RefWrite("cleanup_after_merge", "delete", None, "{branch}"),
]


def test_the_module_has_exactly_three_writers_to_the_integration_branch():
    """The bridge between 'the table is safe' and 'no path bypasses the table'.

    An enumeration over decide_promotion proves I1 only if every write to the
    integration branch is guarded. Checked against the SOURCE, like the other
    structural properties of this gate (bin/test_merge_gate_modules.py). There
    are exactly THREE refspecs targeting the base, each preceded by a guard the
    caller cannot skip:
      * combined_sha — _stale_base's promotion of the re-verified combined
        object (guarded by decide_promotion);
      * preview_sha  — the unmoved-base promotion of the CI-verified preview
        (guarded by decide_promotion);
      * fold_tip     — SABLE-be4lo.7's batch land (guarded by
        assert_batch_budget_present + the per-batch stale-base check + the
        built-on-this-base ancestry precondition, the batch analogues of the
        table's own guards).
    A FOURTH, unguarded writer is how an 'unreachable' path becomes reachable.
    Since SABLE-67qxt it fails in ANY spelling — but be exact about WHERE: this
    test covers a fourth writer whose destination resolves under refs/heads/,
    and a writer aimed anywhere else (the bare-ref form, an unresolved refspec,
    an unclassified verb) fails in
    test_writers_guard_over_real_module_still_finds_three, which pins the whole
    inventory. The pair is the proof; neither half is it alone."""
    writers = [w for w in _writer_report(promote_lib.promote_module_ast()).writes
               if w.mode == "update" and w.dest.startswith("refs/heads/")]
    assert writers == _GUARDED_INTEGRATION_WRITERS, (
        f"the integration branch has writers this proof does not cover: {writers}")


def test_writers_guard_over_real_module_still_finds_three():
    """EQUIVALENCE ON THE REAL ARTIFACT, and the no-false-positive half of the
    standard: run the derived mechanism against the REAL module — real source,
    real import, no fixtures — and it must report the SAME three guarded writers
    the retired regex reported, no more, plus the one reviewed non-integration
    write, and NOTHING it could not decide. A check that flags everything is as
    useless as one that flags nothing; this is the assertion that keeps the new
    mechanism honest in that direction."""
    report = _writer_report(promote_lib.promote_module_ast())
    assert report.undecidable == [], (
        "the analysis cannot decide where these writes land, so it cannot claim "
        f"the base is unwritten by them: {report.undecidable}")
    assert report.unclassified_verbs == [], (
        "this module now hands git a verb nobody has classified — put it in "
        "_GIT_VERBS_THAT_CAN_WRITE_A_REMOTE_REF or in the reviewed-benign set: "
        f"{report.unclassified_verbs}")
    assert report.writes == _REVIEWED_NON_INTEGRATION_WRITES + _GUARDED_INTEGRATION_WRITERS


def test_writers_guard_catches_a_differently_spelled_writer():
    """PLANT-AND-FAIL, evasion 1 — the refspec built in a variable and pushed by
    name. Worth being exact about what the retired regex did here, because it
    counted refspec LITERALS anywhere in the file rather than PUSH SITES: it
    happened to still see the f-string form below (a literal is a literal, even
    one that is never pushed) while seeing nothing at all in the second case,
    which builds the identical refspec by concatenation. The resolver follows
    the single binding in both, from the push site outwards."""
    report = _planted("""
        def sneak(repo, remote, sha, base):
            spec = f"{sha}:refs/heads/{base}"
            git_lib._git(repo, "push", remote, spec, check=False)
            hidden = sha + ":refs/heads/" + base
            git_lib._git(repo, "push", remote, hidden, check=False)
    """)
    assert report.writes == [_RefWrite("sneak", "update", "{sha}", "refs/heads/{base}")] * 2
    assert report.undecidable == []


def test_writers_guard_catches_a_differently_named_branch_variable():
    """PLANT-AND-FAIL, evasion 2 — same shape as a guarded writer but the branch
    variable is called `target`, which the retired regex required to be spelled
    `base`. The check does not try to decide whether `target` IS the base: it
    reports the write, and the inventory pin makes an unreviewed one fail."""
    report = _planted("""
        def sneak(repo, remote, sha, target):
            git_lib._git(repo, "push", remote, f"{sha}:refs/heads/{target}", check=False)
    """)
    assert report.writes == [_RefWrite("sneak", "update", "{sha}", "refs/heads/{target}")]


def test_writers_guard_catches_a_refspec_built_without_an_fstring():
    """PLANT-AND-FAIL, evasion 3 — %-format, str.format and + concatenation.
    Three spellings of one refspec, none of them an f-string, all resolving to
    the same shape as the writers they imitate."""
    report = _planted("""
        def sneak(repo, remote, sha, base):
            git_lib._git(repo, "push", remote, "%s:refs/heads/%s" % (sha, base))
            git_lib._git(repo, "push", remote, "{}:refs/heads/{}".format(sha, base))
            git_lib._git(repo, "push", remote, sha + ":refs/heads/" + base)
    """)
    assert report.writes == [_RefWrite("sneak", "update", "{sha}", "refs/heads/{base}")] * 3
    assert report.undecidable == []


def test_writers_guard_catches_the_bare_ref_push_form():
    """PLANT-AND-FAIL, evasion 4 — the two-argument form. `git push remote sha
    base` is two refspecs, and a bare refspec writes the ref of the SAME NAME on
    the remote, so this lands on the base with no `refs/heads/` text anywhere
    for a regex to match."""
    report = _planted("""
        def sneak(repo, remote, sha, base):
            git_lib._git(repo, "push", remote, sha, base, check=False)
    """)
    assert report.writes == [
        _RefWrite("sneak", "update", "{sha}", "{sha}"),
        _RefWrite("sneak", "update", "{base}", "{base}"),
    ]


def test_writers_guard_catches_a_push_inside_a_helper_or_at_module_scope():
    """PLANT-AND-FAIL, evasion 5 — the push moved out of the guarded functions.
    A helper, a nested closure, a lambda, a class body and module scope are all
    still places this module can push from, so every Python scope kind is
    walked. Each plant also names its branch variable `target`, so none of them
    is visible to the retired regex on top of living where it was not looking."""
    report = _planted("""
        git_lib._git(REPO, "push", REMOTE, f"{boot_sha}:refs/heads/{target}")

        def _helper(repo, remote, sha, target):
            git_lib._git(repo, "push", remote, f"{sha}:refs/heads/{target}")

        def outer(repo, remote, sha, target):
            def inner():
                git_lib._git(repo, "push", remote, f"{sha}:refs/heads/{target}")
            hook = lambda: git_lib._git(repo, "push", remote, f"{sha}:refs/heads/{target}")
            return inner, hook

        class Lander:
            AT_IMPORT = git_lib._git(REPO, "push", REMOTE, f"{boot_sha}:refs/heads/{target}")
    """)
    assert [(w.function, w.dest) for w in report.writes] == [
        ("<module>", "refs/heads/{target}"),
        ("_helper", "refs/heads/{target}"),
        ("inner", "refs/heads/{target}"),
        ("<lambda>", "refs/heads/{target}"),
        ("Lander", "refs/heads/{target}"),
    ]


def test_writers_guard_catches_a_push_through_a_different_runner():
    """PLANT-AND-FAIL, evasion 6 — bypassing git_lib._git entirely. The scan is
    over the argv TOKENS, not the callee, so subprocess.run, an argv assembled in
    a list variable and the module's `_tool(...) + [...]` idiom are all as
    visible as the module's own runner. No plant here is an f-string literal
    either, so none of the three is visible to the retired regex."""
    report = _planted("""
        def sneak(repo, remote, sha, base):
            subprocess.run(["git", "push", remote, "{}:refs/heads/{}".format(sha, base)])
            argv = ["git", "push", remote, sha + ":refs/heads/" + base]
            git_lib._run(argv, cwd=repo)
            git_lib._run(git_lib._tool("SABLE_MG_GIT", "git") + ["push", remote, "HEAD:main"])
    """)
    assert [(w.src, w.dest) for w in report.writes] == [
        ("{sha}", "refs/heads/{base}"),
        ("{sha}", "refs/heads/{base}"),
        ("HEAD", "main"),
    ]


def test_writers_guard_fails_loud_on_a_destination_it_cannot_decide():
    """A writer the analysis cannot classify is the case this check exists to
    stop, so an unresolvable refspec, an unreviewed push flag and a push with no
    refspec at all must FAIL rather than be silently uncounted — the failure
    direction the retired regex got backwards."""
    report = _planted("""
        def sneak(repo, remote, base, specs):
            git_lib._git(repo, "push", remote, _build_spec(base))
            git_lib._git(repo, "push", remote, *specs)
            git_lib._git(repo, "push", "--mirror", remote)
            git_lib._git(repo, "push")
    """)
    no_refspec = ("push with no explicit refspec — the destination is whatever "
                  "push.default resolves to at runtime")
    assert [u.reason for u in report.undecidable] == [
        "refspec destination this parser could not resolve: <call _build_spec>",
        "refspec destination this parser could not resolve: <starred argument>",
        "push flag --mirror this parser has not reasoned about",
        no_refspec,
        no_refspec,
    ]


def test_writers_guard_fails_loud_on_a_git_verb_nobody_has_classified():
    """Step 2 of the derivation. `git replay` writes refs; so do `git subtree
    push` and `git svn dcommit`. None of them is a `push` this parser knows how
    to read, so the arrival of an unclassified verb has to stop the check rather
    than pass through it."""
    report = _planted("""
        def sneak(repo, base, sha):
            git_lib._git(repo, "replay", "--onto", base, sha)
            git_lib._git(repo, "fetch", "origin", base)
    """)
    assert report.unclassified_verbs == ["replay"]


def test_writers_guard_does_not_flag_the_reads_and_local_writes_around_it():
    """NEGATIVE CONTROL, load-bearing: a guard that flags everything gets
    disabled, and then it protects nothing. Reads, local-ref writes and a
    non-git subprocess must all report NOTHING — no write, no undecidable
    destination, no unclassified verb."""
    report = _planted("""
        def ordinary(repo, remote, base, branch, sha):
            git_lib._git(repo, "fetch", remote, base, check=False)
            git_lib._git(repo, "rev-parse", f"refs/heads/{branch}")
            git_lib._git(repo, "branch", "-D", branch, check=False)
            git_lib._git(repo, "worktree", "add", "--detach", "/tmp/wt", sha)
            git_lib._run(["bash", ".github/ci/impact-manifest.sh", "--select"], cwd=repo)
    """)
    assert report == _WriterReport(writes=[], undecidable=[], unclassified_verbs=[])


# --------------------------------------------------------------------------
# run_impact_tier against REAL git worktrees (the runner's own contract)
# --------------------------------------------------------------------------

def _real_repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    for args in (("init", "-q", "-b", "trunk"), ("config", "user.email", "t@sable.invalid"),
                 ("config", "user.name", "SABLE Test")):
        subprocess.run(["git", "-C", str(r), *args], check=True, capture_output=True)
    (r / "bin").mkdir()
    (r / "bin" / "thing.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(r), "commit", "-q", "-m", "init"], check=True,
                   capture_output=True)
    sha = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True).stdout.strip()
    return str(r), sha


def test_run_impact_tier_checks_the_tree_out_for_real_and_reports_green(tmp_path, monkeypatch):
    repo, sha = _real_repo(tmp_path)
    marker = tmp_path / "ran-in"
    probe = tmp_path / "probe.sh"
    probe.write_text(f"#!/bin/sh\npwd > {marker}\ntest -f bin/thing.py\n")
    probe.chmod(0o755)
    monkeypatch.setenv("SABLE_MG_IMPACT", f"bash {probe}")
    outcome, detail = promote_lib.run_impact_tier(repo, sha, ["bin/thing.py"])
    assert outcome == promote_lib.IMPACT_GREEN, detail
    assert marker.read_text().strip() != repo, \
        "the tier must run in a checked-out combined tree, not in the gate's own repo"


def test_run_impact_tier_reports_red_when_the_tier_fails(tmp_path, monkeypatch):
    repo, sha = _real_repo(tmp_path)
    monkeypatch.setenv("SABLE_MG_IMPACT", "false")
    outcome, _ = promote_lib.run_impact_tier(repo, sha, ["bin/thing.py"])
    assert outcome == promote_lib.IMPACT_RED


def test_run_impact_tier_leaves_no_worktree_behind(tmp_path, monkeypatch):
    repo, sha = _real_repo(tmp_path)
    monkeypatch.setenv("SABLE_MG_IMPACT", "true")
    promote_lib.run_impact_tier(repo, sha, ["bin/thing.py"])
    listing = subprocess.run(["git", "-C", repo, "worktree", "list"], check=True,
                             capture_output=True, text=True).stdout
    assert listing.strip().count("\n") == 0, listing


def test_a_repo_with_no_impact_tier_is_an_ERROR_not_a_pass(tmp_path, monkeypatch):
    """The distinction the whole fallback rests on: an absent tier taught us
    nothing about the combined tree, so it must not read as green (which would
    promote) or as red (which would blame an author)."""
    repo, sha = _real_repo(tmp_path)
    monkeypatch.delenv("SABLE_MG_IMPACT", raising=False)
    outcome, detail = promote_lib.run_impact_tier(repo, sha, ["bin/thing.py"])
    assert outcome == promote_lib.IMPACT_ERROR, detail


def test_an_uncheckoutable_tree_is_an_ERROR(tmp_path, monkeypatch):
    repo, _ = _real_repo(tmp_path)
    monkeypatch.setenv("SABLE_MG_IMPACT", "true")
    outcome, _ = promote_lib.run_impact_tier(repo, "0" * 40, ["bin/thing.py"])
    assert outcome == promote_lib.IMPACT_ERROR


# --------------------------------------------------------------------------
# The impact-tier timeout reads the tier-budget SSOT (SABLE-jd5fj.9)
# --------------------------------------------------------------------------
#
# Before this bead, _impact_timeout() returned a hand-copied 900 — the
# merge_preview tier's duration budget copied by hand from
# .github/ci/test-tiers.sh, the exact duplicated-list class SABLE-cmar4.1
# closed for tier membership, reintroduced one level down. It must now derive
# from the same SSOT sable_gate_git_lib.default_mg_timeout and
# sable_gate_budget_lib.tier_budget_sec already read.

_DISTINCTIVE_BUDGET_TIERS_SH = (
    "#!/usr/bin/env bash\n"
    'if [ "$1" = "--budget" ] && [ "$2" = "merge_preview" ]; then\n'
    "  echo 12345\n"
    "  exit 0\n"
    "fi\n"
    "exit 1\n"
)

_BROKEN_TIERS_SH = (
    "#!/usr/bin/env bash\n"
    "exit 1\n"
)


def _write_tiers_sh(repo: str, contents: str) -> None:
    ci_dir = Path(repo) / ".github" / "ci"
    ci_dir.mkdir(parents=True, exist_ok=True)
    path = ci_dir / "test-tiers.sh"
    path.write_text(contents)
    path.chmod(0o755)


def _tiers_sh_with_budget(merge_preview_budget: int) -> str:
    """A test-tiers.sh reporting a caller-chosen merge_preview budget, for
    tests that need to move the SSOT to a SPECIFIC value (not merely a
    distinctive one) and observe what tracks it."""
    return (
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--budget" ] && [ "$2" = "merge_preview" ]; then\n'
        f"  echo {merge_preview_budget}\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )


def test_impact_timeout_reads_the_tier_ssot(tmp_path, monkeypatch):
    """(a) A DISTINCTIVE budget (12345 — cannot pass by coincidence against the
    real 900, the ambient-satisfaction trap that cost jd5fj.15 a revise cycle)
    from a repo-local test-tiers.sh is picked up in place of the old literal."""
    monkeypatch.delenv("SABLE_MG_IMPACT_TIMEOUT", raising=False)
    repo, _ = _real_repo(tmp_path)
    _write_tiers_sh(repo, _DISTINCTIVE_BUDGET_TIERS_SH)
    assert promote_lib._impact_timeout(repo) == 12345.0


def test_impact_timeout_override_still_wins_over_the_ssot(tmp_path, monkeypatch):
    """(b) SABLE_MG_IMPACT_TIMEOUT is an explicit override and must win even
    when the SSOT resolves to a different, equally distinctive value."""
    repo, _ = _real_repo(tmp_path)
    _write_tiers_sh(repo, _DISTINCTIVE_BUDGET_TIERS_SH)
    monkeypatch.setenv("SABLE_MG_IMPACT_TIMEOUT", "42")
    assert promote_lib._impact_timeout(repo) == 42.0


def test_impact_timeout_falls_back_without_raising_on_a_missing_ssot(tmp_path, monkeypatch):
    """(c) No .github/ci/test-tiers.sh at all: never raise, fall back to the
    pre-fix constant (900) — a missing SSOT must not block the gate."""
    monkeypatch.delenv("SABLE_MG_IMPACT_TIMEOUT", raising=False)
    repo, _ = _real_repo(tmp_path)
    assert not (Path(repo) / ".github" / "ci" / "test-tiers.sh").exists()
    assert promote_lib._impact_timeout(repo) == 900.0


def test_impact_timeout_falls_back_without_raising_on_a_broken_ssot(tmp_path, monkeypatch):
    """(c) A test-tiers.sh that exists but cannot answer (non-zero exit, no
    stdout) must fall back the same way, not raise."""
    monkeypatch.delenv("SABLE_MG_IMPACT_TIMEOUT", raising=False)
    repo, _ = _real_repo(tmp_path)
    _write_tiers_sh(repo, _BROKEN_TIERS_SH)
    assert promote_lib._impact_timeout(repo) == 900.0


def test_impact_timeout_defaults_repo_to_cwd_for_repo_less_callers(tmp_path, monkeypatch):
    """impact_budget()/the `promote-budget` CLI are deliberately --repo-less
    (see their own docstrings): _impact_timeout() must still answer by
    falling back to the current working directory."""
    monkeypatch.delenv("SABLE_MG_IMPACT_TIMEOUT", raising=False)
    repo, _ = _real_repo(tmp_path)
    _write_tiers_sh(repo, _DISTINCTIVE_BUDGET_TIERS_SH)
    monkeypatch.chdir(repo)
    assert promote_lib._impact_timeout() == 12345.0


# --------------------------------------------------------------------------
# The coverage-floor check's timeout reads the tier-budget SSOT (SABLE-cmar4.9)
# --------------------------------------------------------------------------
#
# Before this bead, _coverage_floor_timeout() returned a fresh hand-picked
# 600 — a second hardcoded promote-path constant landed after w0zjm's
# derivable-budget mechanism existed, the same class jd5fj.9 just closed for
# _impact_timeout. It must now derive from the same SSOT, borrowing
# merge_preview's budget the same way _impact_timeout does.

_DISTINCTIVE_COVERAGE_BUDGET_TIERS_SH = (
    "#!/usr/bin/env bash\n"
    'if [ "$1" = "--budget" ] && [ "$2" = "merge_preview" ]; then\n'
    "  echo 54321\n"
    "  exit 0\n"
    "fi\n"
    "exit 1\n"
)


def test_coverage_floor_timeout_reads_the_tier_ssot(tmp_path, monkeypatch):
    """(a) A DISTINCTIVE budget (54321 — cannot pass by coincidence against
    the old 600 or the impact tier's 900, the ambient-satisfaction trap that
    cost jd5fj.15 a revise cycle) from a repo-local test-tiers.sh is picked
    up in place of the old literal."""
    monkeypatch.delenv("SABLE_MG_COVERAGE_FLOOR_TIMEOUT", raising=False)
    repo, _ = _real_repo(tmp_path)
    _write_tiers_sh(repo, _DISTINCTIVE_COVERAGE_BUDGET_TIERS_SH)
    assert promote_lib._coverage_floor_timeout(repo) == 54321.0


def test_coverage_floor_timeout_override_still_wins_over_the_ssot(tmp_path, monkeypatch):
    """(b) SABLE_MG_COVERAGE_FLOOR_TIMEOUT is an explicit override and must
    win even when the SSOT resolves to a different, equally distinctive
    value."""
    repo, _ = _real_repo(tmp_path)
    _write_tiers_sh(repo, _DISTINCTIVE_COVERAGE_BUDGET_TIERS_SH)
    monkeypatch.setenv("SABLE_MG_COVERAGE_FLOOR_TIMEOUT", "42")
    assert promote_lib._coverage_floor_timeout(repo) == 42.0


def test_coverage_floor_timeout_falls_back_without_raising_on_a_missing_ssot(tmp_path, monkeypatch):
    """(c) No .github/ci/test-tiers.sh at all: never raise, fall back to the
    pre-fix constant (600) — a missing SSOT must not block the gate."""
    monkeypatch.delenv("SABLE_MG_COVERAGE_FLOOR_TIMEOUT", raising=False)
    repo, _ = _real_repo(tmp_path)
    assert not (Path(repo) / ".github" / "ci" / "test-tiers.sh").exists()
    assert promote_lib._coverage_floor_timeout(repo) == 600.0


def test_coverage_floor_timeout_falls_back_without_raising_on_a_broken_ssot(tmp_path, monkeypatch):
    """(c) A test-tiers.sh that exists but cannot answer (non-zero exit, no
    stdout) must fall back the same way, not raise."""
    monkeypatch.delenv("SABLE_MG_COVERAGE_FLOOR_TIMEOUT", raising=False)
    repo, _ = _real_repo(tmp_path)
    _write_tiers_sh(repo, _BROKEN_TIERS_SH)
    assert promote_lib._coverage_floor_timeout(repo) == 600.0


def test_coverage_floor_timeout_falls_back_without_raising_on_an_unparseable_override(tmp_path, monkeypatch):
    """(c) An unparseable explicit override must not raise either — mirrors
    the pre-fix try/except ValueError contract that guarded this same env
    var before this bead."""
    repo, _ = _real_repo(tmp_path)
    _write_tiers_sh(repo, _DISTINCTIVE_COVERAGE_BUDGET_TIERS_SH)
    monkeypatch.setenv("SABLE_MG_COVERAGE_FLOOR_TIMEOUT", "not-a-number")
    assert promote_lib._coverage_floor_timeout(repo) == 600.0


def test_coverage_floor_timeout_defaults_repo_to_cwd_for_repo_less_callers(tmp_path, monkeypatch):
    """_coverage_floor_timeout() must still answer by falling back to the
    current working directory when called with no repo argument, mirroring
    _impact_timeout's repo-less contract."""
    monkeypatch.delenv("SABLE_MG_COVERAGE_FLOOR_TIMEOUT", raising=False)
    repo, _ = _real_repo(tmp_path)
    _write_tiers_sh(repo, _DISTINCTIVE_COVERAGE_BUDGET_TIERS_SH)
    monkeypatch.chdir(repo)
    assert promote_lib._coverage_floor_timeout() == 54321.0


# --------------------------------------------------------------------------
# The bin/ pytest half's warm/cold .testmondata visibility (SABLE-jd5fj.8)
# --------------------------------------------------------------------------

_DEFAULT_TESTMON_STUB = (
    "#!/usr/bin/env python3\n"
    "import os\n"
    "from pathlib import Path\n"
    'seen = "warm" if Path(".testmondata").is_file() else "cold"\n'
    'marker = os.environ.get("SABLE_TEST_TESTMON_MARKER")\n'
    "if marker:\n"
    "    Path(marker).write_text(seen)\n"
    'print(f"PASS: stub selector saw {seen}")\n'
)

# A stub that mimics tier_selection.py's OWN real contract (build_impact_tier_plan
# prints "tier_selection: <mode> -- <reason>" to stderr before running anything) so
# the parsing logic (_tier_selection_reason) can be exercised without reproducing
# pytest-testmon's real corrupt-map behaviour. Simulates "corrupt" by content, not
# by actually crashing pytest-testmon -- this module's job is to surface whatever
# reason tier_selection.py reports, not to reproduce its own defect.
_CORRUPTION_AWARE_TESTMON_STUB = (
    "#!/usr/bin/env python3\n"
    "import sys\n"
    "from pathlib import Path\n"
    'p = Path(".testmondata")\n'
    'if p.is_file() and p.read_text() == "CORRUPT":\n'
    '    print("tier_selection: full -- simulated corrupt testmon map -- conservative full run", file=sys.stderr)\n'
    'elif p.is_file():\n'
    '    print("tier_selection: selected -- 1 impacted test(s) (testmon=1, impact=0)", file=sys.stderr)\n'
    "else:\n"
    '    print("tier_selection: full -- testmon cache miss (.testmondata absent) -- conservative full run", file=sys.stderr)\n'
    'print("PASS: stub selector ran")\n'
)


def _real_repo_with_bin_impact_tier(tmp_path, stub=_DEFAULT_TESTMON_STUB):
    """A real repo whose combined-tree impact tier reaches the bin/ pytest
    selector: an impact-manifest.sh that selects no shell suites at all
    (isolating the assertions below to the pytest half) plus a fast stub
    tier_selection.py (see `stub`) standing in for the real module."""
    r = tmp_path / "repo"
    r.mkdir()
    for args in (("init", "-q", "-b", "trunk"), ("config", "user.email", "t@sable.invalid"),
                 ("config", "user.name", "SABLE Test")):
        subprocess.run(["git", "-C", str(r), *args], check=True, capture_output=True)
    (r / "bin").mkdir()
    (r / "bin" / "thing.py").write_text("x = 1\n")
    (r / ".github" / "ci").mkdir(parents=True)
    (r / ".github" / "ci" / "impact-manifest.sh").write_text("#!/bin/sh\nexit 0\n")
    (r / ".github" / "ci" / "impact-manifest.sh").chmod(0o755)
    (r / "bin" / "tier_selection.py").write_text(stub)
    (r / "bin" / "tier_selection.py").chmod(0o755)
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(r), "commit", "-q", "-m", "init"], check=True,
                   capture_output=True)
    sha = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True).stdout.strip()
    return str(r), sha


def test_impact_tier_uses_a_warm_testmon_map_when_one_exists(tmp_path, monkeypatch):
    """SABLE-jd5fj.8: run_impact_tier best-effort copies the gate repo's own
    .testmondata into the throwaway combined-tree worktree so the pytest
    selector can use it -- but until this bead, a cold cache (no warm map)
    silently degraded to tier_selection.py's own conservative FULL bin/ run
    with no trace of that degradation in the returned detail string. Per the
    bead's ownership notes: a testmon map is a SELECTOR, not a verdict, and an
    under-selection must FAIL VISIBLE -- so both the copy and its absence must
    show up in the evidence, not just in stderr a caller may not capture."""
    repo, sha = _real_repo_with_bin_impact_tier(tmp_path)
    monkeypatch.delenv("SABLE_MG_IMPACT", raising=False)
    marker = tmp_path / "selector-saw-testmondata"
    monkeypatch.setenv("SABLE_TEST_TESTMON_MARKER", str(marker))

    # Warm case: a .testmondata sitting in the gate's own repo must be copied
    # into the throwaway worktree for the selector to see.
    (Path(repo) / ".testmondata").write_text('{"fake": "warm map"}')
    outcome, detail = promote_lib.run_impact_tier(repo, sha, ["bin/thing.py"])
    assert outcome == promote_lib.IMPACT_GREEN, detail
    assert marker.read_text().strip() == "warm", (
        "the warm .testmondata was not copied into the throwaway worktree")
    assert "warm testmon map" in detail, detail

    # Cold case: no warm map in the gate's own repo -- the tier still runs (a
    # conservative full run inside tier_selection.py) but the fallback must be
    # NAMED in the detail string, not silent.
    (Path(repo) / ".testmondata").unlink()
    outcome, detail = promote_lib.run_impact_tier(repo, sha, ["bin/thing.py"])
    assert outcome == promote_lib.IMPACT_GREEN, detail
    assert marker.read_text().strip() == "cold"
    assert "no warm .testmondata" in detail, detail


def test_impact_tier_falls_back_to_the_gate_persisted_cache_when_the_repo_has_none(
        tmp_path, monkeypatch):
    """SABLE-jd5fj.8 revise: `repo`'s own root .testmondata is optional and a
    checkout like Chuck's may never carry one. When repo has no root map but the
    gate's OWN persisted cache (_warm_testmondata_path) does, that persisted copy
    must be used instead of silently falling all the way to cold."""
    repo, sha = _real_repo_with_bin_impact_tier(tmp_path)
    monkeypatch.delenv("SABLE_MG_IMPACT", raising=False)
    marker = tmp_path / "selector-saw-testmondata"
    monkeypatch.setenv("SABLE_TEST_TESTMON_MARKER", str(marker))
    assert not (Path(repo) / ".testmondata").is_file()

    persisted = promote_lib._warm_testmondata_path(repo)
    persisted.parent.mkdir(parents=True, exist_ok=True)
    persisted.write_text('{"fake": "persisted gate cache"}')

    outcome, detail = promote_lib.run_impact_tier(repo, sha, ["bin/thing.py"])
    assert outcome == promote_lib.IMPACT_GREEN, detail
    assert marker.read_text().strip() == "warm", (
        "the gate's persisted cache was not copied into the throwaway worktree")
    assert "warm testmon map" in detail, detail
    assert "gate cache" in detail, detail


def test_impact_tier_names_a_stale_or_corrupt_warm_map_that_falls_back_internally(
        tmp_path, monkeypatch):
    """SABLE-jd5fj.8 revise: a warm map that is PRESENT but STALE/CORRUPT is
    still handed to the selector -- presence alone (the prior revision's whole
    check) cannot tell a genuinely-used map apart from one that silently
    triggered tier_selection.py's OWN internal collector-failure fallback to a
    full run. Only surfacing the selector's own reported reason catches this,
    and that under-selection is exactly what the bead's ownership notes require
    to fail visible rather than being reported as an ordinary 'warm testmon
    map' success."""
    repo, sha = _real_repo_with_bin_impact_tier(tmp_path, stub=_CORRUPTION_AWARE_TESTMON_STUB)
    monkeypatch.delenv("SABLE_MG_IMPACT", raising=False)

    (Path(repo) / ".testmondata").write_text("CORRUPT")
    outcome, detail = promote_lib.run_impact_tier(repo, sha, ["bin/thing.py"])
    assert outcome == promote_lib.IMPACT_GREEN, detail
    assert "simulated corrupt testmon map" in detail, (
        f"a corrupt warm map's internal fallback must be named, not reported as an "
        f"ordinary warm-map success: {detail}")
    assert "conservative full run" in detail, detail


def test_warm_gate_testmon_cache_refreshes_the_persisted_cache(tmp_path, monkeypatch):
    """SABLE-jd5fj.8: `sable-merge-gate warm-testmon-cache` must populate the gate-owned
    persisted cache (_warm_testmondata_path) from a real (stubbed, for speed)
    --cache-warm run against the repo's own root .testmondata, so the NEXT
    promote's cold-checkout fallback (the test above) has something to find."""
    r = tmp_path / "repo"
    r.mkdir()
    # Real git init (not just mkdir): _warm_testmondata_path resolves through
    # snapshot_lib.state_dir's `git rev-parse --git-common-dir`, which falls
    # back to the REAL $HOME outside a git repo -- a non-repo `r` here would
    # leak this test's state into the operator's actual gate state dir.
    subprocess.run(["git", "-C", str(r), "init", "-q"], check=True, capture_output=True)
    (r / "bin").mkdir()
    (r / "bin" / "tier_selection.py").write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        'if "--cache-warm" in sys.argv:\n'
        '    Path(".testmondata").write_text("REFRESHED")\n'
        '    print("PASS: stub cache-warm ran")\n'
        "    sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    (r / "bin" / "tier_selection.py").chmod(0o755)

    rc = promote_lib.warm_gate_testmon_cache(str(r))
    assert rc == 0
    persisted = promote_lib._warm_testmondata_path(str(r))
    assert persisted.is_file(), "the gate's persisted cache was not written"
    assert persisted.read_text() == "REFRESHED"


# --------------------------------------------------------------------------
# Impact-tier serialization (SABLE-jd5fj.13)
# --------------------------------------------------------------------------
#
# The failure this replaces was not a wrong verdict from a correct run — it was
# SIX CORRECT BRANCHES EJECTED because their tiers ran at the same time as
# somebody else's. The iron-rule suites share live bd/settings/worker state, so
# concurrent tiers false-RED each other; standalone on the same HEAD they were
# 18/18 and 5/5. Chuck's answer was a manual one-at-a-time rule, which is the
# guidance-as-control shape SABLE-rkc3o already refuted. These tests assert the
# mechanical replacement: tier windows cannot overlap, queue time is not charged
# to the tier's budget, and giving up on the queue degrades to a re-preview
# rather than a promotion.


@pytest.fixture()
def isolated_lock(tmp_path, monkeypatch):
    """Point the lock + window log at this test's own tmp dir, so the suite never
    contends with (or corrupts) a real merge seat's state dir."""
    monkeypatch.setenv("SABLE_MG_IMPACT_LOCK", str(tmp_path / "impact-tier.lock"))
    monkeypatch.setenv("SABLE_MG_IMPACT_WINDOW_LOG", str(tmp_path / "windows.jsonl"))
    monkeypatch.delenv("SABLE_MG_IMPACT_SERIALIZE", raising=False)
    monkeypatch.delenv("SABLE_MG_IMPACT_LOCK_TIMEOUT", raising=False)
    return tmp_path


def _concurrent_tier_windows(monkeypatch, hold=0.30, workers=2):
    """Run `workers` run_impact_tier calls concurrently with the tier body
    replaced by a recorder that sleeps `hold` seconds. Returns the list of
    (start, end) windows in start order, plus each call's measured lock wait."""
    windows: list[list[float]] = []
    waits: list[float] = []
    guard = threading.Lock()

    def fake_tier(repo, tree_sha, paths, phases=None):
        window = [time.monotonic(), None]
        with guard:
            windows.append(window)
        time.sleep(hold)
        window[1] = time.monotonic()
        return (promote_lib.IMPACT_GREEN, "recorded")

    monkeypatch.setattr(promote_lib, "_run_impact_tier_locked", fake_tier)

    def record_wait(repo, event, tree_sha, waited, phases=None):
        if event == "start":
            with guard:
                waits.append(waited)

    monkeypatch.setattr(promote_lib, "_stamp_impact_window", record_wait)

    results: list[tuple[str, str]] = []
    barrier = threading.Barrier(workers)

    def go(i):
        barrier.wait()
        results.append(promote_lib.run_impact_tier("/repo", f"{i:040d}", ["bin/thing.py"]))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads), "a tier thread never finished"
    return sorted(windows, key=lambda w: w[0]), waits, results


def test_impact_tier_serialized(isolated_lock, monkeypatch):
    """THE BEAD. Two promotes reaching the local impact tier on one seat must not
    have overlapping tier windows: the second must not START until the first has
    released the lock. Both still reach a verdict — serialization queues work, it
    never drops it."""
    windows, _waits, results = _concurrent_tier_windows(monkeypatch)
    assert len(windows) == 2
    (first_start, first_end), (second_start, second_end) = windows
    assert None not in (first_end, second_end)
    assert second_start >= first_end, (
        f"the second impact tier started {first_end - second_start:.3f}s BEFORE the first "
        f"released the lock — concurrent tiers false-RED each other (SABLE-jd5fj.13)")
    assert results == [(promote_lib.IMPACT_GREEN, "recorded")] * 2


def test_the_negative_control_sees_the_overlap_the_lock_removes(isolated_lock, monkeypatch):
    """The instrument check. With serialization off, the SAME harness must
    observe overlap — otherwise the test above proves only that the threads
    happened not to collide."""
    monkeypatch.setenv("SABLE_MG_IMPACT_SERIALIZE", "0")
    windows, _waits, _results = _concurrent_tier_windows(monkeypatch)
    (first_start, first_end), (second_start, _second_end) = windows
    assert second_start < first_end, (
        "with the lock disabled the two tiers did not overlap, so this harness cannot "
        "detect overlap at all and the serialization assertion is vacuous")


def test_lock_wait_is_not_charged_to_the_impact_tier_timeout_budget(isolated_lock, tmp_path,
                                                                    monkeypatch):
    """SABLE-w0zjm interaction, asserted rather than assumed: a promote that
    QUEUED behind another must still get its full SABLE_MG_IMPACT_TIMEOUT to run
    in. If queue time were charged to the tier budget, the fix for false-REDs
    would become a new source of them — a branch timing out because somebody
    else's tier was slow."""
    repo, sha = _real_repo(tmp_path)
    monkeypatch.setenv("SABLE_MG_IMPACT", "true")
    monkeypatch.setenv("SABLE_MG_IMPACT_TIMEOUT", "123")
    seen: list[float | None] = []
    real_run = git_lib._run

    def spy(argv, **kw):
        if "timeout" in kw:
            seen.append(kw["timeout"])
        return real_run(argv, **kw)

    monkeypatch.setattr(git_lib, "_run", spy)

    holding = threading.Event()
    release = threading.Event()

    def hold():
        with promote_lib.impact_tier_lock(repo):
            holding.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=hold)
    holder.start()
    assert holding.wait(timeout=10), "the holder never acquired the lock"

    # SABLE-kq8kn: t0 is captured BEFORE starting the release timer, not after
    # — under load (e.g. a full bin/ suite run contending for the GIL/CPU),
    # threading.Timer()'s own thread-creation overhead could land BETWEEN "start
    # a 0.40s countdown" and "capture t0", silently eating into the margin the
    # assert below relies on (measured: 0.320s < 0.35s). Capturing t0 first
    # means any such overhead is counted AS PART OF waited_wall instead of
    # stolen from it, so the measured wait can only read >= the timer's own
    # delay, never less. The delay/threshold pair is also widened (0.40s/0.30s,
    # a 0.10s margin instead of 0.05s) as defense in depth against poll-loop
    # granularity (impact_tier_lock polls every 0.05s) on a loaded box.
    t0 = time.monotonic()
    threading.Timer(0.40, release.set).start()
    outcome, detail = promote_lib.run_impact_tier(repo, sha, ["bin/thing.py"])
    waited_wall = time.monotonic() - t0
    holder.join(timeout=10)

    assert outcome == promote_lib.IMPACT_GREEN, detail
    assert waited_wall >= 0.30, f"the queued tier did not actually wait ({waited_wall:.3f}s)"
    assert seen, "the tier ran no timed subprocess, so this asserts nothing"
    assert set(seen) == {123.0}, (
        f"a queued tier was handed a reduced budget {sorted(set(seen))} instead of the full "
        f"123s — lock-wait time leaked into the impact-tier timeout")


def test_giving_up_on_the_lock_is_an_ERROR_not_a_pass(isolated_lock, monkeypatch):
    """A tier that never STARTED taught us nothing about the combined tree, so it
    must read as ERROR (-> full re-preview), never as green (which would promote
    an unverified merge) or red (which would blame an innocent author)."""
    monkeypatch.setenv("SABLE_MG_IMPACT_LOCK_TIMEOUT", "0.2")
    monkeypatch.setattr(promote_lib, "_run_impact_tier_locked",
                        lambda *a, **kw: pytest.fail("the tier ran while the lock was held"))
    holding = threading.Event()
    release = threading.Event()

    def hold():
        with promote_lib.impact_tier_lock("/repo"):
            holding.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=hold)
    holder.start()
    assert holding.wait(timeout=10)
    try:
        outcome, detail = promote_lib.run_impact_tier("/repo", "f" * 40, ["bin/thing.py"])
    finally:
        release.set()
        holder.join(timeout=10)
    assert outcome == promote_lib.IMPACT_ERROR, detail
    assert "never started" in detail
    # ...and that ERROR is already routed away from any promotion by the table.
    assert decide(classify.GREEN, base_moved=True, disjoint=True,
                  impact=promote_lib.IMPACT_ERROR).action == promote_lib.ACTION_REPREVIEW


def test_a_crashed_holder_does_not_wedge_the_seat(isolated_lock, tmp_path):
    """flock, not a pidfile: the kernel drops the lock when the holder dies. A
    promote killed mid-tier (^C at the seat, an OOM, a closed pane) must not
    leave every later promote queued forever."""
    lock = tmp_path / "impact-tier.lock"
    script = tmp_path / "holder.py"
    script.write_text(
        "import fcntl, os, sys, time\n"
        f"fh = open({str(lock)!r}, 'a+')\n"
        "fcntl.flock(fh.fileno(), fcntl.LOCK_EX)\n"
        "print('held', flush=True)\n"
        "time.sleep(30)\n")
    proc = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "held"
        proc.kill()
        proc.wait(timeout=10)
        t0 = time.monotonic()
        with promote_lib.impact_tier_lock("/repo") as waited:
            assert waited < 5.0, f"waited {waited:.1f}s on a lock whose holder was killed"
        assert time.monotonic() - t0 < 5.0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_the_lock_is_per_repo_and_lives_in_the_merge_gate_state_dir(tmp_path, monkeypatch):
    """Every worktree of a repo must contend on ONE file (that is the collision
    being prevented), and a different repo must not contend at all."""
    monkeypatch.delenv("SABLE_MG_IMPACT_LOCK", raising=False)
    monkeypatch.setenv("SABLE_MERGE_GATE_STATE", str(tmp_path / "state"))
    path = promote_lib.impact_lock_path("/repo")
    assert path == tmp_path / "state" / promote_lib.IMPACT_LOCK_FILE
    assert path.parent.is_dir(), "the state dir must exist before the lock is opened"
    monkeypatch.setenv("SABLE_MERGE_GATE_STATE", str(tmp_path / "other"))
    assert promote_lib.impact_lock_path("/repo") != path


def test_the_tier_window_log_records_both_edges(isolated_lock, tmp_path, monkeypatch):
    """The window log is the only direct evidence a human (or
    hooks/test/test-impact-tier-serialization.sh) has that two tiers did not
    overlap — suite results alone cannot show it, which is exactly why the
    pile-up read as six broken branches rather than one broken control."""
    repo, sha = _real_repo(tmp_path)
    monkeypatch.setenv("SABLE_MG_IMPACT", "true")
    promote_lib.run_impact_tier(repo, sha, ["bin/thing.py"])
    lines = [json.loads(ln) for ln in
             Path(isolated_lock / "windows.jsonl").read_text().splitlines() if ln.strip()]
    assert [ln["event"] for ln in lines] == ["start", "end"]
    assert all(ln["pid"] == os.getpid() and ln["tree"] == sha[:12] for ln in lines)
    assert lines[1]["at"] >= lines[0]["at"]


# --------------------------------------------------------------------------
# Per-phase tier telemetry (SABLE-mbkbm) — INSTRUMENT FIRST, ANALYSE SECOND.
# jd5fj.8 was dispatched on the ASSUMPTION that the cold-testmon pytest
# fallback was a first-order tier cost; measured on a real footprint the warm
# cache saved ~2%. These tests are for the instrument that makes the next such
# call a measurement instead of another assumption.
# --------------------------------------------------------------------------

def _real_repo_with_shell_and_pytest_impact_tier(tmp_path):
    """A real repo whose combined-tree impact tier reaches BOTH halves: one
    real, fast, passing shell suite (test-thing.sh) AND the bin/ pytest half
    (a fast stub tier_selection.py) — so a single run_impact_tier call can be
    asserted to carry a "setup", a "shell:test-thing.sh", AND a "pytest" phase
    entry all at once."""
    r = tmp_path / "repo"
    r.mkdir()
    for args in (("init", "-q", "-b", "trunk"), ("config", "user.email", "t@sable.invalid"),
                 ("config", "user.name", "SABLE Test")):
        subprocess.run(["git", "-C", str(r), *args], check=True, capture_output=True)
    (r / "bin").mkdir()
    (r / "bin" / "thing.py").write_text("x = 1\n")
    (r / "bin" / "tier_selection.py").write_text(_DEFAULT_TESTMON_STUB)
    (r / "bin" / "tier_selection.py").chmod(0o755)
    (r / ".github" / "ci").mkdir(parents=True)
    (r / ".github" / "ci" / "impact-manifest.sh").write_text(
        "#!/bin/sh\necho test-thing.sh\n")
    (r / ".github" / "ci" / "impact-manifest.sh").chmod(0o755)
    (r / "hooks" / "test").mkdir(parents=True)
    (r / "hooks" / "test" / "test-thing.sh").write_text(
        "#!/bin/sh\necho 'PASS: real thing suite'\n")
    (r / "hooks" / "test" / "test-thing.sh").chmod(0o755)
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(r), "commit", "-q", "-m", "init"], check=True,
                   capture_output=True)
    sha = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True).stdout.strip()
    return str(r), sha


def test_run_impact_tier_records_a_per_phase_journal_entry_for_each_real_span(
        isolated_lock, tmp_path, monkeypatch):
    """UNIT (test spec, first bullet). A tier reaching both halves must emit a
    versioned end record carrying a distinct phase entry for setup, the ONE
    shell suite it ran (named, not lumped into a single "shell" bucket — the
    whole point is finding which suite dominates), and the pytest half."""
    repo, sha = _real_repo_with_shell_and_pytest_impact_tier(tmp_path)
    monkeypatch.delenv("SABLE_MG_IMPACT", raising=False)
    outcome, detail = promote_lib.run_impact_tier(repo, sha, ["bin/thing.py"])
    assert outcome == promote_lib.IMPACT_GREEN, detail

    lines = [json.loads(ln) for ln in
             Path(isolated_lock / "windows.jsonl").read_text().splitlines() if ln.strip()]
    end = next(ln for ln in lines if ln["event"] == "end")
    assert end["schema"] == promote_lib.IMPACT_WINDOW_SCHEMA_VERSION
    names = [p["name"] for p in end["phases"]]
    assert "setup" in names, names
    assert "shell:test-thing.sh" in names, names
    assert "pytest" in names, names
    # Every recorded span is a real, non-negative measurement — never a
    # fabricated placeholder.
    assert all(isinstance(p["seconds"], (int, float)) and p["seconds"] >= 0
              for p in end["phases"])


def test_a_shell_only_tier_emits_no_pytest_phase_entry(isolated_lock, tmp_path, monkeypatch):
    """UNIT (test spec, NEGATIVE CONTROL). A footprint that never reaches bin/
    must not name "pytest" at all — a fabricated 0.0 would be indistinguishable
    from "ran instantly" and would poison any later aggregate."""
    repo, sha = _real_repo_with_shell_and_pytest_impact_tier(tmp_path)
    monkeypatch.delenv("SABLE_MG_IMPACT", raising=False)
    outcome, detail = promote_lib.run_impact_tier(repo, sha, ["hooks/test/other.sh"])
    assert outcome == promote_lib.IMPACT_GREEN, detail

    lines = [json.loads(ln) for ln in
             Path(isolated_lock / "windows.jsonl").read_text().splitlines() if ln.strip()]
    end = next(ln for ln in lines if ln["event"] == "end")
    names = [p["name"] for p in end["phases"]]
    assert "shell:test-thing.sh" in names, names
    assert "pytest" not in names, (
        f"a footprint with no bin/ path must emit no pytest phase entry at all: {names}")


def test_phase_report_excludes_legacy_records_instead_of_zero_counting_them(
        isolated_lock, tmp_path, monkeypatch):
    """UNIT (test spec, SECOND CONTROL). Old-format records (no "schema" key —
    jd5fj.13's original five keys) must still parse without error and must be
    EXCLUDED from phase aggregates, not counted as zero-duration phases, which
    would silently understate whichever phase actually dominates."""
    journal = isolated_lock / "windows.jsonl"
    legacy_end = {"event": "end", "pid": 111, "at": 1000.0, "tree": "aaaaaaaaaaaa",
                  "waited": 0.0}  # pre-mbkbm shape: no "schema", no "phases"
    new_start = {"schema": 2, "event": "start", "pid": 222, "at": 2000.0,
                "tree": "bbbbbbbbbbbb", "waited": 0.0}
    new_end = {"schema": 2, "event": "end", "pid": 222, "at": 2010.0, "tree": "bbbbbbbbbbbb",
              "waited": 0.0,
              "phases": [{"name": "setup", "seconds": 1.0},
                        {"name": "shell:test-real.sh", "seconds": 7.0},
                        {"name": "pytest", "seconds": 2.0}]}
    with open(journal, "w") as fh:
        for rec in (legacy_end, new_start, new_end):
            fh.write(json.dumps(rec) + "\n")

    report = promote_lib.impact_tier_phase_report("/repo")
    assert report["legacy_records_excluded"] == 1
    assert report["tiers_with_phase_data"] == 1
    assert set(report["phases"]) == {"setup", "shell:test-real.sh", "pytest"}
    assert report["phases"]["shell:test-real.sh"]["n"] == 1
    assert report["phases"]["shell:test-real.sh"]["median_s"] == 7.0
    # The legacy record contributes NOTHING to any phase's sample — folding it
    # in as a zero would drag every median down and hide the real dominant phase.
    for stat in report["phases"].values():
        assert stat["n"] == 1


def test_phase_report_names_the_dominant_phase_with_explicit_n(isolated_lock, tmp_path):
    """The phase-2 acceptance criterion: the report states n explicitly and
    names whichever phase actually accounts for the largest share — never an
    inferred split, and never silent about how many records it is based on."""
    journal = isolated_lock / "windows.jsonl"
    with open(journal, "w") as fh:
        for i in range(3):
            pid, tree = 300 + i, f"{'c' * 11}{i}"
            fh.write(json.dumps({"schema": 2, "event": "start", "pid": pid, "at": float(i * 100),
                                 "tree": tree, "waited": 0.0}) + "\n")
            fh.write(json.dumps({"schema": 2, "event": "end", "pid": pid, "at": float(i * 100 + 20),
                                 "tree": tree, "waited": 0.0,
                                 "phases": [{"name": "setup", "seconds": 1.0},
                                           {"name": "shell:test-slow.sh", "seconds": 15.0},
                                           {"name": "pytest", "seconds": 3.0}]}) + "\n")
    report = promote_lib.impact_tier_phase_report("/repo")
    assert report["tiers_with_phase_data"] == 3
    dominant = max(report["phases"], key=lambda name: report["phases"][name]["total_s"])
    assert dominant == "shell:test-slow.sh"
    assert report["phases"]["shell:test-slow.sh"]["share_of_total"] > 0.5

    text = promote_lib.format_impact_tier_phase_report(report)
    assert "n=3" in text
    assert "shell:test-slow.sh" in text


def test_phase_report_on_zero_records_states_zero_not_an_inferred_split(isolated_lock):
    """DO NOT SKIP TO PHASE 2 ON THIN DATA — the report must say n=0, not
    silently omit the phase section or synthesize a 0% / 100% split."""
    report = promote_lib.impact_tier_phase_report("/repo")
    assert report["tiers_with_phase_data"] == 0
    assert report["phases"] == {}
    text = promote_lib.format_impact_tier_phase_report(report)
    assert "n=0" in text
    assert "no per-phase measurements yet" in text


# --------------------------------------------------------------------------
# The derivable promote budget (SABLE-w0zjm)
# --------------------------------------------------------------------------
#
# The defect was not in this repo at all: chuck wrapped every promote in a
# 900s `timeout`, which is the SAME number as the default
# SABLE_MG_IMPACT_TIMEOUT. That was harmless only while the impact tier
# essentially never ran (0 optimistic paths in 157 promotions). jd5fj.4 moved
# cost from GitHub's CI into the local promote and jd5fj.13 put a queue in
# front of it, so the enclosing budget is now BOTH too small and unverifiable
# from inside the repo. These tests pin the escape hatch: the gate reports its
# own worst case, a wrapper derives from it, and the two cannot drift.


@pytest.fixture()
def clean_budget_env(monkeypatch):
    for var in ("SABLE_MG_IMPACT_TIMEOUT", "SABLE_MG_IMPACT_LOCK_TIMEOUT",
                "SABLE_MG_IMPACT_SERIALIZE", "SABLE_MG_COVERAGE_FLOOR_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_impact_budget_is_queryable(clean_budget_env, capsys):
    """THE BEAD. The gate reports its effective impact budget, and the reported
    value TRACKS the env when overridden — so a wrapper that derives from it
    cannot go stale the way a copied constant did.

    Worst case is the SUM of THREE terms, not the tier budget alone. The lock
    wait is deliberately outside the tier's own budget (jd5fj.13, asserted by
    test_lock_wait_is_not_charged_to_the_impact_tier_timeout_budget), which is
    exactly why a wrapper sized to the tier alone is MORE wrong after that
    bead, not less: under a burst the queue wait alone can exceed it.

    THE COVERAGE FLOOR IS THE THIRD TERM (SABLE-5v3d5). assert_coverage_floor's
    subprocess check runs INSIDE promote(), before the queue wait or the tier
    even start, so its ceiling is spent on every promote of a pruning diff —
    omitting it from the sum is the exact defect this bead fixes. Every value
    below is set explicitly (never left to the ambient repo's own SSOT) so this
    test cannot pass by an accident of what merge_preview's budget happens to
    be today."""
    monkeypatch = clean_budget_env
    monkeypatch.setenv("SABLE_MG_COVERAGE_FLOOR_TIMEOUT", "900")

    stock = promote_lib.impact_budget()
    assert stock["tier_timeout_s"] == 900.0
    assert stock["lock_timeout_s"] == 3600.0
    assert stock["coverage_floor_timeout_s"] == 900.0
    assert stock["worst_case_s"] == 5400.0, (
        "worst case must be queue + tier + coverage floor; omitting any one "
        "term is the exact mis-sizing SABLE-w0zjm/SABLE-5v3d5 exist to prevent")
    assert stock["serialized"] is True

    # Headroom is real headroom: strictly above the worst case, and an integer a
    # shell can hand straight to `timeout`.
    assert isinstance(stock["recommended_wrapper_timeout_s"], int)
    assert stock["recommended_wrapper_timeout_s"] > stock["worst_case_s"]

    # Tracking, all three knobs, so none can silently stop being reported.
    monkeypatch.setenv("SABLE_MG_IMPACT_TIMEOUT", "123")
    monkeypatch.setenv("SABLE_MG_IMPACT_LOCK_TIMEOUT", "456")
    monkeypatch.setenv("SABLE_MG_COVERAGE_FLOOR_TIMEOUT", "789")
    tuned = promote_lib.impact_budget()
    assert (tuned["tier_timeout_s"], tuned["lock_timeout_s"],
            tuned["coverage_floor_timeout_s"]) == (123.0, 456.0, 789.0)
    assert tuned["worst_case_s"] == 1368.0
    assert tuned["recommended_wrapper_timeout_s"] > 1368

    # With serialization off there is no queue to wait in, so charging the
    # wrapper for one would overstate the budget rather than understate it —
    # but the coverage floor is UNAFFECTED by that knob: it runs on every
    # pruning diff regardless of whether the impact tier is ever serialized.
    monkeypatch.setenv("SABLE_MG_IMPACT_SERIALIZE", "0")
    off = promote_lib.impact_budget()
    assert off["lock_timeout_s"] == 0.0
    assert off["worst_case_s"] == off["tier_timeout_s"] + off["coverage_floor_timeout_s"] == 912.0
    assert off["serialized"] is False

    # The decomposition keys are present and PROVABLY sum to the total, so a
    # future reader can attribute a change in worst_case_s to a specific term.
    for budget in (stock, tuned, off):
        assert (budget["tier_timeout_s"] + budget["lock_timeout_s"]
                + budget["coverage_floor_timeout_s"]) == budget["worst_case_s"]

    # The human breakdown names WHICH number is which — a wrapper author reading
    # only one line must not be able to grab the wrong one.
    text = promote_lib.format_impact_budget(stock)
    assert "SABLE_MG_IMPACT_LOCK_TIMEOUT" in text and "SABLE_MG_IMPACT_TIMEOUT" in text
    assert "SABLE_MG_COVERAGE_FLOOR_TIMEOUT" in text
    assert "5400" in text and str(stock["recommended_wrapper_timeout_s"]) in text
    capsys.readouterr()


def test_impact_budget_worst_case_tracks_the_coverage_floor_ssot_in_isolation(
        clean_budget_env, tmp_path):
    """NEGATIVE CONTROL, load-bearing (SABLE-5v3d5). Before this bead,
    _coverage_floor_timeout() derived from the merge_preview SSOT (cmar4.9)
    but impact_budget()['worst_case_s'] never read it — so moving the SSOT
    moved _coverage_floor_timeout()'s own return value while worst_case_s
    stayed exactly the same. That is precisely what happened in the wild:
    cmar4.9 re-pinned the coverage floor from a hardcoded 600 to the
    merge_preview SSOT (900 today), and `sable-merge-gate promote-budget`
    read 5400 before and after, because the changed term was never a
    summand. Tier and lock are pinned via env override here so ONLY the
    coverage floor moves — a test that let tier float too could pass even if
    the sum still silently dropped the coverage-floor term, exactly like
    test_impact_budget_is_queryable's own predecessor did."""
    monkeypatch = clean_budget_env
    monkeypatch.setenv("SABLE_MG_IMPACT_TIMEOUT", "100")
    monkeypatch.setenv("SABLE_MG_IMPACT_LOCK_TIMEOUT", "200")
    monkeypatch.delenv("SABLE_MG_COVERAGE_FLOOR_TIMEOUT", raising=False)

    repo, _ = _real_repo(tmp_path)
    _write_tiers_sh(repo, _DISTINCTIVE_COVERAGE_BUDGET_TIERS_SH)  # merge_preview=54321
    before = promote_lib.impact_budget(repo)
    assert before["coverage_floor_timeout_s"] == 54321.0
    assert before["worst_case_s"] == 100.0 + 200.0 + 54321.0

    _write_tiers_sh(repo, _tiers_sh_with_budget(600))
    after_small = promote_lib.impact_budget(repo)
    assert after_small["coverage_floor_timeout_s"] == 600.0
    assert after_small["worst_case_s"] == 100.0 + 200.0 + 600.0

    _write_tiers_sh(repo, _tiers_sh_with_budget(900))
    after_big = promote_lib.impact_budget(repo)
    assert after_big["coverage_floor_timeout_s"] == 900.0
    assert after_big["worst_case_s"] == 100.0 + 200.0 + 900.0

    # THE CHECK ITSELF: the coverage-floor term moved (600 -> 900) and
    # worst_case_s MUST move with it, by exactly the delta.
    assert after_big["worst_case_s"] != after_small["worst_case_s"], (
        "the coverage-floor SSOT moved but worst_case_s did not -- the exact "
        "instrument-blindness SABLE-5v3d5 reports")
    assert (after_big["worst_case_s"] - after_small["worst_case_s"]
            == after_big["coverage_floor_timeout_s"] - after_small["coverage_floor_timeout_s"]
            == 300.0)


def test_impact_budget_worst_case_sum_is_wrong_for_a_reverted_two_term_sum():
    """PLANT-AND-FAIL (SABLE-5lli.7 pattern). Reproduces the PRE-fix shape of
    impact_budget() inline -- worst = tier + lock, exactly as it read before
    this bead -- and proves the SAME equality assertion the tests above rely
    on (worst_case_s == tier + lock + coverage_floor) correctly REJECTS it.
    If this assertion could not tell the two-term sum apart from the
    three-term one, the tests above would pass today for the wrong reason,
    the exact "two errors cancel" trap this bead's own dispatch calls out."""
    tier, lock, coverage_floor = 900.0, 3600.0, 900.0
    reverted_worst_case_s = tier + lock  # the bug, reproduced on purpose
    assert reverted_worst_case_s != tier + lock + coverage_floor, (
        "the reverted two-term sum must be distinguishable from the correct "
        "three-term sum, or the equality checks above are vacuous")


def test_the_cli_reports_the_same_budget_the_library_computes(clean_budget_env, capsys):
    """The wrapper does not import the library — it shells out. So the number a
    shell can actually reach has to be the same number, in a form `timeout` will
    accept: a bare integer on stdout with exit 0 and nothing else to parse."""
    monkeypatch = clean_budget_env
    monkeypatch.setenv("SABLE_MG_IMPACT_TIMEOUT", "200")
    monkeypatch.setenv("SABLE_MG_IMPACT_LOCK_TIMEOUT", "400")
    monkeypatch.setenv("SABLE_MG_COVERAGE_FLOOR_TIMEOUT", "111")
    expected = promote_lib.impact_budget()

    assert smg.main(["promote-budget", "--seconds"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == str(expected["recommended_wrapper_timeout_s"])
    assert int(out) > 600, "the derived timeout must exceed queue + tier"

    assert smg.main(["promote-budget", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == expected

    assert smg.main(["promote-budget"]) == 0
    assert "worst case" in capsys.readouterr().out

    # No --repo, no git, no network: a wrapper must be able to ask BEFORE it has
    # chosen a repo, and asking must never be able to fail the promote it wraps.
    # All three knobs are explicit overrides here (SABLE_MG_COVERAGE_FLOOR_TIMEOUT
    # included) so the comparison does not depend on whatever cwd="/" (no
    # .github/ci/test-tiers.sh) falls back to for the un-overridden knobs.
    proc = subprocess.run([sys.executable, str(_BIN / "sable-merge-gate"),
                           "promote-budget", "--seconds"],
                          cwd="/", text=True, capture_output=True,
                          env={**os.environ, "SABLE_MG_IMPACT_TIMEOUT": "200",
                               "SABLE_MG_IMPACT_LOCK_TIMEOUT": "400",
                               "SABLE_MG_COVERAGE_FLOOR_TIMEOUT": "111"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(expected["recommended_wrapper_timeout_s"])


# --------------------------------------------------------------------------
# The combined-tree BATCH budget (SABLE-be4lo.6) -- a NEW named field
# --------------------------------------------------------------------------
#
# combined_tree_budget() is purely additive next to impact_budget() -- it
# calls impact_budget() rather than reimplementing any of its terms, so the
# single-branch report cannot drift just because the batch report exists.


def test_recommended_wrapper_timeout_s_is_unchanged_by_the_batch_field(clean_budget_env):
    """REGRESSION, priority 1 (columbo cmar4/be4lo matrix, S1 load-bearing
    case). Fixed fixture, both the FIELD and the VALUE asserted -- the exact
    two-things-on-one-name failure the 5v3d5 lesson names: 5400 sitting on
    both sides of a fix as two different quantities. combined_tree_budget()
    existing must not move recommended_wrapper_timeout_s by one second, nor
    rename/remove it from impact_budget()'s own report."""
    monkeypatch = clean_budget_env
    monkeypatch.setenv("SABLE_MG_IMPACT_TIMEOUT", "200")
    monkeypatch.setenv("SABLE_MG_IMPACT_LOCK_TIMEOUT", "400")
    monkeypatch.setenv("SABLE_MG_COVERAGE_FLOOR_TIMEOUT", "111")

    before = promote_lib.impact_budget()
    # Exercise the new batch path in between, so a shared-mutable-state bug
    # would show up as a moved `before`/`after` value.
    promote_lib.combined_tree_budget([["bin/a.py"], ["bin/b.py"], ["bin/c.py"]])
    after = promote_lib.impact_budget()

    assert "recommended_wrapper_timeout_s" in after
    assert after["recommended_wrapper_timeout_s"] == before["recommended_wrapper_timeout_s"] == 854
    assert after == before


def test_combined_tree_budget_is_monotonic_and_carries_a_bisection_reserve(clean_budget_env):
    """3-member fixture (columbo S1 case): the combined field exists, is
    monotonic (combined worst_case_s >= max of the members' own worst_case_s),
    and includes a non-zero bisection reserve term distinct from the tier/
    lock/coverage-floor terms it is added to."""
    monkeypatch = clean_budget_env
    monkeypatch.setenv("SABLE_MG_IMPACT_TIMEOUT", "300")
    monkeypatch.setenv("SABLE_MG_IMPACT_LOCK_TIMEOUT", "150")
    monkeypatch.setenv("SABLE_MG_COVERAGE_FLOOR_TIMEOUT", "50")

    member_footprints = [["bin/a.py"], ["bin/b.py", "bin/c.py"], ["hooks/test/x.sh"]]
    member_worst_cases = [promote_lib.impact_budget()["worst_case_s"] for _ in member_footprints]

    combined = promote_lib.combined_tree_budget(member_footprints)

    assert combined["member_count"] == 3
    assert combined["union_footprint_paths"] == [
        "bin/a.py", "bin/b.py", "bin/c.py", "hooks/test/x.sh"]
    assert combined["bisection_reserve_runs"] == 3
    assert combined["bisection_reserve_s"] == 3 * combined["tier_timeout_s"] > 0
    assert combined["worst_case_s"] == (
        combined["tier_timeout_s"] + combined["lock_timeout_s"]
        + combined["coverage_floor_timeout_s"] + combined["bisection_reserve_s"])
    assert combined["worst_case_s"] >= max(member_worst_cases)
    assert isinstance(combined["recommended_batch_wrapper_timeout_s"], int)
    assert combined["recommended_batch_wrapper_timeout_s"] > combined["worst_case_s"]
    # A NEW field, never the single-branch one:
    assert "recommended_batch_wrapper_timeout_s" in combined
    assert "recommended_wrapper_timeout_s" not in combined


def test_combined_tree_budget_rejects_an_empty_batch():
    """VACUOUS-PASS GUARD (SABLE-p9n7k applied to this new field): a
    zero-member budget request must error, never return a number that could
    masquerade as "no reserve needed". Matched on the guard's OWN message,
    not merely `pytest.raises(ValueError)` -- an empty member_budgets list
    fed to max() would ALSO raise a bare ValueError with no explicit guard at
    all (confirmed by planting: deleting the guard still passed a
    message-blind version of this test), which is exactly the accidental-
    exception-masquerading-as-a-guard failure mode SABLE-p9n7k exists to
    catch."""
    with pytest.raises(ValueError, match="at least one member footprint"):
        promote_lib.combined_tree_budget([])


def test_the_cli_reports_the_combined_tree_batch_budget(clean_budget_env, capsys):
    """The batch CLI path mirrors the single-branch one (SABLE-w0zjm
    precedent): --member-footprint (repeatable) switches promote-budget to
    the batch report, and --seconds/--json/plain all reach it. The wrapper
    shells out, so the number a shell can actually reach has to be the same
    one the library computes -- and it must be the BATCH field, not the
    single-branch one, so a wrapper cannot silently derive from the wrong
    name."""
    monkeypatch = clean_budget_env
    monkeypatch.setenv("SABLE_MG_IMPACT_TIMEOUT", "200")
    monkeypatch.setenv("SABLE_MG_IMPACT_LOCK_TIMEOUT", "400")
    monkeypatch.setenv("SABLE_MG_COVERAGE_FLOOR_TIMEOUT", "111")
    members = [["bin/a.py"], ["bin/b.py"]]
    expected = promote_lib.combined_tree_budget(members)

    argv = ["promote-budget", "--member-footprint", "bin/a.py",
            "--member-footprint", "bin/b.py"]
    assert smg.main([*argv, "--seconds"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == str(expected["recommended_batch_wrapper_timeout_s"])
    # Never the single-branch field's value under these knobs -- if the CLI
    # wired the wrong field this assertion is the one that would catch it.
    assert out != str(promote_lib.impact_budget()["recommended_wrapper_timeout_s"])

    assert smg.main([*argv, "--json"]) == 0
    reported = json.loads(capsys.readouterr().out)
    assert reported == expected
    assert "recommended_batch_wrapper_timeout_s" in reported

    assert smg.main(argv) == 0
    assert "combined-tree batch budget" in capsys.readouterr().out

    # No --repo, no git: matches the single-branch CLI's own no-repo contract.
    proc = subprocess.run([sys.executable, str(_BIN / "sable-merge-gate"), *argv, "--seconds"],
                          cwd="/", text=True, capture_output=True,
                          env={**os.environ, "SABLE_MG_IMPACT_TIMEOUT": "200",
                               "SABLE_MG_IMPACT_LOCK_TIMEOUT": "400",
                               "SABLE_MG_COVERAGE_FLOOR_TIMEOUT": "111"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(expected["recommended_batch_wrapper_timeout_s"])


# --------------------------------------------------------------------------
# Promote-path timeout completeness (SABLE-5v3d5) -- the general fix
# --------------------------------------------------------------------------
#
# impact_budget()'s coverage-floor omission happened because nothing enforced
# the coupling between "a bounded subprocess call lives in promote()'s path"
# and "its ceiling is a term in impact_budget()'s sum" -- the coupling was
# maintained by memory. unbudgeted_promote_timeouts() derives promote()'s
# real call graph from the AST and flags any `timeout=`-bearing call whose
# source is neither a registered budget term nor a documented exclusion.

def test_promote_path_timeout_completeness_is_clean_today():
    """Running the real check against this module's own source: every
    `timeout=`-bearing call reachable from promote() is either counted in
    impact_budget() or explicitly, visibly excused (see
    _KNOWN_UNBUDGETED_PROMOTE_TIMEOUTS) -- never silently missing both."""
    gaps = promote_lib.unbudgeted_promote_timeouts(promote_lib.promote_module_ast())
    assert gaps == [], (
        f"promote() reaches a bounded call with no budget term and no "
        f"documented exclusion: {gaps}")


def test_promote_path_timeout_completeness_fires_on_a_planted_unbudgeted_timeout():
    """COMPLETENESS, non-vacuous (SABLE-5v3d5): the point of this whole test.
    A SYNTHETIC call graph -- not this module's real source -- with a fresh
    helper function reachable from `promote` that bounds a subprocess with a
    timeout= this check has never seen. Proves the check DETECTS a gap,
    rather than merely observing that today's real code happens to have
    none (a completeness check that has never been shown to fail is
    indistinguishable from one that cannot)."""
    src = textwrap.dedent("""
        def _new_bounded_step(repo):
            return 42

        def promote(repo):
            subprocess.run(["true"], timeout=_new_bounded_step(repo))
            return 0
    """)
    tree = ast.parse(src)
    gaps = promote_lib.unbudgeted_promote_timeouts(tree)
    assert gaps == [("promote", "_new_bounded_step")], gaps


def test_promote_path_timeout_completeness_ignores_a_registered_source():
    """Non-vacuity in the other direction: a call whose source IS registered
    must not be flagged, or the check would be useless noise on every green
    run."""
    src = textwrap.dedent("""
        def _impact_timeout(repo):
            return 900

        def promote(repo):
            subprocess.run(["true"], timeout=_impact_timeout(repo))
            return 0
    """)
    tree = ast.parse(src)
    assert promote_lib.unbudgeted_promote_timeouts(tree) == []


def test_promote_path_timeout_completeness_does_not_follow_unreachable_functions():
    """Scoped to promote()'s OWN wall-clock, not every `timeout=` in the
    module: a bounded call in a function promote() never calls (directly or
    transitively) -- e.g. warm_gate_testmon_cache, a separate CLI entry point
    promote() never reaches -- must not be flagged."""
    src = textwrap.dedent("""
        def unrelated_cli_command(repo):
            subprocess.run(["true"], timeout=1800)

        def promote(repo):
            return 0
    """)
    tree = ast.parse(src)
    assert promote_lib.unbudgeted_promote_timeouts(tree) == []


def test_promote_path_timeout_completeness_flags_a_changed_literal_in_a_known_exclusion():
    """The (function, literal) exclusion is exact, not a function-name
    allowlist: if a currently-excused incidental timeout's literal ever
    changes -- the same way the coverage floor's own 600 once did -- it must
    fall through as a FRESH gap rather than staying silently excused under
    its old value."""
    src = textwrap.dedent("""
        def _report_identifier_decay(branch):
            subprocess.run(["true"], timeout=45)

        def promote(repo):
            _report_identifier_decay(repo)
            return 0
    """)
    tree = ast.parse(src)
    assert promote_lib.unbudgeted_promote_timeouts(tree) == [
        ("_report_identifier_decay", "45")]


def test_entering_the_impact_tier_is_announced_even_with_no_queue_wait(isolated_lock,
                                                                       tmp_path,
                                                                       monkeypatch,
                                                                       capsys):
    """SABLE-w0zjm (c). jd5fj.13's wait line only fires after a 1s+ queue, which
    is precisely the UNCONTENDED case where an externally-killed promote looks
    most like the optimistic path malfunctioning. An unconditional in-tier marker
    naming the budget makes the last line before the silence say how long the
    silence was entitled to be."""
    repo, sha = _real_repo(tmp_path)
    monkeypatch.setenv("SABLE_MG_IMPACT", "true")
    monkeypatch.setenv("SABLE_MG_IMPACT_TIMEOUT", "777")
    promote_lib.run_impact_tier(repo, sha, ["bin/thing.py"])
    out = capsys.readouterr().out
    assert "ENTERING IMPACT TIER" in out
    assert "777s" in out, "the marker must name the budget it is entitled to spend"
    assert "waited" not in out, "an uncontended run must not claim it queued"


# --------------------------------------------------------------------------
# Hermetic suite env (SABLE-jd5fj.15)
# --------------------------------------------------------------------------
#
# jd5fj.13 only QUEUED concurrent tiers around their shared live state (real
# bd, live ~/.claude/settings.json); it did not remove the interaction. This
# hermeticizes it: every suite/override invocation must run under a per-run
# BEADS_DB/HOME/TMPDIR, nested under that run's OWN scratch parent, so two
# tiers could run at once with nothing left to race on. Verified by CAPTURING
# the env handed to git_lib._run's subprocess rather than trusting a
# docstring — the same style test_lock_wait_is_not_charged... above already
# uses for the timeout budget.

HAVE_BD = _REAL_SHUTIL_WHICH("bd") is not None


def test_the_tier_runs_suites_under_isolated_home_and_tmp(isolated_lock, tmp_path, monkeypatch):
    repo, sha = _real_repo(tmp_path)
    monkeypatch.setenv("SABLE_MG_IMPACT", "true")
    real_run = git_lib._run
    seen: list[tuple[str, dict]] = []
    guard = threading.Lock()

    def spy(argv, **kw):
        with guard:
            seen.append((kw["cwd"], kw.get("env")))
        return real_run(argv, **kw)

    monkeypatch.setattr(git_lib, "_run", spy)

    # Two calls that could equally well be concurrent (the lock in
    # isolated_lock still serializes them, exactly like S1 above — this test
    # is about per-run ISOLATION, not overlap, which the integration suite
    # covers) — each must build its own scratch parent from scratch.
    results: list[tuple[str, str]] = []
    barrier = threading.Barrier(2)

    def go():
        barrier.wait()
        results.append(promote_lib.run_impact_tier(repo, sha, ["bin/thing.py"]))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads), "an impact-tier thread never finished"
    assert results == [(promote_lib.IMPACT_GREEN, "impact tier override reported green")] * 2, results

    envs = [(cwd, env) for cwd, env in seen if env is not None]
    assert len(envs) == 2, f"expected one env-carrying invocation per run, got {seen}"

    real_home = os.environ.get("HOME")
    for cwd, env in envs:
        scratch_parent = str(Path(cwd).parent)
        assert env["HOME"].startswith(scratch_parent + os.sep), (
            f"HOME={env['HOME']!r} is not inside this run's own scratch parent {scratch_parent!r}")
        assert env["HOME"] != real_home, "the isolated HOME must not be the real one"
        assert "BEADS_DB" not in env, (
            "this unit test deliberately models bd-absent; the dedicated real-bd "
            "test below owns BEADS_DB initialization coverage"
        )

    parents = {str(Path(cwd).parent) for cwd, _env in envs}
    assert len(parents) == 2, (
        f"two concurrent run_impact_tier calls shared a scratch parent: {parents}")


def test_explicit_bd_subprocess_override_does_not_initialize_host_bd(tmp_path, monkeypatch):
    """The documented SABLE_MG_BD seam is authoritative for injected runs.

    A host `bd` installation must not leak around that seam and build an
    unrelated real store before the configured test double is invoked.
    """
    fake_bd = tmp_path / "fake-bd"
    fake_bd.write_text("#!/bin/sh\nexit 0\n")
    fake_bd.chmod(0o755)
    monkeypatch.setenv("SABLE_MG_BD", str(fake_bd))
    monkeypatch.setattr(promote_lib.shutil, "which", _REAL_SHUTIL_WHICH)

    env = promote_lib._impact_isolated_env(tmp_path)

    assert "BEADS_DB" not in env
    assert not (tmp_path / "beads").exists()


@pytest.mark.skipif(not HAVE_BD, reason="nothing to isolate a bd DB from without bd on PATH")
def test_bd_absent_env_still_isolates_home_but_bd_present_isolates_beads_db(tmp_path,
                                                                            monkeypatch):
    """The audited gap this bead closes: several suites self-skip on `command -v
    bd` (bd ABSENT), not on whether BEADS_DB was redirected. So the isolated env
    must never point BEADS_DB at a DB it could not build — only ever set it once
    bd init on that path actually succeeded."""
    monkeypatch.setattr(promote_lib.shutil, "which", _REAL_SHUTIL_WHICH)
    env = promote_lib._impact_isolated_env(tmp_path)
    assert "BEADS_DB" in env
    beads_db = Path(env["BEADS_DB"])
    assert beads_db.is_relative_to(tmp_path)
    # The isolated DB must actually be USABLE, not merely present as a path —
    # this is the exact "redirected to an uninitialized DB" gap the WHERE note
    # in the bead flags: an uninitialized DB fails bd create, and several call
    # sites read that failure as "skip" rather than "the redirect is broken".
    created = subprocess.run(
        ["bd", "create", "--sandbox", "-q", "--title=isolated-env probe"],
        cwd=str(tmp_path), env={**os.environ, "BEADS_DB": str(beads_db)},
        text=True, capture_output=True, timeout=30)
    assert created.returncode == 0, created.stdout + created.stderr


# --------------------------------------------------------------------------
# MUST-LAND-TOGETHER pairing gate (SABLE-rzkw7)
# --------------------------------------------------------------------------
#
# The near-miss this bead closes: chuck held one half of a deliberately-paired
# change and said he would have promoted it on the other lane's sign-off
# alone, because the pairing lived only in a bead note and a manager's working
# memory — nowhere the promote path reads. The fix is a `landing_pair`
# metadata field the promote decision reads mechanically, so a solo promote of
# either half is refused BY DEFAULT rather than merely by luck.

def _fake_bd_show(responses: dict[str, dict]):
    """Stand in for promote_lib._bd_show: <responses> maps bead id -> the
    bd-show-json body it should answer with (missing keys -> a clean 404-style
    non-zero exit, matching a bd that genuinely doesn't know the bead)."""
    def fake(repo, bead_id):
        body = responses.get(bead_id)
        if body is None:
            return subprocess.CompletedProcess([], 1, stdout="issue not found")
        return subprocess.CompletedProcess([], 0, stdout=json.dumps(body))
    return fake


def test_landing_pair_refuses_a_solo_promote_but_never_touches_an_unpaired_bead(monkeypatch):
    """THE property this bead is accepted or rejected on: a bead declaring a
    landing_pair counterpart that has not already landed is REFUSED, naming
    the counterpart. Negative control IN THE SAME TEST: a bead with no
    landing_pair metadata at all — however similar its footprint to the paired
    one — must never be touched by this check, proving it discriminates on the
    declared relation rather than on any file-level property."""
    monkeypatch.setattr(promote_lib, "_bd_show", _fake_bd_show({
        "SABLE-a": {"metadata": {"landing_pair": "SABLE-b"}, "notes": ""},
        "SABLE-b": {"metadata": {}, "notes": ""},
        "SABLE-c": {"metadata": {}, "notes": ""},
    }))
    with pytest.raises(promote_lib.LandingPairRefused) as exc:
        promote_lib.assert_landing_pair_satisfied("/repo", "SABLE-a")
    assert "SABLE-b" in str(exc.value)

    # Negative control: an unpaired bead promotes its own check without a hitch.
    promote_lib.assert_landing_pair_satisfied("/repo", "SABLE-c")


def test_landing_pair_satisfied_when_the_counterpart_already_landed(monkeypatch):
    """The counterpart's OWN notes carry the exact marker every green promote
    path in this module writes ('promoted byte-identical to') — not bead
    status. SABLE-d5iku already established a closed bead is not a merged
    bead; reusing status here would reopen that exact gap."""
    monkeypatch.setattr(promote_lib, "_bd_show", _fake_bd_show({
        "SABLE-a": {"metadata": {"landing_pair": "SABLE-b"}, "notes": ""},
        "SABLE-b": {"metadata": {}, "status": "open",
                    "notes": "merge-preview ci-verify gate GREEN: ... promoted "
                             "byte-identical to trunk (verdict stored)."},
    }))
    promote_lib.assert_landing_pair_satisfied("/repo", "SABLE-a")  # must not raise


def test_a_closed_but_unlanded_counterpart_still_refuses(monkeypatch):
    """Non-vacuity for the status-vs-landed distinction: CLOSED alone (no
    landed marker in notes) must NOT satisfy the pairing — otherwise the
    worker's close-at-push (which happens before chuck ever merges, per
    SABLE-d5iku) would silently open the exact split-promote hole this bead
    closes."""
    monkeypatch.setattr(promote_lib, "_bd_show", _fake_bd_show({
        "SABLE-a": {"metadata": {"landing_pair": "SABLE-b"}, "notes": ""},
        "SABLE-b": {"metadata": {}, "status": "closed", "notes": "closed at push time"},
    }))
    with pytest.raises(promote_lib.LandingPairRefused):
        promote_lib.assert_landing_pair_satisfied("/repo", "SABLE-a")


def test_atomic_batch_satisfies_pair_only_when_counterpart_is_a_member(monkeypatch):
    """A pair authorization must correspond to the batch writer's concrete
    all-or-nothing member set, never a caller-supplied acknowledgement."""
    monkeypatch.setattr(promote_lib, "_bd_show", _fake_bd_show({
        "SABLE-a": {"metadata": {"landing_pair": "SABLE-b"}, "notes": ""},
        "SABLE-b": {"metadata": {"landing_pair": "SABLE-a"}, "notes": ""},
    }))
    members = [
        promote_lib.BatchMember(
            "wk-a", "a" * 40, ("SABLE-a",), ("bin/a.py",)),
        promote_lib.BatchMember(
            "wk-b", "b" * 40, ("SABLE-b",), ("bin/b.py",)),
    ]
    promote_lib.assert_batch_landing_pairs_satisfied("/repo", members)


def test_batch_landing_evidence_counts_as_genuinely_landed(monkeypatch):
    """The pair recovery read must recognize every successful writer owned by
    this module, including the batch writer's durable evidence."""
    monkeypatch.setattr(promote_lib, "_bd_show", _fake_bd_show({
        "SABLE-a": {"metadata": {"landing_pair": "SABLE-b"}, "notes": ""},
        "SABLE-b": {"metadata": {}, "status": "open",
                    "notes": "BATCH LANDED: 2 member(s) fast-forwarded trunk "
                             "to fold tip abcdef0 in ONE cycle."},
    }))
    promote_lib.assert_landing_pair_satisfied("/repo", "SABLE-a")


def test_declared_landing_pair_parses_comma_and_whitespace_separated_ids(monkeypatch):
    monkeypatch.setattr(promote_lib, "_bd_show", _fake_bd_show({
        "SABLE-a": {"metadata": {"landing_pair": "SABLE-b, SABLE-c\nSABLE-d"}},
    }))
    assert promote_lib.declared_landing_pair("/repo", "SABLE-a") == {
        "SABLE-b", "SABLE-c", "SABLE-d"}


def test_declared_landing_pair_is_empty_when_bd_cannot_be_read(monkeypatch):
    """Fail OPEN on the bead's OWN unreadable metadata — the pre-existing
    default for every bead this mechanism does not apply to (almost all of
    them) must not become 'refuse everything' just because bd hiccuped once."""
    monkeypatch.setattr(promote_lib, "_bd_show",
                        lambda repo, bead_id: subprocess.CompletedProcess([], 1, stdout=""))
    assert promote_lib.declared_landing_pair("/repo", "SABLE-a") == frozenset()
    promote_lib.assert_landing_pair_satisfied("/repo", "SABLE-a")  # must not raise


def test_an_unresolvable_counterpart_fails_closed(monkeypatch):
    """Fail CLOSED on an unresolvable COUNTERPART: a declared pairing whose
    counterpart bd cannot even find is exactly the uncertain case this bead
    defends against, and must refuse rather than silently assume landed."""
    monkeypatch.setattr(promote_lib, "_bd_show", _fake_bd_show({
        "SABLE-a": {"metadata": {"landing_pair": "SABLE-ghost"}, "notes": ""},
    }))
    with pytest.raises(promote_lib.LandingPairRefused) as exc:
        promote_lib.assert_landing_pair_satisfied("/repo", "SABLE-a")
    assert "SABLE-ghost" in str(exc.value)


def test_promote_refuses_a_paired_bead_before_any_git_work(gate, monkeypatch):
    """Wiring: promote() itself consults the check, and as the SECOND thing it
    does (right after the freeze check) — before the fetch, before the
    preview, before any verdict is read. A correct decision function proves
    nothing if promote() never asks it."""
    monkeypatch.setattr(promote_lib, "_bd_show", _fake_bd_show({
        "SABLE-x": {"metadata": {"landing_pair": "SABLE-y"}, "notes": ""},
        "SABLE-y": {"metadata": {}, "notes": ""},
    }))
    monkeypatch.setattr(git_lib, "resolve_commit", lambda *a, **kw: pytest.fail(
        "the landing-pair check must refuse before any git work happens"))
    with pytest.raises(classify.GateError) as exc:
        _run()
    assert exc.value.code == classify.EXIT_PAIR_REFUSED
    assert "SABLE-y" in str(exc.value)
    assert gate["pushes"] == []


def test_promote_is_unaffected_for_a_bead_with_no_landing_pair_metadata(gate, monkeypatch):
    """Non-vacuity for the whole feature: a bead with no landing_pair metadata
    at all is not slowed or altered by this bead in any way."""
    monkeypatch.setattr(promote_lib, "_bd_show", _fake_bd_show({
        "SABLE-x": {"metadata": {}, "notes": ""},
    }))
    _arm(gate, disjoint=True, impact=promote_lib.IMPACT_GREEN)
    assert _run() == 0
