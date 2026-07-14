#!/usr/bin/env python3
"""Integration tests for bin/sable-fixture-tripwire (SABLE-0ssz.2).

Real composition: invoke the actual script as a subprocess (the way CI does) and
assert acceptance (b) — it FAILS on a planted violation and PASSES on the
audited-clean suite (both a synthetic clean fixture and the real repo tree).
"""
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent / "sable-fixture-tripwire"
REPO = Path(__file__).resolve().parent.parent


def run(*paths):
    return subprocess.run(
        [sys.executable, str(BIN), *[str(p) for p in paths]],
        capture_output=True, text=True,
    )


def test_passes_on_real_repo():
    # acceptance (b): passes on the audited-clean suite (the real fixtures)
    r = run()  # no args -> scans the repo's own fixtures
    assert r.returncode == 0, f"tripwire unexpectedly failed on the real repo:\n{r.stdout}\n{r.stderr}"
    assert "CLEAN" in r.stdout


def test_fails_on_planted_unguarded_cd(tmp_path):
    bad = tmp_path / "test-planted.sh"
    bad.write_text(
        "#!/usr/bin/env bash\n"
        'FIX=$(mktemp -d)\n'
        'cd "$FIX"\n'                       # the planted violation: bare, unguarded
        'git config user.name "Evil"\n'
    )
    r = run(bad)
    assert r.returncode == 1, f"tripwire did not fail on a planted unguarded cd:\n{r.stdout}"
    assert "cd-unguarded" in r.stdout
    assert "test-planted.sh" in r.stdout


def test_fails_on_planted_real_repo_git(tmp_path):
    bad = tmp_path / "test_planted.py"
    bad.write_text(
        "from pathlib import Path\n"
        "import subprocess\n"
        "repo = Path(__file__).resolve().parent.parent\n"
        'subprocess.run(["git", "-C", str(repo), "push", "origin", "main"])\n'
    )
    r = run(bad)
    assert r.returncode == 1, f"tripwire did not fail on a planted real-repo git op:\n{r.stdout}"
    assert "real-repo-git" in r.stdout


def test_passes_on_planted_clean_fixture(tmp_path):
    good = tmp_path / "test-clean.sh"
    good.write_text(
        "#!/usr/bin/env bash\n"
        'FIX=$(mktemp -d)\n'
        'cd "$FIX" || exit 1\n'             # guarded — the z776 pattern
        'git -C "$FIX" config user.name "Test"\n'
        'cd - >/dev/null\n'
    )
    r = run(good)
    assert r.returncode == 0, f"tripwire wrongly failed on a clean fixture:\n{r.stdout}"


def test_allow_marker_excuses_planted(tmp_path):
    marked = tmp_path / "test-marked.sh"
    marked.write_text(
        "#!/usr/bin/env bash\n"
        'cd "$FIX"   # tripwire-allow: deliberate escape reproduction\n'
    )
    r = run(marked)
    assert r.returncode == 0, f"inline tripwire-allow marker was not honored:\n{r.stdout}"


def test_manifest_lists_tracked_exceptions():
    r = subprocess.run(
        [sys.executable, str(BIN), "--manifest"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0
    assert "excused (tracked)" in r.stdout
    # the two 0ssz tracked debts are named
    assert "0ssz.3" in r.stdout or "test_sable_dolt_push_integration.py" in r.stdout
    assert "test_sable_spawn_worker_integration.py" in r.stdout
