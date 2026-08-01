#!/usr/bin/env python3
"""Tests for sable_test_cost_profile_lib — the shared machine-local cost
profile store and its publisher (SABLE-y4nom.7.2).

Contract under test (codex-cleared v2.1):
- ONE canonical <git-common-dir>/sable/test-cost/profile.json embedding
  metadata AND both cost maps; staging -> fsync -> os.replace; readers see
  old-or-new, never partial. Raw reporter outputs are staging-only.
- Kernel flock, NONBLOCKING: a second publisher is refused while the first
  holds; the lock auto-releases when the holder dies.
- The publisher RUNS the producers itself (injectable runner — these tests
  fake the producer PROCESSES, never the protocol around them), validates
  everything, and refuses on HEAD/porcelain/fingerprint movement.
- Catalog split: stale digest is a read-time refusal naming added/removed;
  missing measurement rows under a FRESH digest are explicitly provisional.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sable_test_cost_profile_lib as profile_lib  # noqa: E402


# ---------------------------------------------------------------------------
# fixture repo: a real git repo with the minimal catalog surfaces the
# publisher reads (ALLOW via sourcing shell-run-set.sh; python tests via
# git ls-files bin/test_*.py).
# ---------------------------------------------------------------------------

RUNSET = """\
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ALLOW=(
  test-fx-a.sh
  test-fx-b.sh
)
"""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    (repo / ".github/ci").mkdir(parents=True)
    (repo / "hooks/test").mkdir(parents=True)
    (repo / "bin").mkdir()
    (repo / ".github/ci/shell-run-set.sh").write_text(RUNSET)
    for suite in ("test-fx-a.sh", "test-fx-b.sh"):
        path = repo / "hooks/test" / suite
        path.write_text("#!/usr/bin/env bash\nexit 0\n")
        path.chmod(0o755)
    (repo / "bin/test_alpha.py").write_text("def test_a(): assert True\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


PY_REPORT = {
    "thresholds": {"ordinary_test_seconds": 5.0, "ordinary_module_seconds": 30.0},
    "tests": [],
    "modules": [{"module": "bin/test_alpha.py", "seconds": 1.5}],
    "violations": [],
}
SHELL_TSV = "suite\tstatus\tseconds\ntest-fx-a.sh\tpass\t2.0\ntest-fx-b.sh\tpass\t3.0\n"


def fake_producer_runner(
    py_report: dict | None = None,
    shell_tsv: str | None = None,
    py_rc: int = 0,
    shell_rc: int = 0,
    hook=None,
):
    """A runner that impersonates the two producer PROCESSES: it writes the
    staging files the argv names, exactly as the real producers would."""

    def runner(argv, **kwargs):
        if hook is not None:
            hook(argv)
        argv = [str(a) for a in argv]
        joined = " ".join(argv)
        if "--sable-test-cost-report=" in joined:
            target = next(
                a.split("=", 1)[1] for a in argv
                if a.startswith("--sable-test-cost-report=")
            )
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_text(json.dumps(py_report or PY_REPORT))
            return subprocess.CompletedProcess(argv, py_rc, stdout="", stderr="")
        if "--profile" in argv:
            target = argv[argv.index("--profile") + 1]
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_text(
                shell_tsv if shell_tsv is not None else SHELL_TSV
            )
            return subprocess.CompletedProcess(argv, shell_rc, stdout="", stderr="")
        # everything else (git, doctor, version probes) runs for real
        return subprocess.run(argv, **kwargs)

    return runner


# ---------------------------------------------------------------------------
# store resolution
# ---------------------------------------------------------------------------

def test_store_dir_lives_in_the_git_common_dir(tmp_path):
    repo = make_repo(tmp_path)
    store = profile_lib.store_dir(repo)
    common = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    assert store == common / "sable" / "test-cost"


def test_linked_worktrees_resolve_the_same_store(tmp_path):
    repo = make_repo(tmp_path)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-b", "wt", str(linked))
    assert profile_lib.store_dir(repo) == profile_lib.store_dir(linked)


# ---------------------------------------------------------------------------
# publish happy path + provenance
# ---------------------------------------------------------------------------

def test_publish_writes_one_canonical_profile_with_both_maps(tmp_path):
    repo = make_repo(tmp_path)
    profile = profile_lib.publish(repo, runner=fake_producer_runner())
    store = profile_lib.store_dir(repo)
    on_disk = json.loads((store / profile_lib.PROFILE_NAME).read_text())
    assert on_disk == profile
    assert on_disk["schema"] == profile_lib.SCHEMA_VERSION
    assert on_disk["python_costs"] == {"bin/test_alpha.py": 1.5}
    assert on_disk["shell_costs"] == {"test-fx-a.sh": 2.0, "test-fx-b.sh": 3.0}
    # staging files were deleted after embedding — canonical file only
    leftovers = [p.name for p in store.iterdir()
                 if p.name not in (profile_lib.PROFILE_NAME, profile_lib.LOCK_NAME)]
    assert leftovers == []


def test_publish_provenance_records_argv_arrays_head_and_durations(tmp_path):
    repo = make_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    profile = profile_lib.publish(repo, runner=fake_producer_runner())
    prov = profile["provenance"]
    assert prov["source_sha"] == head
    argvs = prov["producer_argvs"]
    assert len(argvs) == 2
    assert any("--sable-test-cost-report=" in " ".join(a) for a in argvs)
    assert any("--profile" in a for a in argvs)
    assert all(isinstance(a, list) for a in argvs)
    assert set(prov["wall_seconds"]) == {"python", "shell"}
    assert "timestamp" in prov and "doctor" in prov
    # source sha is provenance, NOT part of the fingerprint (per contract)
    assert "source_sha" not in json.dumps(profile["fingerprint"])


def test_fingerprint_carries_resolved_identities_and_catalog(tmp_path):
    import shutil
    repo = make_repo(tmp_path)
    fp = profile_lib.compute_fingerprint(repo)
    assert fp["python_executable"] == sys.executable
    assert fp["python_executable_resolved"] == os.path.realpath(sys.executable)
    assert sys.version.split()[0] in fp["python_version"]
    # collision-safe sorted [name, distribution, version] records
    plugins = fp["pytest11_plugins"]
    assert isinstance(plugins, list)
    assert plugins == sorted(plugins)
    assert all(len(rec) == 3 for rec in plugins)
    for tool in ("bd", "dolt", "tmux", "git"):
        assert tool in fp["tools"]
        located = shutil.which(tool)
        if located:
            # RESOLVED path, not the symlink shim (round-1 B5)
            assert fp["tools"][tool]["path"] == os.path.realpath(located)
    assert fp["cpu_count"] == os.cpu_count()
    assert fp["catalog"]["python_tests"] == ["bin/test_alpha.py"]
    assert fp["catalog"]["shell_allow"] == ["test-fx-a.sh", "test-fx-b.sh"]
    assert fp["catalog_digest"]


# ---------------------------------------------------------------------------
# atomicity: a refused publish never damages the existing canonical file
# ---------------------------------------------------------------------------

def test_failed_producer_leaves_existing_profile_byte_identical(tmp_path):
    repo = make_repo(tmp_path)
    profile_lib.publish(repo, runner=fake_producer_runner())
    store = profile_lib.store_dir(repo)
    before = (store / profile_lib.PROFILE_NAME).read_bytes()
    with pytest.raises(profile_lib.ProfilePublishError, match="producer"):
        profile_lib.publish(repo, runner=fake_producer_runner(py_rc=1))
    assert (store / profile_lib.PROFILE_NAME).read_bytes() == before


# ---------------------------------------------------------------------------
# flock: nonblocking refusal + auto-release on death
# ---------------------------------------------------------------------------

HOLDER = """\
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("held", flush=True)
time.sleep(120)
"""


def _hold_lock(lock_path: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(lock_path)],
        stdout=subprocess.PIPE, text=True,
    )
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_second_publisher_is_refused_immediately_while_lock_held(tmp_path):
    repo = make_repo(tmp_path)
    store = profile_lib.store_dir(repo)
    store.mkdir(parents=True, exist_ok=True)
    holder = _hold_lock(store / profile_lib.LOCK_NAME)
    try:
        started = time.monotonic()
        with pytest.raises(profile_lib.ProfilePublishError, match="another publisher"):
            profile_lib.publish(repo, runner=fake_producer_runner())
        # NONBLOCKING: refusal is immediate, not a 120s wait for the holder
        assert time.monotonic() - started < 10
    finally:
        holder.kill()
        holder.wait()


def test_lock_auto_releases_when_holder_dies(tmp_path):
    repo = make_repo(tmp_path)
    store = profile_lib.store_dir(repo)
    store.mkdir(parents=True, exist_ok=True)
    holder = _hold_lock(store / profile_lib.LOCK_NAME)
    os.kill(holder.pid, signal.SIGKILL)
    holder.wait()
    profile = profile_lib.publish(repo, runner=fake_producer_runner())
    assert profile["schema"] == profile_lib.SCHEMA_VERSION


# ---------------------------------------------------------------------------
# producer cleanliness + source-motion guards (B7/C3)
# ---------------------------------------------------------------------------

def test_dirty_worktree_is_refused(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "bin/test_alpha.py").write_text("def test_a(): assert 1\n")
    with pytest.raises(profile_lib.ProfilePublishError, match="clean"):
        profile_lib.publish(repo, runner=fake_producer_runner())


def test_head_movement_during_producers_is_refused(tmp_path):
    repo = make_repo(tmp_path)

    def move_head(argv):
        if any("--profile" in str(a) for a in argv):
            (repo / "moved.txt").write_text("x\n")
            _git(repo, "add", "moved.txt")
            _git(repo, "commit", "-qm", "moved mid-publish")

    with pytest.raises(profile_lib.ProfilePublishError, match="HEAD"):
        profile_lib.publish(repo, runner=fake_producer_runner(hook=move_head))


def test_tree_dirtied_during_producers_is_refused(tmp_path):
    repo = make_repo(tmp_path)

    def dirty_tree(argv):
        if any("--profile" in str(a) for a in argv):
            (repo / "stray.txt").write_text("x\n")

    with pytest.raises(profile_lib.ProfilePublishError, match="clean|porcelain"):
        profile_lib.publish(repo, runner=fake_producer_runner(hook=dirty_tree))


# ---------------------------------------------------------------------------
# producer validation (B6)
# ---------------------------------------------------------------------------

def test_missing_allow_suite_row_is_refused(tmp_path):
    repo = make_repo(tmp_path)
    tsv = "suite\tstatus\tseconds\ntest-fx-a.sh\tpass\t2.0\n"
    with pytest.raises(profile_lib.ProfilePublishError, match="test-fx-b.sh"):
        profile_lib.publish(repo, runner=fake_producer_runner(shell_tsv=tsv))


def test_unexpected_shell_identity_is_refused(tmp_path):
    repo = make_repo(tmp_path)
    tsv = SHELL_TSV + "test-not-in-allow.sh\tpass\t1.0\n"
    with pytest.raises(profile_lib.ProfilePublishError, match="test-not-in-allow.sh"):
        profile_lib.publish(repo, runner=fake_producer_runner(shell_tsv=tsv))


def test_duplicate_shell_identity_is_refused(tmp_path):
    repo = make_repo(tmp_path)
    tsv = SHELL_TSV + "test-fx-a.sh\tpass\t2.5\n"
    with pytest.raises(profile_lib.ProfilePublishError, match="duplicate"):
        profile_lib.publish(repo, runner=fake_producer_runner(shell_tsv=tsv))


def test_nonpass_allow_suite_is_refused(tmp_path):
    repo = make_repo(tmp_path)
    tsv = "suite\tstatus\tseconds\ntest-fx-a.sh\tpass\t2.0\ntest-fx-b.sh\tfail\t3.0\n"
    with pytest.raises(profile_lib.ProfilePublishError, match="test-fx-b.sh"):
        profile_lib.publish(repo, runner=fake_producer_runner(shell_tsv=tsv))


def test_python_violations_are_refused(tmp_path):
    repo = make_repo(tmp_path)
    report = {"modules": PY_REPORT["modules"], "violations": [{"module": "x"}]}
    with pytest.raises(profile_lib.ProfilePublishError, match="violation"):
        profile_lib.publish(repo, runner=fake_producer_runner(py_report=report))


def test_python_gap_is_recorded_explicitly_provisional(tmp_path):
    repo = make_repo(tmp_path)
    (repo / "bin/test_beta.py").write_text("def test_b(): assert True\n")
    _git(repo, "add", "bin/test_beta.py")
    _git(repo, "commit", "-qm", "second test module")
    # the report covers only alpha — beta is a gap, allowed but NAMED
    profile = profile_lib.publish(repo, runner=fake_producer_runner())
    assert profile["provisional_python"] == ["bin/test_beta.py"]


# ---------------------------------------------------------------------------
# read side: fingerprint gate + catalog split (B5)
# ---------------------------------------------------------------------------

def test_load_profile_round_trips_after_publish(tmp_path):
    repo = make_repo(tmp_path)
    profile_lib.publish(repo, runner=fake_producer_runner())
    costs, message = profile_lib.load_profile(repo)
    assert message is None
    assert costs["python"] == {"bin/test_alpha.py": 1.5}
    assert costs["shell"] == {"test-fx-a.sh": 2.0, "test-fx-b.sh": 3.0}


def test_load_profile_from_linked_worktree_shares_the_store(tmp_path):
    repo = make_repo(tmp_path)
    profile_lib.publish(repo, runner=fake_producer_runner())
    linked = tmp_path / "linked2"
    _git(repo, "worktree", "add", "-b", "wt2", str(linked))
    costs, message = profile_lib.load_profile(linked)
    assert message is None
    assert costs["shell"]["test-fx-b.sh"] == 3.0


def test_missing_store_reads_as_absent_not_error(tmp_path):
    repo = make_repo(tmp_path)
    costs, message = profile_lib.load_profile(repo)
    assert costs is None
    assert "no shared cost profile" in (message or "")


def test_tampered_fingerprint_is_a_loud_refusal(tmp_path):
    repo = make_repo(tmp_path)
    profile_lib.publish(repo, runner=fake_producer_runner())
    store = profile_lib.store_dir(repo)
    on_disk = json.loads((store / profile_lib.PROFILE_NAME).read_text())
    on_disk["fingerprint"]["cpu_count"] = 9999
    (store / profile_lib.PROFILE_NAME).write_text(json.dumps(on_disk))
    costs, message = profile_lib.load_profile(repo)
    assert costs is None
    assert "cpu_count" in message


def test_stale_catalog_digest_names_added_entries(tmp_path):
    repo = make_repo(tmp_path)
    profile_lib.publish(repo, runner=fake_producer_runner())
    (repo / "bin/test_gamma.py").write_text("def test_g(): assert True\n")
    _git(repo, "add", "bin/test_gamma.py")
    _git(repo, "commit", "-qm", "new catalog entry")
    costs, message = profile_lib.load_profile(repo)
    assert costs is None
    assert "test_gamma" in message


def test_corrupt_profile_is_a_loud_refusal(tmp_path):
    repo = make_repo(tmp_path)
    store = profile_lib.store_dir(repo)
    store.mkdir(parents=True, exist_ok=True)
    (store / profile_lib.PROFILE_NAME).write_text("{ not json")
    costs, message = profile_lib.load_profile(repo)
    assert costs is None
    assert message


# ---------------------------------------------------------------------------
# codex early-check discriminators (1)-(7): sharper than key-presence.
# ---------------------------------------------------------------------------

def test_reader_during_publish_window_sees_the_old_complete_profile(tmp_path):
    """(1) A reader while a publish is in flight parses the OLD canonical
    profile completely — the replace is atomic, the window shows old bytes."""
    repo = make_repo(tmp_path)
    first = profile_lib.publish(repo, runner=fake_producer_runner())
    seen_during = []

    def read_mid_publish(argv):
        if any("--profile" in str(a) for a in argv):
            costs, message = profile_lib.load_profile(repo)
            seen_during.append((costs, message))

    second_tsv = "suite\tstatus\tseconds\ntest-fx-a.sh\tpass\t7.0\ntest-fx-b.sh\tpass\t8.0\n"
    profile_lib.publish(
        repo, runner=fake_producer_runner(shell_tsv=second_tsv, hook=read_mid_publish),
    )
    assert seen_during, "mid-publish read never happened"
    costs, message = seen_during[0]
    assert message is None
    assert costs["shell"] == first["shell_costs"]  # OLD values, complete
    after, message = profile_lib.load_profile(repo)
    assert after["shell"]["test-fx-a.sh"] == 7.0  # NEW after replace


def test_env_fingerprint_drift_with_clean_tree_is_refused(tmp_path, monkeypatch):
    """(2) HEAD/porcelain unchanged but the ENVIRONMENT moved between the
    pre and post fingerprint computations — refuse."""
    repo = make_repo(tmp_path)
    real = profile_lib.compute_fingerprint
    calls = {"n": 0}

    def drifting(repo_root, **kwargs):
        fp = real(repo_root, **kwargs)
        calls["n"] += 1
        if calls["n"] > 1:
            fp = dict(fp)
            fp["cpu_count"] = (fp.get("cpu_count") or 0) + 1
        return fp

    monkeypatch.setattr(profile_lib, "compute_fingerprint", drifting)
    with pytest.raises(profile_lib.ProfilePublishError, match="fingerprint"):
        profile_lib.publish(repo, runner=fake_producer_runner())


def test_python_duplicate_module_rows_are_refused(tmp_path):
    report = {
        "thresholds": {"ordinary_test_seconds": 5.0, "ordinary_module_seconds": 30.0}, "tests": [],
        "modules": [
            {"module": "bin/test_alpha.py", "seconds": 1.5},
            {"module": "bin/test_alpha.py", "seconds": 2.5},
        ],
        "violations": [],
    }
    with pytest.raises(profile_lib.ProfilePublishError, match="duplicate"):
        profile_lib.publish(repo := make_repo(tmp_path), runner=fake_producer_runner(py_report=report))


def test_python_unexpected_module_identity_is_refused(tmp_path):
    report = {
        "thresholds": {"ordinary_test_seconds": 5.0, "ordinary_module_seconds": 30.0}, "tests": [],
        "modules": [
            {"module": "bin/test_alpha.py", "seconds": 1.5},
            {"module": "bin/test_never_tracked.py", "seconds": 1.0},
        ],
        "violations": [],
    }
    with pytest.raises(profile_lib.ProfilePublishError, match="test_never_tracked"):
        profile_lib.publish(make_repo(tmp_path), runner=fake_producer_runner(py_report=report))


def test_malformed_python_report_json_is_refused_not_crashed(tmp_path):
    repo = make_repo(tmp_path)

    def runner(argv, **kwargs):
        argv = [str(a) for a in argv]
        joined = " ".join(argv)
        if "--sable-test-cost-report=" in joined:
            target = next(a.split("=", 1)[1] for a in argv
                          if a.startswith("--sable-test-cost-report="))
            Path(target).write_text("{ not json")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if "--profile" in argv:
            target = argv[argv.index("--profile") + 1]
            Path(target).write_text(SHELL_TSV)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        return subprocess.run(argv, **kwargs)

    with pytest.raises(profile_lib.ProfilePublishError):
        profile_lib.publish(repo, runner=runner)


def test_malformed_shell_header_is_refused(tmp_path):
    with pytest.raises(profile_lib.ProfilePublishError):
        profile_lib.publish(
            make_repo(tmp_path),
            runner=fake_producer_runner(shell_tsv="wrong\theader\nrow\n"),
        )


def test_catalog_removed_entry_is_named_at_read_time(tmp_path):
    """(5) removal diagnostics, the other polarity of the added case."""
    repo = make_repo(tmp_path)
    profile_lib.publish(repo, runner=fake_producer_runner())
    _git(repo, "rm", "-q", "bin/test_alpha.py")
    _git(repo, "commit", "-qm", "remove catalog entry")
    costs, message = profile_lib.load_profile(repo)
    assert costs is None
    assert "test_alpha" in message


def test_partial_canonical_object_is_refused_at_read(tmp_path):
    """(6) valid JSON, missing required section — refuse, never guess."""
    repo = make_repo(tmp_path)
    profile_lib.publish(repo, runner=fake_producer_runner())
    store = profile_lib.store_dir(repo)
    on_disk = json.loads((store / profile_lib.PROFILE_NAME).read_text())
    del on_disk["python_costs"]
    (store / profile_lib.PROFILE_NAME).write_text(json.dumps(on_disk))
    costs, message = profile_lib.load_profile(repo)
    assert costs is None
    assert "python_costs" in message


def test_lock_fd_is_not_inherited_by_producer_children(tmp_path, monkeypatch):
    """(7) a REAL producer child must not hold the lock fd — a leaked
    inherited fd would keep the lock alive past the publisher's death."""
    repo = make_repo(tmp_path)
    probe_out = tmp_path / "fd-probe.txt"
    probe = (
        "import os,sys\n"
        "hits=[]\n"
        "for f in os.listdir('/proc/self/fd'):\n"
        "    try: t=os.readlink(f'/proc/self/fd/{f}')\n"
        "    except OSError: continue\n"
        "    if 'publish.lock' in t: hits.append(t)\n"
        f"open({str(probe_out)!r},'w').write(repr(hits))\n"
    )

    def probing_argvs(repo_root, staging_py, staging_tsv):
        py_writer = (
            f"open({str(staging_py)!r},'w').write({json.dumps(PY_REPORT)!r})\n"
            + probe
        )
        tsv_writer = f"open({str(staging_tsv)!r},'w').write({SHELL_TSV!r})\n"
        return [
            [sys.executable, "-c", py_writer],
            [sys.executable, "-c", tsv_writer],
        ]

    monkeypatch.setattr(profile_lib, "_producer_argvs", probing_argvs)
    profile_lib.publish(repo)  # real subprocess runner
    assert probe_out.read_text() == "[]"


def test_provenance_pins_the_exact_default_producer_argv_shapes(tmp_path):
    """v2.1 C4: byte-equivalence must be mechanically assessable — pin the
    exact argv prefixes, not key presence."""
    repo = make_repo(tmp_path)
    profile = profile_lib.publish(repo, runner=fake_producer_runner())
    py_argv, shell_argv = profile["provenance"]["producer_argvs"]
    assert py_argv[:9] == [
        sys.executable, "-m", "pytest", "bin/", "-q", "-rs", "-p",
        "no:cacheprovider", "--sable-report-skip-set",
    ]
    assert py_argv[9].startswith("--sable-test-cost-report=")
    assert shell_argv[:3] == ["bash", ".github/ci/shell-run-set.sh", "--profile"]
    assert len(shell_argv) == 4


# ---------------------------------------------------------------------------
# round-1 review controls (codex B1-B5): typed canonical boundary, actual
# fail-closed catalog, strict finite numbers, normalized refusals + cleanup,
# resolved identities.
# ---------------------------------------------------------------------------

def _tamper(repo, mutate):
    store = profile_lib.store_dir(repo)
    on_disk = json.loads((store / profile_lib.PROFILE_NAME).read_text())
    mutated = mutate(on_disk)
    (store / profile_lib.PROFILE_NAME).write_text(json.dumps(mutated))


def test_string_cost_map_is_refused_not_bound(tmp_path):
    repo = make_repo(tmp_path)
    profile_lib.publish(repo, runner=fake_producer_runner())

    def mutate(profile):
        profile["python_costs"] = "not-a-map"
        return profile

    _tamper(repo, mutate)
    costs, message = profile_lib.load_profile(repo)
    assert costs is None
    assert "python_costs" in message and "not a map" in message


def test_top_level_list_with_key_strings_degrades_never_raises(tmp_path):
    repo = make_repo(tmp_path)
    store = profile_lib.store_dir(repo)
    store.mkdir(parents=True, exist_ok=True)
    (store / profile_lib.PROFILE_NAME).write_text(
        json.dumps(list(profile_lib._REQUIRED_PROFILE_KEYS))
    )
    costs, message = profile_lib.load_profile(repo)
    assert costs is None
    assert "not an object" in message


def test_corrupt_nested_fingerprint_catalog_and_provenance_degrade(tmp_path):
    repo = make_repo(tmp_path)
    for mutate, needle in (
        (lambda p: {**p, "fingerprint": "x"}, "fingerprint is not an object"),
        (lambda p: {**p, "fingerprint": {**p["fingerprint"], "catalog": 7}},
         "catalog is not an object"),
        (lambda p: {**p, "provenance": []}, "provenance is not an object"),
    ):
        profile_lib.publish(repo, runner=fake_producer_runner())
        _tamper(repo, mutate)
        costs, message = profile_lib.load_profile(repo)
        assert costs is None
        assert needle in message


def test_self_inconsistent_stored_digest_is_refused(tmp_path):
    """Tampering the stored ARRAYS without recomputing the stored digest is
    caught by digest recomputation — a self-inconsistent fingerprint cannot
    bind (round-1 B1)."""
    repo = make_repo(tmp_path)
    profile_lib.publish(repo, runner=fake_producer_runner())

    def mutate(profile):
        profile["fingerprint"]["catalog"]["python_tests"] = []
        return profile

    _tamper(repo, mutate)
    costs, message = profile_lib.load_profile(repo)
    assert costs is None
    assert "self-inconsistent" in message


def test_nonfinite_and_boolean_stored_costs_are_refused(tmp_path):
    repo = make_repo(tmp_path)
    for value, label in ((float("nan"), "nan"), (float("inf"), "inf"), (True, "bool")):
        profile_lib.publish(repo, runner=fake_producer_runner())

        def mutate(profile, value=value):
            profile["shell_costs"]["test-fx-a.sh"] = value
            return profile

        store = profile_lib.store_dir(repo)
        on_disk = json.loads((store / profile_lib.PROFILE_NAME).read_text())
        on_disk = mutate(on_disk)
        (store / profile_lib.PROFILE_NAME).write_text(
            json.dumps(on_disk)  # stdlib allows NaN/Inf on write — the READER must refuse
        )
        costs, message = profile_lib.load_profile(repo)
        assert costs is None, label
        assert "finite positive" in message, label


def test_untracked_python_test_module_is_a_named_catalog_refusal(tmp_path):
    """round-1 B2: the catalog is the ACTUAL working tree — an UNTRACKED new
    test module must invalidate, exactly like a committed one (the
    changed-path collector sees untracked files, so the catalog must)."""
    repo = make_repo(tmp_path)
    profile_lib.publish(repo, runner=fake_producer_runner())
    (repo / "bin/test_beta.py").write_text("def test_b(): assert True\n")
    costs, message = profile_lib.load_profile(repo)
    assert costs is None
    assert "test_beta" in message


def test_broken_shell_run_set_is_a_loud_discovery_refusal(tmp_path):
    repo = make_repo(tmp_path)
    profile_lib.publish(repo, runner=fake_producer_runner())
    (repo / ".github/ci/shell-run-set.sh").write_text("exit 3\n")
    costs, message = profile_lib.load_profile(repo)
    assert costs is None
    assert "catalog discovery failed" in message


def test_python_nan_and_bool_seconds_are_refused_at_publish(tmp_path):
    for bad in (float("nan"), float("inf"), True):
        report = {
            "thresholds": {"ordinary_test_seconds": 5.0, "ordinary_module_seconds": 30.0}, "tests": [],
            "modules": [{"module": "bin/test_alpha.py", "seconds": bad}],
            "violations": [],
        }
        with pytest.raises(profile_lib.ProfilePublishError, match="finite positive"):
            profile_lib.publish(
                make_repo(tmp_path, f"nanrepo-{bad!r}"[:24].replace("'", "")),
                runner=fake_producer_runner(py_report=report),
            )


def test_shell_nan_seconds_are_refused_at_publish(tmp_path):
    tsv = "suite\tstatus\tseconds\ntest-fx-a.sh\tpass\tnan\ntest-fx-b.sh\tpass\t3.0\n"
    with pytest.raises(profile_lib.ProfilePublishError, match="finite positive"):
        profile_lib.publish(
            make_repo(tmp_path), runner=fake_producer_runner(shell_tsv=tsv),
        )


def test_missing_or_wrong_typed_violations_member_is_refused(tmp_path):
    for report, label in (
        ({"thresholds": {"ordinary_test_seconds": 5.0, "ordinary_module_seconds": 30.0}, "tests": [], "modules": []}, "missing"),
        ({"thresholds": {"ordinary_test_seconds": 5.0, "ordinary_module_seconds": 30.0}, "tests": [], "modules": [],
           "violations": "x"}, "wrong-type"),
    ):
        with pytest.raises(
            profile_lib.ProfilePublishError, match="violations",
        ):
            profile_lib.publish(
                make_repo(tmp_path, f"violrepo-{label}"),
                runner=fake_producer_runner(py_report=report),
            )


def test_producer_spawn_oserror_is_a_normalized_refusal(tmp_path):
    repo = make_repo(tmp_path)
    profile_lib.publish(repo, runner=fake_producer_runner())
    store = profile_lib.store_dir(repo)
    before = (store / profile_lib.PROFILE_NAME).read_bytes()

    def exploding_runner(argv, **kwargs):
        raise OSError("no such interpreter")

    with pytest.raises(profile_lib.ProfilePublishError, match="spawned"):
        profile_lib.publish(repo, runner=exploding_runner)
    assert (store / profile_lib.PROFILE_NAME).read_bytes() == before
    assert not list(store.glob(".staging-*"))


def test_replace_oserror_is_normalized_and_leaves_no_staging(
    tmp_path, monkeypatch,
):
    repo = make_repo(tmp_path)
    profile_lib.publish(repo, runner=fake_producer_runner())
    store = profile_lib.store_dir(repo)
    before = (store / profile_lib.PROFILE_NAME).read_bytes()
    real_replace = os.replace

    def failing_replace(src, dst):
        if "profile.json" in str(dst):
            raise OSError("device full")
        return real_replace(src, dst)

    monkeypatch.setattr(profile_lib.os, "replace", failing_replace)
    with pytest.raises(
        profile_lib.ProfilePublishError, match="cannot write the canonical",
    ):
        profile_lib.publish(repo, runner=fake_producer_runner())
    assert (store / profile_lib.PROFILE_NAME).read_bytes() == before
    assert not list(store.glob(".staging-*"))


def test_empty_container_provenance_polarities_are_refused(tmp_path):
    """codex live check: empty producer_argvs / wall_seconds satisfy an
    all(...) predicate vacuously — the exact-shape pins must refuse them,
    plus empty source_sha/timestamp and an untyped doctor."""
    repo = make_repo(tmp_path)
    cases = (
        (lambda p: {**p, "provenance": {**p["provenance"], "producer_argvs": []}},
         "exactly two non-empty"),
        (lambda p: {**p, "provenance": {**p["provenance"], "wall_seconds": {}}},
         "exactly {python, shell}"),
        (lambda p: {**p, "provenance": {**p["provenance"], "source_sha": ""}},
         "source_sha"),
        (lambda p: {**p, "provenance": {**p["provenance"], "timestamp": ""}},
         "timestamp"),
        (lambda p: {**p, "provenance": {**p["provenance"], "doctor": {}}},
         "doctor"),
    )
    for mutate, needle in cases:
        profile_lib.publish(repo, runner=fake_producer_runner())
        _tamper(repo, mutate)
        costs, message = profile_lib.load_profile(repo)
        assert costs is None, needle
        assert needle in message, (needle, message)


# ---------------------------------------------------------------------------
# round-2 narrow NO-GO controls: tool-specific version probes + the three
# implemented-but-unpinned refusal seams.
# ---------------------------------------------------------------------------

def test_tool_version_argvs_are_tool_specific():
    assert profile_lib._TOOL_VERSION_ARGS == {
        "bd": ["--version"],
        "dolt": ["version"],
        "tmux": ["-V"],
        "git": ["--version"],
    }


def test_present_tool_versions_are_real_identities_not_error_strings(tmp_path):
    """Real-host discriminator: every PRESENT tool's recorded version must
    carry a digit — 'unknown option' error strings do not — so this cannot
    pass by key/path presence."""
    import shutil
    repo = make_repo(tmp_path)
    fp = profile_lib.compute_fingerprint(repo)
    for tool in ("bd", "dolt", "tmux", "git"):
        if shutil.which(tool):
            version = fp["tools"][tool]["version"]
            assert version and any(c.isdigit() for c in version), (tool, version)
            assert "unknown option" not in version, (tool, version)
            assert "Failed to parse" not in version, (tool, version)


def test_present_tool_with_failing_version_probe_refuses_fingerprint(
    tmp_path, monkeypatch,
):
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    fake_dolt = fake_bin / "dolt"
    fake_dolt.write_text("#!/usr/bin/env bash\necho 'unknown option' >&2\nexit 1\n")
    fake_dolt.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    repo = make_repo(tmp_path)
    with pytest.raises(profile_lib.ProfilePublishError, match="dolt"):
        profile_lib.compute_fingerprint(repo)


def test_catalog_bash_spawn_oserror_degrades_load_profile(tmp_path, monkeypatch):
    """load_profile's never-raises contract holds even when the catalog
    discovery spawn itself fails."""
    repo = make_repo(tmp_path)
    profile_lib.publish(repo, runner=fake_producer_runner())
    real_run = subprocess.run

    def failing_bash(argv, **kwargs):
        if argv and argv[0] == "bash":
            raise OSError("bash vanished")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(profile_lib.subprocess, "run", failing_bash)
    costs, message = profile_lib.load_profile(repo)
    assert costs is None
    assert "cannot spawn bash" in message


def test_git_state_nonzero_is_a_publish_refusal(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    real_run = subprocess.run

    def failing_revparse(argv, **kwargs):
        if argv[:4] == ["git", "-C", str(repo), "rev-parse"] and argv[4] == "HEAD":
            return subprocess.CompletedProcess(argv, 128, stdout="", stderr="fatal: bad")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(profile_lib.subprocess, "run", failing_revparse)
    with pytest.raises(
        profile_lib.ProfilePublishError, match="cannot establish the source state",
    ):
        profile_lib.publish(repo, runner=fake_producer_runner())


def test_unserializable_profile_content_is_a_normalized_refusal(
    tmp_path, monkeypatch,
):
    """codex exact repro: a doctor record carrying a non-JSON object must
    normalize to ProfilePublishError (TypeError boundary), leave the old
    canonical byte-identical, and leave zero staging files."""
    repo = make_repo(tmp_path)
    profile_lib.publish(repo, runner=fake_producer_runner())
    store = profile_lib.store_dir(repo)
    before = (store / profile_lib.PROFILE_NAME).read_bytes()
    monkeypatch.setattr(
        profile_lib, "_doctor_record",
        lambda _repo: {"status": "rc0", "bad": object()},
    )
    with pytest.raises(
        profile_lib.ProfilePublishError, match="cannot write the canonical",
    ):
        profile_lib.publish(repo, runner=fake_producer_runner())
    assert (store / profile_lib.PROFILE_NAME).read_bytes() == before
    assert not list(store.glob(".staging-*"))
