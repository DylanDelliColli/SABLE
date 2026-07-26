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
import json
import os
import shutil
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
    """Prefix COVERAGE is unchanged by SABLE-g0elq's tokenizer filter, and the two
    layers must not be confused: `_entry_covers` still expands any directory entry
    over everything beneath it. What changed is that the TOKENIZER will no longer
    MANUFACTURE a bare 'bin/' out of prose — see
    test_a_bare_directory_prefix_is_not_tokenized_from_prose below. This entry is
    therefore built directly, which is now the only way a depth-1 directory can
    reach this predicate at all."""
    declared = fp.footprint({"bin/"})
    assert fp.is_disjoint(declared, fp.footprint({"bin/deep/nested.py"})).disjoint is False
    assert fp.is_disjoint(declared, fp.footprint({"binary/nested.py"})).disjoint is True, \
        "a prefix must match on path segments, not on raw string prefix"
    # A DEEP directory entry — the form the tokenizer does still produce — covers
    # beneath itself identically. Asserted here so the filter cannot be "fixed"
    # later by disabling prefix coverage instead of narrowing the tokenizer.
    assert fp.is_disjoint(fp.footprint({"hooks/test/"}),
                          fp.footprint({"hooks/test/test-x.sh"})).disjoint is False


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
    # *** ASSERTION DELIBERATELY INVERTED (SABLE-g0elq), NOT RELAXED. ***
    # This line read `assert "bin/" in entries` and encoded the behaviour that
    # was later measured as a defect: the fixture body above is EXACTLY the live
    # prose form ('bin/ (annotation)') that a corpus census found on 6 beads, and
    # once the parenthetical is stripped the surviving 'bin/' is not a path but a
    # QUANTIFIER OVER EVERY FILE UNDER bin/ at fp._entry_covers — measured
    # non-disjoint against unrelated bin/ work, which silently destroys optimistic
    # parallel promotion for the bead. The replacement is an equally strong
    # positive claim about the opposite outcome, and every other assertion in this
    # test is untouched. Deliberate directory declarations still parse at depth >=2
    # (test_a_bare_directory_prefix_is_not_tokenized_from_prose).
    assert "bin/" not in entries, (
        "a bare top-level directory prefix scraped from prose became a declared "
        "entry — at _entry_covers that quantifies over every file beneath it")
    assert "bin/sable-merge-gate" in entries
    assert "hooks/test/test-optimistic-promotion.sh" in entries
    assert not any("should-not-be-picked-up" in e for e in entries), \
        "parsing ran past the end of the footprint section"


def test_a_bare_directory_prefix_is_not_tokenized_from_prose():
    """The depth rule, stated on its own and with its NEGATIVE CONTROL, because
    the threshold is the whole decision and it was measured rather than guessed:
    over 3257 live beads the depth-1 form was prose every time (6 beads), while
    all 9 deeper directory entries were deliberate declarations."""
    assert fp.parse_declared_footprint(
        "S.\n\n## File footprint\nbin/, hooks/, /\n") == frozenset()
    assert fp.parse_declared_footprint(
        "S.\n\n## File footprint\n.github/ci/, hooks/test/, "
        ".claude/sable/state/planning/SABLE-jd5fj/\n"
    ) == {".github/ci/", "hooks/test/",
          ".claude/sable/state/planning/SABLE-jd5fj/"}
    # Rejected, never silent: the tokens are reported through the SABLE-zx2yv
    # dropped channel so the refusal downstream can name them.
    _, entries, dropped = fp._collect_section(
        "S.\n\n## File footprint\nbin/, hooks/\n", fp._FOOTPRINT_HEADING)
    assert entries == frozenset()
    assert {"bin/", "hooks/"} <= dropped


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


def test_declared_footprint_prefers_the_structured_field(repo, tmp_path, monkeypatch):
    """SABLE-jd5fj.10: a structured `footprint_writes` metadata field is used
    VERBATIM and the prose parser is not consulted at all — even when a
    (deliberately different) '## File footprint' prose section is ALSO
    present in the same description, its entries must not leak into the
    result."""
    record = [{
        "description": "story text\n\n## File footprint\nprose/only.py",
        "metadata": {"footprint_writes": "structured/one.py,structured/two.py"},
    }]
    fake_bd = tmp_path / "fake-bd-structured"
    # printf, not echo -- dash's echo interprets \n escapes by default and
    # would corrupt the JSON's own \n escape into a literal newline.
    fake_bd.write_text(f"#!/bin/sh\nprintf '%s' '{json.dumps(record)}'\n")
    fake_bd.chmod(0o755)
    monkeypatch.setenv("SABLE_MG_BD", str(fake_bd))

    result = fp.declared_footprint(str(repo), "SABLE-x")
    assert result.entries == {"structured/one.py", "structured/two.py"}
    assert "prose/only.py" not in result.entries, (
        "the prose parser must not be consulted when the structured field is present")

    # A bead that predates the field carries no metadata key at all (and a bd
    # stub predating --json support just echoes raw prose regardless of the
    # flag) -- the prose parser must still be the fallback.
    fake_bd_prose = tmp_path / "fake-bd-prose-only"
    fake_bd_prose.write_text(
        "#!/bin/sh\necho '## File footprint'\necho 'legacy/prose.py'\n")
    fake_bd_prose.chmod(0o755)
    monkeypatch.setenv("SABLE_MG_BD", str(fake_bd_prose))
    fallback = fp.declared_footprint(str(repo), "SABLE-y")
    assert fallback.entries == {"legacy/prose.py"}


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


def test_assess_returns_the_union_of_paths_for_impact_scoping(repo, monkeypatch, tmp_path):
    """SABLE-jd5fj.18: write-write disjointness alone no longer suffices — a
    declared (even explicitly empty) read footprint is required to reach
    disjoint=True at all, so this end-to-end path is exercised with reads
    declared rather than with the old bare `SABLE_MG_BD=true` stub."""
    fake_bd = tmp_path / "fake-bd-reads-declared-empty"
    fake_bd.write_text("#!/bin/sh\necho '## File reads'\necho 'none'\n")
    fake_bd.chmod(0o755)
    monkeypatch.setenv("SABLE_MG_BD", str(fake_bd))
    base = _sha(repo)
    (repo / "left.py").write_text("l\n")
    branch = _commit(repo, "left")
    _git(repo, "checkout", "-q", "-b", "moved", base)
    (repo / "right.py").write_text("r\n")
    new_base = _commit(repo, "right")
    a = fp.assess(str(repo), "SABLE-x", base, branch, new_base)
    assert a.disjoint is True, a.reason
    assert set(a.paths) == {"left.py", "right.py"}


# --------------------------------------------------------------------------
# 6. Read-write coupling floor (SABLE-jd5fj.18)
# --------------------------------------------------------------------------

def test_is_rw_disjoint_coupled_pair_is_not_parallel_safe():
    """THE unit spec, literally: branch A writes={x.py} reads={t.sh}; branch B
    writes={t.sh} reads={}. The write footprints are file-disjoint (neither
    writes what the other writes); the read-write coupling must still report
    NOT-parallel-safe — the live defect this bead exists to close."""
    writes_a, reads_a = fp.footprint({"x.py"}), fp.footprint({"t.sh"})
    writes_b, reads_b = fp.footprint({"t.sh"}), fp.footprint(())
    verdict = fp.is_rw_disjoint(writes_a, reads_a, writes_b, reads_b)
    assert verdict.disjoint is False
    assert "t.sh" in verdict.reason


def test_is_rw_disjoint_negative_control_genuinely_independent_pairs_are_parallel_safe():
    """Non-vacuity: a predicate that always says 'unsafe' is trivially correct
    and destroys the entire optimistic path. Truly independent write AND read
    sets must still report PARALLEL-SAFE."""
    writes_a, reads_a = fp.footprint({"x.py"}), fp.footprint({"y.py"})
    writes_b, reads_b = fp.footprint({"z.py"}), fp.footprint({"w.py"})
    verdict = fp.is_rw_disjoint(writes_a, reads_a, writes_b, reads_b)
    assert verdict.disjoint is True


def test_is_rw_disjoint_catches_the_coupling_from_either_side():
    """The coupling can point either way — B reading what A writes must be
    caught exactly like A reading what B writes."""
    writes_a, reads_a = fp.footprint({"m.py"}), fp.footprint(())
    writes_b, reads_b = fp.footprint({"n.py"}), fp.footprint({"m.py"})
    verdict = fp.is_rw_disjoint(writes_a, reads_a, writes_b, reads_b)
    assert verdict.disjoint is False
    assert "m.py" in verdict.reason


def test_is_rw_disjoint_write_write_overlap_still_wins_first():
    """A write/write overlap must still be reported as such (unchanged from
    before this bead), not silently reframed as a read/write coupling."""
    writes_a, reads_a = fp.footprint({"shared.py"}), fp.footprint(())
    writes_b, reads_b = fp.footprint({"shared.py"}), fp.footprint(())
    verdict = fp.is_rw_disjoint(writes_a, reads_a, writes_b, reads_b)
    assert verdict.disjoint is False
    assert verdict.reason.startswith("write/write:")


def test_parse_declared_reads_distinguishes_absent_from_declared_empty():
    declared, entries = fp.parse_declared_reads("no reads section at all")
    assert declared is False
    assert entries == frozenset()
    declared2, entries2 = fp.parse_declared_reads("## File reads\nnone\n")
    assert declared2 is True
    assert entries2 == frozenset()


def test_parse_declared_reads_reads_the_bead_section():
    description = (
        "Story blah blah.\n\n"
        "## File reads\n"
        ".github/ci/test-tiers.sh\n\n"
        "## Test spec\n"
        "hooks/test/test-should-not-be-picked-up.sh\n"
    )
    declared, entries = fp.parse_declared_reads(description)
    assert declared is True
    assert ".github/ci/test-tiers.sh" in entries
    assert not any("should-not-be-picked-up" in e for e in entries), \
        "parsing ran past the end of the reads section"


def test_declared_reads_raises_when_section_is_absent(repo, monkeypatch):
    """The floor's whole point: an absent '## File reads' section is a
    non-answer, not an empty footprint — unlike parse_declared_footprint."""
    monkeypatch.setenv("SABLE_MG_BD", "true")
    with pytest.raises(fp.FootprintUndetermined):
        fp.declared_reads(str(repo), "SABLE-x")


def test_declared_reads_returns_empty_footprint_when_explicitly_declared_empty(repo, tmp_path, monkeypatch):
    fake_bd = tmp_path / "fake-bd-reads-empty"
    fake_bd.write_text("#!/bin/sh\necho '## File reads'\necho 'none'\n")
    fake_bd.chmod(0o755)
    monkeypatch.setenv("SABLE_MG_BD", str(fake_bd))
    result = fp.declared_reads(str(repo), "SABLE-x")
    assert result.entries == frozenset()


def test_declared_reads_raises_when_bd_cannot_be_read(repo, monkeypatch):
    monkeypatch.setenv("SABLE_MG_BD", "false")
    with pytest.raises(fp.FootprintUndetermined):
        fp.declared_reads(str(repo), "SABLE-nope")


def test_assess_forces_serialize_when_read_footprint_is_undeclared(repo, monkeypatch):
    """SECOND CONTROL (SABLE-jd5fj.18): write-write disjointness alone is not
    enough. With no '## File reads' section at all, the branch's read set is
    UNKNOWN, and an unknown read set must fail toward serialization — not
    toward the old silent 'parallel-safe' default this bead removes."""
    monkeypatch.setenv("SABLE_MG_BD", "true")
    base = _sha(repo)
    (repo / "left.py").write_text("l\n")
    branch = _commit(repo, "left")
    _git(repo, "checkout", "-q", "-b", "moved", base)
    (repo / "right.py").write_text("r\n")
    new_base = _commit(repo, "right")
    a = fp.assess(str(repo), "SABLE-x", base, branch, new_base)
    assert a.disjoint is None, a.reason
    assert "undetermined" in a.reason.lower()


def test_assess_serializes_when_branch_reads_what_base_move_writes(repo, monkeypatch, tmp_path):
    """THE concrete defect (SABLE-jd5fj.18), reproduced end to end through
    assess(): SABLE-jd5fj.8 declares it reads .github/ci/test-tiers.sh;
    SABLE-cmar4.5 (played here by the base-move) edits it. The write
    footprints are file-disjoint — no path is written by both — but the
    read-write coupling must still resolve to NOT-disjoint."""
    fake_bd = tmp_path / "fake-bd-reads-coupled"
    fake_bd.write_text("#!/bin/sh\necho '## File reads'\necho '.github/ci/test-tiers.sh'\n")
    fake_bd.chmod(0o755)
    monkeypatch.setenv("SABLE_MG_BD", str(fake_bd))

    base = _sha(repo)
    (repo / "bin").mkdir()
    (repo / "bin" / "tier_selection.py").write_text("x = 1\n")
    branch = _commit(repo, "branch reads test-tiers.sh, writes only bin/")
    _git(repo, "checkout", "-q", "-b", "moved", base)
    (repo / ".github").mkdir()
    (repo / ".github" / "ci").mkdir()
    (repo / ".github" / "ci" / "test-tiers.sh").write_text("budget=1\n")
    new_base = _commit(repo, "base move edits test-tiers.sh")

    ww = fp.is_disjoint(fp.mechanical_footprint(str(repo), base, branch),
                        fp.mechanical_footprint(str(repo), base, new_base))
    assert ww.disjoint is True, "the write footprints must already be disjoint, or this proves nothing new"

    a = fp.assess(str(repo), "SABLE-x", base, branch, new_base)
    assert a.disjoint is False, a.reason
    assert "test-tiers.sh" in a.reason
    assert "read/write coupling" in a.reason


def test_assess_still_promotes_when_reads_are_declared_and_disjoint(repo, monkeypatch, tmp_path):
    """Non-vacuity of the floor: a bead that DOES declare its read footprint
    and is genuinely disjoint — on writes AND reads — from the base-move
    still takes the optimistic path. The fix does not just serialize
    everything."""
    fake_bd = tmp_path / "fake-bd-reads-disjoint"
    fake_bd.write_text("#!/bin/sh\necho '## File reads'\necho 'unrelated/other.txt'\n")
    fake_bd.chmod(0o755)
    monkeypatch.setenv("SABLE_MG_BD", str(fake_bd))

    base = _sha(repo)
    (repo / "left.py").write_text("l\n")
    branch = _commit(repo, "left")
    _git(repo, "checkout", "-q", "-b", "moved", base)
    (repo / "right.py").write_text("r\n")
    new_base = _commit(repo, "right")

    a = fp.assess(str(repo), "SABLE-x", base, branch, new_base)
    assert a.disjoint is True, a.reason
    assert set(a.paths) == {"left.py", "right.py"}


def test_declared_reads_raises_when_a_token_in_the_section_is_unrecognised(repo, tmp_path, monkeypatch):
    """SABLE-zx2yv: the jd5fj.18 floor catches an ABSENT '## File reads'
    heading but used to miss a PRESENT one whose body contains a token the
    tokenizer cannot recognise as a path (no slash, no known code suffix) —
    'Makefile' here. That token used to vanish silently from the declared
    set, so a bead that actually reads Makefile looked like it read only
    bin/foo.py. Present-but-incomplete must be indistinguishable from an
    absent heading, never from a complete one. Plant-and-fail: against the
    pre-fix code this section returns a plain complete Footprint of one
    entry and this assertion fails."""
    fake_bd = tmp_path / "fake-bd-reads-partial"
    fake_bd.write_text(
        "#!/bin/sh\necho '## File reads'\necho 'Makefile'\necho 'bin/foo.py'\n")
    fake_bd.chmod(0o755)
    monkeypatch.setenv("SABLE_MG_BD", str(fake_bd))
    with pytest.raises(fp.FootprintUndetermined):
        fp.declared_reads(str(repo), "SABLE-x")


def test_declared_reads_negative_control_a_fully_recognised_section_stays_complete(repo, tmp_path, monkeypatch):
    """LOAD-BEARING negative control (SABLE-zx2yv): a '## File reads' section
    whose every token is path-shaped must still return an ordinary complete
    Footprint with no serialization penalty. Without this, a fix that marks
    every reads section undetermined would also pass the test above and
    silently serialize the whole fleet — the gate-that-can-never-release
    failure (SABLE-47try's DO-NOT clause, same polarity)."""
    fake_bd = tmp_path / "fake-bd-reads-complete"
    fake_bd.write_text(
        "#!/bin/sh\necho '## File reads'\necho 'bin/foo.py'\necho 'bin/bar.sh'\n")
    fake_bd.chmod(0o755)
    monkeypatch.setenv("SABLE_MG_BD", str(fake_bd))
    result = fp.declared_reads(str(repo), "SABLE-x")
    assert result.entries == frozenset({"bin/foo.py", "bin/bar.sh"})


def test_declared_footprint_writes_path_is_unaffected_by_a_dropped_token(repo, tmp_path, monkeypatch):
    """SABLE-zx2yv acceptance criterion: the fix must not make the WRITES
    path (parse_declared_footprint / declared_footprint) any stricter. A
    dropped token in a '## File footprint' section still just falls out of
    the declared set exactly as before this bead — the mechanical footprint
    governs the write side regardless (rule 3 in this module's docstring),
    so there is nothing for the write side to fail toward."""
    description = "## File footprint\nMakefile\nbin/foo.py\n"
    assert fp.parse_declared_footprint(description) == frozenset({"bin/foo.py"})

    fake_bd = tmp_path / "fake-bd-writes-partial"
    fake_bd.write_text(
        "#!/bin/sh\necho '## File footprint'\necho 'Makefile'\necho 'bin/foo.py'\n")
    fake_bd.chmod(0o755)
    monkeypatch.setenv("SABLE_MG_BD", str(fake_bd))
    result = fp.declared_footprint(str(repo), "SABLE-x")
    assert result.entries == frozenset({"bin/foo.py"})


def test_assess_serializes_when_reads_section_has_an_unrecognised_token(repo, tmp_path, monkeypatch):
    """End-to-end through assess() (SABLE-zx2yv): a bead's '## File reads'
    section names a bare repo-root filename ('Makefile') alongside a real
    path. Before this fix, 'Makefile' silently vanished from the declared
    read set, so a base-move that only edits Makefile looked write/write
    disjoint from the branch and the whole assessment reported
    disjoint=True — the RELEASING failure direction the bead describes, not
    a tokenizer nit. After the fix the incomplete declaration forces the
    same undetermined/serialize outcome an absent heading would."""
    fake_bd = tmp_path / "fake-bd-reads-partial-assess"
    fake_bd.write_text(
        "#!/bin/sh\necho '## File reads'\necho 'Makefile'\necho 'bin/foo.py'\n")
    fake_bd.chmod(0o755)
    monkeypatch.setenv("SABLE_MG_BD", str(fake_bd))

    base = _sha(repo)
    (repo / "left.py").write_text("l\n")
    branch = _commit(repo, "left")
    _git(repo, "checkout", "-q", "-b", "moved", base)
    (repo / "Makefile").write_text("all:\n\techo hi\n")
    new_base = _commit(repo, "edit Makefile")

    a = fp.assess(str(repo), "SABLE-x", base, branch, new_base)
    assert a.disjoint is None, a.reason
    assert "undetermined" in a.reason.lower()


def test_the_git_seam_is_the_shared_one(monkeypatch):
    """This module must be stubbable through the same seam as the rest of the
    gate — one monkeypatch of git_lib._git reaches it too."""
    monkeypatch.setattr(git_lib, "_git", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("reached the real git")))
    with pytest.raises(AssertionError):
        fp.changed_paths("/nowhere", "a" * 40, "b" * 40)


# --------------------------------------------------------------------------
# 7. Integration section (SABLE-be4lo.3): real bd sandbox, real pairwise loop
# --------------------------------------------------------------------------
#
# Every case above stubs `bd show` with a shell script via SABLE_MG_BD — the
# right choice for testing the parser and the disjointness algebra in
# isolation, but the merge-trains admission check (SABLE-be4lo.3) consumes
# declared_footprint() through the REAL `bd` binary for two REAL, live-shaped
# beads and then runs the actual pairwise disjointness loop over them. This
# section proves that path survives the real seam, not just a hand-written
# stub — mirrors test_footprint_lib_integration.py's sandbox discipline
# (throwaway HOME + `bd init --non-interactive`, never the developer's own
# beads DB) without touching that file, which SABLE-kznzo's redo worker was
# editing at dispatch time.

HAVE_BD = shutil.which("bd") is not None
_integration = pytest.mark.skipif(
    not HAVE_BD,
    reason="ci-verify clean-room has no bd by design; real-bd integration self-skips",
)

_ENV_LEAKS = ("CLAUDE_AGENT_NAME", "TMUX_PANE", "SABLE_TMUX_SOCKET", "SABLE_MG_BD")


def _robust_bd_init(work, home):
    """Mirrors test_footprint_lib_integration.py's helper: `bd init` on the
    embedded-Dolt backend can leave a partial DB on a first-run race (rc 0
    but no .beads/config.yaml) — gate success on that artifact and
    wipe+retry rather than run against a broken DB."""
    env = {k: v for k, v in os.environ.items() if k not in _ENV_LEAKS}
    env["HOME"] = str(home)
    env["BD_NON_INTERACTIVE"] = "1"
    env["CI"] = "true"
    beads = work / ".beads"
    last = None
    for _ in range(4):
        if beads.exists():
            shutil.rmtree(beads)
        last = subprocess.run(["bd", "init", "--non-interactive"], cwd=str(work),
                              env=env, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=180, check=False)
        if last.returncode == 0 and (beads / "config.yaml").is_file():
            return last
    raise AssertionError(f"bd init never produced a clean DB: {last.stdout if last else '<none>'}")


def _bd_create(work, home, title, description):
    env = {k: v for k, v in os.environ.items() if k not in _ENV_LEAKS}
    env["HOME"] = str(home)
    env["BD_NON_INTERACTIVE"] = "1"
    env["CI"] = "true"
    cp = subprocess.run(
        ["bd", "create", f"--title={title}", "--type=task",
         f"--description={description}"],
        cwd=str(work), env=env, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=60, check=True)
    for tok in cp.stdout.split():
        if tok.startswith("work-"):
            return tok.rstrip(":")
    raise AssertionError(f"could not find created bead id in: {cp.stdout!r}")


@pytest.fixture()
def bd_sandbox(tmp_path, monkeypatch):
    work = tmp_path / "work"
    home = tmp_path / "home"
    work.mkdir()
    home.mkdir()
    _git(work, "init", "-q", "-b", "trunk")
    (work / ".gitignore").write_text(".beads/\n")
    (work / "root.txt").write_text("root\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    _robust_bd_init(work, home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BD_NON_INTERACTIVE", "1")
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("SABLE_MG_BD", raising=False)
    return work, home


@_integration
def test_real_declared_footprints_of_a_disjoint_pair_admit(bd_sandbox):
    """Two real, live-shaped beads (own '## File footprint' sections) whose
    declared write sets share no path: the real pairwise loop through
    declared_footprint() + is_disjoint() must say disjoint=True."""
    work, _home = bd_sandbox
    bead_a = _bd_create(work, _home, "be4lo.3 integration: member A",
                        "Trains member.\n\n## File footprint\nbin/member_a.py\n")
    bead_b = _bd_create(work, _home, "be4lo.3 integration: member B",
                        "Trains member.\n\n## File footprint\nbin/member_b.py\n")

    fp_a = fp.declared_footprint(str(work), bead_a)
    fp_b = fp.declared_footprint(str(work), bead_b)
    verdict = fp.is_disjoint(fp_a, fp_b)
    assert verdict.disjoint is True, verdict.reason


@_integration
def test_real_declared_footprints_of_an_overlapping_pair_exclude(bd_sandbox):
    """The other polarity, same real seam: two real beads whose declared
    footprints share a path must come back NOT disjoint, naming the overlap
    — this is the exact check SABLE-be4lo.3's admission loop excludes a
    candidate on."""
    work, _home = bd_sandbox
    bead_a = _bd_create(work, _home, "be4lo.3 integration: member C",
                        "Trains member.\n\n## File footprint\nbin/shared_module.py\n")
    bead_b = _bd_create(work, _home, "be4lo.3 integration: member D",
                        "Trains member.\n\n## File footprint\nbin/shared_module.py\n")

    fp_a = fp.declared_footprint(str(work), bead_a)
    fp_b = fp.declared_footprint(str(work), bead_b)
    verdict = fp.is_disjoint(fp_a, fp_b)
    assert verdict.disjoint is False
    assert "bin/shared_module.py" in verdict.reason


# --------------------------------------------------------------------------
# The CLI seam the SHELL gates consume (SABLE-7gesd)
# --------------------------------------------------------------------------
# hooks/multi-manager/pre-dispatch-overlap.sh has no parser of its own any more —
# it pipes bd's JSON through this module. These pin the stream contract, because
# the shell side reads it with sed/grep and a silent format change would turn the
# gate inert (wired-and-firing-and-checking-nothing, SABLE-nn54x).

def _cli(mode, payload, *extra):
    import subprocess
    lib = str(Path(__file__).resolve().parent / "sable_footprint_lib.py")
    cp = subprocess.run([sys.executable, lib, mode, *extra],
                        input=json.dumps(payload), capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.splitlines()


def test_cli_read_declared_emits_the_four_source_union():
    bead = {
        "id": "SABLE-x",
        "notes": "WIP-CLAIMS: hooks/a.sh",
        "description": "S.\n\n## File footprint\nhooks/b.sh\n",
        "metadata": {"wip_claims": "hooks/c.sh", "footprint_writes": "hooks/d.sh"},
    }
    lines = _cli("--read-declared", [bead])
    assert sorted(lines) == ["fhooks/a.sh", "fhooks/b.sh", "fhooks/c.sh", "fhooks/d.sh"]


def test_cli_read_declared_emits_the_unreadable_channel_separately():
    lines = _cli("--read-declared",
                 [{"id": "SABLE-x",
                   "description": "S.\n\n## File footprint\nGitHub settings\n"}])
    assert not [ln for ln in lines if ln.startswith("f")]
    u = [ln for ln in lines if ln.startswith("u")]
    assert len(u) == 1 and "File footprint" in u[0] and "GitHub" in u[0]


def test_cli_scavenge_fires_on_an_ABSENT_heading_not_on_an_empty_result():
    """The leg-3 condition, pinned. It must be "no section heading", NOT "no files
    found": gating on emptiness would narrow the dispatch-side claim set for a bead
    that has metadata but no section, and fewer files compared means a real overlap
    can go unseen (the releasing direction, SABLE-546m5).

    And the complement, which is what stops the scavenge laundering a bad
    declaration: a heading that IS present but unreadable stays unreadable rather
    than being topped up from prose (SABLE-47try)."""
    # metadata present, NO heading -> scavenge still runs and adds prose tokens
    lines = _cli("--read-declared",
                 [{"id": "SABLE-x", "metadata": {"wip_claims": "hooks/claimed.sh"},
                   "description": "touches bin/scavenged.py as well"}],
                 "--scavenge")
    files = {ln[1:] for ln in lines if ln.startswith("f")}
    assert files == {"hooks/claimed.sh", "bin/scavenged.py"}

    # heading PRESENT but unreadable -> NOT topped up; stays could-not-assess
    lines = _cli("--read-declared",
                 [{"id": "SABLE-x",
                   "description": "mentions bin/elsewhere.py\n\n"
                                  "## File footprint\nprose only\n"}],
                 "--scavenge")
    assert not [ln for ln in lines if ln.startswith("f")], (
        "the scavenge laundered a mis-authored section into a successful parse")
    assert [ln for ln in lines if ln.startswith("u")]


def test_cli_read_declared_list_groups_by_bead_id():
    lines = _cli("--read-declared-list", [
        {"id": "SABLE-a", "description": "S.\n\n## File footprint\nhooks/a.sh\n"},
        {"id": "SABLE-b", "metadata": {"footprint_writes": "hooks/b.sh"}},
        {"description": "no id — skipped"},
    ])
    assert sorted(lines) == ["SABLE-a\tfhooks/a.sh", "SABLE-b\tfhooks/b.sh"]


def test_cli_never_crashes_the_gate_on_unreadable_input():
    """The hook runs under `set -euo pipefail`; a non-zero exit here would take
    the whole gate down and fail OPEN. Garbage in means an empty stream out, which
    the caller already handles as its own no-declaration verdict."""
    import subprocess
    lib = str(Path(__file__).resolve().parent / "sable_footprint_lib.py")
    for junk in ("", "not json at all", "null", "42", '{"unclosed":'):
        cp = subprocess.run([sys.executable, lib, "--read-declared"], input=junk,
                            capture_output=True, text=True)
        assert cp.returncode == 0, f"{junk!r} exited {cp.returncode}: {cp.stderr}"


# --------------------------------------------------------------------------
# section_bodies — EVERY matching section, not the first (SABLE-9dmuu)
# --------------------------------------------------------------------------

def test_section_bodies_returns_every_section_in_document_order():
    """The scan this replaces set a `found` flag it never reset and BROKE OUT of
    the loop at the first '#' after the first heading, so only the FIRST section
    was ever read. Asserted directly on the scanner, not only through the
    footprint reader, so the multiplicity property has more than one point of
    detection — a single failing test is a single point of failure for a control."""
    desc = ("Story.\n"
            "## File footprint\n"
            "first-a, first-b\n"
            "\n"
            "## Test spec\n"
            "words that must not be collected\n"
            "\n"
            "## File footprint\n"
            "second-a\n"
            "\n"
            "## Another\n"
            "more words\n"
            "\n"
            "## File footprint\n"
            "third-a\n")
    bodies = fp.section_bodies(desc, fp._FOOTPRINT_HEADING)
    assert len(bodies) == 3, f"sections dropped: {bodies}"
    assert "first-a, first-b" in bodies[0]
    assert "second-a" in bodies[1]
    assert "third-a" in bodies[2]
    assert not any("must not be collected" in b for b in bodies)
    assert not any("more words" in b for b in bodies)


def test_section_bodies_absent_heading_is_an_empty_list_not_one_empty_body():
    """The absent/declared-empty distinction at the scanner layer: NO sections is
    an empty list, while a PRESENT-but-empty section is one empty body. Collapsing
    them here would destroy the SABLE-47try trichotomy at its source, before any
    caller could draw it."""
    assert fp.section_bodies("no heading at all", fp._FOOTPRINT_HEADING) == []
    assert fp.section_bodies("S.\n## File footprint\n\n## Next\nx",
                             fp._FOOTPRINT_HEADING) == [""]


def test_multiple_sections_union_and_are_reported_by_the_footprint_reader():
    """Union + report, end to end at the lib layer. UNION because that is the
    reading a human gives an appended correction and it is the WIDENING (safe)
    direction; REPORT because silence is what let a discarded correction look
    identical to a clean read."""
    desc = ("S.\n\n## File footprint\nbin/first.py\n\n"
            "## Test spec\nx\n\n## File footprint\nbin/second.py\n")
    read = fp.read_footprint_section(desc)
    assert read.files == {"bin/first.py", "bin/second.py"}
    assert read.could_not_assess is False
    assert any("2" in s and "File footprint" in s for s in read.unreadable_sources)
    # parse_declared_footprint (the PROMOTE gate's entry point) unions too, so the
    # two moments cannot disagree about what the bead declared.
    assert fp.parse_declared_footprint(desc) == {"bin/first.py", "bin/second.py"}


def test_a_single_section_reports_no_multiplicity():
    """NEGATIVE CONTROL for the reporting half: 301 of 303 live beads with a
    footprint section have exactly one, so a multiplicity warning on the ordinary
    case would be noise on nearly every dispatch."""
    read = fp.read_footprint_section("S.\n\n## File footprint\nbin/a.py\n\n## Next\nx")
    assert read.files == {"bin/a.py"}
    assert read.unreadable_sources == ()
