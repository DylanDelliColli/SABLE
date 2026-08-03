#!/usr/bin/env python3
"""Unit tests for bin/sable-screen (SABLE-23upx).

Covers the pure decision logic (metadata-declaration reading, the
collision-screening predicate) with no git/bd involved, plus the git-plumbing
primitives against REAL minimal git fixtures (no bd, no network remote —
`git update-ref refs/remotes/origin/<name> <sha>` fabricates a
remote-tracking ref without an actual origin, the same trick
test_sable_contained.py uses for resolve_integration_ref).

test_reaped_branch_regression_* is the load-bearing suite: it pins the
concrete defect this bead exists for (bare `git rev-parse` echoing an
unresolvable ref back to stdout) by exercising the SAME git plumbing that
produced the 300-false-positive run, with a branch name guaranteed absent
from the fixture repo, and it explicitly demonstrates the trap alongside the
fix so a reader does not have to take the docstring's word for it.

THREE LATER HARDENING AXES are pinned here too, each a case of the screen
answering a NARROWER question than its caller asked and saying CLEAR anyway.
Every one of them is tested as a PAIR, because the positive test alone cannot
tell a real fix from a tool that just says no to everything:
  * SABLE-1muvg — candidate-vs-candidate (leg c). Overlapping candidates must
    SIBLING-COLLIDE *and* disjoint candidates must both stay CLEAR, plus the
    set-level case (3 candidates where only the 2nd and 3rd overlap) that an
    each-against-the-first implementation would pass a 2-candidate test and
    then miss.
  * SABLE-qyr0f — readiness. A blocked bead must not read CLEAR *and* a ready
    bead with the same footprint shape must still read CLEAR, with the
    closed-blocker control that separates reading dep STATUS from reading
    mere dep presence.
  * SABLE-jhako — branch-scoped holds. A bundled branch held via one sibling
    reads held from the branch row *and* a genuinely unheld branch still
    reports NEEDS-HOLD, exactly once.

Full end-to-end coverage (real git remote + real bd sandbox store) is in
bin/test_sable_screen_integration.py.
"""
import importlib.util
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

_LOADER = SourceFileLoader(
    "sable_screen", str(Path(__file__).resolve().parent / "sable-screen")
)
_SPEC = importlib.util.spec_from_loader("sable_screen", _LOADER)
ss = importlib.util.module_from_spec(_SPEC)
# sable-screen's ScreenVerdict is a @dataclass under `from __future__ import
# annotations`; dataclasses resolves `cls.__module__` via sys.modules at class
# creation time, so the module must be registered THERE before exec_module
# runs or that lookup finds nothing and raises.
sys.modules["sable_screen"] = ss
_LOADER.exec_module(ss)


def _run(*args, cwd):
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True)


def _git_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run("git", "init", "-q", cwd=str(repo))
    _run("git", "config", "user.email", "test@example.invalid", cwd=str(repo))
    _run("git", "config", "user.name", "SABLE Test", cwd=str(repo))
    return repo


def _commit(repo, name, content="x"):
    (repo / name).write_text(content)
    _run("git", "add", name, cwd=str(repo))
    _run("git", "commit", "-qm", f"add {name}", cwd=str(repo))
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                          capture_output=True, text=True, check=True).stdout.strip()


def _fake_remote_ref(repo, name, sha):
    """`git update-ref refs/remotes/origin/<name> <sha>` — a remote-tracking
    ref with no real remote configured, matching test_sable_contained.py's
    fixture trick. Sufficient for every function under test here: none of
    them touch the network, they only resolve refs already in the object
    database."""
    _run("git", "update-ref", f"refs/remotes/origin/{name}", sha, cwd=str(repo))


# ===========================================================================
# declared_writes_from_metadata — pure, no git/bd
# ===========================================================================

def test_no_declaration_when_neither_key_present():
    declared, files = ss.declared_writes_from_metadata({"branch": "wk-foo"})
    assert declared is False
    assert files == frozenset()


def test_no_declaration_for_empty_metadata():
    declared, files = ss.declared_writes_from_metadata({})
    assert declared is False


def test_declared_from_wip_claims():
    declared, files = ss.declared_writes_from_metadata({"wip_claims": "bin/a.py, bin/b.py"})
    assert declared is True
    assert files == frozenset({"bin/a.py", "bin/b.py"})


def test_declared_from_footprint_writes():
    declared, files = ss.declared_writes_from_metadata({"footprint_writes": "bin/c.py"})
    assert declared is True
    assert files == frozenset({"bin/c.py"})


def test_declared_unions_both_keys_when_both_present():
    declared, files = ss.declared_writes_from_metadata({
        "wip_claims": "bin/a.py",
        "footprint_writes": "bin/b.py",
    })
    assert declared is True
    assert files == frozenset({"bin/a.py", "bin/b.py"})


def test_present_but_empty_value_is_declared_not_absent():
    """A key present with a blank value is a real 'declares nothing' answer
    — distinct from the key being absent entirely (the NO-DECLARATION
    case). Collapsing the two would silently convert a bead that explicitly
    stamped an empty declaration back into an undeclared one."""
    declared, files = ss.declared_writes_from_metadata({"wip_claims": ""})
    assert declared is True
    assert files == frozenset()


def test_no_prose_fallback_ever_consulted():
    """A bead whose ONLY footprint information lives in a '## File
    footprint' description section (no structured metadata key at all) must
    still read as NO-DECLARATION — this function takes no description
    argument at all, so there is nothing for a prose fallback to reach for."""
    declared, files = ss.declared_writes_from_metadata({})
    assert declared is False
    assert files == frozenset()


# ===========================================================================
# screen_candidate — pure collision decision
# ===========================================================================

def test_candidate_declaring_nothing_is_no_declaration_not_clear_or_collides():
    verdict = ss.screen_candidate("SABLE-x", False, frozenset(), {
        "in_progress:SABLE-y": frozenset({"bin/a.py"}),
    })
    assert verdict.verdict == ss.NO_DECLARATION
    assert verdict.verdict != ss.CLEAR
    assert verdict.verdict != ss.COLLIDES


def test_negative_control_disjoint_candidate_is_clear():
    """Required negative control: a screen that reported COLLIDES
    unconditionally would still pass every positive-collision test below
    without this one."""
    verdict = ss.screen_candidate("SABLE-x", True, frozenset({"bin/only_mine.py"}), {
        "in_progress:SABLE-y": frozenset({"bin/a.py"}),
        "branch:wk-other": frozenset({"bin/b.py"}),
    })
    assert verdict.verdict == ss.CLEAR
    assert verdict.hits == {}


def test_candidate_colliding_with_in_progress_occupant():
    verdict = ss.screen_candidate("SABLE-x", True, frozenset({"bin/shared.py", "bin/mine.py"}), {
        "in_progress:SABLE-y": frozenset({"bin/shared.py"}),
    })
    assert verdict.verdict == ss.COLLIDES
    assert verdict.hits == {"in_progress:SABLE-y": ("bin/shared.py",)}


def test_candidate_colliding_with_branch_occupant_ground_truth():
    """SABLE-krbxd's leg: an uncontained branch's ACTUAL changed files (not a
    declaration) collides with the candidate."""
    verdict = ss.screen_candidate("SABLE-x", True, frozenset({"bin/shared.py"}), {
        "branch:wk-undeclared-collateral": frozenset({"bin/shared.py", "bin/other.py"}),
    })
    assert verdict.verdict == ss.COLLIDES
    assert verdict.hits == {"branch:wk-undeclared-collateral": ("bin/shared.py",)}


def test_collision_names_both_branch_and_shared_files():
    verdict = ss.screen_candidate("SABLE-x", True, frozenset({"a.py", "b.py"}), {
        "branch:wk-foo": frozenset({"a.py", "c.py"}),
    })
    assert "branch:wk-foo" in verdict.hits
    assert verdict.hits["branch:wk-foo"] == ("a.py",)


# ===========================================================================
# sibling_overlaps — leg (c), candidate-vs-candidate (SABLE-1muvg)
# ===========================================================================

def test_sibling_overlaps_names_both_ids_and_the_shared_path():
    """THE DEFECT: `dispatch A B` returned CLEAR/CLEAR for two beads that
    declared the same files, because nothing ever compared the candidates to
    each other. Both directions must be reported — a manager reading either
    bead's row has to see the conflict."""
    overlaps = ss.sibling_overlaps({
        "SABLE-a": frozenset({"bin/shared.py", "bin/a_only.py"}),
        "SABLE-b": frozenset({"bin/shared.py", "bin/b_only.py"}),
    })
    assert overlaps == {
        "SABLE-a": {"SABLE-b": ("bin/shared.py",)},
        "SABLE-b": {"SABLE-a": ("bin/shared.py",)},
    }


def test_sibling_overlaps_catches_byte_identical_declarations():
    """The MAXIMAL-overlap case, measured live: `dispatch SABLE-su3j3
    SABLE-xgb29` returned CLEAR/CLEAR for byte-identical three-path declared
    sets. If the pairwise axis cannot catch total identity it is catching
    nothing at all."""
    identical = frozenset({"bin/sable-recover", "bin/test_sable_recover.py",
                           "bin/test_sable_recover_integration.py"})
    overlaps = ss.sibling_overlaps({"SABLE-su3j3": identical,
                                    "SABLE-xgb29": identical})
    assert overlaps["SABLE-su3j3"]["SABLE-xgb29"] == tuple(sorted(identical))
    assert overlaps["SABLE-xgb29"]["SABLE-su3j3"] == tuple(sorted(identical))


def test_sibling_overlaps_negative_control_disjoint_candidates():
    """REQUIRED NEGATIVE CONTROL: without it, "every pair collides" passes
    every positive test above and silently destroys co-dispatch — the screen
    that can never release, which is exactly as broken as one that can never
    deny."""
    assert ss.sibling_overlaps({
        "SABLE-a": frozenset({"bin/a.py"}),
        "SABLE-b": frozenset({"bin/b.py"}),
        "SABLE-c": frozenset({"bin/c.py"}),
    }) == {}


def test_sibling_overlaps_single_candidate_cannot_fire():
    """N=1 has no pair to form, so single-arg behaviour is untouched by leg
    (c) — the compatibility guarantee the fix was required to keep."""
    assert ss.sibling_overlaps({"SABLE-a": frozenset({"bin/a.py"})}) == {}


def test_sibling_overlaps_is_all_pairs_not_each_against_the_first():
    """SET-LEVEL, which no two-candidate fixture can state: three candidates
    where ONLY the 2nd and 3rd overlap. An implementation that compares each
    candidate against the FIRST one passes every 2-candidate test above and
    fails here."""
    overlaps = ss.sibling_overlaps({
        "SABLE-first": frozenset({"bin/lonely.py"}),
        "SABLE-second": frozenset({"bin/contested.py"}),
        "SABLE-third": frozenset({"bin/contested.py"}),
    })
    assert "SABLE-first" not in overlaps
    assert overlaps["SABLE-second"] == {"SABLE-third": ("bin/contested.py",)}
    assert overlaps["SABLE-third"] == {"SABLE-second": ("bin/contested.py",)}


def test_sibling_overlaps_reports_every_shared_path_not_just_the_first():
    overlaps = ss.sibling_overlaps({
        "SABLE-a": frozenset({"bin/x.py", "bin/y.py", "bin/a.py"}),
        "SABLE-b": frozenset({"bin/x.py", "bin/y.py", "bin/b.py"}),
    })
    assert overlaps["SABLE-a"]["SABLE-b"] == ("bin/x.py", "bin/y.py")


def test_sibling_overlaps_undeclared_candidates_are_absent_not_clear():
    """SABLE-e2ic3's trichotomy, applied to the pairwise axis: candidates
    that declare nothing are never handed to sibling_overlaps at all (see
    run_dispatch), so a pair of undeclared beads can never be reported as
    'compared, found nothing'. Two beads that declared an EMPTY footprint are
    a real declaration and legitimately do not overlap."""
    assert ss.sibling_overlaps({"SABLE-a": frozenset(), "SABLE-b": frozenset()}) == {}


def test_screen_candidate_sibling_collision_is_its_own_verdict():
    verdict = ss.screen_candidate(
        "SABLE-a", True, frozenset({"bin/shared.py"}), {},
        sibling_hits={"SABLE-b": ("bin/shared.py",)})
    assert verdict.verdict == ss.SIBLING_COLLISION
    assert verdict.verdict != ss.CLEAR
    assert verdict.verdict != ss.COLLIDES
    assert verdict.sibling_hits == {"SABLE-b": ("bin/shared.py",)}


def test_screen_candidate_occupant_collision_outranks_sibling_but_keeps_both():
    """A candidate that hits an occupant AND a sibling reads COLLIDES (the
    stronger fact wins the label), but the sibling hit is still carried —
    neither remedy is discarded."""
    verdict = ss.screen_candidate(
        "SABLE-a", True, frozenset({"bin/occupied.py", "bin/shared.py"}),
        {"in_progress:SABLE-z": frozenset({"bin/occupied.py"})},
        sibling_hits={"SABLE-b": ("bin/shared.py",)})
    assert verdict.verdict == ss.COLLIDES
    assert verdict.hits == {"in_progress:SABLE-z": ("bin/occupied.py",)}
    assert verdict.sibling_hits == {"SABLE-b": ("bin/shared.py",)}


def test_screen_candidate_no_declaration_outranks_a_sibling_hit():
    """An undeclared candidate has no operands on any axis; it must stay
    NO-DECLARATION rather than acquire a footprint verdict from a sibling."""
    verdict = ss.screen_candidate("SABLE-a", False, frozenset(), {},
                                  sibling_hits={"SABLE-b": ("bin/x.py",)})
    assert verdict.verdict == ss.NO_DECLARATION


def test_render_sibling_collision_names_both_ids_the_path_and_the_remedy():
    """The rendered line is the whole deliverable for a manager: the live
    defect was that the contradicting operands were printed one line BELOW a
    CLEAR verdict, so a reader skimming verdicts learned the wrong thing."""
    out = ss.render_dispatch_text([{
        "bead_id": "SABLE-a", "title": "lead", "verdict": ss.SIBLING_COLLISION,
        "declared_files": ["bin/shared.py"], "hits": {},
        "sibling_hits": {"SABLE-b": ("bin/shared.py",)},
        "readiness": ss.READY, "blockers": [], "status": "open",
    }], "origin/tmux-only")
    assert "SIBLING-COLLISION" in out
    assert "SABLE-b" in out
    assert "bin/shared.py" in out
    assert "--bundle" in out


# ===========================================================================
# readiness axis — pure (SABLE-qyr0f)
#
# Shape note: `bd show --json` resolves each dependency to a full record with
# a 'status' and a 'dependency_type'; `bd list --json` returns raw edges
# ({'depends_on_id', 'type'}) with no status. Both shapes are exercised.
# ===========================================================================

def _dep(dep_id, status="open", dtype="blocks"):
    return {"id": dep_id, "status": status, "dependency_type": dtype}


def test_blocked_bead_declaring_only_new_files_is_not_reported_clear():
    """THE DEFECT, in its exact live shape: SABLE-21rug.3 declared two files
    that existed nowhere yet — so it overlapped nothing and screened a clean
    ✓ CLEAR, the most confident output the tool can emit — while blocked by
    an open sibling. A bead declaring only NEW files is BOTH maximally likely
    to screen CLEAR and exactly the shape of a later-in-the-chain bead."""
    bead = {"id": "SABLE-21rug.3", "status": "open",
            "dependencies": [_dep("SABLE-21rug.2", status="open")]}
    readiness, blockers = ss.readiness_for(bead, frozenset())
    assert readiness == ss.BLOCKED
    assert blockers == ("SABLE-21rug.2",)

    verdict = ss.screen_candidate(
        "SABLE-21rug.3", True,
        frozenset({"bin/sable-tier-runner", "bin/test_sable_tier_runner.py"}),
        {"in_progress:SABLE-other": frozenset({"bin/unrelated.py"})},
        readiness=readiness, blockers=blockers)
    # the footprint axis is honestly CLEAR — the readiness axis is what denies
    assert verdict.verdict == ss.CLEAR
    assert verdict.readiness == ss.BLOCKED
    assert verdict.blockers == ("SABLE-21rug.2",)
    assert ss.dispatch_exit_code([{"verdict": verdict.verdict,
                                   "readiness": verdict.readiness}]) == 1


def test_ready_bead_with_no_overlap_still_reports_clear():
    """NEGATIVE CONTROL: the fix must not pass by making everything
    non-dispatchable."""
    bead = {"id": "SABLE-ready", "status": "open", "dependencies": []}
    readiness, blockers = ss.readiness_for(bead, frozenset({"SABLE-ready"}))
    assert readiness == ss.READY
    assert blockers == ()
    verdict = ss.screen_candidate("SABLE-ready", True, frozenset({"bin/new.py"}),
                                  {}, readiness=readiness, blockers=blockers)
    assert verdict.verdict == ss.CLEAR
    assert ss.dispatch_exit_code([{"verdict": ss.CLEAR,
                                   "readiness": ss.READY}]) == 0


def test_blocked_and_colliding_reports_both_facts():
    """Two different facts with two different remedies. Folding readiness
    into COLLIDES would recreate the very class this bead is about."""
    bead = {"id": "SABLE-both", "status": "open",
            "dependencies": [_dep("SABLE-blocker")]}
    readiness, blockers = ss.readiness_for(bead, frozenset())
    verdict = ss.screen_candidate(
        "SABLE-both", True, frozenset({"bin/contested.py"}),
        {"in_progress:SABLE-occupant": frozenset({"bin/contested.py"})},
        readiness=readiness, blockers=blockers)
    assert verdict.verdict == ss.COLLIDES
    assert verdict.hits == {"in_progress:SABLE-occupant": ("bin/contested.py",)}
    assert verdict.readiness == ss.BLOCKED
    assert verdict.blockers == ("SABLE-blocker",)


def test_closed_blocker_does_not_block():
    """Guards against reading dep PRESENCE instead of dep STATUS — a screen
    that counted every dependency would deny every bead that ever had one."""
    bead = {"id": "SABLE-x", "status": "open",
            "dependencies": [_dep("SABLE-done", status="closed")]}
    readiness, blockers = ss.readiness_for(bead, frozenset({"SABLE-x"}))
    assert readiness == ss.READY
    assert blockers == ()


def test_relates_to_and_parent_child_edges_do_not_block():
    """Only 'blocks' gates readiness. 442 of this store's 538 edges are
    'relates-to' (and SABLE-1muvg, the bead that produced this very fix,
    carries three open ones) — counting them would report almost every
    cross-referenced bead as blocked."""
    bead = {"id": "SABLE-1muvg", "status": "open", "dependencies": [
        _dep("SABLE-4t97d", status="open", dtype="relates-to"),
        _dep("SABLE-k88ci", status="open", dtype="relates-to"),
        _dep("SABLE-epic", status="open", dtype="parent-child"),
    ]}
    assert ss.open_blocking_deps(bead) == ()
    readiness, _ = ss.readiness_for(bead, frozenset({"SABLE-1muvg"}))
    assert readiness == ss.READY


def test_open_blocking_deps_accepts_the_bd_list_edge_shape_too():
    """`bd list --json` spells the same edge {'depends_on_id', 'type'} with
    no status. An unreadable status must fall toward the DENY, never toward
    the release."""
    bead = {"id": "SABLE-x", "dependencies": [
        {"issue_id": "SABLE-x", "depends_on_id": "SABLE-blk", "type": "blocks"},
    ]}
    assert ss.open_blocking_deps(bead) == ("SABLE-blk",)


def test_absent_from_ready_without_a_blocker_is_not_ready_not_blocked():
    """optimus's caveat, pinned: an in_progress bead is ALSO absent from `bd
    ready`, so absence alone means 'not dispatchable now', not 'blocked by a
    dependency'. Measured against this repo's store, two open beads with zero
    blockers sit outside the ready pool — collapsing that into BLOCKED would
    name a blocker that does not exist."""
    bead = {"id": "SABLE-claimed", "status": "in_progress", "dependencies": []}
    readiness, blockers = ss.readiness_for(bead, frozenset({"SABLE-other"}))
    assert readiness == ss.NOT_READY
    assert readiness != ss.BLOCKED
    assert blockers == ()
    # reported, but it does not deny: the signal is ambiguous by construction
    assert ss.dispatch_exit_code([{"verdict": ss.CLEAR,
                                   "readiness": ss.NOT_READY}]) == 0


def test_unreadable_ready_set_is_unknown_not_a_guess_in_either_direction():
    """A probe that cannot fire is indistinguishable from a condition never
    met: if `bd ready` could not be read at all, the screen says so rather
    than inventing READY (a silent release) or NOT-READY (a false alarm on
    every candidate)."""
    bead = {"id": "SABLE-x", "status": "open", "dependencies": []}
    readiness, blockers = ss.readiness_for(bead, None)
    assert readiness == ss.READINESS_UNKNOWN
    assert readiness not in (ss.READY, ss.NOT_READY, ss.BLOCKED)
    assert blockers == ()


def test_unreadable_ready_set_still_denies_on_the_precise_blocker_leg():
    """The two readiness sources have different precisions, and the precise
    one does not depend on `bd ready` at all — a broken ready query must not
    disarm the blocker check."""
    bead = {"id": "SABLE-x", "status": "open",
            "dependencies": [_dep("SABLE-blocker")]}
    readiness, blockers = ss.readiness_for(bead, None)
    assert readiness == ss.BLOCKED
    assert blockers == ("SABLE-blocker",)


def test_render_blocked_names_the_blocker_ids():
    out = ss.render_dispatch_text([{
        "bead_id": "SABLE-21rug.3", "title": "later-chain bead",
        "verdict": ss.CLEAR, "declared_files": ["bin/sable-tier-runner"],
        "hits": {}, "sibling_hits": {}, "readiness": ss.BLOCKED,
        "blockers": ["SABLE-21rug.2"], "status": "open",
    }], "origin/tmux-only")
    assert "BLOCKED" in out
    assert "SABLE-21rug.2" in out


def test_render_ready_candidate_adds_no_readiness_line():
    """The compatibility guarantee: the ordinary case prints exactly what it
    printed before the readiness axis existed."""
    row = {"bead_id": "SABLE-x", "title": "t", "verdict": ss.CLEAR,
           "declared_files": ["bin/a.py"], "hits": {}, "sibling_hits": {},
           "readiness": ss.READY, "blockers": [], "status": "open"}
    out = ss.render_dispatch_text([row], "origin/tmux-only")
    assert out == ("integration ref: origin/tmux-only\n"
                   "\n"
                   "✓ SABLE-x [CLEAR] t\n"
                   "    declares: bin/a.py\n")


# ===========================================================================
# in_progress_occupants — pure, no git/bd
# ===========================================================================

def test_in_progress_declaration_still_counted():
    beads = [
        {"id": "SABLE-a", "metadata": {"wip_claims": "bin/a.py"}},
        {"id": "SABLE-b", "metadata": {"wip_claims": "bin/b.py"}},
    ]
    occ = ss.in_progress_occupants(beads, exclude_ids={"SABLE-a"})
    assert "in_progress:SABLE-a" not in occ
    assert occ == {"in_progress:SABLE-b": frozenset({"bin/b.py"})}


def test_in_progress_occupants_skips_no_declaration_beads():
    """A bead that declares nothing does not occupy anything — that is a
    fact about occupancy, separate from a CANDIDATE's own no-declaration
    verdict."""
    beads = [{"id": "SABLE-a", "metadata": {}}]
    occ = ss.in_progress_occupants(beads, exclude_ids=set())
    assert occ == {}


# ===========================================================================
# resolve_ref — REAL git fixture, no bd, no network remote
# ===========================================================================

def test_resolve_ref_returns_sha_for_existing_ref(tmp_path):
    repo = _git_repo(tmp_path)
    sha = _commit(repo, "f.txt")
    _fake_remote_ref(repo, "wk-exists", sha)
    assert ss.resolve_ref(str(repo), "origin/wk-exists") == sha


def test_reaped_branch_regression_resolve_ref_returns_empty_for_absent_ref(tmp_path):
    """THE LOAD-BEARING REGRESSION. A branch that no longer exists on origin
    (landed and reaped, or never pushed) must resolve to EMPTY — never to a
    truthy string a caller could mistake for a real SHA. Uses a branch name
    guaranteed absent from this fixture repo (no ref of that name was ever
    created here)."""
    repo = _git_repo(tmp_path)
    _commit(repo, "f.txt")  # some history exists, but no wk-* ref at all
    assert ss.resolve_ref(str(repo), "origin/wk-definitely-absent-9f3ac1") == ""


def test_reaped_branch_regression_plant_and_fail_demonstrates_the_trap(tmp_path):
    """Exercises the EXACT rev-parse path that produced the 300-false-
    positive run: the bare form `git rev-parse <ref>` ECHOES the literal ref
    string to stdout for an absent ref (exit status is the only place the
    failure shows up), while resolve_ref's --verify --quiet form returns
    empty for the identical ref. A regression test that only ever calls the
    FIXED form could not tell a correct implementation from one that
    silently regressed back to the bare form — this test pins both sides so
    that if resolve_ref were ever rewritten to use the bare form again, this
    assertion goes red."""
    repo = _git_repo(tmp_path)
    _commit(repo, "f.txt")
    absent_ref = "origin/wk-definitely-absent-9f3ac1"

    bare = subprocess.run(["git", "rev-parse", absent_ref], cwd=str(repo),
                          capture_output=True, text=True)
    assert bare.returncode != 0, "absent ref must fail by exit status"
    assert bare.stdout.strip() == absent_ref, (
        "the trap: bare `git rev-parse` echoes the literal ref back to stdout "
        "for a ref that does not exist — this is what silently produced the "
        "300 false NEEDS-HOLD findings when a caller read stdout as the answer"
    )

    assert ss.resolve_ref(str(repo), absent_ref) == "", (
        "resolve_ref (the fix) must return EMPTY for the same absent ref, "
        "never the echoed literal string"
    )


# ===========================================================================
# is_ancestor — REAL git fixture
# ===========================================================================

def test_is_ancestor_true_for_real_ancestor(tmp_path):
    repo = _git_repo(tmp_path)
    base = _commit(repo, "base.txt")
    tip = _commit(repo, "tip.txt")
    assert ss.is_ancestor(str(repo), base, tip) is True


def test_is_ancestor_false_for_sibling_branch(tmp_path):
    repo = _git_repo(tmp_path)
    _commit(repo, "base.txt")
    _run("git", "checkout", "-qb", "side", cwd=str(repo))
    side_tip = _commit(repo, "side.txt")
    _run("git", "checkout", "-q", "-", cwd=str(repo))
    main_tip = _commit(repo, "main.txt")
    assert ss.is_ancestor(str(repo), side_tip, main_tip) is False


def test_is_ancestor_none_for_unresolvable_ref(tmp_path):
    repo = _git_repo(tmp_path)
    tip = _commit(repo, "f.txt")
    assert ss.is_ancestor(str(repo), "not-a-real-ref-at-all", tip) is None


# ===========================================================================
# hold_verdict_for_bead — REAL git fixture, no bd
# ===========================================================================

def test_hold_reaped_branch_is_not_needs_hold(tmp_path):
    """The reaped-branch regression, applied at the full function level: a
    closed bead whose branch no longer exists on origin must NOT be
    reported as needing a hold, regardless of its metadata."""
    repo = _git_repo(tmp_path)
    spine = _commit(repo, "spine.txt")
    _fake_remote_ref(repo, "tmux-only", spine)
    verdict, fields = ss.hold_verdict_for_bead(
        str(repo), "origin/tmux-only", "wk-gone-forever", {})
    assert verdict == ss.REAPED_OR_UNPUSHED
    assert verdict != ss.NEEDS_HOLD
    assert fields == ()


def test_hold_uncontained_branch_missing_fields_needs_hold(tmp_path):
    repo = _git_repo(tmp_path)
    spine = _commit(repo, "spine.txt")
    _fake_remote_ref(repo, "tmux-only", spine)
    _run("git", "checkout", "-qb", "wk-uncontained", cwd=str(repo))
    branch_tip = _commit(repo, "feature.txt")
    _run("git", "checkout", "-q", "-", cwd=str(repo))
    _fake_remote_ref(repo, "wk-uncontained", branch_tip)

    verdict, missing = ss.hold_verdict_for_bead(
        str(repo), "origin/tmux-only", "wk-uncontained", {})
    assert verdict == ss.NEEDS_HOLD
    assert set(missing) == set(ss.HOLD_FIELDS)


def test_hold_uncontained_branch_all_four_fields_present_is_held_ok(tmp_path):
    repo = _git_repo(tmp_path)
    spine = _commit(repo, "spine.txt")
    _fake_remote_ref(repo, "tmux-only", spine)
    _run("git", "checkout", "-qb", "wk-held", cwd=str(repo))
    branch_tip = _commit(repo, "feature.txt")
    _run("git", "checkout", "-q", "-", cwd=str(repo))
    _fake_remote_ref(repo, "wk-held", branch_tip)

    metadata = {"hold": "reason", "hold_by": "chuck",
                "hold_since": "2026-07-01", "hold_until": "2026-08-01"}
    verdict, missing = ss.hold_verdict_for_bead(
        str(repo), "origin/tmux-only", "wk-held", metadata)
    assert verdict == ss.HELD_OK
    assert missing == ()


def test_hold_contained_branch_is_landed_clean(tmp_path):
    repo = _git_repo(tmp_path)
    _commit(repo, "base.txt")
    _run("git", "checkout", "-qb", "wk-landed", cwd=str(repo))
    branch_tip = _commit(repo, "feature.txt")
    _run("git", "checkout", "-q", "-", cwd=str(repo))
    _run("git", "merge", "--ff-only", "wk-landed", cwd=str(repo))
    spine = _run("git", "rev-parse", "HEAD", cwd=str(repo)).stdout.strip()
    _fake_remote_ref(repo, "tmux-only", spine)
    _fake_remote_ref(repo, "wk-landed", branch_tip)

    verdict, fields = ss.hold_verdict_for_bead(
        str(repo), "origin/tmux-only", "wk-landed", {})
    assert verdict == ss.LANDED_CLEAN
    assert fields == ()


def test_hold_landed_branch_with_stale_hold_fields_is_flagged(tmp_path):
    repo = _git_repo(tmp_path)
    _commit(repo, "base.txt")
    _run("git", "checkout", "-qb", "wk-landed2", cwd=str(repo))
    branch_tip = _commit(repo, "feature.txt")
    _run("git", "checkout", "-q", "-", cwd=str(repo))
    _run("git", "merge", "--ff-only", "wk-landed2", cwd=str(repo))
    spine = _run("git", "rev-parse", "HEAD", cwd=str(repo)).stdout.strip()
    _fake_remote_ref(repo, "tmux-only", spine)
    _fake_remote_ref(repo, "wk-landed2", branch_tip)

    verdict, present = ss.hold_verdict_for_bead(
        str(repo), "origin/tmux-only", "wk-landed2", {"hold": "stale reason"})
    assert verdict == ss.LANDED_STALE_HOLD
    assert present == ("hold",)


# ===========================================================================
# hold_verdict_for_branch — bundled branches (SABLE-jhako), REAL git fixture
# ===========================================================================

def _uncontained_branch(repo, name):
    """A wk-* branch pushed to (fake) origin and NOT merged into tmux-only —
    the closed-but-unlanded shape a hold exists to protect."""
    spine = _commit(repo, f"spine-{name}.txt")
    _fake_remote_ref(repo, "tmux-only", spine)
    _run("git", "checkout", "-qb", name, cwd=str(repo))
    tip = _commit(repo, f"{name}-feature.txt")
    _run("git", "checkout", "-q", "-", cwd=str(repo))
    _fake_remote_ref(repo, name, tip)


_FULL_HOLD = {"hold": "merge seat", "hold_by": "chuck",
              "hold_since": "2026-07-24", "hold_until": "2026-07-31"}


def test_bundled_branch_held_via_one_sibling_reads_held_for_the_branch(tmp_path):
    """THE DEFECT: wk-recycle-handoff carried SABLE-vsfvl + SABLE-uc7kh;
    chuck held uc7kh and ONE run reported 'NEEDS HOLD: SABLE-vsfvl
    wk-recycle-handoff' next to 'SABLE-uc7kh held-ok' — the same branch
    simultaneously held and unheld depending on which bead you asked. The
    hold fields live on the bead; the thing held is the BRANCH."""
    repo = _git_repo(tmp_path)
    _uncontained_branch(repo, "wk-recycle-handoff")

    verdict, fields, holders = ss.hold_verdict_for_branch(
        str(repo), "origin/tmux-only", "wk-recycle-handoff",
        [{}, dict(_FULL_HOLD)])          # vsfvl bare, uc7kh fully held
    assert verdict == ss.HELD_OK
    assert verdict != ss.NEEDS_HOLD
    assert fields == ()
    assert holders == [1]


def test_bundled_branch_hold_order_does_not_matter(tmp_path):
    """ANY bead on the branch carrying the hold holds the branch — the sweep
    must not depend on which sibling the placer happened to be shown."""
    repo = _git_repo(tmp_path)
    _uncontained_branch(repo, "wk-bundled-first")

    verdict, fields, holders = ss.hold_verdict_for_branch(
        str(repo), "origin/tmux-only", "wk-bundled-first",
        [dict(_FULL_HOLD), {}])
    assert verdict == ss.HELD_OK
    assert holders == [0]


def test_bundled_branch_with_no_hold_anywhere_still_needs_a_hold(tmp_path):
    """NEGATIVE CONTROL: branch-scoping must not make every bundled branch
    read as held. A genuinely unheld branch is still a finding — and exactly
    ONE finding, not one per bead."""
    repo = _git_repo(tmp_path)
    _uncontained_branch(repo, "wk-nobody-held")

    verdict, missing, holders = ss.hold_verdict_for_branch(
        str(repo), "origin/tmux-only", "wk-nobody-held", [{}, {}])
    assert verdict == ss.NEEDS_HOLD
    assert set(missing) == set(ss.HOLD_FIELDS)
    assert holders == []


def test_bundled_branch_partial_holds_report_the_shortest_remaining_remedy(tmp_path):
    """No bead carries a COMPLETE hold, so the branch still needs one — and
    the reported remedy is what is missing on the best-covered bead, not the
    union across beads (which would ask a placer to re-add a field a sibling
    already carries)."""
    repo = _git_repo(tmp_path)
    _uncontained_branch(repo, "wk-partial")

    verdict, missing, holders = ss.hold_verdict_for_branch(
        str(repo), "origin/tmux-only", "wk-partial",
        [{"hold": "x"},
         {"hold": "x", "hold_by": "chuck", "hold_since": "2026-07-24"}])
    assert verdict == ss.NEEDS_HOLD
    assert missing == ("hold_until",)
    assert holders == []


def test_single_bead_branch_is_unchanged_by_branch_scoping(tmp_path):
    """SABLE-jhako's third control: the one-bead branch — every branch in the
    fleet before bundling existed — must behave exactly as it did, on both
    polarities."""
    repo = _git_repo(tmp_path)
    _uncontained_branch(repo, "wk-solo")

    bare = ss.hold_verdict_for_branch(str(repo), "origin/tmux-only", "wk-solo", [{}])
    assert bare[0] == ss.NEEDS_HOLD
    assert set(bare[1]) == set(ss.HOLD_FIELDS)

    held = ss.hold_verdict_for_branch(str(repo), "origin/tmux-only", "wk-solo",
                                      [dict(_FULL_HOLD)])
    assert held[0] == ss.HELD_OK

    # and the single-bead spelling agrees with the branch spelling
    assert ss.hold_verdict_for_bead(str(repo), "origin/tmux-only", "wk-solo",
                                    {}) == (ss.NEEDS_HOLD, bare[1])


def test_reaped_bundled_branch_is_reaped_not_needs_hold(tmp_path):
    """The reaped-branch regression survives branch-scoping: no ref on origin
    means REAPED_OR_UNPUSHED regardless of how many beads name the branch or
    what their metadata carries."""
    repo = _git_repo(tmp_path)
    spine = _commit(repo, "spine.txt")
    _fake_remote_ref(repo, "tmux-only", spine)
    verdict, fields, holders = ss.hold_verdict_for_branch(
        str(repo), "origin/tmux-only", "wk-gone-forever", [{}, dict(_FULL_HOLD)])
    assert verdict == ss.REAPED_OR_UNPUSHED
    assert verdict != ss.NEEDS_HOLD


def test_group_by_branch_puts_bundled_siblings_in_one_row():
    groups = ss.group_by_branch([
        {"id": "SABLE-vsfvl", "metadata": {"branch": "wk-recycle-handoff"}},
        {"id": "SABLE-uc7kh", "metadata": {"branch": "wk-recycle-handoff"}},
        {"id": "SABLE-solo", "metadata": {"branch": "wk-solo"}},
        {"id": "SABLE-nobranch", "metadata": {}},
    ])
    assert list(groups) == ["wk-recycle-handoff", "wk-solo"]
    assert [b["id"] for b in groups["wk-recycle-handoff"]] == ["SABLE-vsfvl", "SABLE-uc7kh"]
    assert [b["id"] for b in groups["wk-solo"]] == ["SABLE-solo"]


def test_render_holds_emits_one_row_per_branch_naming_every_bead():
    """The rendered sweep is where the contradiction was visible: one branch
    must produce ONE line, and the NEEDS HOLD section must not list a branch
    that some other bead already holds."""
    out = ss.render_holds_text([
        {"branch": "wk-recycle-handoff", "beads": ["SABLE-vsfvl", "SABLE-uc7kh"],
         "bead_id": "SABLE-vsfvl", "verdict": ss.HELD_OK, "fields": [],
         "held_by": ["SABLE-uc7kh"]},
        {"branch": "wk-stranded", "beads": ["SABLE-lonely"],
         "bead_id": "SABLE-lonely", "verdict": ss.NEEDS_HOLD,
         "fields": list(ss.HOLD_FIELDS), "held_by": []},
    ], "origin/tmux-only")

    assert out.count("wk-recycle-handoff") == 1
    assert "NEEDS HOLD (1)" in out
    needs_section = out.split("*** NEEDS HOLD")[1].split("other (")[0]
    assert "wk-recycle-handoff" not in needs_section
    assert "wk-stranded" in needs_section
    # both bundled beads are still named, so the reader can see who holds it
    assert "SABLE-vsfvl" in out and "SABLE-uc7kh" in out


# ===========================================================================
# wk_branch_occupants — REAL git fixture, no bd
# ===========================================================================

def test_closed_bead_with_uncontained_branch_still_occupies(tmp_path):
    """Branch occupancy deliberately has no bead-status input: a pushed
    branch remains live after its authoring bead closes.  A leg-(a)-only
    implementation returns an empty map for this fixture."""
    repo = _git_repo(tmp_path)
    _commit(repo, "base.txt")

    _run("git", "checkout", "-qb", "wk-landed", cwd=str(repo))
    landed_tip = _commit(repo, "landed.txt")
    _run("git", "checkout", "-q", "-", cwd=str(repo))
    _run("git", "merge", "--ff-only", "wk-landed", cwd=str(repo))
    spine = _run("git", "rev-parse", "HEAD", cwd=str(repo)).stdout.strip()

    _run("git", "checkout", "-qb", "wk-uncontained", spine, cwd=str(repo))
    uncontained_tip = _commit(repo, "uncontained.txt")
    _run("git", "checkout", "-q", "-", cwd=str(repo))

    _fake_remote_ref(repo, "tmux-only", spine)
    _fake_remote_ref(repo, "wk-landed", landed_tip)
    _fake_remote_ref(repo, "wk-uncontained", uncontained_tip)

    occ = ss.wk_branch_occupants(str(repo), "origin/tmux-only")
    assert occ.get("branch:wk-uncontained") == frozenset({"uncontained.txt"})


def test_contained_branch_stops_occupying(tmp_path, monkeypatch):
    """Negative control: the branch leg releases a path once its tip is
    contained.  An always-occupies implementation would be safe but unusable."""
    repo = _git_repo(tmp_path)
    _commit(repo, "base.txt")

    _run("git", "checkout", "-qb", "wk-landed", cwd=str(repo))
    landed_tip = _commit(repo, "landed.txt")
    _run("git", "checkout", "-q", "-", cwd=str(repo))
    _run("git", "merge", "--ff-only", "wk-landed", cwd=str(repo))
    spine = _run("git", "rev-parse", "HEAD", cwd=str(repo)).stdout.strip()

    _fake_remote_ref(repo, "tmux-only", spine)
    _fake_remote_ref(repo, "wk-landed", landed_tip)

    # Make the test depend on the containment decision itself.  In a normal
    # repository merge-base(integration, contained-tip) == tip, so the later
    # diff is empty and this test would accidentally pass even if the
    # containment guard were deleted.  A planted downstream path ensures only
    # the guard can release the branch.
    monkeypatch.setattr(
        ss, "branch_diff_files",
        lambda _repo, _base, _tip: frozenset({"landed.txt"}))

    occ = ss.wk_branch_occupants(str(repo), "origin/tmux-only")
    assert "branch:wk-landed" not in occ


def test_occupancy_consumers_route_through_shared_screen():
    """Architecture control: both production dispatch gates consume the
    canonical occupant-set command instead of maintaining private censuses."""
    root = Path(__file__).resolve().parents[1]
    spawn = (root / "bin" / "sable-spawn-worker").read_text()
    hook = (root / "hooks" / "multi-manager" /
            "pre-dispatch-overlap.sh").read_text()
    assert "_shared_occupancy_module" in spawn
    assert 'occupants --repo' in hook


def test_spawn_consumer_sees_uncontained_branch_and_releases_after_merge(tmp_path):
    """Exercise the Python consumer, not only sable-screen itself: its
    candidate parser remains local, while occupancy comes from the shared
    branch census and therefore survives a closed lifecycle."""
    root = Path(__file__).resolve().parents[1]
    name = "sable_spawn_occupancy_test"
    loader = SourceFileLoader(name, str(root / "bin" / "sable-spawn-worker"))
    spec = importlib.util.spec_from_loader(name, loader)
    spawn = importlib.util.module_from_spec(spec)
    sys.modules[name] = spawn
    loader.exec_module(spawn)

    repo = _git_repo(tmp_path)
    base = _commit(repo, "base.txt")
    _fake_remote_ref(repo, "tmux-only", base)
    (repo / ".sable").write_text("integrationBranch=tmux-only\n")

    _run("git", "checkout", "-qb", "wk-closed", cwd=str(repo))
    tip = _commit(repo, "shared.py")
    _run("git", "checkout", "-q", "-", cwd=str(repo))
    _fake_remote_ref(repo, "wk-closed", tip)

    candidate = {
        "id": "SABLE-candidate",
        "description": "work\n\n## File footprint\nshared.py",
        "metadata": {},
    }
    occupied = spawn.overlap_check(
        "SABLE-candidate", candidate, [], repo=str(repo))
    assert occupied.decision == "deny"
    assert "branch:wk-closed" in occupied.message
    assert "shared.py" in occupied.message

    _run("git", "merge", "--ff-only", "wk-closed", cwd=str(repo))
    merged = _run("git", "rev-parse", "HEAD", cwd=str(repo)).stdout.strip()
    _fake_remote_ref(repo, "tmux-only", merged)
    released = spawn.overlap_check(
        "SABLE-candidate", candidate, [], repo=str(repo))
    assert released.decision == "none"
