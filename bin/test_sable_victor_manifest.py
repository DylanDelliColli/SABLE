#!/usr/bin/env python3
"""Contract tests for Victor's validated-at anchor manifest (SABLE-t6bv6.1)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest


_BIN = Path(__file__).resolve().parent
_REPO = _BIN.parent
_CLI = _BIN / "sable-victor-manifest"
_LOADER = SourceFileLoader("sable_victor_manifest", str(_CLI))
_SPEC = importlib.util.spec_from_loader("sable_victor_manifest", _LOADER)
manifest = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = manifest
_LOADER.exec_module(manifest)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    cp = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and cp.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {cp.stderr}")
    return cp


def _commit(repo: Path, subject: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", subject)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture()
def git_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "tests@sable.invalid")
    _git(repo, "config", "user.name", "SABLE tests")
    (repo / "src").mkdir()
    (repo / "src" / "alpha.py").write_text("alpha = 1\n", encoding="utf-8")
    (repo / "src" / "beta.py").write_text("beta = 1\n", encoding="utf-8")
    (repo / "old-name.txt").write_text("rename me\n", encoding="utf-8")
    return repo, _commit(repo, "base")


def _hash(description: str) -> str:
    return hashlib.sha256(description.encode("utf-8")).hexdigest()


def _stamp(
    sha: str,
    description: str,
    paths: list[str] | None = None,
    **updates,
) -> str:
    value = {
        "schema": 1,
        "sha": sha,
        "at": "2026-08-02T12:34:56Z",
        "paths": paths if paths is not None else ["src/alpha.py"],
        "description_sha256": _hash(description),
    }
    value.update(updates)
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _record(
    bead_id: str,
    description: str,
    stamp: str | None = None,
    *,
    notes: str = "",
) -> dict:
    metadata = {} if stamp is None else {manifest.METADATA_KEY: stamp}
    return {
        "id": bead_id,
        "description": description,
        "notes": notes,
        "metadata": metadata,
    }


def _decision(result: dict, bead_id: str) -> dict:
    rows = result["fresh_anchors_unmoved"] + result["deep_pass"]
    return next(row for row in rows if row["id"] == bead_id)


def test_description_digest_is_exact_utf8_content_not_normalized():
    assert manifest.description_sha256("line one\nline two\n") == _hash(
        "line one\nline two\n"
    )
    assert manifest.description_sha256("line one\nline two") != _hash(
        "line one\nline two\n"
    )


@pytest.mark.parametrize(
    ("mutation", "reason_fragment"),
    [
        (lambda value: value | {"extra": "not allowed"}, "keys"),
        (lambda value: value | {"schema": True}, "schema"),
        (lambda value: value | {"schema": 2}, "schema"),
        (lambda value: value | {"sha": "abc1234"}, "sha"),
        (lambda value: value | {"at": "yesterday"}, "timestamp"),
        (lambda value: value | {"paths": []}, "paths"),
        (lambda value: value | {"paths": ["src/z.py", "src/a.py"]}, "sorted"),
        (lambda value: value | {"paths": ["src/a.py", "src/a.py"]}, "unique"),
        (lambda value: value | {"paths": ["/tmp/escape.py"]}, "repository-relative"),
        (lambda value: value | {"paths": ["src/../escape.py"]}, "repository-relative"),
        (lambda value: value | {"paths": ["src/"]}, "concrete"),
        (lambda value: value | {"description_sha256": "not-a-digest"}, "digest"),
    ],
)
def test_stamp_schema_is_exact_and_fail_closed(mutation, reason_fragment):
    description = "Inspect src/alpha.py"
    base = json.loads(_stamp("a" * 40, description))
    record = _record(
        "SABLE-bad",
        description,
        json.dumps(mutation(base), separators=(",", ":")),
    )

    with pytest.raises(manifest.InvalidStamp, match=reason_fragment):
        manifest.parse_stamp(record)


@pytest.mark.parametrize("bad_value", [None, {}, [], 17, "", "not json", "[]"])
def test_missing_or_non_object_metadata_is_never_a_stamp(bad_value):
    record = _record("SABLE-bad", "desc")
    if bad_value is not None:
        record["metadata"][manifest.METADATA_KEY] = bad_value

    with pytest.raises(manifest.InvalidStamp):
        manifest.parse_stamp(record)


def test_historical_note_marker_is_audit_only_never_a_fallback(git_repo):
    repo, _base = git_repo
    record = _record(
        "SABLE-old",
        "Inspect src/alpha.py",
        notes=(
            "victor-validated-at 2026-07-15T20:13Z HEAD=7ef6c02 "
            "paths=[src/alpha.py] verdict=LIVE"
        ),
    )

    result = manifest.partition_records([record], repo=repo, head_ref="HEAD")

    row = _decision(result, "SABLE-old")
    assert row["category"] == "deep_pass"
    assert row["reason_code"] == "missing-stamp"


@pytest.mark.parametrize("label", ["reference", "runbook"])
def test_reference_policy_short_circuits_before_any_stamp_diff(git_repo, label):
    repo, base = git_repo
    description = "Standing operating reference"
    record = _record(
        "SABLE-reference",
        description,
        _stamp(base, description, ["src/alpha.py"]),
    )
    record["labels"] = [label]
    calls: list[tuple[str, ...]] = []

    def recording_git(repo_path, *args):
        calls.append(tuple(args))
        return manifest._run_git(repo_path, *args)

    result = manifest.partition_records(
        [record], repo=repo, head_ref="HEAD", git_run=recording_git
    )

    row = _decision(result, "SABLE-reference")
    assert row["category"] == "deep_pass"
    assert row["reason_code"] == "reference-short-circuit"
    assert not any("diff" in call or "ls-tree" in call for call in calls)


def test_description_edit_after_stamp_forces_deep_pass(git_repo):
    repo, base = git_repo
    old = "Inspect src/alpha.py"
    record = _record("SABLE-edited", old + " and src/beta.py", _stamp(base, old))

    result = manifest.partition_records([record], repo=repo, head_ref="HEAD")

    assert _decision(result, "SABLE-edited")["reason_code"] == "description-changed"


def test_real_repo_partitions_only_the_changed_anchor_into_deep_pass(git_repo):
    repo, base = git_repo
    alpha_desc = "Inspect src/alpha.py"
    beta_desc = "Inspect src/beta.py"
    records = [
        _record("SABLE-alpha", alpha_desc, _stamp(base, alpha_desc, ["src/alpha.py"])),
        _record("SABLE-beta", beta_desc, _stamp(base, beta_desc, ["src/beta.py"])),
    ]
    (repo / "src" / "alpha.py").write_text("alpha = 2\n", encoding="utf-8")
    head = _commit(repo, "change one anchor")

    result = manifest.partition_records(records, repo=repo, head_ref=head)

    assert result["head"] == head
    assert result["claim_bound"] == manifest.ANCHOR_ONLY_CLAIM
    assert [row["id"] for row in result["fresh_anchors_unmoved"]] == ["SABLE-beta"]
    assert [row["id"] for row in result["deep_pass"]] == ["SABLE-alpha"]
    assert [row["id"] for row in result["deep_pass_records"]] == ["SABLE-alpha"]
    assert _decision(result, "SABLE-alpha")["changed_anchors"] == ["src/alpha.py"]
    assert _decision(result, "SABLE-beta")["reason_code"] == "anchors-unchanged"
    assert "fresh" not in _decision(result, "SABLE-beta")["reason"].lower()


def test_same_stamp_sha_reuses_one_tree_and_one_diff_read(git_repo):
    repo, base = git_repo
    records = []
    for name in ("alpha", "beta"):
        description = f"Inspect src/{name}.py"
        records.append(
            _record(
                f"SABLE-{name}",
                description,
                _stamp(base, description, [f"src/{name}.py"]),
            )
        )
    calls: list[tuple[str, ...]] = []

    def recording_git(repo_path, *args):
        calls.append(tuple(args))
        return manifest._run_git(repo_path, *args)

    manifest.partition_records(records, repo=repo, head_ref="HEAD", git_run=recording_git)

    assert sum("ls-tree" in call for call in calls) == 1
    assert sum("diff" in call for call in calls) == 1


def test_deleted_anchor_is_a_change_not_an_absence(git_repo):
    repo, base = git_repo
    description = "Inspect src/alpha.py"
    record = _record("SABLE-delete", description, _stamp(base, description))
    (repo / "src" / "alpha.py").unlink()
    _commit(repo, "delete anchor")

    result = manifest.partition_records([record], repo=repo, head_ref="HEAD")

    row = _decision(result, "SABLE-delete")
    assert row["reason_code"] == "anchors-changed"
    assert row["changed_anchors"] == ["src/alpha.py"]


def test_rename_lists_both_sides_without_similarity_detection(git_repo):
    repo, base = git_repo
    description = "Inspect old-name.txt"
    record = _record("SABLE-rename", description, _stamp(base, description, ["old-name.txt"]))
    _git(repo, "mv", "old-name.txt", "new-name.txt")
    _commit(repo, "rename anchor")
    calls: list[tuple[str, ...]] = []

    def recording_git(repo_path, *args):
        calls.append(tuple(args))
        return manifest._run_git(repo_path, *args)

    result = manifest.partition_records(
        [record], repo=repo, head_ref="HEAD", git_run=recording_git
    )

    assert _decision(result, "SABLE-rename")["reason_code"] == "anchors-changed"
    [diff_call] = [call for call in calls if "diff" in call]
    assert "--no-renames" in diff_call
    assert f"{base}..{_git(repo, 'rev-parse', 'HEAD').stdout.strip()}" in diff_call


def test_unresolvable_validation_sha_fails_toward_deep_pass(git_repo):
    repo, _base = git_repo
    description = "Inspect src/alpha.py"
    record = _record("SABLE-missing", description, _stamp("f" * 40, description))

    result = manifest.partition_records([record], repo=repo, head_ref="HEAD")

    assert _decision(result, "SABLE-missing")["reason_code"] == "unresolved-validation-sha"


def test_off_spine_validation_sha_fails_toward_deep_pass(git_repo):
    repo, _base = git_repo
    main = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "--orphan", "other")
    for child in repo.iterdir():
        if child.name != ".git":
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    (repo / "other.txt").write_text("other\n", encoding="utf-8")
    other = _commit(repo, "orphan")
    _git(repo, "checkout", "-q", "main")
    description = "Inspect other.txt"
    record = _record("SABLE-orphan", description, _stamp(other, description, ["other.txt"]))

    result = manifest.partition_records([record], repo=repo, head_ref=main)

    assert _decision(result, "SABLE-orphan")["reason_code"] == "validation-not-on-spine"


def test_anchor_absent_from_validated_tree_cannot_pass_vacuously(git_repo):
    repo, base = git_repo
    description = "Inspect never-there.py"
    record = _record(
        "SABLE-absent", description, _stamp(base, description, ["never-there.py"])
    )

    result = manifest.partition_records([record], repo=repo, head_ref="HEAD")

    assert _decision(result, "SABLE-absent")["reason_code"] == "anchor-missing-at-validation"


def test_invalid_head_is_a_global_cannot_assess_not_an_all_deep_result(git_repo):
    repo, base = git_repo
    description = "Inspect src/alpha.py"
    record = _record("SABLE-one", description, _stamp(base, description))

    with pytest.raises(manifest.ManifestUnavailable, match="head"):
        manifest.partition_records([record], repo=repo, head_ref="does-not-exist")


def test_stamp_builds_sorted_manifest_and_one_atomic_tracker_update(git_repo):
    repo, base = git_repo
    description = "Inspect two anchors"
    bead = _record("SABLE-stamp", description)
    calls: list[list[str]] = []

    def fake_bd(argv, cwd):
        calls.append(list(argv))
        if argv[:2] == ["bd", "show"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps([bead]), "")
        if argv[:2] == ["bd", "update"]:
            return subprocess.CompletedProcess(argv, 0, "[]", "")
        raise AssertionError(argv)

    published = manifest.publish_stamp(
        "SABLE-stamp",
        repo=repo,
        sha_ref=base,
        paths=["src/beta.py", "src/alpha.py", "src/beta.py"],
        now="2026-08-02T12:34:56Z",
        bd_run=fake_bd,
    )

    assert [call[:2] for call in calls] == [["bd", "show"], ["bd", "update"]]
    update = calls[1]
    assert update[:3] == ["bd", "update", "SABLE-stamp"]
    [metadata_arg] = [arg for arg in update if arg.startswith(f"{manifest.METADATA_KEY}=")]
    stored = json.loads(metadata_arg.split("=", 1)[1])
    assert stored == {
        "at": "2026-08-02T12:34:56Z",
        "description_sha256": _hash(description),
        "paths": ["src/alpha.py", "src/beta.py"],
        "schema": 1,
        "sha": base,
    }
    note = update[update.index("--append-notes") + 1]
    assert note.startswith("victor-validated-at: ")
    assert f"sha={base}" in note
    assert 'paths=["src/alpha.py","src/beta.py"]' in note
    assert published["record"] == stored


def test_stamp_refuses_missing_path_before_any_tracker_write(git_repo):
    repo, base = git_repo
    bead = _record("SABLE-stamp", "Inspect a missing path")
    calls: list[list[str]] = []

    def fake_bd(argv, cwd):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, json.dumps([bead]), "")

    with pytest.raises(manifest.ManifestUnavailable, match="not present"):
        manifest.publish_stamp(
            "SABLE-stamp",
            repo=repo,
            sha_ref=base,
            paths=["missing.py"],
            bd_run=fake_bd,
        )

    assert [call[:2] for call in calls] == [["bd", "show"]]


def test_partition_cli_uses_one_unlimited_open_snapshot(git_repo, monkeypatch, capsys):
    repo, base = git_repo
    description = "Inspect src/alpha.py"
    records = [_record("SABLE-one", description, _stamp(base, description))]
    calls: list[list[str]] = []

    def fake_bd(argv, cwd):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, json.dumps(records), "")

    monkeypatch.setattr(manifest, "_run_bd", fake_bd)
    rc = manifest.main(["partition", "--repo", str(repo), "--head", "HEAD", "--json"])

    assert rc == 0
    assert calls == [[
        "bd", "list", "--status", "open", "--no-assignee", "--json", "--limit", "0"
    ]]
    payload = json.loads(capsys.readouterr().out)
    assert payload["claim_bound"] == manifest.ANCHOR_ONLY_CLAIM


@pytest.mark.skipif(shutil.which("bd") is None, reason="bd unavailable")
def test_real_bd_stamp_then_partition_round_trip(git_repo, tmp_path):
    repo, base = git_repo
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ)
    for key in ("BEADS_DB", "BEADS_DIR", "SABLE_RC_BD"):
        env.pop(key, None)
    env.update({"HOME": str(home), "BD_NON_INTERACTIVE": "1", "CI": "true"})
    init = subprocess.run(
        ["bd", "init", "--prefix=VMF", "--non-interactive", "--skip-agents", "--skip-hooks", "--quiet"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )
    if init.returncode != 0:
        pytest.skip(f"bd init unavailable here: {init.stderr.strip()[:200]}")
    created = subprocess.run(
        [
            "bd",
            "create",
            "--title=Victor manifest fixture",
            "--description=Inspect src/alpha.py",
            "--type=task",
            "--json",
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
        check=True,
    )
    payload = json.loads(created.stdout)
    if isinstance(payload, list):
        payload = payload[0]
    bead_id = payload["id"]

    stamped = subprocess.run(
        [
            str(_CLI),
            "stamp",
            bead_id,
            "--repo",
            str(repo),
            "--sha",
            base,
            "--path",
            "src/alpha.py",
            "--json",
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert stamped.returncode == 0, stamped.stderr
    shown = subprocess.run(
        ["bd", "show", bead_id, "--json"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
        check=True,
    )
    shown_record = json.loads(shown.stdout)[0]
    assert manifest.METADATA_KEY in shown_record["metadata"]
    assert "victor-validated-at:" in shown_record["notes"]

    unchanged = subprocess.run(
        [str(_CLI), "partition", "--repo", str(repo), "--head", "HEAD", "--json"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert unchanged.returncode == 0, unchanged.stderr
    assert [row["id"] for row in json.loads(unchanged.stdout)["fresh_anchors_unmoved"]] == [bead_id]

    (repo / "src" / "alpha.py").write_text("alpha = 3\n", encoding="utf-8")
    _commit(repo, "change after validation")
    changed = subprocess.run(
        [str(_CLI), "partition", "--repo", str(repo), "--head", "HEAD", "--json"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert changed.returncode == 0, changed.stderr
    row = json.loads(changed.stdout)["deep_pass"][0]
    assert row["id"] == bead_id
    assert row["reason_code"] == "anchors-changed"


@pytest.mark.parametrize("copy_mode", [False, True])
def test_bin_installer_makes_the_new_tool_runnable(tmp_path, copy_mode):
    dest = tmp_path / ("copy-bin" if copy_mode else "link-bin")
    argv = [str(_BIN / "sable-bin-install"), "--dir", str(dest)]
    if copy_mode:
        argv.append("--copy")
    installed = subprocess.run(argv, text=True, capture_output=True, timeout=60)
    assert installed.returncode == 0, installed.stderr
    target = dest / "sable-victor-manifest"
    assert target.exists()
    help_result = subprocess.run([str(target), "--help"], text=True, capture_output=True, timeout=30)
    assert help_result.returncode == 0, help_result.stderr
    assert "fresh_anchors_unmoved" in help_result.stdout


def test_victor_source_uses_tool_and_states_bootstrap_and_claim_bound():
    card = (_REPO / "templates/multi-manager/roles/victor.md").read_text(encoding="utf-8")

    assert "sable-victor-manifest partition" in card
    assert "sable-victor-manifest stamp" in card
    assert "fresh-anchors-unmoved" in card
    assert "first sweep" in card.lower() and "deep" in card.lower()
    assert "note" in card.lower() and "fallback" in card.lower()
    assert "git diff --name-only <last-sha>..HEAD" not in card
