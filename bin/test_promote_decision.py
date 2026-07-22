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
import importlib.util
import itertools
import json
import os
import shutil
import subprocess
import sys
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


def test_the_module_has_exactly_two_writers_to_the_integration_branch():
    """The bridge between 'the table is safe' and 'no path bypasses the table'.

    An enumeration over decide_promotion proves I1 only if every write to the
    integration branch is guarded by it. Checked against the SOURCE, like the
    other structural properties of this gate (bin/test_merge_gate_modules.py):
    there are exactly TWO refspecs targeting the base — the unmoved-base
    promotion of the CI-verified preview, and _stale_base's promotion of the
    re-verified combined object — and no third one can appear without failing
    here. A third writer is how an 'unreachable' path becomes reachable."""
    import inspect
    import re
    writers = re.findall(r'f"\{(\w+)\}:refs/heads/\{base\}"', inspect.getsource(promote_lib))
    assert writers == ["combined_sha", "preview_sha"], (
        f"the integration branch has writers this proof does not cover: {writers}")


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
    """SABLE-jd5fj.8 revise: `repo`'s own root .testmondata is CI's copy and a
    checkout like Chuck's typically never carries one -- that is the actual gap
    this bead's approach (a) closes. When repo has no root .testmondata but the
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
    """SABLE-jd5fj.8: `sable-merge-gate warm-testmon-cache` is the LOCAL answer
    to CI's testmon-cache-warm.sh -- it must actually populate the gate-owned
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
    threading.Timer(0.40, release.set).start()

    t0 = time.monotonic()
    outcome, detail = promote_lib.run_impact_tier(repo, sha, ["bin/thing.py"])
    waited_wall = time.monotonic() - t0
    holder.join(timeout=10)

    assert outcome == promote_lib.IMPACT_GREEN, detail
    assert waited_wall >= 0.35, f"the queued tier did not actually wait ({waited_wall:.3f}s)"
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
                "SABLE_MG_IMPACT_SERIALIZE"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_impact_budget_is_queryable(clean_budget_env, capsys):
    """THE BEAD. The gate reports its effective impact budget, and the reported
    value TRACKS the env when overridden — so a wrapper that derives from it
    cannot go stale the way a copied constant did.

    Worst case is the SUM, not the tier budget. The lock wait is deliberately
    outside the tier's own budget (jd5fj.13, asserted by
    test_lock_wait_is_not_charged_to_the_impact_tier_timeout_budget), which is
    exactly why a wrapper sized to the tier alone is MORE wrong after that bead,
    not less: under a burst the queue wait alone can exceed it."""
    monkeypatch = clean_budget_env

    stock = promote_lib.impact_budget()
    assert stock["tier_timeout_s"] == 900.0
    assert stock["lock_timeout_s"] == 3600.0
    assert stock["worst_case_s"] == 4500.0, (
        "worst case must be queue + tier; reporting the tier budget alone is the "
        "exact mis-sizing SABLE-w0zjm exists to prevent")
    assert stock["serialized"] is True

    # Headroom is real headroom: strictly above the worst case, and an integer a
    # shell can hand straight to `timeout`.
    assert isinstance(stock["recommended_wrapper_timeout_s"], int)
    assert stock["recommended_wrapper_timeout_s"] > stock["worst_case_s"]

    # Tracking, both knobs, so neither can silently stop being reported.
    monkeypatch.setenv("SABLE_MG_IMPACT_TIMEOUT", "123")
    monkeypatch.setenv("SABLE_MG_IMPACT_LOCK_TIMEOUT", "456")
    tuned = promote_lib.impact_budget()
    assert (tuned["tier_timeout_s"], tuned["lock_timeout_s"]) == (123.0, 456.0)
    assert tuned["worst_case_s"] == 579.0
    assert tuned["recommended_wrapper_timeout_s"] > 579

    # With serialization off there is no queue to wait in, so charging the
    # wrapper for one would overstate the budget rather than understate it.
    monkeypatch.setenv("SABLE_MG_IMPACT_SERIALIZE", "0")
    off = promote_lib.impact_budget()
    assert off["lock_timeout_s"] == 0.0
    assert off["worst_case_s"] == off["tier_timeout_s"] == 123.0
    assert off["serialized"] is False

    # The human breakdown names WHICH number is which — a wrapper author reading
    # only one line must not be able to grab the wrong one.
    text = promote_lib.format_impact_budget(stock)
    assert "SABLE_MG_IMPACT_LOCK_TIMEOUT" in text and "SABLE_MG_IMPACT_TIMEOUT" in text
    assert "4500" in text and str(stock["recommended_wrapper_timeout_s"]) in text
    capsys.readouterr()


def test_the_cli_reports_the_same_budget_the_library_computes(clean_budget_env, capsys):
    """The wrapper does not import the library — it shells out. So the number a
    shell can actually reach has to be the same number, in a form `timeout` will
    accept: a bare integer on stdout with exit 0 and nothing else to parse."""
    monkeypatch = clean_budget_env
    monkeypatch.setenv("SABLE_MG_IMPACT_TIMEOUT", "200")
    monkeypatch.setenv("SABLE_MG_IMPACT_LOCK_TIMEOUT", "400")
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
    proc = subprocess.run([sys.executable, str(_BIN / "sable-merge-gate"),
                           "promote-budget", "--seconds"],
                          cwd="/", text=True, capture_output=True,
                          env={**os.environ, "SABLE_MG_IMPACT_TIMEOUT": "200",
                               "SABLE_MG_IMPACT_LOCK_TIMEOUT": "400"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(expected["recommended_wrapper_timeout_s"])


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

HAVE_BD = shutil.which("bd") is not None


def test_the_tier_runs_suites_under_an_isolated_bd_db(isolated_lock, tmp_path, monkeypatch):
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
        if HAVE_BD:
            assert "BEADS_DB" in env, "bd is on PATH, so this run must have an isolated BEADS_DB"
            assert env["BEADS_DB"].startswith(scratch_parent + os.sep), (
                f"BEADS_DB={env['BEADS_DB']!r} is not inside this run's own scratch "
                f"parent {scratch_parent!r}")
            assert not env["BEADS_DB"].startswith(repo), (
                "BEADS_DB must not point inside the gate's own repo")

    parents = {str(Path(cwd).parent) for cwd, _env in envs}
    assert len(parents) == 2, (
        f"two concurrent run_impact_tier calls shared a scratch parent: {parents}")


@pytest.mark.skipif(not HAVE_BD, reason="nothing to isolate a bd DB from without bd on PATH")
def test_bd_absent_env_still_isolates_home_but_bd_present_isolates_beads_db(tmp_path,
                                                                            monkeypatch):
    """The audited gap this bead closes: several suites self-skip on `command -v
    bd` (bd ABSENT), not on whether BEADS_DB was redirected. So the isolated env
    must never point BEADS_DB at a DB it could not build — only ever set it once
    bd init on that path actually succeeded."""
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
