#!/usr/bin/env python3
"""Unit tests for sable_footprint_lib (SABLE-jd5fj.4).

The footprint is the EVIDENCE optimistic disjoint promotion rests on, so the
cases here are chosen around the four ways a disjointness check goes wrong in
practice rather than around the module's public surface:

  * a RENAME that is only counted at its destination makes an edit of the
    original file look disjoint from the rename;
  * a DELETION treated as "the path is gone, so it is not in the footprint"
    makes the modify/delete pair look disjoint — the single most cited
    textually-clean-but-broken merge shape;
  * a LOCKFILE or committed generated artifact makes two changes entangled
    through state that neither diff describes, with no path intersection to
    show for it;
  * a planner-declared footprint that NARROWS the mechanical one silently
    shrinks the blast radius the whole decision is computed over.

Real git is used wherever the answer depends on git's own behaviour (rename
detection thresholds, status letters) — a hand-written --name-status fixture
would be testing this module against my belief about git rather than against
git. The pure parser and the set algebra are tested directly, without a repo,
because they have no such dependency.
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sable_footprint_lib as fp  # noqa: E402
import sable_gate_git_lib as git_lib  # noqa: E402


# --------------------------------------------------------------------------
# Real-git fixtures
# --------------------------------------------------------------------------

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], text=True,
                          capture_output=True, check=True)


@pytest.fixture
def repo(tmp_path):
    """A throwaway repo with its own identity — never the caller's config."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "trunk")
    _git(r, "config", "user.email", "t@sable.invalid")
    _git(r, "config", "user.name", "SABLE Test")
    (r / "alpha.py").write_text("\n".join(f"alpha line {i}" for i in range(40)) + "\n")
    (r / "beta.py").write_text("beta\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    return r


def _sha(repo, ref="HEAD"):
    return _git(repo, "rev-parse", ref).stdout.strip()


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _sha(repo)


# --------------------------------------------------------------------------
# 1. Rename detection — BOTH sides of the rename are in the footprint
# --------------------------------------------------------------------------

def test_rename_puts_both_the_old_and_the_new_path_in_the_footprint(repo):
    base = _sha(repo)
    _git(repo, "mv", "alpha.py", "renamed_alpha.py")
    head = _commit(repo, "rename alpha")
    paths = fp.changed_paths(str(repo), base, head)
    assert "renamed_alpha.py" in paths, "the rename destination is missing"
    assert "alpha.py" in paths, (
        "the rename SOURCE is missing — a footprint that names only the "
        "destination calls a rename disjoint from an edit of the original file")


def test_a_rename_is_not_disjoint_from_an_edit_of_the_original_path(repo):
    """The property the case above exists for, stated end to end."""
    base = _sha(repo)
    _git(repo, "mv", "alpha.py", "renamed_alpha.py")
    renamed = _commit(repo, "rename alpha")
    _git(repo, "checkout", "-q", "-b", "other", base)
    (repo / "alpha.py").write_text("edited by the other side\n")
    edited = _commit(repo, "edit alpha")

    a = fp.mechanical_footprint(str(repo), base, renamed)
    b = fp.mechanical_footprint(str(repo), base, edited)
    verdict = fp.is_disjoint(a, b)
    assert verdict.disjoint is False
    assert "alpha.py" in verdict.reason


def test_rename_detection_is_actually_on(repo):
    """Non-vacuity: without --find-renames git would report A+D, which would
    ALSO include both paths. Assert the R status is what produced them, so this
    suite would notice the flag being dropped."""
    base = _sha(repo)
    _git(repo, "mv", "alpha.py", "renamed_alpha.py")
    head = _commit(repo, "rename alpha")
    out = _git(repo, "diff", "--name-status", "--find-renames", base, head).stdout
    assert out.startswith("R"), out


# --------------------------------------------------------------------------
# 2. D-status inclusion — a deletion is a change to that path
# --------------------------------------------------------------------------

def test_deleted_paths_are_in_the_footprint(repo):
    base = _sha(repo)
    _git(repo, "rm", "-q", "beta.py")
    head = _commit(repo, "delete beta")
    assert "beta.py" in fp.changed_paths(str(repo), base, head)


def test_modify_delete_pair_is_non_disjoint(repo):
    """THE case: one side deletes beta.py, the other edits it. Treating the
    deleted path as absent would report these as disjoint and license promoting
    a merge in which an edited file no longer exists."""
    base = _sha(repo)
    _git(repo, "rm", "-q", "beta.py")
    deleted = _commit(repo, "delete beta")
    _git(repo, "checkout", "-q", "-b", "editor", base)
    (repo / "beta.py").write_text("beta, still very much alive\n")
    edited = _commit(repo, "edit beta")

    verdict = fp.is_disjoint(fp.mechanical_footprint(str(repo), base, deleted),
                             fp.mechanical_footprint(str(repo), base, edited))
    assert verdict.disjoint is False
    assert "beta.py" in verdict.overlap


def test_genuinely_disjoint_changes_are_reported_disjoint(repo):
    """Non-vacuity for every case above: the check is not simply always False."""
    base = _sha(repo)
    (repo / "one.py").write_text("one\n")
    left = _commit(repo, "add one")
    _git(repo, "checkout", "-q", "-b", "right", base)
    (repo / "two.py").write_text("two\n")
    right = _commit(repo, "add two")
    verdict = fp.is_disjoint(fp.mechanical_footprint(str(repo), base, left),
                             fp.mechanical_footprint(str(repo), base, right))
    assert verdict.disjoint is True


# --------------------------------------------------------------------------
# 3. Sentinels — lockfiles and committed generated artifacts
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "package-lock.json", "yarn.lock", "poetry.lock", "go.sum", "Cargo.lock",
    "requirements.txt", "sub/project/uv.lock", "app/Gemfile.lock",
    "dist/bundle.js", "build/out.o", "vendor/lib/x.go", ".beads/metadata.json",
    "web/node_modules/pkg/index.js", "api/generated/client.ts",
    "static/app.min.js", "proto/service.pb.go", "schema_pb2.py",
    "src/__snapshots__/x.snap",
])
def test_sentinel_paths_are_recognized(path):
    assert fp.is_sentinel(path) is True, path


@pytest.mark.parametrize("path", [
    "bin/sable-merge-gate", "bin/sable_footprint_lib.py", "README.md",
    "hooks/test/test-preview-kick.sh", "src/distance.py", "docs/building.md",
    "lockfile_docs.md",
])
def test_ordinary_paths_are_not_sentinels(path):
    assert fp.is_sentinel(path) is False, path


def test_a_sentinel_on_either_side_forces_overlap_with_no_shared_path():
    """A sentinel does not need a counterpart. Both footprints below are
    path-disjoint; the lockfile alone decides it."""
    a = fp.footprint({"src/a.py", "package-lock.json"})
    b = fp.footprint({"docs/b.md"})
    assert fp.is_disjoint(a, b).disjoint is False
    assert fp.is_disjoint(b, a).disjoint is False, "the rule must be symmetric"


def test_two_lockfile_touching_changes_are_never_disjoint(repo):
    base = _sha(repo)
    (repo / "poetry.lock").write_text("left resolution\n")
    left = _commit(repo, "left dep bump")
    _git(repo, "checkout", "-q", "-b", "rightside", base)
    (repo / "docs.md").write_text("docs\n")
    (repo / "poetry.lock").write_text("right resolution\n")
    right = _commit(repo, "right dep bump")
    verdict = fp.is_disjoint(fp.mechanical_footprint(str(repo), base, left),
                             fp.mechanical_footprint(str(repo), base, right))
    assert verdict.disjoint is False
    assert "sentinel" in verdict.reason


# --------------------------------------------------------------------------
# 4. Wider governs — declared and mechanical footprints combine by UNION
# --------------------------------------------------------------------------

def test_widen_is_a_union_not_an_intersection():
    a = fp.footprint({"bin/one.py"})
    b = fp.footprint({"hooks/two.sh"})
    assert fp.widen(a, b).entries == {"bin/one.py", "hooks/two.sh"}


def test_a_declared_footprint_can_only_widen_the_answer():
    """A declared footprint that names MORE than the diff makes an otherwise
    disjoint pair overlap. This is the direction the contract requires: when the
    planner and the diff disagree, take the union of risk."""
    mechanical = fp.footprint({"bin/one.py"})
    declared = fp.footprint({"hooks/multi-manager/pre-push-rebase-test.sh"})
    other = fp.footprint({"hooks/multi-manager/pre-push-rebase-test.sh"})
    assert fp.is_disjoint(mechanical, other).disjoint is True
    assert fp.is_disjoint(fp.widen(mechanical, declared), other).disjoint is False


def test_a_declared_footprint_never_narrows_the_mechanical_one():
    """The unsafe direction, pinned: a narrow declaration cannot shrink a wide
    diff into a disjoint answer."""
    mechanical = fp.footprint({"bin/one.py", "bin/two.py"})
    declared = fp.footprint({"bin/one.py"})
    combined = fp.widen(mechanical, declared)
    assert mechanical.entries <= combined.entries
    assert fp.is_disjoint(combined, fp.footprint({"bin/two.py"})).disjoint is False


def test_declared_directory_prefixes_cover_everything_beneath_them():
    declared = fp.footprint({"bin/"})
    assert fp.is_disjoint(declared, fp.footprint({"bin/deep/nested.py"})).disjoint is False
    assert fp.is_disjoint(declared, fp.footprint({"binary/nested.py"})).disjoint is True, \
        "a prefix must match on path segments, not on raw string prefix"


def test_parse_declared_footprint_reads_the_bead_section():
    description = (
        "Story S2 blah blah.\n\n"
        "## File footprint\n"
        "bin/ (new sable_footprint_lib.py), bin/sable-merge-gate promote module,\n"
        "hooks/test/test-optimistic-promotion.sh\n\n"
        "## Test spec\n"
        "hooks/test/test-should-not-be-picked-up.sh\n"
    )
    entries = fp.parse_declared_footprint(description)
    assert "bin/" in entries
    assert "bin/sable-merge-gate" in entries
    assert "hooks/test/test-optimistic-promotion.sh" in entries
    assert not any("should-not-be-picked-up" in e for e in entries), \
        "parsing ran past the end of the footprint section"


def test_parse_declared_footprint_is_empty_without_a_section():
    assert fp.parse_declared_footprint("no footprint here at all") == frozenset()


# --------------------------------------------------------------------------
# 5. Fail-closed: a non-answer is never an empty (vacuously disjoint) footprint
# --------------------------------------------------------------------------

def test_unparseable_diff_output_raises_rather_than_returning_empty():
    with pytest.raises(fp.FootprintUndetermined):
        fp.parse_name_status("this is not a name-status line\n")


def test_unmerged_status_raises():
    with pytest.raises(fp.FootprintUndetermined):
        fp.parse_name_status("U\tconflicted.py\n")


def test_a_failed_diff_raises(repo):
    with pytest.raises(fp.FootprintUndetermined):
        fp.changed_paths(str(repo), "0" * 40, "1" * 40)


def test_declared_footprint_raises_when_bd_cannot_be_read(repo, monkeypatch):
    monkeypatch.setenv("SABLE_MG_BD", "false")
    with pytest.raises(fp.FootprintUndetermined):
        fp.declared_footprint(str(repo), "SABLE-nope")


def test_assess_reports_undetermined_instead_of_raising(repo, monkeypatch):
    """assess() is the caller-facing entry point and must never throw — but its
    tri-state answer must distinguish 'we looked' from 'we could not look'."""
    monkeypatch.setenv("SABLE_MG_BD", "false")
    base = _sha(repo)
    (repo / "one.py").write_text("one\n")
    head = _commit(repo, "one")
    a = fp.assess(str(repo), "SABLE-x", base, head, base)
    assert a.disjoint is None
    assert "undetermined" in a.reason.lower()


def test_assess_widens_the_branch_side_with_the_declared_footprint(repo, monkeypatch, tmp_path):
    """End to end through the bd seam: a bead that declares a path the diff does
    not touch still makes the pair overlap."""
    fake_bd = tmp_path / "fake-bd"
    fake_bd.write_text("#!/bin/sh\necho '## File footprint'\necho 'shared/thing.py'\n")
    fake_bd.chmod(0o755)
    monkeypatch.setenv("SABLE_MG_BD", str(fake_bd))

    base = _sha(repo)
    (repo / "branch_only.py").write_text("x\n")
    branch = _commit(repo, "branch work")
    _git(repo, "checkout", "-q", "-b", "moved", base)
    (repo / "shared").mkdir()
    (repo / "shared" / "thing.py").write_text("moved base\n")
    new_base = _commit(repo, "base move")

    a = fp.assess(str(repo), "SABLE-x", base, branch, new_base)
    assert a.disjoint is False, a.reason
    assert "shared/thing.py" in a.reason


def test_assess_returns_the_union_of_paths_for_impact_scoping(repo, monkeypatch):
    monkeypatch.setenv("SABLE_MG_BD", "true")
    base = _sha(repo)
    (repo / "left.py").write_text("l\n")
    branch = _commit(repo, "left")
    _git(repo, "checkout", "-q", "-b", "moved", base)
    (repo / "right.py").write_text("r\n")
    new_base = _commit(repo, "right")
    a = fp.assess(str(repo), "SABLE-x", base, branch, new_base)
    assert a.disjoint is True
    assert set(a.paths) == {"left.py", "right.py"}


def test_the_git_seam_is_the_shared_one(monkeypatch):
    """This module must be stubbable through the same seam as the rest of the
    gate — one monkeypatch of git_lib._git reaches it too."""
    monkeypatch.setattr(git_lib, "_git", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("reached the real git")))
    with pytest.raises(AssertionError):
        fp.changed_paths("/nowhere", "a" * 40, "b" * 40)
