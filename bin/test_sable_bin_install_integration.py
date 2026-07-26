#!/usr/bin/env python3
"""Integration tests for the SABLE-bdskx sibling-loader classifier widening.

Classification is a claim about runtime behaviour: --classify exists so a
caller can decide whether a bin is safe to copy as a single file. The unit
suite (test_sable_bin_install.py) asserts the classifier's STRING output;
these tests EXECUTE the predicted outcome instead -- install a
sibling-loading bin into a scratch dir with no sibling present, run it for
real, and confirm the "snapshot" verdict actually predicted the failure
(and, as a control, that a genuine "plain" verdict predicts success).

MANDATORY SANDBOX (SABLE-33hw3): sable-bin-install's snapshot LIB dir is
governed by SABLE_LIB_DIR (bin/sable-bin-install:181), a SEPARATE override
from --dir (which scopes only the bin directory). Every subprocess call
below exports SABLE_LIB_DIR to a scratch path so this suite can never write
into the live ~/.local/lib snapshot tree -- an unscoped run of a sibling
suite already did exactly that on 2026-07-21 and silently fed unmerged code
into chuck's merge gate for ~4 promotions.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT_SRC = REPO / "bin" / "sable-bin-install"


def _env(scratch_lib_dir):
    return {**os.environ, "SABLE_LIB_DIR": str(scratch_lib_dir)}


def make_fixture_repo(tmp_path):
    """A throwaway git repo whose bin/ contains a sibling-loading entrypoint
    (SourceFileLoader on a Path(__file__).resolve().parent join -- the same
    shape as the real bin/sable-reconcile-handoffs loading bin/sable-merge-gate)
    plus the sibling it loads, and an ordinary python bin with zero sibling
    coupling for the negative-direction control."""
    repo = tmp_path / "repo"
    bin_dir = repo / "bin"
    bin_dir.mkdir(parents=True)

    shutil.copy(SCRIPT_SRC, bin_dir / "sable-bin-install")
    (bin_dir / "sable-bin-install").chmod(0o755)

    (bin_dir / "sable-sibling-entrypoint").write_text(
        "#!/usr/bin/env python3\n"
        "SIBLING_MARK = 'sibling-v1'\n"
        "def helper():\n"
        "    return SIBLING_MARK\n"
    )
    (bin_dir / "sable-sibling-entrypoint").chmod(0o755)

    (bin_dir / "sable-loader-main").write_text(
        "#!/usr/bin/env python3\n"
        "from importlib.machinery import SourceFileLoader\n"
        "import importlib.util\n"
        "from pathlib import Path\n"
        "_SIB_PATH = Path(__file__).resolve().parent / 'sable-sibling-entrypoint'\n"
        "_SIB_LOADER = SourceFileLoader('sable_sibling_entrypoint', str(_SIB_PATH))\n"
        "_SIB_SPEC = importlib.util.spec_from_loader('sable_sibling_entrypoint', _SIB_LOADER)\n"
        "_sib = importlib.util.module_from_spec(_SIB_SPEC)\n"
        "_SIB_LOADER.exec_module(_sib)\n"
        "print(f'loader ok: {_sib.helper()}')\n"
    )
    (bin_dir / "sable-loader-main").chmod(0o755)

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    return repo, bin_dir


def classify(bin_dir, dest, target, scratch_lib):
    result = subprocess.run(
        ["bash", str(bin_dir / "sable-bin-install"), "--dir", str(dest), "--classify", target],
        capture_output=True, text=True, timeout=30, env=_env(scratch_lib),
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_snapshot_verdict_predicts_out_of_tree_copy_failure(tmp_path):
    repo, bin_dir = make_fixture_repo(tmp_path)

    verdict = classify(bin_dir, tmp_path / "dest", "sable-loader-main", tmp_path / "scratch-lib")
    assert verdict == "snapshot"

    # Simulate a naive per-file copy -- exactly what a "plain" verdict would
    # have claimed was safe: the entrypoint alone, into a dir with NO sibling.
    lone_copy_dir = tmp_path / "lone-copy"
    lone_copy_dir.mkdir()
    shutil.copy(bin_dir / "sable-loader-main", lone_copy_dir / "sable-loader-main")
    (lone_copy_dir / "sable-loader-main").chmod(0o755)

    run_result = subprocess.run(
        [sys.executable, str(lone_copy_dir / "sable-loader-main")],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert run_result.returncode != 0, (
        "the entrypoint should fail without its sibling present -- if it "
        "didn't, the 'snapshot' verdict above no longer predicts reality"
    )
    assert "sable-sibling-entrypoint" in run_result.stderr

    # Confirm it DOES run once installed alongside its sibling, pinning the
    # exact asymmetry the widened classifier exists to warn about.
    together_dir = tmp_path / "together"
    together_dir.mkdir()
    shutil.copy(bin_dir / "sable-loader-main", together_dir / "sable-loader-main")
    shutil.copy(bin_dir / "sable-sibling-entrypoint", together_dir / "sable-sibling-entrypoint")
    (together_dir / "sable-loader-main").chmod(0o755)
    (together_dir / "sable-sibling-entrypoint").chmod(0o755)

    run_result_together = subprocess.run(
        [sys.executable, str(together_dir / "sable-loader-main")],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert run_result_together.returncode == 0, run_result_together.stderr
    assert "loader ok: sibling-v1" in run_result_together.stdout


# --- self-scoping SABLE_LIB_DIR guard (SABLE-rkc3o) -------------------------------
#
# The classify() helper above already scopes SABLE_LIB_DIR for every call in
# this file, but --classify never writes to LIB_DIR at all -- only
# --pin-snapshot does (bin/sable-bin-install:~243). This suite never actually
# exercised the write path this bead is about. These two tests do, with a
# sandboxed $HOME (never the real machine's), and directly prove causation:
# the same fixture, run the same way, WITHOUT the guard's redirect (simulated
# by pointing SABLE_LIB_DIR at exactly the path the guard would otherwise
# have avoided) DOES write, and WITH the guard active (SABLE_LIB_DIR left
# unset, suite context detected via PYTEST_CURRENT_TEST) does NOT -- so the
# absence of pollution in the positive case is the guard's doing, not the
# suite simply never reaching that code path.

def _count_snapshot_dirs(lib_dir):
    if not lib_dir.exists():
        return 0
    return len([p for p in lib_dir.iterdir() if p.is_dir() and p.name.startswith("sable-")])


def test_unscoped_pin_snapshot_leaves_sandboxed_home_lib_unchanged(tmp_path):
    repo, bin_dir = make_fixture_repo(tmp_path)
    dest = tmp_path / "dest"
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    fake_home_lib = fake_home / ".local" / "lib"

    before = _count_snapshot_dirs(fake_home_lib)
    assert before == 0

    env = dict(os.environ)
    env.pop("SABLE_LIB_DIR", None)
    env["HOME"] = str(fake_home)
    env["PYTEST_CURRENT_TEST"] = "test_unscoped_pin_snapshot_leaves_sandboxed_home_lib_unchanged (call)"

    result = subprocess.run(
        ["bash", str(bin_dir / "sable-bin-install"), "--dir", str(dest),
         "--pin-snapshot", "sable-loader-main"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 0, result.stderr

    after = _count_snapshot_dirs(fake_home_lib)
    assert after == before == 0, (
        "an unscoped --pin-snapshot run wrote into the sandboxed HOME's "
        "/.local/lib -- the self-scoping guard failed to redirect it"
    )


def test_negative_control_same_run_does_write_when_pointed_at_the_would_be_default(tmp_path):
    """Proves the guard above is load-bearing, not coincidental: point
    SABLE_LIB_DIR explicitly at fake-home/.local/lib -- the exact path
    resolve_lib_dir's fallback branch would have picked had the suite-context
    self-scope not intervened -- and confirm the identical fixture/args DOES
    write a snapshot there. This is the "disable the guard" negative control:
    same code, same inputs, only the env differs, and the outcome flips."""
    repo, bin_dir = make_fixture_repo(tmp_path)
    dest = tmp_path / "dest"
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    fake_home_lib = fake_home / ".local" / "lib"

    before = _count_snapshot_dirs(fake_home_lib)
    assert before == 0

    env = dict(os.environ)
    env["HOME"] = str(fake_home)
    env["SABLE_LIB_DIR"] = str(fake_home_lib)  # explicit -- simulates "no guard"

    result = subprocess.run(
        ["bash", str(bin_dir / "sable-bin-install"), "--dir", str(dest),
         "--pin-snapshot", "sable-loader-main"],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 0, result.stderr

    after = _count_snapshot_dirs(fake_home_lib)
    assert after == before + 1, (
        "negative control did not write a snapshot -- if this fails, the "
        "positive test above proves nothing about the guard specifically"
    )


def test_plain_verdict_predicts_out_of_tree_copy_success(tmp_path):
    """Negative-direction control: a bin the classifier calls "plain" really
    is safe to copy alone with no sibling present -- confirms the widened
    detector didn't turn into an over-match that would wrongly block an
    ordinary per-file install."""
    repo, bin_dir = make_fixture_repo(tmp_path)

    verdict = classify(bin_dir, tmp_path / "dest", "sable-sibling-entrypoint", tmp_path / "scratch-lib")
    assert verdict == "plain"

    lone_copy_dir = tmp_path / "lone-copy-plain"
    lone_copy_dir.mkdir()
    shutil.copy(bin_dir / "sable-sibling-entrypoint", lone_copy_dir / "sable-sibling-entrypoint")
    (lone_copy_dir / "sable-sibling-entrypoint").chmod(0o755)

    run_result = subprocess.run(
        [sys.executable, str(lone_copy_dir / "sable-sibling-entrypoint")],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert run_result.returncode == 0, run_result.stderr
