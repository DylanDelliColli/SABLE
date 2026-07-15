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
    result = subprocess.run(
        ["bash", str(INSTALLER)],
        env={**os.environ, "HOME": str(home_dir)},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, f"install.sh failed:\n{result.stdout}\n{result.stderr}"


def run_doctor(claude_dir: Path, *extra_args):
    return subprocess.run(
        [sys.executable, str(DOCTOR), "--repo", str(REPO), "--claude-dir", str(claude_dir), *extra_args],
        capture_output=True, text=True, timeout=30,
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


def run_doctor_project(cwd: Path, *extra_args):
    return subprocess.run(
        [sys.executable, str(DOCTOR), "--repo", str(REPO), "--project", *extra_args],
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
