#!/usr/bin/env python3
"""Integration tests for bin/sable-doctor (SABLE-1i6m).

Real composition: the actual `install.sh` installer run against a fresh temp
HOME (same technique as hooks/test/test-install-agent-defs.sh), then the real
`sable-doctor` binary run as a subprocess against that real installed tree and
the real repo — no synthetic fixtures, no mocked filesystem. Proves the tool
against the exact install.sh output it is meant to audit, and reproduces the
two real drift incidents this bead was filed from (SABLE-4ba stale hook,
missing tarzan worker-cap block).
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOCTOR = REPO / "bin" / "sable-doctor"
INSTALLER = REPO / "install.sh"
HAVE_BD = shutil.which("bd") is not None
pytestmark = pytest.mark.skipif(not HAVE_BD, reason="install.sh requires bd; not installed in this clean-room")


def run_install(home_dir: Path):
    # --from-here: this suite commonly runs from a linked SABLE worker worktree
    # (SABLE-3ydb, subsuming SABLE-5r3i/xu1s) which install.sh otherwise refuses
    # to install from; scoped here only, since the HOME being installed into is
    # a throwaway tmp_path either way.
    result = subprocess.run(
        ["bash", str(INSTALLER), "--from-here"],
        env={**os.environ, "HOME": str(home_dir)},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, f"install.sh failed:\n{result.stdout}\n{result.stderr}"


def run_doctor(claude_dir: Path, *extra_args, env=None, bin_dir=None):
    # bin_dir defaults to the fixture's OWN ~/.local/bin (claude_dir's sibling
    # under the same redirected HOME run_install used) — never the real
    # machine's ~/.local/bin. Without this, sable-doctor's --bin-dir default
    # (~/.local/bin under the subprocess's real $HOME) would silently pull
    # the actual dev machine's pinned bins into every assertion here.
    if bin_dir is None:
        bin_dir = claude_dir.parent / ".local" / "bin"
    return subprocess.run(
        [sys.executable, str(DOCTOR), "--repo", str(REPO), "--claude-dir", str(claude_dir),
         "--bin-dir", str(bin_dir), *extra_args],
        capture_output=True, text=True, timeout=30,
        env=env if env is not None else os.environ,
    )


def run_project_install(project_dir: Path):
    # --from-here: this suite itself commonly runs from a linked SABLE worker
    # worktree (SABLE-5r3i/xu1s track install.sh's canonical-checkout refusal
    # for the pre-existing --claude-dir fixture); scoped here only, since the
    # HOME being installed into is a throwaway tmp_path project either way.
    result = subprocess.run(
        ["bash", str(INSTALLER), "--from-here"],
        env={**os.environ, "HOME": str(project_dir)},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, f"install.sh failed:\n{result.stdout}\n{result.stderr}"


def run_doctor_project(cwd: Path, *extra_args, bin_dir=None):
    # The hybrid contract keeps CLI tools global under the HOME used at
    # install time (project_install below runs install.sh with HOME=cwd), so
    # that's the bin_dir to check here — never the real machine's ~/.local/bin
    # (see run_doctor's comment for why that matters).
    if bin_dir is None:
        bin_dir = cwd / ".local" / "bin"
    return subprocess.run(
        [sys.executable, str(DOCTOR), "--repo", str(REPO), "--project",
         "--bin-dir", str(bin_dir), *extra_args],
        cwd=str(cwd),
        capture_output=True, text=True, timeout=30,
    )


@pytest.fixture()
def project_install(tmp_path):
    # the project IS its own git root, and HOME=project makes install.sh's
    # ${HOME}/.claude land exactly at <project-root>/.claude — the same path
    # --project resolves via git-common-dir.
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    run_project_install(project)
    return project


@pytest.fixture()
def installed_claude_dir(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    run_install(home)
    return home / ".claude"


# --- fresh install is clean ---------------------------------------------------

def test_fresh_install_is_clean(installed_claude_dir):
    result = run_doctor(installed_claude_dir)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "clean" in result.stdout
    assert "DRIFT" not in result.stdout


def test_fresh_install_json_reports_clean_true(installed_claude_dir):
    result = run_doctor(installed_claude_dir, "--json")
    assert result.returncode == 0
    import json
    payload = json.loads(result.stdout)
    assert payload["clean"] is True
    assert len(payload["results"]) > 30  # real manifest: dozens of files
    assert all(r["status"] == "clean" for r in payload["results"])


# --- real incident #1: stale installed hook missing a later fix --------------

def test_stale_hook_missing_a_fix_is_detected(installed_claude_dir):
    # models SABLE-4ba / the tdd-evidence.sh f6aw fix: the installed copy is
    # older than repo HEAD, so its content differs even though both exist.
    hook = installed_claude_dir / "hooks" / "tdd-evidence.sh"
    original = hook.read_text()
    hook.write_text(original.replace("\n", "", 1))  # any byte-level divergence from HEAD

    result = run_doctor(installed_claude_dir)
    assert result.returncode == 1
    assert "DRIFT DETECTED" in result.stdout
    assert "tdd-evidence.sh" in result.stdout
    assert "bash install.sh" in result.stdout


# --- real incident #2: installed role file missing a block -------------------

def test_role_file_missing_a_block_is_detected(installed_claude_dir):
    # models the installed tarzan.md missing the SABLE-mmdt worker-cap block
    # that exists in the repo copy.
    role = installed_claude_dir / "sable" / "roles" / "tarzan.md"
    real_repo_role = (REPO / "templates" / "multi-manager" / "roles" / "tarzan.md").read_text()
    truncated = "\n".join(real_repo_role.splitlines()[:-3]) + "\n"  # drop the tail block
    assert truncated != real_repo_role
    role.write_text(truncated)

    result = run_doctor(installed_claude_dir)
    assert result.returncode == 1
    assert "manager roles" in result.stdout
    assert "tarzan.md" in result.stdout


# --- missing file entirely ----------------------------------------------------

def test_missing_installed_agent_def_is_detected(installed_claude_dir):
    (installed_claude_dir / "agents" / "sherlock.md").unlink()
    result = run_doctor(installed_claude_dir)
    assert result.returncode == 1
    assert "MISSING" in result.stdout
    assert "sherlock.md" in result.stdout


# --- quiet mode: silent on clean, one line to stderr on drift ----------------

def test_quiet_mode_silent_when_clean(installed_claude_dir):
    result = run_doctor(installed_claude_dir, "--quiet")
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_quiet_mode_one_line_on_drift(installed_claude_dir):
    (installed_claude_dir / "hooks" / "tdd-gate.sh").write_text("tampered\n")
    result = run_doctor(installed_claude_dir, "--quiet")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "drifted" in result.stderr
    assert "bash install.sh" in result.stderr


# --- --project flag: targets the current git project's own install ----------

def test_doctor_project_against_fresh_project_install_exits_clean_zero(project_install):
    result = run_doctor_project(project_install)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "clean" in result.stdout


def test_doctor_project_detects_drifted_project_hook_copy_reports_drift_exit_one(project_install):
    # mirrors test_stale_hook_missing_a_fix_is_detected, but targeting the
    # project's OWN .claude via --project instead of an explicit --claude-dir.
    hook = project_install / ".claude" / "hooks" / "tdd-evidence.sh"
    original = hook.read_text()
    hook.write_text(original.replace("\n", "", 1))

    result = run_doctor_project(project_install)
    assert result.returncode == 1
    assert "DRIFT DETECTED" in result.stdout
    assert "tdd-evidence.sh" in result.stdout


def test_doctor_project_errors_clearly_when_no_project_install_present_not_false_clean(tmp_path):
    project = tmp_path / "empty-project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)

    result = run_doctor_project(project)
    assert result.returncode != 0
    assert "clean" not in result.stdout           # must never false-clean
    combined = (result.stdout + result.stderr).lower()
    assert "no project install" in combined


# --- re-running install.sh heals the drift it flagged -------------------------

def test_rerunning_installer_heals_flagged_drift(installed_claude_dir, tmp_path):
    (installed_claude_dir / "hooks" / "tdd-gate.sh").write_text("tampered\n")
    assert run_doctor(installed_claude_dir).returncode == 1

    home = installed_claude_dir.parent
    run_install(home)

    result = run_doctor(installed_claude_dir)
    assert result.returncode == 0, result.stdout + result.stderr


# --- worker cap line (SABLE-61dy): real subprocess, real sable_pane_lib import -

def test_doctor_run_shows_cap_line(installed_claude_dir):
    env_unset = {k: v for k, v in os.environ.items() if k != "SABLE_MAX_WORKERS"}
    result = run_doctor(installed_claude_dir, env=env_unset)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "worker cap: 8 (default)" in result.stdout

    env_set = {**os.environ, "SABLE_MAX_WORKERS": "2"}
    result = run_doctor(installed_claude_dir, env=env_set)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "worker cap: 2 (env SABLE_MAX_WORKERS)" in result.stdout


# --- guarded remedy (SABLE-mkj6k acceptance criterion) --------------------------
#
# sable-doctor's SessionStart hook (`sable-doctor --quiet 2>&1 || true`) fires
# on every fresh pane and, on drift, told the agent to `bash install.sh` —
# while the pin-preservation fix (this same bead) is unverified fleet-wide,
# that instruction can silently un-pin a deliberately pinned spine bin
# (DEFECT 2). These invoke the REAL hook command against a real install with
# a genuinely established pin (via the real sable-bin-install) that then gets
# reverted to a symlink exactly as a stale/pre-fix install.sh would — and
# assert the hook's ACTUAL emitted text, not a function's return value.

def _establish_real_pin(bin_dir: Path, target_name: str):
    """Turn an existing symlinked tool into a real pin the way an operator
    would: overwrite it with a real copy, then run the REAL sable-bin-install
    so it detects and records the pin for real (.sable-pinned marker)."""
    target = bin_dir / target_name
    content = target.resolve().read_bytes()
    target.unlink()
    target.write_bytes(content)
    target.chmod(0o755)
    result = subprocess.run(
        ["bash", str(REPO / "bin" / "sable-bin-install"), "--dir", str(bin_dir)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert target.is_file() and not target.is_symlink()
    assert (bin_dir / ".sable-pinned").is_file()


def _revert_pin_to_symlink(bin_dir: Path, target_name: str):
    """Simulate DEFECT 2 directly: something reverted a pinned bin back to a
    symlink without updating the marker (a stale pre-fix install.sh, by
    construction, since the FIXED sable-bin-install refuses to do this)."""
    target = bin_dir / target_name
    target.unlink()
    target.symlink_to(REPO / "bin" / target_name)


def test_guarded_pinned_bin_sessionstart_hook_output_has_no_install_sh_instruction(installed_claude_dir):
    bin_dir = installed_claude_dir.parent / ".local" / "bin"
    target_name = "sable-merge-gate"
    _establish_real_pin(bin_dir, target_name)
    _revert_pin_to_symlink(bin_dir, target_name)

    # The REAL SessionStart hook invocation: `sable-doctor --quiet 2>&1 || true`.
    result = subprocess.run(
        [sys.executable, str(DOCTOR), "--repo", str(REPO), "--claude-dir", str(installed_claude_dir),
         "--bin-dir", str(bin_dir), "--quiet"],
        capture_output=True, text=True, timeout=30,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "bash install.sh" not in combined
    # quiet mode is deliberately a one-liner (no filenames) — same contract
    # the ordinary drift path already has; the full report (next test) is
    # where the safe per-file path gets named.
    assert "pinned bin" in combined


def test_guarded_pinned_bin_full_report_names_the_safe_cp_path_not_install_sh(installed_claude_dir):
    bin_dir = installed_claude_dir.parent / ".local" / "bin"
    target_name = "sable-merge-gate"
    _establish_real_pin(bin_dir, target_name)
    _revert_pin_to_symlink(bin_dir, target_name)

    result = run_doctor(installed_claude_dir, bin_dir=bin_dir)
    assert result.returncode == 1
    assert "bash install.sh" not in result.stdout
    assert "UNPINNED" in result.stdout
    assert f"cp {REPO / 'bin' / target_name}" in result.stdout


def test_unguarded_drift_sessionstart_hook_still_names_install_sh(installed_claude_dir):
    # Over-suppression check: a drifted UNGUARDED file (no pinning involved
    # at all) must still get the ordinary remedy — silencing it globally
    # would hide real drift, which is its own regression.
    (installed_claude_dir / "hooks" / "tdd-gate.sh").write_text("tampered\n")
    bin_dir = installed_claude_dir.parent / ".local" / "bin"

    result = subprocess.run(
        [sys.executable, str(DOCTOR), "--repo", str(REPO), "--claude-dir", str(installed_claude_dir),
         "--bin-dir", str(bin_dir), "--quiet"],
        capture_output=True, text=True, timeout=30,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 1
    assert "bash install.sh" in combined


def test_pinned_bin_survives_real_reinstall_after_the_fix(installed_claude_dir):
    # The other half of the acceptance criterion, exercised through
    # sable-doctor: once genuinely pinned, a real re-run of THIS (fixed)
    # install.sh must leave the pin clean, not flagged.
    home = installed_claude_dir.parent
    bin_dir = home / ".local" / "bin"
    target_name = "sable-merge-gate"
    _establish_real_pin(bin_dir, target_name)

    run_install(home)

    result = run_doctor(installed_claude_dir, bin_dir=bin_dir)
    assert result.returncode == 0, result.stdout + result.stderr
    target = bin_dir / target_name
    assert target.is_file() and not target.is_symlink()


# --- install provenance, end-to-end (SABLE-78kxu) -----------------------------
#
# Reproduces today's incident as a regression test: install.sh runs against a
# sandbox HOME (never the real machine's ~/.claude — SABLE-mkj6k), then the
# fixture REPO gets a new commit the installed set has never seen. The
# manifest compare still reports the installed files clean (the new commit
# doesn't touch any installed file), but the provenance stamp now visibly
# PREDATES that new commit — an unresolvable "is X deployed?" is answered
# with one git merge-base check instead of ad-hoc grepping.
#
# Uses a throwaway `git init` seeded with a COPY of the real working tree
# (not a clone of its history) — this needs a repo it can freely commit a new
# file into without touching the real SABLE repo or its git history.

PROVENANCE_STAMP_NAME = ".sable-install-provenance"


def make_fixture_repo(dest: Path):
    shutil.copytree(
        REPO, dest,
        ignore=shutil.ignore_patterns(".git", ".beads", ".pytest_cache", "__pycache__"),
    )
    subprocess.run(["git", "init", "-q"], cwd=dest, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "snapshot"],
        cwd=dest, check=True,
    )
    return dest


def run_install_from(repo_dir: Path, home_dir: Path):
    result = subprocess.run(
        ["bash", str(repo_dir / "install.sh")],
        env={**os.environ, "HOME": str(home_dir)},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, f"install.sh failed:\n{result.stdout}\n{result.stderr}"


def git_head(repo_dir: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture()
def provenance_fixture(tmp_path):
    fixture_repo = make_fixture_repo(tmp_path / "fixture-repo")
    home = tmp_path / "home"
    home.mkdir()
    run_install_from(fixture_repo, home)
    return fixture_repo, home / ".claude"


def test_install_writes_provenance_stamp_with_the_actual_head_sha(provenance_fixture):
    fixture_repo, claude_dir = provenance_fixture
    expected_sha = git_head(fixture_repo)
    stamp = claude_dir / PROVENANCE_STAMP_NAME
    assert stamp.is_file()
    content = stamp.read_text()
    assert f"commit={expected_sha}" in content
    assert "branch=" in content
    assert "dirty=false" in content
    assert "timestamp=" in content


def test_provenance_reproduces_the_incident_clean_report_with_a_provable_predate(provenance_fixture):
    fixture_repo, claude_dir = provenance_fixture
    installed_sha = git_head(fixture_repo)

    (fixture_repo / "NEW_GUARD_FILE.md").write_text("a file the installed set has never seen\n")
    subprocess.run(["git", "add", "NEW_GUARD_FILE.md"], cwd=fixture_repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t.com", "-c", "user.name=t", "commit", "-q", "-m", "add new file"],
        cwd=fixture_repo, check=True,
    )
    new_head = git_head(fixture_repo)
    assert new_head != installed_sha

    result = subprocess.run(
        [sys.executable, str(DOCTOR), "--repo", str(fixture_repo), "--claude-dir", str(claude_dir),
         "--bin-dir", str(claude_dir.parent / ".local" / "bin")],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sable-doctor: clean" in result.stdout      # installed files: unaffected by the new commit
    assert installed_sha in result.stdout               # the SHA the install actually came from

    ancestor_check = subprocess.run(
        ["git", "-C", str(fixture_repo), "merge-base", "--is-ancestor", installed_sha, new_head],
    )
    assert ancestor_check.returncode == 0  # installed sha genuinely predates the new commit


def test_installed_from_flag_prints_the_bare_sha(provenance_fixture):
    fixture_repo, claude_dir = provenance_fixture
    installed_sha = git_head(fixture_repo)
    result = subprocess.run(
        [sys.executable, str(DOCTOR), "--claude-dir", str(claude_dir), "--installed-from"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == installed_sha


def test_installed_from_flag_fails_clearly_without_a_stamp(installed_claude_dir):
    # models a pre-existing install from before this bead: no stamp at all.
    (installed_claude_dir / PROVENANCE_STAMP_NAME).unlink(missing_ok=True)
    result = subprocess.run(
        [sys.executable, str(DOCTOR), "--claude-dir", str(installed_claude_dir), "--installed-from"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "no provenance stamp" in result.stderr


def test_install_from_a_dirty_tree_stamps_dirty_true(tmp_path):
    fixture_repo = make_fixture_repo(tmp_path / "fixture-repo")
    (fixture_repo / "UNCOMMITTED_CHANGE.md").write_text("dirties the tree post-commit\n")
    home = tmp_path / "home"
    home.mkdir()
    run_install_from(fixture_repo, home)
    stamp = (home / ".claude" / PROVENANCE_STAMP_NAME).read_text()
    assert "dirty=true" in stamp


def test_quiet_mode_sessionstart_hook_stays_silent_on_a_fresh_provenance_stamped_install(provenance_fixture):
    # SABLE-78kxu must not make the highest-traffic path (`sable-doctor
    # --quiet`, the SessionStart hook) start speaking on every healthy run
    # just because provenance now exists.
    fixture_repo, claude_dir = provenance_fixture
    result = subprocess.run(
        [sys.executable, str(DOCTOR), "--repo", str(fixture_repo), "--claude-dir", str(claude_dir),
         "--bin-dir", str(claude_dir.parent / ".local" / "bin"), "--quiet"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
