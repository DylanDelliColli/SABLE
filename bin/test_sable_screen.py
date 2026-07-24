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
# in_progress_occupants — pure, no git/bd
# ===========================================================================

def test_in_progress_occupants_excludes_dispatch_ids():
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
# wk_branch_occupants — REAL git fixture, no bd
# ===========================================================================

def test_wk_branch_occupants_excludes_landed_includes_uncontained(tmp_path):
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
    assert "branch:wk-landed" not in occ
    assert occ.get("branch:wk-uncontained") == frozenset({"uncontained.txt"})
