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
import subprocess
import sys
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
