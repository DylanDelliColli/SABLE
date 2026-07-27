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


@pytest.fixture(autouse=True)
def _no_live_fleet_side_effects(monkeypatch, tmp_path):
    monkeypatch.setattr(coordinator.promote, "_notify", lambda *args: None)
    monkeypatch.setattr(coordinator.promote, "_append_evidence", lambda *args: None)
    monkeypatch.setenv(
        "SABLE_MG_BATCH_RECORD_LOG", str(tmp_path / "batch-records.jsonl"))


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
