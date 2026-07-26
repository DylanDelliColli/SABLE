#!/usr/bin/env python3
"""test_clean_room_dep_parity.py — mechanical guard against SABLE-kh4sx's
recurrence (SABLE-kh4sx).

THE DEFECT THIS EXISTS FOR
---------------------------
.github/workflows/ci-verify.yml and .github/workflows/green-snapshot.yml each
carry their own hand-authored clean-room `pip install ...` line, but
.github/ci/test-tiers.sh declares their tiers (merge_preview, full_snapshot)
as the exact SAME suite set -- both alias .github/ci/shell-run-set.sh's ALLOW
array BY REFERENCE, not a copy. Two sites that must carry identical
dependencies, kept in sync by hand, with nothing checking that they do.
SABLE-cmar4.5/4.6/4.8 fixed ci-verify.yml's line three times (most recently
adding diff_cover); green-snapshot.yml's matching line was never touched and
silently drifted until SABLE-kh4sx caught it by hand, verified it was still
dormant (green-snapshot.yml is absent from origin/main, so its `on: schedule`
trigger cannot yet fire), and fixed it before it did.

DERIVATION, NOT A SECOND HARDCODED LIST
----------------------------------------
This test does not enumerate what packages test-coverage-floor-gate.sh (or any
other suite in the tier) needs -- that would just be a THIRD hand-maintained
list, reintroducing the exact bug one layer up. Instead it reads the
requirement FROM ci-verify.yml's own install line, which the cmar4 fixes
already made correct for precisely this suite set, and separately confirms
(test_merge_preview_and_full_snapshot_alias_the_same_suite_set) that
test-tiers.sh really does declare merge_preview and full_snapshot as the same
ALLOW-aliased suites -- the fact that licenses treating ci-verify.yml's set as
the requirement for green-snapshot.yml's clean room. If that tier-aliasing
ever changes, this derivation stops being valid and that test goes red first,
on purpose: the fix then is to change the derivation, not to widen the assert.

Clean-room safe (SABLE-59zu): the two static parity tests read only tracked
workflow/script text, no subprocess. The two coverage-floor-gate integration
tests below shell out to a REAL hooks/test/test-coverage-floor-gate.sh run and
self-skip (not silently pass) when diff-cover is not on this machine's PATH,
since that PATH state is exactly the precondition they exist to reproduce.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_VERIFY_YML = REPO_ROOT / ".github" / "workflows" / "ci-verify.yml"
GREEN_SNAPSHOT_YML = REPO_ROOT / ".github" / "workflows" / "green-snapshot.yml"
TEST_TIERS_SH = REPO_ROOT / ".github" / "ci" / "test-tiers.sh"
COVERAGE_FLOOR_GATE_SH = REPO_ROOT / "hooks" / "test" / "test-coverage-floor-gate.sh"

# Matches a clean-room `pip install <packages>` line, excluding the
# `pip install --upgrade pip` bootstrap line above it (that installs pip
# itself, not a test dependency, and both workflows carry it verbatim).
_PIP_INSTALL_RE = re.compile(r"^[ \t]*pip install (?!--upgrade\b)(.+?)[ \t]*$", re.MULTILINE)

_TIER_ALIAS_RE = re.compile(
    r'^SABLE_TIER_(MERGE_PREVIEW|FULL_SNAPSHOT)=\(\s*"\$\{ALLOW\[@\]\}"\s*\)',
    re.MULTILINE,
)


# --- pure helpers (this is what's under test) --------------------------------

def clean_room_pip_packages(workflow_text: str) -> set[str]:
    """Package names on a ci-verify/green-snapshot-shaped workflow's
    clean-room `pip install ...` line. Exactly one such line is expected in
    each of these two files today; if more ever appear, take the first rather
    than silently unioning them, so a second install step doesn't quietly
    launder a missing dependency into "present"."""
    matches = _PIP_INSTALL_RE.findall(workflow_text)
    if not matches:
        return set()
    return set(matches[0].split())


def merge_preview_and_full_snapshot_share_suites(test_tiers_text: str) -> bool:
    """True when SABLE_TIER_MERGE_PREVIEW and SABLE_TIER_FULL_SNAPSHOT are both
    declared as the literal `("${ALLOW[@]}")` alias in test-tiers.sh -- the
    fact this whole test file's derivation depends on."""
    hits = {m.group(1) for m in _TIER_ALIAS_RE.finditer(test_tiers_text)}
    return hits == {"MERGE_PREVIEW", "FULL_SNAPSHOT"}


def _scrub_path_of(executable_name: str) -> str:
    """PATH with every entry that provides an executable `executable_name`
    removed. Removal, not front-shadowing, mirrors bin/sable-clean-room-
    verify's scrub_path: a stub placed earlier on PATH still leaves the real
    binary reachable by anything that resolves PATH differently, so the
    directory has to actually go missing to reproduce a runner where the tool
    was never installed."""
    kept = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry) / executable_name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            continue
        kept.append(entry)
    return os.pathsep.join(kept)


# --- static parity checks -----------------------------------------------------

def test_merge_preview_and_full_snapshot_alias_the_same_suite_set():
    text = TEST_TIERS_SH.read_text(encoding="utf-8")
    assert merge_preview_and_full_snapshot_share_suites(text), (
        "test-tiers.sh no longer declares merge_preview and full_snapshot as "
        "the same ALLOW-aliased suite set. If that's an intentional split, "
        "ci-verify.yml's clean-room install line is no longer a valid stand-in "
        "for what green-snapshot.yml's full_snapshot tier requires -- fix this "
        "test's derivation (don't just widen the assert below)."
    )


def test_derived_requirement_does_not_demand_an_unrelated_package():
    """Negative control: the requirement comes from ci-verify.yml's ACTUAL
    install line, not from "every package that could ever be named" -- a
    package ci-verify.yml never installs must not show up as required, or the
    superset check below would be satisfiable by any workflow installing
    enough unrelated packages, proving nothing about real coverage."""
    required = clean_room_pip_packages(CI_VERIFY_YML.read_text(encoding="utf-8"))
    assert required, "sanity: ci-verify.yml's clean-room install line must parse to something"
    assert "numpy" not in required
    assert "requests" not in required


def test_green_snapshot_clean_room_installs_every_ci_verify_dependency():
    """THE CHECK (SABLE-kh4sx). ci-verify.yml and green-snapshot.yml run the
    same suite set (confirmed above), so whatever ci-verify.yml's already-
    fixed clean room installs is exactly what green-snapshot.yml's clean room
    needs too. Before SABLE-kh4sx's fix this failed: ci-verify.yml carries
    diff_cover (SABLE-cmar4.6 third revise) and green-snapshot.yml did not."""
    required = clean_room_pip_packages(CI_VERIFY_YML.read_text(encoding="utf-8"))
    actual = clean_room_pip_packages(GREEN_SNAPSHOT_YML.read_text(encoding="utf-8"))
    missing = required - actual
    assert not missing, (
        f"green-snapshot.yml's clean-room install line is missing {sorted(missing)}, "
        "which ci-verify.yml installs for the SAME suite set (test-tiers.sh's "
        "merge_preview/full_snapshot ALLOW alias). Mirror ci-verify.yml's "
        "clean-room pip install line."
    )


# --- integration: the real guard, under a real scrubbed/restored PATH --------

def test_coverage_floor_gate_fatals_without_diff_cover_on_path():
    if shutil.which("diff-cover") is None:
        pytest.skip("diff-cover not on this machine's PATH -- nothing to scrub")
    env = dict(os.environ, PATH=_scrub_path_of("diff-cover"))
    result = subprocess.run(
        ["bash", str(COVERAGE_FLOOR_GATE_SH)],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "FATAL: diff-cover not on PATH" in result.stdout


def test_coverage_floor_gate_runs_for_real_with_diff_cover_on_path():
    if shutil.which("diff-cover") is None:
        pytest.skip("diff-cover not on this machine's PATH -- cannot verify the positive case")
    result = subprocess.run(
        ["bash", str(COVERAGE_FLOOR_GATE_SH)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FATAL" not in result.stdout
