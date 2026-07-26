#!/usr/bin/env python3
"""Unit tests for sable_batch_admission_lib (SABLE-be4lo.3): merge-trains
admission.

Real git is used for everything that depends on git's own behaviour — the
mechanical footprint (non_gate_class) and the fold check (zero_conflicts) —
mirroring test_sable_batch_fold_lib.py and test_footprint_lib.py's own
convention of never hand-simulating what git would actually say. The bd seam
(declared_footprint / declared_reads / the hold read) is stubbed at
sable_footprint_lib._read_bead — the ONE bd-read function every one of those
three consult, so faking it once covers all three, exactly like
git_lib._git is the one seam every git-dependent module shares (see
test_footprint_lib.py's own `test_the_git_seam_is_the_shared_one`). The
verdict/adoption seam (individually_green / clean_ff_adoption) is stubbed at
sable_gate_preview_lib.adopt_kicked_preview / read_verdict, matching how
test_sable_gate_promote_lib.py stubs the same module for ITS composition
tests rather than simulating `gh` underneath them.
"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import sable_batch_admission_lib as adm  # noqa: E402
import sable_footprint_lib as fp  # noqa: E402
import sable_gate_classify_lib as classify  # noqa: E402
import sable_gate_preview_lib as preview_lib  # noqa: E402
from sable_gate_classify_lib import GateError  # noqa: E402

Candidate = adm.Candidate


# --------------------------------------------------------------------------
# Real-repo fixture helpers (mirrors test_sable_batch_fold_lib.py)
# --------------------------------------------------------------------------

def _run(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _sha(repo, ref="HEAD"):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", ref],
                          check=True, capture_output=True, text=True).stdout.strip()


def _write(repo, name, content):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _run(r, "init", "-q", "-b", "trunk")
    _run(r, "config", "user.email", "t@sable.invalid")
    _run(r, "config", "user.name", "SABLE Test")
    _write(r, "root.txt", "root\n")
    _run(r, "add", "-A")
    _run(r, "commit", "-q", "-m", "root")
    return r


def _branch(repo, base_sha, label, filename, content):
    _run(repo, "checkout", "-q", "-b", label, base_sha)
    _write(repo, filename, content)
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", f"on {label}")
    return _sha(repo)


# --------------------------------------------------------------------------
# The shared bd-read seam: fake fp._read_bead, records keyed by bead id
# --------------------------------------------------------------------------

def _bead_record(writes="", reads="", hold=None):
    metadata = {"footprint_writes": writes, "footprint_reads_declared": reads}
    if hold:
        metadata["hold"] = hold
    return {"description": "", "metadata": metadata}


def _stub_bd(monkeypatch, records: dict):
    def _read(repo, bead):
        if bead not in records:
            raise fp.FootprintUndetermined(f"no such fixture bead: {bead}")
        return records[bead]
    monkeypatch.setattr(fp, "_read_bead", _read)


# --------------------------------------------------------------------------
# The shared verdict/adoption seam
# --------------------------------------------------------------------------

def _stub_all_green(monkeypatch, previews: dict):
    """previews: {branch_sha: preview_sha}. Every branch adopts and is GREEN."""
    def _adopt(repo, remote, branch, base_sha, branch_sha):
        if branch_sha not in previews:
            return None
        return (previews[branch_sha], f"ci-verify/{branch}-fixture")
    def _verdict(repo, ref, preview_sha):
        return classify.Verdict("success", "", preview_sha, ref,
                                source="precomputed", complete=True)
    monkeypatch.setattr(preview_lib, "adopt_kicked_preview", _adopt)
    monkeypatch.setattr(preview_lib, "read_verdict", _verdict)


# --------------------------------------------------------------------------
# gate_class_roster
# --------------------------------------------------------------------------

def test_gate_class_roster_is_derived_never_hand_restated(repo):
    """The roster is computed by walking bin/sable-merge-gate's real import
    graph in THIS repo (SABLE), not the fixture repo — the fixture repo has
    no bin/sable-merge-gate at all, so the derivation must degrade to just
    the explicit additions rather than raising."""
    roster = adm.gate_class_roster(str(repo))
    assert roster == adm.GATE_TIER_FILES | {adm.DISPATCH_FILE}


def test_gate_class_roster_against_the_real_sable_repo_finds_the_known_modules():
    """Non-vacuity against the real repo this test itself lives in: the
    derivation must actually walk something, not just return the fallback."""
    real_repo = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    roster = adm.gate_class_roster(real_repo)
    for expected in ("bin/sable-merge-gate", "bin/sable_gate_promote_lib.py",
                     "bin/sable_gate_preview_lib.py", "bin/sable_footprint_lib.py"):
        assert expected in roster, f"{expected} missing from derived roster: {sorted(roster)}"


# --------------------------------------------------------------------------
# is_this_branch_mechanical — per-clause behaviour
# --------------------------------------------------------------------------

def test_mechanical_verdict_reports_all_five_clauses_by_name(repo, monkeypatch):
    base_sha = _sha(repo)
    branch_sha = _branch(repo, base_sha, "b1", "b1.py", "x = 1\n")
    _stub_bd(monkeypatch, {"SABLE-b1": _bead_record(writes="b1.py")})
    _stub_all_green(monkeypatch, {branch_sha: "deadbeef" * 5})

    verdict = adm.is_this_branch_mechanical(str(repo), "origin", "SABLE-b1", "b1",
                                            base_sha, branch_sha)
    names = {c.name for c in verdict.clauses}
    assert names == {"non_gate_class", "individually_green", "zero_holds",
                     "clean_ff_adoption", "zero_conflicts"}
    assert verdict.mechanical is True
    assert verdict.reason == "all clauses passed"


def test_non_gate_class_fails_when_footprint_touches_the_gate_roster(repo, monkeypatch):
    """Roster derivation itself is covered by the two gate_class_roster tests
    above; this fixes a roster in place so the clause logic is tested
    independently of whether THIS throwaway fixture repo happens to carry a
    real bin/sable-merge-gate."""
    base_sha = _sha(repo)
    branch_sha = _branch(repo, base_sha, "gate", "ordinary.py", "x = 1\n")
    _stub_bd(monkeypatch, {"SABLE-g1": _bead_record(writes="bin/sable_gate_promote_lib.py")})
    _stub_all_green(monkeypatch, {branch_sha: "cafef00d" * 5})
    monkeypatch.setattr(adm, "gate_class_roster",
                        lambda repo_: frozenset({"bin/sable_gate_promote_lib.py"})
                        | adm.GATE_TIER_FILES | {adm.DISPATCH_FILE})

    verdict = adm.is_this_branch_mechanical(str(repo), "origin", "SABLE-g1", "gate",
                                            base_sha, branch_sha)
    assert verdict.mechanical is False
    assert verdict.clause("non_gate_class").passed is False
    assert "sable_gate_promote_lib.py" in verdict.clause("non_gate_class").reason


def test_non_gate_class_fires_on_78qck_tier_mechanism_file_and_not_on_ordinary_bin(repo, monkeypatch):
    """AMENDMENT case, both polarities: a fixture branch touching
    .github/ci/impact-manifest.sh trips the SABLE-78qck exclusion; an
    ordinary bin/ change does not."""
    base_sha = _sha(repo)
    tier_branch_sha = _branch(repo, base_sha, "tier", ".github/ci/impact-manifest.sh", "# x\n")
    _stub_bd(monkeypatch, {"SABLE-t1": _bead_record(writes=".github/ci/impact-manifest.sh")})
    _stub_all_green(monkeypatch, {tier_branch_sha: "a1a1a1a1" * 5})
    tier_verdict = adm.is_this_branch_mechanical(str(repo), "origin", "SABLE-t1", "tier",
                                                 base_sha, tier_branch_sha)
    assert tier_verdict.clause("non_gate_class").passed is False

    ordinary_sha = _branch(repo, base_sha, "ordinary", "bin/plain_thing.py", "x = 1\n")
    _stub_bd(monkeypatch, {"SABLE-o1": _bead_record(writes="bin/plain_thing.py")})
    _stub_all_green(monkeypatch, {ordinary_sha: "b2b2b2b2" * 5})
    ordinary_verdict = adm.is_this_branch_mechanical(str(repo), "origin", "SABLE-o1", "ordinary",
                                                      base_sha, ordinary_sha)
    assert ordinary_verdict.clause("non_gate_class").passed is True


def test_individually_green_and_ff_fail_together_when_nothing_was_kicked(repo, monkeypatch):
    base_sha = _sha(repo)
    branch_sha = _branch(repo, base_sha, "nokick", "x.py", "x = 1\n")
    _stub_bd(monkeypatch, {"SABLE-x1": _bead_record(writes="x.py")})
    monkeypatch.setattr(preview_lib, "adopt_kicked_preview", lambda *a, **kw: None)
    monkeypatch.setattr(preview_lib, "read_verdict",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("read_verdict must not be called with no adopted preview")))

    verdict = adm.is_this_branch_mechanical(str(repo), "origin", "SABLE-x1", "nokick",
                                            base_sha, branch_sha)
    assert verdict.clause("clean_ff_adoption").passed is False
    assert verdict.clause("individually_green").passed is False
    assert verdict.mechanical is False


def test_individually_green_fails_on_a_red_verdict(repo, monkeypatch):
    base_sha = _sha(repo)
    branch_sha = _branch(repo, base_sha, "red", "x.py", "x = 1\n")
    _stub_bd(monkeypatch, {"SABLE-r1": _bead_record(writes="x.py")})
    preview_sha = "c3c3c3c3" * 5
    monkeypatch.setattr(preview_lib, "adopt_kicked_preview",
                        lambda *a, **kw: (preview_sha, "ci-verify/red-fixture"))
    monkeypatch.setattr(preview_lib, "read_verdict",
                        lambda *a, **kw: classify.Verdict("failure", "", preview_sha,
                                                          "ci-verify/red-fixture",
                                                          source="precomputed", complete=True))

    verdict = adm.is_this_branch_mechanical(str(repo), "origin", "SABLE-r1", "red",
                                            base_sha, branch_sha)
    assert verdict.clause("clean_ff_adoption").passed is True, "adoption itself still succeeded"
    assert verdict.clause("individually_green").passed is False
    assert "not green" in verdict.clause("individually_green").reason
    assert verdict.mechanical is False


def test_zero_holds_fails_when_the_bead_is_held(repo, monkeypatch):
    base_sha = _sha(repo)
    branch_sha = _branch(repo, base_sha, "held", "x.py", "x = 1\n")
    _stub_bd(monkeypatch, {"SABLE-h1": _bead_record(writes="x.py", hold="operator paused this lane")})
    _stub_all_green(monkeypatch, {branch_sha: "d4d4d4d4" * 5})

    verdict = adm.is_this_branch_mechanical(str(repo), "origin", "SABLE-h1", "held",
                                            base_sha, branch_sha)
    assert verdict.clause("zero_holds").passed is False
    assert "operator paused this lane" in verdict.clause("zero_holds").reason
    assert verdict.mechanical is False


def test_zero_conflicts_fails_when_the_single_member_fold_conflicts_with_base(repo, monkeypatch):
    """A branch that itself does not fold cleanly onto the current base (its
    OWN merge-tree against base conflicts) fails zero_conflicts even with an
    otherwise-green verdict."""
    base_sha = _sha(repo)
    _write(repo, "shared.py", "base\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "shared.py on trunk")
    based_on = _sha(repo)
    branch_sha = _branch(repo, based_on, "conflicter", "shared.py", "branch version\n")
    # Simulate the base having ALSO since diverged shared.py -- fold_check's
    # base_sha argument is the CURRENT tip, not `based_on`.
    _run(repo, "checkout", "-q", "trunk")
    _write(repo, "shared.py", "moved on\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "base moved shared.py")
    current_base_sha = _sha(repo)

    _stub_bd(monkeypatch, {"SABLE-c1": _bead_record(writes="shared.py")})
    _stub_all_green(monkeypatch, {branch_sha: "e5e5e5e5" * 5})

    verdict = adm.is_this_branch_mechanical(str(repo), "origin", "SABLE-c1", "conflicter",
                                            current_base_sha, branch_sha)
    assert verdict.clause("zero_conflicts").passed is False
    assert "fold-check conflict" in verdict.clause("zero_conflicts").reason
    assert verdict.mechanical is False


# --------------------------------------------------------------------------
# admit_batch — the PLANT-AND-FAIL matrix
# --------------------------------------------------------------------------

def test_vacuous_pass_guard_empty_candidate_set_is_rejected_loudly(repo):
    base_sha = _sha(repo)
    with pytest.raises(ValueError, match="empty"):
        adm.admit_batch(str(repo), "origin", base_sha, [])


def test_plant_and_fail_positive_four_disjoint_members_all_admitted(repo, monkeypatch):
    """4 low-risk, individually-green, mutually rw-disjoint members: all
    admitted, all 6 pairwise checks pass."""
    base_sha = _sha(repo)
    shas = {}
    beads = {}
    for label, fname in [("m1", "m1.py"), ("m2", "m2.py"), ("m3", "m3.py"), ("m4", "m4.py")]:
        sha = _branch(repo, base_sha, label, fname, f"# {label}\n")
        shas[label] = sha
        beads[f"SABLE-{label}"] = _bead_record(writes=fname)

    _stub_bd(monkeypatch, beads)
    _stub_all_green(monkeypatch, {sha: str(i).zfill(40) for i, sha in enumerate(shas.values())})

    pair_calls = []
    real_rw_disjoint = fp.is_rw_disjoint
    def _counting_rw_disjoint(*a, **kw):
        pair_calls.append(1)
        return real_rw_disjoint(*a, **kw)
    monkeypatch.setattr(fp, "is_rw_disjoint", _counting_rw_disjoint)

    candidates = [Candidate(bead=f"SABLE-{label}", branch=label, sha=shas[label])
                 for label in ("m1", "m2", "m3", "m4")]
    result = adm.admit_batch(str(repo), "origin", base_sha, candidates)

    assert [c.branch for c in result.admitted] == ["m1", "m2", "m3", "m4"]
    assert result.excluded == ()
    assert len(pair_calls) == 6, "C(4,2) == 6 pairwise rw-disjoint checks expected"


def test_plant_and_fail_negative_overlapping_footprint_excludes_with_no_combined_run_spent(repo, monkeypatch):
    base_sha = _sha(repo)
    sha_a = _branch(repo, base_sha, "a", "a.py", "a = 1\n")
    sha_b = _branch(repo, base_sha, "b", "b.py", "b = 1\n")
    beads = {
        "SABLE-a": _bead_record(writes="shared.py"),
        "SABLE-b": _bead_record(writes="shared.py"),
    }
    _stub_bd(monkeypatch, beads)
    _stub_all_green(monkeypatch, {sha_a: "1" * 40, sha_b: "2" * 40})

    fold_calls = []
    import sable_batch_fold_lib as fold_lib
    real_fold_check = fold_lib.fold_check
    def _counting_fold_check(*a, **kw):
        fold_calls.append(a)
        return real_fold_check(*a, **kw)
    monkeypatch.setattr(fold_lib, "fold_check", _counting_fold_check)

    candidates = [Candidate(bead="SABLE-a", branch="a", sha=sha_a),
                 Candidate(bead="SABLE-b", branch="b", sha=sha_b)]
    result = adm.admit_batch(str(repo), "origin", base_sha, candidates)

    assert [c.branch for c in result.admitted] == ["a"]
    assert len(result.excluded) == 1
    assert result.excluded[0].candidate.branch == "b"
    assert "overlaps admitted member" in result.excluded[0].reason
    assert "shared.py" in result.excluded[0].reason
    # Both 'a' and 'b' independently pay their OWN single-member zero_conflicts
    # fold_check (that clause always runs, per branch, regardless of batching).
    # What must NOT happen is a COMBINED (multi-member) run for 'b' -- the
    # footprint overlap excludes it BEFORE any combined run is spent.
    combined_calls = [call for call in fold_calls if len(call[2]) > 1]
    assert combined_calls == [], f"no combined (multi-member) fold_check may run: {combined_calls}"


def test_excludes_a_gate_class_branch_loudly_remaining_members_still_form_a_batch(repo, monkeypatch):
    base_sha = _sha(repo)
    sha_ok1 = _branch(repo, base_sha, "ok1", "ok1.py", "x = 1\n")
    sha_gate = _branch(repo, base_sha, "gate", "bin/sable_gate_promote_lib.py", "x = 1\n")
    sha_ok2 = _branch(repo, base_sha, "ok2", "ok2.py", "x = 1\n")
    beads = {
        "SABLE-ok1": _bead_record(writes="ok1.py"),
        "SABLE-gate": _bead_record(writes="bin/sable_gate_promote_lib.py"),
        "SABLE-ok2": _bead_record(writes="ok2.py"),
    }
    _stub_bd(monkeypatch, beads)
    _stub_all_green(monkeypatch, {sha_ok1: "3" * 40, sha_gate: "4" * 40, sha_ok2: "5" * 40})
    monkeypatch.setattr(adm, "gate_class_roster",
                        lambda repo_: frozenset({"bin/sable_gate_promote_lib.py"})
                        | adm.GATE_TIER_FILES | {adm.DISPATCH_FILE})

    candidates = [Candidate(bead="SABLE-ok1", branch="ok1", sha=sha_ok1),
                 Candidate(bead="SABLE-gate", branch="gate", sha=sha_gate),
                 Candidate(bead="SABLE-ok2", branch="ok2", sha=sha_ok2)]
    result = adm.admit_batch(str(repo), "origin", base_sha, candidates)

    assert [c.branch for c in result.admitted] == ["ok1", "ok2"]
    assert len(result.excluded) == 1
    assert result.excluded[0].candidate.branch == "gate"
    assert "non_gate_class" in result.excluded[0].reason


def test_excludes_a_not_individually_green_branch_reason_recorded(repo, monkeypatch):
    base_sha = _sha(repo)
    sha_ok = _branch(repo, base_sha, "ok", "ok.py", "x = 1\n")
    sha_red = _branch(repo, base_sha, "red", "red.py", "x = 1\n")
    beads = {
        "SABLE-ok": _bead_record(writes="ok.py"),
        "SABLE-red": _bead_record(writes="red.py"),
    }
    _stub_bd(monkeypatch, beads)

    previews = {sha_ok: "6" * 40, sha_red: "7" * 40}
    def _adopt(repo_, remote, branch, base_sha_, branch_sha):
        if branch_sha not in previews:
            return None
        return (previews[branch_sha], f"ci-verify/{branch}-fixture")
    def _verdict(repo_, ref, preview_sha):
        conclusion = "failure" if preview_sha == previews[sha_red] else "success"
        return classify.Verdict(conclusion, "", preview_sha, ref, source="precomputed", complete=True)
    monkeypatch.setattr(preview_lib, "adopt_kicked_preview", _adopt)
    monkeypatch.setattr(preview_lib, "read_verdict", _verdict)

    candidates = [Candidate(bead="SABLE-ok", branch="ok", sha=sha_ok),
                 Candidate(bead="SABLE-red", branch="red", sha=sha_red)]
    result = adm.admit_batch(str(repo), "origin", base_sha, candidates)

    assert [c.branch for c in result.admitted] == ["ok"]
    assert len(result.excluded) == 1
    assert result.excluded[0].candidate.branch == "red"
    assert "individually_green" in result.excluded[0].reason


def test_rejects_an_overlapping_pair_via_fold_check_failure_falls_back_to_serial(repo, monkeypatch):
    """Declared footprints are DISJOINT (A only declares a.py; B only
    declares shared.py) but A's REAL diff also touches shared.py, undeclared
    -- so the cheap pairwise check passes and only the real fold_check (on
    the accumulated set) catches the conflict. Distinct exclusion message
    from the declared-footprint-overlap case."""
    _write(repo, "shared.py", "base\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "add shared.py")
    root_with_shared = _sha(repo)

    _run(repo, "checkout", "-q", "-b", "a", root_with_shared)
    _write(repo, "a.py", "a = 1\n")
    _write(repo, "shared.py", "from a\n")   # undeclared touch
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "on a")
    sha_a = _sha(repo)

    _run(repo, "checkout", "-q", "-b", "b", root_with_shared)
    _write(repo, "shared.py", "from b\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "on b")
    sha_b = _sha(repo)

    beads = {
        "SABLE-a": _bead_record(writes="a.py"),          # shared.py NOT declared
        "SABLE-b": _bead_record(writes="shared.py"),
    }
    _stub_bd(monkeypatch, beads)
    _stub_all_green(monkeypatch, {sha_a: "8" * 40, sha_b: "9" * 40})

    # Sanity: the declared-footprint pairwise check alone sees these as disjoint.
    rw = fp.is_rw_disjoint(fp.footprint({"a.py"}), fp.footprint(()),
                           fp.footprint({"shared.py"}), fp.footprint(()))
    assert rw.disjoint is True, "test setup must start from a declared-disjoint pair"

    candidates = [Candidate(bead="SABLE-a", branch="a", sha=sha_a),
                 Candidate(bead="SABLE-b", branch="b", sha=sha_b)]
    # root_with_shared (which already carries shared.py) is the fold base, so
    # the merge-tree step actually has shared.py present on every side.
    result = adm.admit_batch(str(repo), "origin", root_with_shared, candidates)

    assert [c.branch for c in result.admitted] == ["a"]
    assert len(result.excluded) == 1
    assert result.excluded[0].candidate.branch == "b"
    assert "fold-check FAILURE" in result.excluded[0].reason
    assert "falls back to serial" in result.excluded[0].reason
    assert "overlaps admitted member" not in result.excluded[0].reason, (
        "must be distinguishable from the declared-footprint-overlap exclusion")


def test_declared_footprint_undetermined_excludes_rather_than_raising(repo, monkeypatch):
    """A candidate whose bead cannot be read at all (bd failure / no
    fixture) is excluded, not a crash of the whole batch."""
    base_sha = _sha(repo)
    sha_ok = _branch(repo, base_sha, "ok", "ok.py", "x = 1\n")
    sha_unknown = _branch(repo, base_sha, "unknown", "unknown.py", "x = 1\n")
    _stub_bd(monkeypatch, {"SABLE-ok": _bead_record(writes="ok.py")})
    _stub_all_green(monkeypatch, {sha_ok: "a" * 40, sha_unknown: "b" * 40})

    candidates = [Candidate(bead="SABLE-ok", branch="ok", sha=sha_ok),
                 Candidate(bead="SABLE-unknown", branch="unknown", sha=sha_unknown)]
    result = adm.admit_batch(str(repo), "origin", base_sha, candidates)

    assert [c.branch for c in result.admitted] == ["ok"]
    assert len(result.excluded) == 1
    assert result.excluded[0].candidate.branch == "unknown"
