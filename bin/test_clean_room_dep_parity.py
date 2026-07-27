#!/usr/bin/env python3
"""Guards for the shared, pinned clean-room test environment.

ci-verify and green-snapshot run the same ALLOW suite set. They once carried
two drifting inline dependency lists; both now consume one exact requirements
file. The static checks keep that consolidation and the tier-alias premise
true. The integration checks exercise the real coverage-floor precondition.
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
TEST_REQUIREMENTS = REPO_ROOT / ".github" / "ci" / "test-requirements.txt"

# Matches a clean-room `pip install <arguments>` line, excluding the
# `pip install --upgrade pip` bootstrap line above it (that installs pip
# itself, not a test dependency, and both workflows carry it verbatim).
_PIP_INSTALL_RE = re.compile(r"^[ \t]*pip install (?!--upgrade\b)(.+?)[ \t]*$", re.MULTILINE)

_TIER_ALIAS_RE = re.compile(
    r'^SABLE_TIER_(MERGE_PREVIEW|FULL_SNAPSHOT)=\(\s*"\$\{ALLOW\[@\]\}"\s*\)',
    re.MULTILINE,
)


# --- pure helpers (this is what's under test) --------------------------------

def clean_room_pip_arguments(workflow_text: str) -> tuple[str, ...]:
    """Arguments on the first non-bootstrap clean-room pip install."""
    matches = _PIP_INSTALL_RE.findall(workflow_text)
    if not matches:
        return ()
    return tuple(matches[0].split())


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


def test_both_clean_rooms_install_the_one_shared_requirement_file():
    expected = ("-r", ".github/ci/test-requirements.txt")
    assert clean_room_pip_arguments(
        CI_VERIFY_YML.read_text(encoding="utf-8")
    ) == expected
    assert clean_room_pip_arguments(
        GREEN_SNAPSHOT_YML.read_text(encoding="utf-8")
    ) == expected


def test_shared_clean_room_requirement_file_pins_every_dependency():
    lines = [
        line.strip()
        for line in TEST_REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert lines
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+==[^=\s]+", line) for line in lines)


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
