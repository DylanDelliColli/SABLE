#!/usr/bin/env python3
"""Unit tests for bin/sable-bin-install's snapshot-pinning mechanism (SABLE-9boz4).

Follow-up to SABLE-mkj6k's regular-file pin protection: sable-spawn-worker and
sable-msg import sibling repo modules (sable_pane_lib.py, ...), so copying just
the one file severs the import (ImportError). --pin-snapshot instead freezes
the WHOLE bin/ directory as one versioned unit (~/.local/lib/sable-<sha>/) and
atomically repoints the entry symlink into it, so the import closure travels
with the pin regardless of which sibling libs a bin needs today or gains,
loses, or is renamed to tomorrow.

Real fixture git repos + real subprocess calls to the actual script (no
mocking of the shell logic) — synthetic bin/ trees under tmp_path, not the
real SABLE repo, so tests stay fast and independent of repo/bin's real
contents. The end-to-end "does the pinned tool actually run outside the repo"
proof lives in the integration suite (hooks/test/test-spine-pinning.sh); this
covers the classifier + the atomic-repoint/pin-protection unit logic.
"""
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT_SRC = REPO / "bin" / "sable-bin-install"


def make_fixture_repo(tmp_path, *, extra_files=None):
    """A throwaway git repo with a bin/ dir: a real copy of sable-bin-install
    (so BIN_DIR resolves to THIS fixture's bin/, not the real repo's), a
    plain shell tool, a python tool with zero sibling imports, and a python
    tool that imports a sibling module physically present in bin/ — the
    0-import / N-import split the classifier exists to make."""
    repo = tmp_path / "repo"
    bin_dir = repo / "bin"
    bin_dir.mkdir(parents=True)

    shutil.copy(SCRIPT_SRC, bin_dir / "sable-bin-install")
    (bin_dir / "sable-bin-install").chmod(0o755)

    (bin_dir / "sable-plain").write_text("#!/usr/bin/env bash\necho plain\n")
    (bin_dir / "sable-plain").chmod(0o755)

    (bin_dir / "sable-nolib-py").write_text(
        "#!/usr/bin/env python3\nimport sys\nprint('no sibling import')\n"
    )
    (bin_dir / "sable-nolib-py").chmod(0o755)

    (bin_dir / "sable_helper_lib.py").write_text(
        "MARK = 'helper-lib-v1'\n"
    )

    (bin_dir / "sable-importer").write_text(
        "#!/usr/bin/env python3\n"
        "from __future__ import annotations\n"
        "import os, sys\n"
        "sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))\n"
        "from sable_helper_lib import MARK  # noqa: E402\n"
        "print(f'importer ok: {MARK}')\n"
    )
    (bin_dir / "sable-importer").chmod(0o755)

    if extra_files:
        for name, content in extra_files.items():
            (bin_dir / name).write_text(content)

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    return repo, bin_dir


def run_install(bin_dir, dest, *extra_args, env=None):
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["bash", str(bin_dir / "sable-bin-install"), "--dir", str(dest), *extra_args],
        capture_output=True, text=True, timeout=30, env=full_env,
    )


# --- import-check classifier (--classify) --------------------------------------

def test_classify_shell_script_is_plain(tmp_path):
    repo, bin_dir = make_fixture_repo(tmp_path)
    result = run_install(bin_dir, tmp_path / "dest", "--classify", "sable-plain")
    assert result.stdout.strip() == "plain", result.stderr


def test_classify_python_with_no_sibling_import_is_plain(tmp_path):
    repo, bin_dir = make_fixture_repo(tmp_path)
    result = run_install(bin_dir, tmp_path / "dest", "--classify", "sable-nolib-py")
    assert result.stdout.strip() == "plain", result.stderr


def test_classify_python_importing_sibling_module_is_snapshot(tmp_path):
    repo, bin_dir = make_fixture_repo(tmp_path)
    result = run_install(bin_dir, tmp_path / "dest", "--classify", "sable-importer")
    assert result.stdout.strip() == "snapshot", result.stderr


def test_classify_import_of_absent_module_is_still_plain(tmp_path):
    # imports a module NAME that looks like a sibling but the .py file isn't
    # actually present in bin/ -- must not false-positive to "snapshot".
    repo, bin_dir = make_fixture_repo(tmp_path, extra_files={
        "sable-ghost-import": (
            "#!/usr/bin/env python3\nfrom sable_ghost_lib import X\nprint(X)\n"
        ),
    })
    (bin_dir / "sable-ghost-import").chmod(0o755)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add ghost"], cwd=repo, check=True)
    result = run_install(bin_dir, tmp_path / "dest", "--classify", "sable-ghost-import")
    assert result.stdout.strip() == "plain", result.stderr


# --- sibling-loader classifier (SABLE-bdskx) -------------------------------------
#
# is_python_importing only greps for a static `import`/`from` statement.
# SourceFileLoader / spec_from_file_location dynamically load a sibling by
# building a __file__-relative path at runtime -- e.g.
# bin/sable-reconcile-handoffs loading bin/sable-merge-gate. That shape has
# no `import sable_*` line to grep for, so it classified "plain" (safe for a
# per-file copy) even though a per-file copy severs the sibling load.

def test_classify_sourcefileloader_sibling_load_is_snapshot(tmp_path):
    repo, bin_dir = make_fixture_repo(tmp_path, extra_files={
        "sable-sourcefileloader-importer": (
            "#!/usr/bin/env python3\n"
            "from importlib.machinery import SourceFileLoader\n"
            "import importlib.util\n"
            "from pathlib import Path\n"
            "_SIB_PATH = Path(__file__).resolve().parent / 'sable-plain'\n"
            "_SIB_LOADER = SourceFileLoader('sable_plain', str(_SIB_PATH))\n"
            "_SIB_SPEC = importlib.util.spec_from_loader('sable_plain', _SIB_LOADER)\n"
            "_sib = importlib.util.module_from_spec(_SIB_SPEC)\n"
            "_SIB_LOADER.exec_module(_sib)\n"
        ),
    })
    (bin_dir / "sable-sourcefileloader-importer").chmod(0o755)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add sourcefileloader fixture"], cwd=repo, check=True)
    result = run_install(bin_dir, tmp_path / "dest", "--classify", "sable-sourcefileloader-importer")
    assert result.stdout.strip() == "snapshot", result.stderr


def test_classify_spec_from_file_location_sibling_load_is_snapshot(tmp_path):
    repo, bin_dir = make_fixture_repo(tmp_path, extra_files={
        "sable-specfromfile-importer": (
            "#!/usr/bin/env python3\n"
            "import importlib.util\n"
            "import os\n"
            "_SIB_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'sable-plain')\n"
            "_SIB_SPEC = importlib.util.spec_from_file_location('sable_plain', _SIB_PATH)\n"
            "_sib = importlib.util.module_from_spec(_SIB_SPEC)\n"
            "_SIB_SPEC.loader.exec_module(_sib)\n"
        ),
    })
    (bin_dir / "sable-specfromfile-importer").chmod(0o755)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add spec_from_file_location fixture"], cwd=repo, check=True)
    result = run_install(bin_dir, tmp_path / "dest", "--classify", "sable-specfromfile-importer")
    assert result.stdout.strip() == "snapshot", result.stderr


def test_classify_genuinely_plain_python_is_still_plain(tmp_path):
    # Negative direction: a detector that over-matches on loader-ish words
    # alone would wrongly block per-file installs of ordinary bins. Neither
    # SourceFileLoader/spec_from_file_location nor __file__ appear here.
    repo, bin_dir = make_fixture_repo(tmp_path)
    result = run_install(bin_dir, tmp_path / "dest", "--classify", "sable-nolib-py")
    assert result.stdout.strip() == "plain", result.stderr


def test_classify_real_reconcile_handoffs_is_not_plain(tmp_path):
    # Real-world case (SABLE-bdskx): bin/sable-reconcile-handoffs loads its
    # sibling bin/sable-merge-gate via SourceFileLoader on a
    # Path(__file__).resolve().parent join. It is NOT plain -- a per-file
    # copy without the sibling present dies at import.
    repo, bin_dir = make_fixture_repo(tmp_path)
    real_reconcile = REPO / "bin" / "sable-reconcile-handoffs"
    shutil.copy(real_reconcile, bin_dir / "sable-reconcile-handoffs")
    (bin_dir / "sable-reconcile-handoffs").chmod(0o755)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add real reconcile-handoffs"], cwd=repo, check=True)
    result = run_install(bin_dir, tmp_path / "dest", "--classify", "sable-reconcile-handoffs")
    assert result.stdout.strip() != "plain", result.stderr


# --- --pin-snapshot: versioned snapshot dir + atomic repoint --------------------

def test_pin_snapshot_auto_detects_python_importing_tools(tmp_path):
    repo, bin_dir = make_fixture_repo(tmp_path)
    dest = tmp_path / "dest"
    result = run_install(bin_dir, dest, "--pin-snapshot")
    assert result.returncode == 0, result.stderr
    assert (dest / "sable-importer").is_symlink()
    target = Path(os.path.realpath(dest / "sable-importer"))
    assert str(bin_dir) not in str(target.parent)  # resolves OUTSIDE the live repo bin/
    assert "sable-" in target.parent.name
    # the plain shell tool and the no-import python tool are untouched by
    # the auto-detected snapshot pin
    assert not (dest / "sable-plain").exists()


def test_pin_snapshot_creates_versioned_dir_with_bin_and_sibling_lib(tmp_path):
    repo, bin_dir = make_fixture_repo(tmp_path)
    dest = tmp_path / "dest"
    result = run_install(bin_dir, dest, "--pin-snapshot", "sable-importer")
    assert result.returncode == 0, result.stderr
    target = Path(os.path.realpath(dest / "sable-importer"))
    snapshot_dir = target.parent
    # ONE unit: the entrypoint AND its sibling lib travel together, not an
    # enumerated per-file copy (SABLE-9boz4 design constraint).
    assert (snapshot_dir / "sable-importer").is_file()
    assert (snapshot_dir / "sable_helper_lib.py").is_file()


def test_pin_snapshot_entry_point_runs_successfully_outside_repo(tmp_path):
    repo, bin_dir = make_fixture_repo(tmp_path)
    dest = tmp_path / "dest"
    result = run_install(bin_dir, dest, "--pin-snapshot", "sable-importer")
    assert result.returncode == 0, result.stderr

    outside_cwd = tmp_path / "elsewhere"
    outside_cwd.mkdir()
    run_result = subprocess.run(
        [sys.executable, str(dest / "sable-importer")],
        cwd=outside_cwd, capture_output=True, text=True, timeout=10,
    )
    assert run_result.returncode == 0, run_result.stderr
    assert "importer ok: helper-lib-v1" in run_result.stdout


def test_pin_snapshot_dry_run_writes_nothing(tmp_path):
    repo, bin_dir = make_fixture_repo(tmp_path)
    dest = tmp_path / "dest"
    result = run_install(bin_dir, dest, "--pin-snapshot", "sable-importer", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert not dest.exists()
    lib_dir = tmp_path / "nonexistent-home" / ".local" / "lib"
    assert not lib_dir.exists()


def test_pin_snapshot_missing_target_name_reports_and_skips(tmp_path):
    repo, bin_dir = make_fixture_repo(tmp_path)
    dest = tmp_path / "dest"
    result = run_install(bin_dir, dest, "--pin-snapshot", "sable-does-not-exist")
    assert "not found in snapshot" in result.stderr


# --- pin protection: a snapshot pin survives a plain re-install -----------------

def test_plain_install_does_not_revert_a_snapshot_pin(tmp_path):
    repo, bin_dir = make_fixture_repo(tmp_path)
    dest = tmp_path / "dest"
    run_install(bin_dir, dest, "--pin-snapshot", "sable-importer")
    pinned_target = os.path.realpath(dest / "sable-importer")

    result = run_install(bin_dir, dest)
    assert result.returncode == 0, result.stderr
    assert "pinned" in result.stderr.lower()
    assert os.path.realpath(dest / "sable-importer") == pinned_target, (
        "a plain re-install must not silently re-symlink a snapshot pin back "
        "to the live repo (the exact y6ik3-style hazard this bead prevents)"
    )


def test_plain_install_records_snapshot_pin_in_marker(tmp_path):
    repo, bin_dir = make_fixture_repo(tmp_path)
    dest = tmp_path / "dest"
    run_install(bin_dir, dest, "--pin-snapshot", "sable-importer")
    run_install(bin_dir, dest)
    marker = (dest / ".sable-pinned").read_text()
    assert "sable-importer" in marker.splitlines()


def test_repin_reverts_a_snapshot_pin_to_the_live_repo(tmp_path):
    repo, bin_dir = make_fixture_repo(tmp_path)
    dest = tmp_path / "dest"
    run_install(bin_dir, dest, "--pin-snapshot", "sable-importer")

    result = run_install(bin_dir, dest, "--repin")
    assert result.returncode == 0, result.stderr
    assert os.path.realpath(dest / "sable-importer") == str((bin_dir / "sable-importer").resolve())


def test_copy_mode_does_not_refuse_on_an_existing_snapshot_pin(tmp_path):
    """--copy is deliberately unaffected by pin protection (mkj6k design: an
    explicit --copy request is itself pin-like intent) -- confirm it neither
    errors nor reports a skip. NOTE: `cp` writing THROUGH a pre-existing
    symlink instead of replacing it (so the destination stays a symlink
    either way) is a separate, pre-existing defect unrelated to snapshot
    pinning specifically -- tracked as SABLE-venvs, not this bead's fix
    target."""
    repo, bin_dir = make_fixture_repo(tmp_path)
    dest = tmp_path / "dest"
    run_install(bin_dir, dest, "--pin-snapshot", "sable-importer")

    result = run_install(bin_dir, dest, "--copy")
    assert result.returncode == 0, result.stderr
    assert "skipped pinned" not in result.stderr.lower()
