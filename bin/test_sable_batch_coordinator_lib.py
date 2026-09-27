#!/usr/bin/env python3
"""End-to-end tests for one rolling merge-train cycle."""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import sable_batch_coordinator_lib as coordinator  # noqa: E402
import sable_footprint_lib as footprint  # noqa: E402
import sable_gate_classify_lib as classify  # noqa: E402


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True,
        capture_output=True, text=True).stdout.strip()


def _sandbox(tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "trunk", str(bare)],
        check=True, capture_output=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk")
    _git(repo, "config", "user.email", "test@sable.invalid")
    _git(repo, "config", "user.name", "SABLE Test")
    (repo / "root.txt").write_text("root\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "root")
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "origin", "trunk")
    _git(repo, "fetch", "-q", "origin")
    return repo, bare, _git(repo, "rev-parse", "HEAD")


def _member(repo, base_sha, branch, filename):
    _git(repo, "checkout", "-q", "-b", branch, base_sha)
    path = repo / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{branch.replace('-', '_')} = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", branch)
    tip = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-q", "origin", branch)
    _git(repo, "checkout", "-q", "trunk")
    return tip


def _remote_head(bare, branch):
    return subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "--verify",
         f"refs/heads/{branch}"],
        check=True, capture_output=True, text=True).stdout.strip()


def _install_admission_evidence(monkeypatch, records):
    def read_bead(repo, bead):
        if bead not in records:
            raise footprint.FootprintUndetermined(f"missing fixture {bead}")
        return records[bead]

    monkeypatch.setattr(footprint, "_read_bead", read_bead)
    monkeypatch.setattr(
        coordinator.admission.preview_lib, "adopt_kicked_preview",
        lambda repo, remote, branch, base_sha, branch_sha:
            (branch_sha, f"ci-verify/{branch}-qualified"))
    monkeypatch.setattr(
        coordinator.admission.preview_lib, "read_verdict",
        lambda repo, ref, sha: classify.Verdict(
            "success", "", sha, ref, source="precomputed", complete=True))


def _bead(filename):
    return {
        "description": "",
        "metadata": {
            "footprint_writes": filename,
            "footprint_reads_declared": "",
        },
    }


def _work_record(bead, branch, created_at, filename, *, status="closed", **metadata):
    return {
        "id": bead,
        "status": status,
        "created_at": created_at,
        "closed_at": created_at if status == "closed" else None,
        "metadata": {
            "branch": branch,
            "footprint_writes": filename,
            "footprint_reads_declared": "",
            **metadata,
        },
    }


@pytest.fixture(autouse=True)
def _no_live_fleet_side_effects(monkeypatch, tmp_path):
    monkeypatch.setattr(coordinator.promote, "_notify", lambda *args: None)
    monkeypatch.setattr(coordinator.promote, "_append_evidence", lambda *args: None)
    monkeypatch.setenv(
        "SABLE_MG_BATCH_RECORD_LOG", str(tmp_path / "batch-records.jsonl"))
    monkeypatch.setenv(
        "SABLE_MG_BATCH_DRAIN_LOG", str(tmp_path / "batch-drain-records.jsonl"))


def test_green_cycle_lands_three_members_with_one_combined_verdict(
        tmp_path, monkeypatch):
    repo, bare, base_sha = _sandbox(tmp_path)
    specs = []
    records = {}
    tips = []
    for label, filename in (
            ("wk-a", "a.py"), ("wk-b", "b.py"), ("wk-c", "c.py")):
        bead = f"SABLE-{label[3:]}"
        tips.append(_member(repo, base_sha, label, filename))
        specs.append(f"{label}:{bead}")
        records[bead] = _bead(filename)
    _install_admission_evidence(monkeypatch, records)
    acquired = []

    def green(repo_, ref, sha):
        acquired.append((ref, sha))
        return classify.Verdict(
            "success", "", sha, ref, source="precomputed", complete=True)

    monkeypatch.setattr(coordinator.preview, "acquire_verdict", green)

    result = coordinator.run_batch_cycle(
        "trunk", specs, str(repo), "origin", max_members=8)

    assert result.outcome == coordinator.CYCLE_LANDED
    assert result.exit_code == 0
    assert len(result.members) == 3
    assert acquired == [(result.combined_ref, result.fold_tip)]
    assert _remote_head(bare, "trunk") == result.fold_tip
    for tip in tips:
        ancestry = subprocess.run(
            ["git", "-C", str(bare), "merge-base", "--is-ancestor",
             tip, result.fold_tip])
        assert ancestry.returncode == 0


def test_red_cycle_bisects_and_lands_nothing(tmp_path, monkeypatch):
    repo, bare, base_sha = _sandbox(tmp_path)
    specs = []
    records = {}
    for label, filename in (
            ("wk-a", "a.py"), ("wk-bad", "bad.py"), ("wk-c", "c.py")):
        bead = f"SABLE-{label[3:]}"
        _member(repo, base_sha, label, filename)
        specs.append(f"{label}:{bead}")
        records[bead] = _bead(filename)
    _install_admission_evidence(monkeypatch, records)

    def content_verdict(repo_, ref, sha):
        files = _git(repo_, "ls-tree", "-r", "--name-only", sha).splitlines()
        conclusion = "failure" if "bad.py" in files else "success"
        return classify.Verdict(
            conclusion, "", sha, ref, source="precomputed", complete=True)

    monkeypatch.setattr(coordinator.preview, "acquire_verdict", content_verdict)

    result = coordinator.run_batch_cycle(
        "trunk", specs, str(repo), "origin", max_members=8)

    assert result.outcome == coordinator.CYCLE_RED
    assert result.exit_code == classify.EXIT_RED
    assert "wk-bad" in result.reason
    assert "CULPRIT ISOLATED" in result.reason
    assert _remote_head(bare, "trunk") == base_sha


def test_cycle_cap_defers_newer_arrivals_to_the_next_cycle(
        tmp_path, monkeypatch):
    """The cap seals the current car; later arrivals remain explicit rather
    than being dropped or expanding an in-flight object."""
    repo, _bare, base_sha = _sandbox(tmp_path)
    specs = []
    records = {}
    for index in range(3):
        label = f"wk-{index}"
        filename = f"m{index}.py"
        bead = f"SABLE-{index}"
        _member(repo, base_sha, label, filename)
        specs.append(f"{label}:{bead}")
        records[bead] = _bead(filename)
    _install_admission_evidence(monkeypatch, records)
    monkeypatch.setattr(
        coordinator.preview, "acquire_verdict",
        lambda repo_, ref, sha: classify.Verdict(
            "success", "", sha, ref, source="precomputed", complete=True))

    result = coordinator.run_batch_cycle(
        "trunk", specs, str(repo), "origin", max_members=2)

    assert result.outcome == coordinator.CYCLE_LANDED
    assert len(result.members) == 2
    assert result.deferred_specs == ("wk-2:SABLE-2",)
    assert "next cycle" in coordinator.render_cycle_result(result)


def test_discovery_orders_oldest_ready_and_excludes_unreadable_or_held(
        tmp_path, monkeypatch):
    repo, _bare, base_sha = _sandbox(tmp_path)
    for branch, filename in (
            ("wk-new", "new.py"), ("wk-old", "old.py"),
            ("wk-held", "held.py"), ("wk-unreadable", "bad.py")):
        _member(repo, base_sha, branch, filename)

    records = {
        "wk-new": ([_work_record(
            "SABLE-new", "wk-new", "2026-07-27T11:00:00Z", "new.py")], ""),
        "wk-old": ([_work_record(
            "SABLE-old", "wk-old", "2026-07-27T10:00:00Z", "old.py")], ""),
        "wk-held": ([_work_record(
            "SABLE-held", "wk-held", "2026-07-27T09:00:00Z", "held.py",
            hold="operator pause")], ""),
        "wk-unreadable": ([], "Beads query returned unreadable JSON"),
    }
    monkeypatch.setattr(
        coordinator, "_branch_records",
        lambda repo_, branch: records[branch])

    snapshot = coordinator.discover_queue(
        "trunk", str(repo), "origin")

    assert snapshot.base_sha == base_sha
    assert [candidate.spec for candidate in snapshot.candidates] == [
        "wk-old:SABLE-old", "wk-new:SABLE-new"]
    reasons = {excluded.branch: excluded.reason
               for excluded in snapshot.excluded}
    assert "held" in reasons["wk-held"]
    assert "unreadable JSON" in reasons["wk-unreadable"]


def test_work_bead_resolution_fails_closed_on_ambiguous_join():
    records = [
        _work_record(
            "SABLE-a", "wk-a", "2026-07-27T10:00:00Z", "a.py"),
        _work_record(
            "SABLE-b", "wk-a", "2026-07-27T11:00:00Z", "b.py"),
    ]
    candidate, reason = coordinator._candidate_from_records("wk-a", records)
    assert candidate is None
    assert "ambiguous branch" in reason
    assert "SABLE-a" in reason and "SABLE-b" in reason


def test_in_progress_work_bead_is_not_ready_for_automatic_landing():
    records = [
        _work_record(
            "SABLE-a", "wk-a", "2026-07-27T10:00:00Z", "a.py",
            status="in_progress"),
    ]

    candidate, reason = coordinator._candidate_from_records(
        "wk-a", records, "a" * 40, "2026-07-27T10:00:00Z")

    assert candidate is None
    assert "no closed work bead" in reason


def test_malformed_bead_metadata_fails_closed_without_crashing():
    candidate, reason = coordinator._candidate_from_records(
        "wk-a", [{"id": "SABLE-a", "status": "closed", "metadata": "bad"}])

    assert candidate is None
    assert "malformed metadata" in reason


def test_landing_pair_is_never_split_by_the_batch_cap(monkeypatch):
    candidates = [
        coordinator.admission.Candidate("SABLE-a", "wk-a", "a" * 40),
        coordinator.admission.Candidate("SABLE-b", "wk-b", "b" * 40),
        coordinator.admission.Candidate("SABLE-c", "wk-c", "c" * 40),
    ]
    monkeypatch.setattr(
        coordinator.promote, "validated_landing_pair",
        lambda repo, bead: (
            (frozenset({"SABLE-b"}), {}) if bead == "SABLE-a"
            else (frozenset({"SABLE-a"}), {}) if bead == "SABLE-b"
            else (frozenset(), {})))

    selected, deferred, excluded = coordinator._seal_admitted_members(
        "/repo", candidates, max_members=2)

    assert [candidate.bead for candidate in selected] == [
        "SABLE-a", "SABLE-b"]
    assert deferred == ("wk-c:SABLE-c",)
    assert excluded == ()


def test_incomplete_landing_pair_excludes_the_whole_connected_group(monkeypatch):
    candidates = [
        coordinator.admission.Candidate("SABLE-a", "wk-a", "a" * 40),
        coordinator.admission.Candidate("SABLE-b", "wk-b", "b" * 40),
    ]
    monkeypatch.setattr(
        coordinator.promote, "validated_landing_pair",
        lambda repo, bead: (
            (frozenset({"SABLE-missing"}), {}) if bead == "SABLE-a"
            else (frozenset({"SABLE-a"}), {})))
    monkeypatch.setattr(
        coordinator.promote, "bead_landed", lambda *args: False)

    selected, deferred, excluded = coordinator._seal_admitted_members(
        "/repo", candidates, max_members=8)

    assert selected == []
    assert deferred == ()
    assert {item.candidate.bead for item in excluded} == {
        "SABLE-a", "SABLE-b"}


def test_asymmetric_landing_pair_is_excluded_before_batch_ci(monkeypatch):
    candidate = coordinator.admission.Candidate(
        "SABLE-b", "wk-b", "b" * 40)

    def asymmetric(repo, bead):
        raise coordinator.promote.LandingPairRefused(
            "asymmetric landing-pair declaration for [SABLE-a, SABLE-b]")

    monkeypatch.setattr(
        coordinator.promote, "validated_landing_pair", asymmetric)

    selected, deferred, excluded = coordinator._seal_admitted_members(
        "/repo", [candidate], max_members=8)

    assert selected == []
    assert deferred == ()
    assert len(excluded) == 1
    assert "asymmetric" in excluded[0].reason


def test_drain_rediscovers_rolling_arrivals_only_between_sealed_cycles(
        tmp_path, monkeypatch):
    def snapshot(index, candidates):
        return coordinator.QueueSnapshot(
            "trunk", f"{index:040x}", float(index), tuple(candidates), ())

    a = coordinator.QueueCandidate(
        "wk-a", "SABLE-a", "2026-07-27T10:00:00Z", "a" * 40)
    b = coordinator.QueueCandidate(
        "wk-b", "SABLE-b", "2026-07-27T10:01:00Z", "b" * 40)
    a_new_tip = coordinator.QueueCandidate(
        "wk-a", "SABLE-a", "2026-07-27T10:02:00Z", "e" * 40)
    c = coordinator.QueueCandidate(
        "wk-c", "SABLE-c", "2026-07-27T10:03:00Z", "c" * 40)
    d = coordinator.QueueCandidate(
        "wk-d", "SABLE-d", "2026-07-27T10:04:00Z", "d" * 40)
    snapshots = iter([
        snapshot(1, [a, b]),
        snapshot(2, [a_new_tip, c, d]),
        snapshot(3, []),
    ])
    monkeypatch.setattr(
        coordinator, "discover_queue",
        lambda *args, **kwargs: next(snapshots))
    sealed = []

    def land(base, specs, repo, remote, manager, max_members, extra_pairs,
             refresh_refs, expected_tips, expected_base_sha):
        assert refresh_refs is False
        assert expected_base_sha
        assert set(expected_tips) == {
            spec.partition(":")[0] for spec in specs}
        sealed.append(tuple(specs))
        return coordinator.BatchCycleResult(
            coordinator.CYCLE_LANDED,
            tuple(spec.partition(":")[0] for spec in specs),
            (), (), "ci-verify/batch-x", "f" * 40, "landed",
            ci_rounds=1)

    monkeypatch.setattr(coordinator, "run_batch_cycle", land)

    result = coordinator.run_batch_drain(
        "trunk", str(tmp_path), "origin", max_cycles=4)

    assert sealed == [
        ("wk-a:SABLE-a", "wk-b:SABLE-b"),
        ("wk-a:SABLE-a", "wk-c:SABLE-c", "wk-d:SABLE-d"),
    ]
    assert result.arrivals == 3
    assert result.landed_members == 5
    assert result.final_snapshot.depth == 0
    assert result.record is not None
    assert result.record.ci_rounds == 2
    assert coordinator.read_drain_records(str(tmp_path)) == [result.record]


def test_discovered_tip_stays_sealed_if_remote_tracking_ref_moves(
        tmp_path):
    repo, _bare, base_sha = _sandbox(tmp_path)
    original_tip = _member(repo, base_sha, "wk-a", "a.py")
    _git(repo, "checkout", "-q", "wk-a")
    (repo / "later.py").write_text("later = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "later arrival on same branch")
    _git(repo, "push", "-q", "origin", "wk-a")
    moved_tip = _git(repo, "rev-parse", "wk-a")
    assert moved_tip != original_tip

    members = coordinator.promote.resolve_batch_members(
        str(repo), "origin", base_sha, ["wk-a:SABLE-a"], "test",
        refresh_refs=False, expected_tips={"wk-a": original_tip})

    assert members[0].tip_sha == original_tip
    assert "later.py" not in members[0].footprint_paths


def test_batch_metrics_report_fill_rates_and_extra_red_ci_rounds():
    records = [
        coordinator.BatchDrainRecord(
            0.0, 60.0, 8, 2, 2, 8, 2, 8, 2,
            (coordinator.CYCLE_LANDED, coordinator.CYCLE_LANDED), (4, 4)),
        coordinator.BatchDrainRecord(
            60.0, 120.0, 4, 4, 1, 0, 1, 8, 4,
            (coordinator.CYCLE_RED,), (4,)),
    ]
    report = coordinator.batch_metrics(records)
    assert report["arrival_rate_per_minute"] == 1.5
    assert report["drain_rate_per_minute"] == 4.0
    assert report["batch_fill"] == 0.5
    assert report["ci_rounds"] == 6
    assert report["outcomes"][coordinator.CYCLE_RED] == 1
