#!/usr/bin/env python3
"""End-to-end integration tests for the /sable-onboarding flow (SABLE-gn7a.5).

This is the COMPOSITION layer — the suite that proves the epic's success
metric: an arbitrary, zero-SABLE repo can be scanned, gate-applied, and driven
to a green `sable-doctor --project` with a working beads loop, and that the
scanner never writes a byte it isn't supposed to.

Technique (mirrors bin/test_sable_doctor_integration.py's run_doctor pattern):
the REAL binaries — bin/sable-onboard, install.sh, sable-doctor, and bd — are
run as subprocesses against tmp git repos. No synthetic fixtures for the tools,
no mocked filesystem. Every zero-writes / byte-identical claim is backed by a
recursive sha256 snapshot() over BOTH a throwaway sandboxed HOME and the fixture
repo. Each case gets its OWN sandbox HOME (a real dir — dolt/bd stat it) so
install.sh's double-fire guard never trips and nothing leaks between cases.

The fixture is a tiny REAL pytest project with zero SABLE presence, built by
make_fixture_project(); the unit layers for each moving part live in the sibling
suites (test_sable_onboard.py, test_sable_doctor*.py). install.sh is run with
--from-here because THIS checkout is a linked SABLE worker worktree (the
canonical-checkout refusal, SABLE-s6qk); the target is always the throwaway
project, so scoping --from-here here is safe.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"
ONBOARD = BIN / "sable-onboard"
DOCTOR = BIN / "sable-doctor"
REGISTRY_LIB = BIN / "sable_registry_lib.py"
INSTALLER = REPO / "install.sh"

HAVE_BD = shutil.which("bd") is not None
HAVE_GIT = shutil.which("git") is not None
pytestmark = pytest.mark.skipif(
    not (HAVE_BD and HAVE_GIT),
    reason="the onboarding E2E shells the real install.sh/bd/git; not present in this clean-room",
)

# HOME's ONLY sanctioned global footprint is ~/.local/bin (the CLI symlinks —
# the hybrid contract, SABLE-59t6). Everything else onboarding does is
# project-scoped, so the HOME snapshot excludes ~/.local/bin by design.
HOME_EXCLUDE = (".local/bin",)

# SABLE runtime env that, if inherited from a fleet session, would make the
# subprocess behave unlike a clean external adopter — chiefly the registry
# override, which would flip the project-first resolver's scope verdict.
_SABLE_ENV_LEAKS = ("SABLE_AGENTS_YAML", "SABLE_REGISTRY", "SABLE_DISPATCH_DIR")


# ---------------------------------------------------------------------------
# env + subprocess runners — every one pins HOME at the case's sandbox
# ---------------------------------------------------------------------------

def _env(home):
    env = {k: v for k, v in os.environ.items() if k not in _SABLE_ENV_LEAKS}
    env["HOME"] = str(home)
    return env


def run_onboard(project, home, *args):
    return subprocess.run(
        [sys.executable, str(ONBOARD), "--repo", str(project), *args],
        env=_env(home), cwd=str(project),
        capture_output=True, text=True, timeout=90,
    )


def run_install_project(project, home):
    result = subprocess.run(
        ["bash", str(INSTALLER), "--from-here", "--project"],
        env=_env(home), cwd=str(project),
        capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, f"install.sh --project failed:\n{result.stdout}\n{result.stderr}"
    return result


def run_doctor_project(project, home):
    # --repo defaults to the doctor's OWN repo (this checkout) — exactly what
    # install.sh --from-here copied from, so a fresh project install is clean.
    return subprocess.run(
        [sys.executable, str(DOCTOR), "--project"],
        env=_env(home), cwd=str(project),
        capture_output=True, text=True, timeout=90,
    )


def run_bd(home, cwd, *args):
    return subprocess.run(
        ["bd", *args],
        env=_env(home), cwd=str(cwd),
        capture_output=True, text=True, timeout=90,
    )


def registry_scope(project, home):
    """The project-first resolver's scope verdict (override|project|global) for
    the fixture — the SABLE-59t6.1 CLI a bash caller would reach."""
    r = subprocess.run(
        [sys.executable, str(REGISTRY_LIB), "scope"],
        env=_env(home), cwd=str(project),
        capture_output=True, text=True, timeout=30,
    )
    return r.stdout.strip()


# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------

def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_fixture_project(tmp_path):
    """A tiny REAL pytest project with ZERO SABLE presence, in its own git repo
    on a non-default working branch with an initial commit — the 'unknown repo'
    the scanner and the whole apply flow are pointed at."""
    proj = tmp_path / "fixture"
    proj.mkdir()
    _git(proj, "init", "-q", "-b", "work")
    (proj / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n", encoding="utf-8")
    src = proj / "src"
    src.mkdir()
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    tests = proj / "tests"
    tests.mkdir()
    (tests / "test_calc.py").write_text(
        "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8")
    (proj / "README.md").write_text("# fixture\n\nA tiny pytest project.\n", encoding="utf-8")
    _git(proj, "add", "-A")
    _git(proj, "-c", "user.email=t@t.com", "-c", "user.name=t",
         "commit", "-q", "-m", "init fixture")
    return proj


def sandbox_home(tmp_path):
    """A throwaway HOME for one case: a real dir (dolt/bd stat it on init) with
    zero prior SABLE state, so install.sh's double-fire guard never trips."""
    home = tmp_path / "home"
    home.mkdir()
    return home


# ---------------------------------------------------------------------------
# snapshot — the recursive-hash helper backing every zero-writes assertion
# ---------------------------------------------------------------------------

def snapshot(root, *, exclude=()):
    """sha256 of every file under root except .git internals and any file under
    an excluded relative subtree (e.g. ".local/bin"). The shared backbone of
    every zero-writes / byte-identical claim over both HOME and the repo."""
    root = Path(root)
    excluded_parts = tuple(Path(e).parts for e in exclude)
    snap = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file() and not p.is_symlink():
            continue
        rel = p.relative_to(root)
        parts = rel.parts
        if ".git" in parts:
            continue
        if any(parts[:len(e)] == e for e in excluded_parts):
            continue
        # symlinks (the ~/.local/bin tools) are already excluded above; hash by
        # bytes for regular files.
        snap[str(rel)] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snap


# ---------------------------------------------------------------------------
# apply helpers — the named delegations the skill drives, one per gap
# ---------------------------------------------------------------------------

def apply_bd_init(project, home):
    r = run_bd(home, project, "init")
    assert r.returncode == 0, r.stdout + r.stderr


def apply_sable_contract(project):
    # The .sable artifact the skill authors via sable_stack_detect.write() after
    # an execute-once pass; here we write the finished contract directly (the
    # writer + execute-once gate are unit-tested in the sibling suite). Shape is
    # what the sable-contract check validates.
    (project / ".sable").write_text(
        "testCommand=pytest -q\nintegrationBranch=work\n", encoding="utf-8")


def exit_report(doctor_result, beads, *, branch):
    """The skill's Exit report (SKILL.md §Exit report) reduced to its checkable
    facts: doctor-green completion, whether the proof run happened (derived from
    the observed workspace bead count, never asserted into existence), and the
    confirmed branch of record."""
    return {
        "doctor_green": doctor_result.returncode == 0,
        "proof": "ran" if len(beads) > 0 else "skipped",
        "branch_of_record": branch,
    }


# ===========================================================================
# S1 — the scan is byte-for-byte read-only, HOME and repo alike
# ===========================================================================

def test_scan_performs_zero_writes_home_and_repo_snapshot(tmp_path):
    """S1 E2E: a scan of a zero-SABLE repo mutates NOTHING — the real binary run
    as a subprocess, with before/after sha256 snapshots over both the target
    repo AND the sandboxed HOME."""
    project = make_fixture_project(tmp_path)
    home = sandbox_home(tmp_path)

    repo_before = snapshot(project)
    home_before = snapshot(home, exclude=HOME_EXCLUDE)
    assert home_before == {}, "sandbox HOME should start pristine"

    text = run_onboard(project, home)
    assert "sable-onboard: scan of" in text.stdout
    assert text.returncode == 1          # a pristine repo has real gaps to report
    run_onboard(project, home, "--json")  # the json renderer must also write nothing

    assert snapshot(project) == repo_before, "scan mutated the target repo (S1)"
    assert snapshot(home, exclude=HOME_EXCLUDE) == home_before, \
        "scan mutated the sandboxed HOME (S1)"


# ===========================================================================
# S2 — apply flips a check green; a declined step writes nothing
# ===========================================================================

def test_apply_then_recheck_flips_green(tmp_path):
    """S2: an apply (install.sh --project) flips a red check green. install-scope
    is a GAP before the install and OK after, addressed via --check exactly as
    the skill re-verifies each applied step."""
    project = make_fixture_project(tmp_path)
    home = sandbox_home(tmp_path)

    before = run_onboard(project, home, "--check", "install-scope")
    assert before.returncode == 1
    assert "GAP" in before.stdout

    run_install_project(project, home)

    after = run_onboard(project, home, "--check", "install-scope")
    assert after.returncode == 0, after.stdout + after.stderr
    assert "OK" in after.stdout
    assert "install matches repo HEAD" in after.stdout


def test_declined_step_leaves_repo_byte_identical(tmp_path):
    """S2: a DECLINED step writes nothing. install + bd init are applied, then
    the .sable step is declined; a re-scan leaves the repo byte-identical and the
    report names that step's manual remedy verbatim."""
    project = make_fixture_project(tmp_path)
    home = sandbox_home(tmp_path)

    run_install_project(project, home)
    apply_bd_init(project, home)
    # .sable deliberately NOT written — the operator declined this step.

    baseline = snapshot(project)
    res = run_onboard(project, home)
    assert snapshot(project) == baseline, "a declined step still mutated the repo"

    assert res.returncode == 1                        # the declined step keeps a gap
    assert "sable-contract" in res.stdout
    assert "Establish a valid .sable" in res.stdout   # the manual remedy, named


# ===========================================================================
# S5 — the full fixture flow: scan → applies → doctor-green + a real bead
# ===========================================================================

def test_e2e_zero_sable_repo_scan_to_doctor_green(tmp_path):
    """S5: the epic's success metric — a zero-SABLE repo, scanned, then the
    install applied, drives sable-doctor --project to a clean exit 0, and the
    scanner's own install-scope check (same delegate) agrees."""
    project = make_fixture_project(tmp_path)
    home = sandbox_home(tmp_path)

    scan = run_onboard(project, home, "--check", "install-scope")
    assert scan.returncode == 1                       # pristine repo: install-scope GAP

    run_install_project(project, home)

    doctor = run_doctor_project(project, home)
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    assert "clean" in doctor.stdout

    rescan = run_onboard(project, home, "--check", "install-scope")
    assert rescan.returncode == 0, rescan.stdout + rescan.stderr


def test_e2e_quick_plan_produces_well_formed_bead(tmp_path):
    """S5: after onboarding stands up the project install + beads workspace, the
    quick-tier proof run (create ONE sample bead) yields exactly one well-formed
    bead. HARD DEP on SABLE-59t6.1 (project-first registry resolver — governance
    resolves the REPO's own registry, not a fail-open global) and SABLE-59t6.3
    (install --project). Bead shape validated via bd show --json."""
    project = make_fixture_project(tmp_path)
    home = sandbox_home(tmp_path)

    run_install_project(project, home)                 # installs the project registry
    apply_bd_init(project, home)

    # SABLE-59t6.1: project-only governance resolves the repo's own registry.
    assert (project / ".claude" / "sable" / "agents.yaml").is_file()
    assert registry_scope(project, home) == "project"

    created = run_bd(home, project, "create",
                     "--title=Proof: SABLE executes end to end",
                     "--type=task", "--priority=2")
    assert created.returncode == 0, created.stdout + created.stderr

    listing = json.loads(run_bd(home, project, "list", "--json").stdout)
    assert len(listing) == 1, f"expected exactly one bead, got {listing}"
    bead_id = listing[0]["id"]

    shown = json.loads(run_bd(home, project, "show", bead_id, "--json").stdout)
    assert isinstance(shown, list) and len(shown) == 1
    bead = shown[0]
    assert bead["id"] == bead_id
    assert bead["title"] == "Proof: SABLE executes end to end"
    assert bead["issue_type"] == "task"
    assert bead["status"] == "open"
    assert bead["priority"] == 2


def test_e2e_proof_run_skip_path_variant(tmp_path):
    """S5: declining the (offered-default-yes) proof run still COMPLETES
    onboarding. install + bd init + .sable applied, proof step declined:
    sable-doctor --project stays green, the full scan is all-green, the workspace
    holds zero beads (nothing silently created), and the exit report — reduced
    from that observed state — is marked proof-skipped."""
    project = make_fixture_project(tmp_path)
    home = sandbox_home(tmp_path)

    run_install_project(project, home)
    apply_bd_init(project, home)
    apply_sable_contract(project)
    # step 7 proof run DECLINED — no sample bead is created.

    doctor = run_doctor_project(project, home)
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr

    scan = run_onboard(project, home)
    assert scan.returncode == 0, scan.stdout + scan.stderr   # every required check green
    assert "all green" in scan.stdout

    beads = json.loads(run_bd(home, project, "list", "--json").stdout)
    assert beads == [], "the proof was declined — no bead should exist"

    report = exit_report(doctor, beads, branch="work")
    assert report == {
        "doctor_green": True,
        "proof": "skipped",
        "branch_of_record": "work",
    }


def test_e2e_global_env_snapshot_byte_identical(tmp_path):
    """S5: onboarding's ONLY global-env footprint is ~/.local/bin (the CLI
    symlinks — hybrid contract). Across the whole scanner flow — first scan, the
    install apply, doctor, and the exit-report scan — the sandboxed HOME is
    byte-identical once ~/.local/bin is excluded; the baseline is genuinely
    empty, so nothing SABLE-scoped leaks into the global home.

    The flow stops at doctor (no bd init): the beads step writes bd/dolt's OWN
    ~/.dolt global config, which is that tool being itself, outside SABLE's
    hybrid contract — its footprint is exercised in the bead-loop cases above."""
    project = make_fixture_project(tmp_path)
    home = sandbox_home(tmp_path)

    def home_snap():
        return snapshot(home, exclude=HOME_EXCLUDE)

    snaps = [home_snap()]                                   # pristine baseline
    run_onboard(project, home, "--check", "install-scope")  # first scan
    snaps.append(home_snap())
    run_install_project(project, home)                      # the apply
    snaps.append(home_snap())
    run_doctor_project(project, home)                       # doctor
    snaps.append(home_snap())
    run_onboard(project, home)                              # exit-report scan
    snaps.append(home_snap())

    assert snaps[0] == {}, "only ~/.local/bin should ever land in HOME"
    for i, s in enumerate(snaps):
        assert s == snaps[0], f"HOME diverged at checkpoint {i} (outside ~/.local/bin)"
