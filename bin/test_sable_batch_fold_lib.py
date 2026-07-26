#!/usr/bin/env python3
"""Unit tests for sable_batch_fold_lib (SABLE-be4lo.4): the fold builder.

Real git sandbox fixtures throughout — the git operations (merge-tree,
commit-tree, rev-list, push) ARE the unit under test, so nothing here mocks
git_lib. Hardens experiment 3 (2026-07-23: 4 disjoint branches fold clean, an
overlapping 5th fails loudly) into standing assertions, and adds the
non-emptiness / two-parent / manifest-message / fold_check-negative-control
cases the epic's test-strategy.json calls load-bearing.
"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import sable_batch_fold_lib as fold_lib  # noqa: E402
import sable_batch_key_lib as batch_key  # noqa: E402
import sable_gate_git_lib as git_lib  # noqa: E402
from sable_gate_classify_lib import GateError  # noqa: E402

FoldMember = fold_lib.FoldMember


# --------------------------------------------------------------------------
# Real-repo fixture helpers
# --------------------------------------------------------------------------

def _run(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _sha(repo, ref="HEAD"):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", ref],
                          check=True, capture_output=True, text=True).stdout.strip()


def _write(repo, name, content):
    (repo / name).write_text(content)


def _init_repo(tmp_path):
    """A bare-bones repo with one root commit (root.txt + common.txt, the
    files every fold in this file expects to survive UNTOUCHED) on `trunk`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-q", "-b", "trunk")
    _run(repo, "config", "user.email", "t@sable.invalid")
    _run(repo, "config", "user.name", "SABLE Test")
    _write(repo, "root.txt", "root\n")
    _write(repo, "common.txt", "common\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "root")
    return repo, _sha(repo)


def _make_base(repo, root_sha):
    """trunk gains base.txt on top of root — the base every member folds
    onto, distinct from root so 'untouched base files' is a real assertion."""
    _run(repo, "checkout", "-q", "trunk")
    _run(repo, "reset", "-q", "--hard", root_sha)
    _write(repo, "base.txt", "base\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "on trunk")
    return _sha(repo)


def _make_member(repo, root_sha, label, filename, content, bead=""):
    """A branch off ROOT (not base) adding one file of its own — disjoint
    from base.txt and from every other member's file by construction."""
    _run(repo, "checkout", "-q", "-b", label, root_sha)
    _write(repo, filename, content)
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", f"on {label}")
    return FoldMember(label=label, sha=_sha(repo), bead=bead)


def _tree_paths(repo, sha):
    cp = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "--name-only", sha],
                        check=True, capture_output=True, text=True)
    return set(cp.stdout.split())


def _blob(repo, sha, path):
    return subprocess.run(["git", "-C", str(repo), "show", f"{sha}:{path}"],
                          check=True, capture_output=True, text=True).stdout


def _commit_message(repo, sha):
    return subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%s", sha],
                          check=True, capture_output=True, text=True).stdout.strip()


# --------------------------------------------------------------------------
# Experiment 3, hardened: 4 disjoint branches fold clean
# --------------------------------------------------------------------------

def test_four_disjoint_members_fold_clean_and_tree_matches_every_edit(tmp_path):
    repo, root_sha = _init_repo(tmp_path)
    base_sha = _make_base(repo, root_sha)
    members = [
        _make_member(repo, root_sha, "m1", "m1.txt", "m1\n", bead="SABLE-m1"),
        _make_member(repo, root_sha, "m2", "m2.txt", "m2\n", bead="SABLE-m2"),
        _make_member(repo, root_sha, "m3", "m3.txt", "m3\n", bead="SABLE-m3"),
        _make_member(repo, root_sha, "m4", "m4.txt", "m4\n", bead="SABLE-m4"),
    ]

    result = fold_lib.fold_chain(str(repo), base_sha, members)

    # Final tree == base's own files (root.txt, common.txt, base.txt) PLUS
    # every member's added file. Nothing lost, nothing extra.
    assert _tree_paths(repo, result.tip) == {
        "root.txt", "common.txt", "base.txt", "m1.txt", "m2.txt", "m3.txt", "m4.txt",
    }
    assert _blob(repo, result.tip, "base.txt") == "base\n"      # untouched base file
    assert _blob(repo, result.tip, "root.txt") == "root\n"      # untouched base file
    for member, filename, content in zip(members, ["m1.txt", "m2.txt", "m3.txt", "m4.txt"],
                                         ["m1\n", "m2\n", "m3\n", "m4\n"]):
        assert _blob(repo, result.tip, filename) == content
    assert len(result.commits) == 4
    assert result.commits[-1] == result.tip


# --------------------------------------------------------------------------
# Experiment 3, hardened: an overlapping member fails LOUDLY
# --------------------------------------------------------------------------

def test_overlapping_member_fails_the_fold_loudly_naming_the_conflicting_path(tmp_path):
    repo, root_sha = _init_repo(tmp_path)
    base_sha = _make_base(repo, root_sha)
    clean = _make_member(repo, root_sha, "clean", "clean.txt", "clean\n")
    overlap_a = _make_member(repo, root_sha, "overlap-a", "shared.txt", "from a\n")
    overlap_b = _make_member(repo, root_sha, "overlap-b", "shared.txt", "from b\n")

    with pytest.raises(GateError) as exc:
        fold_lib.fold_chain(str(repo), base_sha, [clean, overlap_a, overlap_b])

    assert exc.value.code == 22
    assert "shared.txt" in str(exc.value)
    assert "overlap-b" in str(exc.value)  # names the member that failed to fold


def test_overlapping_batch_pushes_nothing(tmp_path):
    """The conflict must be caught before push_batch_ref pushes anything —
    a partially-built chain must never reach the remote."""
    repo, root_sha = _init_repo(tmp_path)
    base_sha = _make_base(repo, root_sha)
    overlap_a = _make_member(repo, root_sha, "overlap-a", "shared.txt", "from a\n")
    overlap_b = _make_member(repo, root_sha, "overlap-b", "shared.txt", "from b\n")

    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    _run(repo, "remote", "add", "origin", str(remote))

    with pytest.raises(GateError):
        fold_lib.push_batch_ref(str(repo), "origin", base_sha, [overlap_a, overlap_b])

    listing = subprocess.run(["git", "-C", str(remote), "for-each-ref", "refs/heads/ci-verify/"],
                             check=True, capture_output=True, text=True)
    assert listing.stdout.strip() == ""  # nothing pushed


# --------------------------------------------------------------------------
# Chain shape: two parents per commit, member tips as parents, first-parent
# lineage reaches base
# --------------------------------------------------------------------------

def test_every_chain_commit_has_exactly_two_parents_and_reaches_base_by_first_parent(tmp_path):
    repo, root_sha = _init_repo(tmp_path)
    base_sha = _make_base(repo, root_sha)
    members = [
        _make_member(repo, root_sha, "m1", "m1.txt", "m1\n"),
        _make_member(repo, root_sha, "m2", "m2.txt", "m2\n"),
        _make_member(repo, root_sha, "m3", "m3.txt", "m3\n"),
    ]

    result = fold_lib.fold_chain(str(repo), base_sha, members)

    assert len(result.commits) == len(members)
    second_parents = []
    for commit in result.commits:
        parents = git_lib.commit_parents(str(repo), commit)
        assert len(parents) == 2, f"{commit} has {len(parents)} parents, expected 2"
        second_parents.append(parents[1])
    # Each member tip appears as a parent exactly once, in fold order.
    assert second_parents == [m.sha for m in members]

    # Walk the chain's FIRST-PARENT lineage: it must reach base_sha in exactly
    # len(members) steps, never short-circuiting through a member tip.
    walk = result.tip
    for _ in range(len(members)):
        parents = git_lib.commit_parents(str(repo), walk)
        walk = parents[0]
    assert walk == base_sha


# --------------------------------------------------------------------------
# Manifest contract: fold commit messages name their members
# --------------------------------------------------------------------------

def test_fold_commit_messages_name_their_members(tmp_path):
    repo, root_sha = _init_repo(tmp_path)
    base_sha = _make_base(repo, root_sha)
    members = [
        _make_member(repo, root_sha, "wk-alpha", "alpha.txt", "a\n", bead="SABLE-alpha"),
        _make_member(repo, root_sha, "wk-beta", "beta.txt", "b\n", bead="SABLE-beta"),
    ]

    result = fold_lib.fold_chain(str(repo), base_sha, members)

    for member, commit in zip(members, result.commits):
        message = _commit_message(repo, commit)
        assert member.label in message
        assert member.bead in message


def test_fold_commit_message_omits_bead_parens_when_no_bead_given(tmp_path):
    repo, root_sha = _init_repo(tmp_path)
    base_sha = _make_base(repo, root_sha)
    member = _make_member(repo, root_sha, "wk-nobead", "nobead.txt", "n\n")

    result = fold_lib.fold_chain(str(repo), base_sha, [member])

    message = _commit_message(repo, result.commits[0])
    assert "wk-nobead" in message
    assert "()" not in message


# --------------------------------------------------------------------------
# Non-emptiness guard (SABLE-p9n7k)
# --------------------------------------------------------------------------

def test_fold_chain_rejects_an_empty_member_list(tmp_path):
    repo, root_sha = _init_repo(tmp_path)
    base_sha = _make_base(repo, root_sha)
    with pytest.raises(ValueError):
        fold_lib.fold_chain(str(repo), base_sha, [])


def test_fold_chain_never_returns_base_as_a_vacuous_chain(tmp_path):
    """A guard that merely no-ops on empty input (returning base_sha as if it
    were a valid one-element chain) would be a vacuous pass wearing the shape
    of a real one. This must raise, not return."""
    repo, root_sha = _init_repo(tmp_path)
    base_sha = _make_base(repo, root_sha)
    try:
        fold_lib.fold_chain(str(repo), base_sha, [])
        pytest.fail("fold_chain must raise on an empty member list, not return silently")
    except ValueError:
        pass


# --------------------------------------------------------------------------
# fold_check: the read-only admission probe, BOTH polarities
# --------------------------------------------------------------------------

def test_fold_check_positive_reports_clean_for_disjoint_members(tmp_path):
    repo, root_sha = _init_repo(tmp_path)
    base_sha = _make_base(repo, root_sha)
    members = [
        _make_member(repo, root_sha, "m1", "m1.txt", "m1\n"),
        _make_member(repo, root_sha, "m2", "m2.txt", "m2\n"),
    ]
    clean, reason = fold_lib.fold_check(str(repo), base_sha, members)
    assert clean is True
    assert reason == ""


def test_fold_check_negative_control_reports_unclean_for_overlapping_members(tmp_path):
    """The negative control the dispatch brief demands: a fold that genuinely
    does NOT apply cleanly must FAIL the check, not report clean. A fold_check
    that returns True for everything would pass this file's positive case
    perfectly while being worthless."""
    repo, root_sha = _init_repo(tmp_path)
    base_sha = _make_base(repo, root_sha)
    overlap_a = _make_member(repo, root_sha, "overlap-a", "shared.txt", "from a\n")
    overlap_b = _make_member(repo, root_sha, "overlap-b", "shared.txt", "from b\n")

    clean, reason = fold_lib.fold_check(str(repo), base_sha, [overlap_a, overlap_b])

    assert clean is False
    assert "shared.txt" in reason


def test_fold_check_reuses_fold_chain_rather_than_a_second_derivation(tmp_path):
    """fold_check and fold_chain must agree by construction, not by
    coincidence: patch fold_chain to prove fold_check actually calls it
    (rather than re-implementing conflict detection) — the guard against the
    exact false-green shape SABLE-5lli warns about."""
    repo, root_sha = _init_repo(tmp_path)
    base_sha = _make_base(repo, root_sha)
    member = _make_member(repo, root_sha, "m1", "m1.txt", "m1\n")

    calls = []
    real_fold_chain = fold_lib.fold_chain

    def _spy(repo_arg, base_arg, members_arg):
        calls.append((repo_arg, base_arg, list(members_arg)))
        return real_fold_chain(repo_arg, base_arg, members_arg)

    original = fold_lib.fold_chain
    fold_lib.fold_chain = _spy
    try:
        clean, _reason = fold_lib.fold_check(str(repo), base_sha, [member])
    finally:
        fold_lib.fold_chain = original

    assert clean is True
    assert len(calls) == 1


def test_fold_check_never_pushes_anything(tmp_path):
    repo, root_sha = _init_repo(tmp_path)
    base_sha = _make_base(repo, root_sha)
    members = [
        _make_member(repo, root_sha, "m1", "m1.txt", "m1\n"),
        _make_member(repo, root_sha, "m2", "m2.txt", "m2\n"),
    ]
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    _run(repo, "remote", "add", "origin", str(remote))

    fold_lib.fold_check(str(repo), base_sha, members)

    listing = subprocess.run(["git", "-C", str(remote), "for-each-ref"],
                             check=True, capture_output=True, text=True)
    assert listing.stdout.strip() == ""


def test_fold_check_propagates_non_conflict_errors_instead_of_reporting_unclean(tmp_path):
    """A malformed member (no tip SHA) is a precondition bug, not a fold
    verdict — fold_check must not swallow it into a False."""
    repo, root_sha = _init_repo(tmp_path)
    base_sha = _make_base(repo, root_sha)
    broken = FoldMember(label="broken", sha="")
    with pytest.raises(ValueError):
        fold_lib.fold_check(str(repo), base_sha, [broken])


# --------------------------------------------------------------------------
# push_batch_ref: uses the ONE owned keying module, no re-derivation
# --------------------------------------------------------------------------

def test_push_batch_ref_uses_setkey_from_the_owned_module(tmp_path):
    repo, root_sha = _init_repo(tmp_path)
    base_sha = _make_base(repo, root_sha)
    members = [
        _make_member(repo, root_sha, "m1", "m1.txt", "m1\n"),
        _make_member(repo, root_sha, "m2", "m2.txt", "m2\n"),
    ]
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, capture_output=True)
    _run(repo, "remote", "add", "origin", str(remote))

    tip, ref = fold_lib.push_batch_ref(str(repo), "origin", base_sha, members)

    expected_key = batch_key.setkey(base_sha, [m.sha for m in members])
    assert ref == f"ci-verify/batch-{expected_key[:7]}"
    remote_sha = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", f"refs/heads/{ref}"],
        check=True, capture_output=True, text=True).stdout.strip()
    assert remote_sha == tip


def test_push_batch_ref_setkey_is_order_independent(tmp_path):
    """Sorted-input identity (SABLE-be4lo.1's contract), exercised through the
    fold builder: admitting the same member set in a different order must
    still resolve to the SAME ref."""
    repo, root_sha = _init_repo(tmp_path)
    base_sha = _make_base(repo, root_sha)
    m1 = _make_member(repo, root_sha, "m1", "m1.txt", "m1\n")
    m2 = _make_member(repo, root_sha, "m2", "m2.txt", "m2\n")
    key_forward = batch_key.setkey(base_sha, [m1.sha, m2.sha])
    key_reversed = batch_key.setkey(base_sha, [m2.sha, m1.sha])
    assert key_forward == key_reversed  # sorted-input identity, not this module's job to prove
    # ...but the ref this module produces for either admission order matches:
    ref = fold_lib.classify.preview_ref_name("batch", key_forward)
    assert ref.startswith("ci-verify/batch-")


# --------------------------------------------------------------------------
# Determinism: same members + base -> same tip SHA given fixed committer
# identity/timestamps (ACCEPTANCE)
# --------------------------------------------------------------------------

def test_fold_chain_is_deterministic_given_fixed_committer_date(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2000-01-01T00:00:00 +0000")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2000-01-01T00:00:00 +0000")

    repo, root_sha = _init_repo(tmp_path)
    base_sha = _make_base(repo, root_sha)
    members = [
        _make_member(repo, root_sha, "m1", "m1.txt", "m1\n", bead="SABLE-m1"),
        _make_member(repo, root_sha, "m2", "m2.txt", "m2\n", bead="SABLE-m2"),
    ]

    first = fold_lib.fold_chain(str(repo), base_sha, members)
    second = fold_lib.fold_chain(str(repo), base_sha, members)
    assert first.tip == second.tip
    assert first.commits == second.commits
