#!/usr/bin/env python3
"""Integration coverage for SABLE-sx1rb — the impact tier's scratch bd DB
does not collide with a suite's own isolated bd DB, through the REAL
run_impact_tier entry point, a real git worktree, and a real `bd`.

The reported defect: _impact_isolated_env (bin/sable_gate_promote_lib.py)
builds and exports a per-run BEADS_DB pointing at the tier's OWN scratch bd
DB before running any selected suite. hooks/test/test-overlap-dispatch-
e2e.sh:101 (and ~11 sibling call sites) ran their OWN `bd init` without
unsetting that ambient var, so `bd` followed BEADS_DB instead of CWD and
re-initialized the tier's already-initialized DB — "This workspace is
already initialized" — which the tier then read as a suite failure (rc=2)
and reported as IMPACT_RED, naming whichever branch's promote happened to
be the first to enter the tier. Measured twice in the wild on two unrelated
branches (SABLE-r0mzn, SABLE-6eu9w) sharing no files.

This suite's fixture script reproduces the fixed pattern real suites now
carry — `env -u BEADS_DB ... bd init` at its OWN scratch dir — and drives
it through the real tier TWICE in succession, because the reported
mechanism was a double-init WITHIN one run (the tier's own init, then the
suite's), not cross-run leftover contamination (ruled out separately by
three independent self-collision observations logged on the bead). Running
twice in the same test proves a correctly-guarded suite's isolation holds
run over run, not just once.
"""
import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "bin"))
import sable_gate_promote_lib as promote_lib  # noqa: E402

HAVE_BD = shutil.which("bd") is not None


@pytest.fixture()
def isolated_lock(tmp_path, monkeypatch):
    """Point the lock + window log at this test's own tmp dir, so the suite
    never contends with (or corrupts) a real merge seat's state dir. Mirrors
    bin/test_sable_gate_promote_lib.py's fixture of the same name/purpose."""
    monkeypatch.setenv("SABLE_MG_IMPACT_LOCK", str(tmp_path / "impact-tier.lock"))
    monkeypatch.setenv("SABLE_MG_IMPACT_WINDOW_LOG", str(tmp_path / "windows.jsonl"))
    monkeypatch.delenv("SABLE_MG_IMPACT_SERIALIZE", raising=False)
    monkeypatch.delenv("SABLE_MG_IMPACT_LOCK_TIMEOUT", raising=False)
    monkeypatch.delenv("SABLE_MG_IMPACT_TIMEOUT", raising=False)
    monkeypatch.delenv("SABLE_MG_IMPACT", raising=False)
    return tmp_path


# The fixed pattern real hooks/test/*.sh suites now carry (SABLE-sx1rb Part
# A): -u BEADS_DB so the suite's own `bd init` never follows the tier's
# already-exported pointer. Without that guard this reproduces the exact
# reported "already initialized" collision, rc=2, on the SECOND `bd init`
# inside a single tier run (the tier's own at _impact_isolated_env, then
# this one) — never on the first.
_SUITE_SCRIPT = """\
#!/bin/sh
# Deliberately NOT `set -e` (matches hooks/test/test-overlap-dispatch-e2e.sh's
# own `set -uo pipefail`): the whole point of the `if [ ! -d ... ]` check
# below is to run AFTER a failed `bd init`, which `set -e` would skip past by
# killing the script at the failing assignment instead.
set -u
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT
INIT_OUT="$(cd "$SCRATCH" && env -u BEADS_DB BD_NON_INTERACTIVE=1 bd init --prefix=sxrbit 2>&1)"
if [ ! -d "$SCRATCH/.beads" ]; then
  echo "FATAL: could not initialize an isolated per-run bd DB: $INIT_OUT"
  exit 2
fi
echo "PASS: suite built its own clean DB despite the tier's ambient BEADS_DB"
exit 0
"""


def _real_repo_with_bd_suite(tmp_path):
    """A real repo whose combined-tree impact tier selects exactly one real
    shell suite that does its own real `bd init`, isolated from the tier's."""
    r = tmp_path / "repo"
    r.mkdir()
    for args in (("init", "-q", "-b", "trunk"), ("config", "user.email", "t@sable.invalid"),
                 ("config", "user.name", "SABLE Test")):
        subprocess.run(["git", "-C", str(r), *args], check=True, capture_output=True)
    (r / ".github" / "ci").mkdir(parents=True)
    (r / ".github" / "ci" / "impact-manifest.sh").write_text(
        "#!/bin/sh\necho test-sx1rb-bd-init.sh\n")
    (r / ".github" / "ci" / "impact-manifest.sh").chmod(0o755)
    (r / "hooks" / "test").mkdir(parents=True)
    suite_path = r / "hooks" / "test" / "test-sx1rb-bd-init.sh"
    suite_path.write_text(_SUITE_SCRIPT)
    suite_path.chmod(0o755)
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(r), "commit", "-q", "-m", "init"], check=True,
                   capture_output=True)
    sha = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True).stdout.strip()
    return str(r), sha


def _sable_impact_scratch_dirs():
    return set(glob.glob(os.path.join(os.environ.get("TMPDIR", "/tmp"), "sable-impact-*")))


@pytest.mark.skipif(not HAVE_BD, reason="requires a real bd on PATH")
def test_impact_tier_runs_twice_without_scratch_collision(isolated_lock, tmp_path):
    """The real regression test for SABLE-sx1rb: a correctly-isolated suite
    (env -u BEADS_DB at its own `bd init`) must complete GREEN through the
    real tier, twice in a row, with each run's own scratch fully swept —
    proving the second run neither reuses nor collides with the first's."""
    repo, sha = _real_repo_with_bd_suite(tmp_path)
    paths = ["hooks/test/test-sx1rb-bd-init.sh"]

    before = _sable_impact_scratch_dirs()

    outcome1, detail1 = promote_lib.run_impact_tier(repo, sha, paths)
    assert outcome1 == promote_lib.IMPACT_GREEN, (
        f"run 1 did not complete GREEN — a correctly-guarded suite's own "
        f"isolated bd init must not collide with the tier's: {detail1!r}")
    after_run1 = _sable_impact_scratch_dirs()
    leaked_after_1 = after_run1 - before
    assert not leaked_after_1, (
        f"run 1 left scratch behind instead of sweeping it: {leaked_after_1}")

    outcome2, detail2 = promote_lib.run_impact_tier(repo, sha, paths)
    assert outcome2 == promote_lib.IMPACT_GREEN, (
        f"run 2 did not complete GREEN — it must not reuse or collide with "
        f"run 1's already-cleaned-up scratch: {detail2!r}")
    after_run2 = _sable_impact_scratch_dirs()
    leaked_after_2 = after_run2 - before
    assert not leaked_after_2, (
        f"run 2 left scratch behind instead of sweeping it: {leaked_after_2}")


@pytest.mark.skipif(not HAVE_BD, reason="requires a real bd on PATH")
def test_impact_tier_unguarded_suite_init_reproduces_the_reported_collision(
        isolated_lock, tmp_path):
    """PLANT-AND-FAIL, load-bearing: proves the fixture harness above is
    actually sensitive to the defect rather than vacuously green. The SAME
    scenario without the `-u BEADS_DB` guard must reproduce the exact
    reported signature — a suite-level rc=2 "already initialized" abort —
    which pre-fix (Part C absent) would have surfaced as IMPACT_RED naming
    the branch; post-fix (Part C present) it correctly reads IMPACT_ERROR."""
    r = tmp_path / "repo"
    r.mkdir()
    for args in (("init", "-q", "-b", "trunk"), ("config", "user.email", "t@sable.invalid"),
                 ("config", "user.name", "SABLE Test")):
        subprocess.run(["git", "-C", str(r), *args], check=True, capture_output=True)
    (r / ".github" / "ci").mkdir(parents=True)
    (r / ".github" / "ci" / "impact-manifest.sh").write_text(
        "#!/bin/sh\necho test-sx1rb-bd-init-unguarded.sh\n")
    (r / ".github" / "ci" / "impact-manifest.sh").chmod(0o755)
    (r / "hooks" / "test").mkdir(parents=True)
    suite_path = r / "hooks" / "test" / "test-sx1rb-bd-init-unguarded.sh"
    # The pre-fix shape: no `-u BEADS_DB` on this suite's own `bd init`.
    unguarded_script = _SUITE_SCRIPT.replace("env -u BEADS_DB BD_NON_INTERACTIVE=1",
                                              "env BD_NON_INTERACTIVE=1")
    suite_path.write_text(unguarded_script)
    suite_path.chmod(0o755)
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(r), "commit", "-q", "-m", "init"], check=True,
                   capture_output=True)
    sha = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True).stdout.strip()

    outcome, detail = promote_lib.run_impact_tier(
        str(r), sha, ["hooks/test/test-sx1rb-bd-init-unguarded.sh"])
    assert outcome == promote_lib.IMPACT_ERROR, (
        f"an unguarded suite init should reproduce the reported collision as "
        f"an infrastructure fault (Part C), not silently pass: {detail!r}")
    assert "already initialized" in detail.lower() or "infrastructure" in detail.lower(), (
        f"expected the reported collision signature in the detail: {detail!r}")
