#!/usr/bin/env python3
"""Real-git/filesystem integration tests for SABLE-tvhzw."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import sable_activation_lib as activation  # noqa: E402
import sable_activation_debt_lib as debt  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")


def git(repo: Path, *args: str) -> str:
    cp = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=test",
         "-c", "user.email=test@example.invalid", *args],
        capture_output=True, text=True, timeout=30,
    )
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.strip()


def test_real_installed_hook_copy_is_detected_stale_against_a_blob(tmp_path):
    repo = tmp_path / "repo"
    hook = repo / "hooks/multi-manager/demo.sh"
    hook.parent.mkdir(parents=True)
    hook.write_text("old\n")
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "old")

    claude = tmp_path / "home/.claude"
    installed = claude / "hooks/multi-manager/demo.sh"
    installed.parent.mkdir(parents=True)
    shutil.copy2(hook, installed)
    hook.write_text("landed\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "--amend", "--no-edit")
    roots = debt.Roots(str(claude), str(tmp_path / "home/.local/bin"))

    assert activation.stale_installed(
        ["hooks/multi-manager/demo.sh"], "HEAD", repo_root=repo, roots=roots
    ) == ["hooks/multi-manager/demo.sh"]


def test_real_symlink_farm_classifies_each_entry_by_shape(tmp_path):
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    (repo / "hooks/multi-manager").mkdir(parents=True)
    claude = tmp_path / "home/.claude"
    local_bin = tmp_path / "home/.local/bin"
    (claude / "hooks/multi-manager").mkdir(parents=True)
    local_bin.mkdir(parents=True)
    roots = debt.Roots(str(claude), str(local_bin))

    live = repo / "bin/sable-live"
    pinned = repo / "bin/sable-pinned"
    copied = repo / "hooks/multi-manager/demo.sh"
    for path in (live, pinned, copied):
        path.write_text("source\n")
    (local_bin / live.name).symlink_to(live)
    snapshot = tmp_path / "snapshot/sable-pinned"
    snapshot.parent.mkdir()
    snapshot.write_text("snapshot\n")
    (local_bin / pinned.name).symlink_to(snapshot)
    shutil.copy2(copied, claude / "hooks/multi-manager/demo.sh")

    assert activation.activation_obligations(
        ["bin/sable-live", "bin/sable-pinned", "hooks/multi-manager/demo.sh"],
        repo_root=repo, roots=roots,
    ) == {
        activation.Shape.PULL_HOT_SWAP: ["bin/sable-live"],
        activation.Shape.REPIN: ["bin/sable-pinned"],
        activation.Shape.INSTALL_REFRESH: ["hooks/multi-manager/demo.sh"],
    }
