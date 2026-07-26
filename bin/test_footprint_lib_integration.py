#!/usr/bin/env python3
"""Integration tests for sable_footprint_lib against a REAL sandbox bd + real
git (SABLE-zx2yv).

Every other suite for this module (test_footprint_lib.py) stubs `bd show`
with a shell script via SABLE_MG_BD — enough to prove the parser and the
disjointness algebra in isolation, but the bead this fixes is specifically
about `declared_reads()`'s consumption of a REAL `bd show <id> --json`
record, through the REAL `bd` binary, the same seam production code uses.
This file proves the fix survives that seam, not just a hand-written stub.

Fixture discipline matches test_sable_bd_remember_integration.py: a
throwaway HOME + `bd init --non-interactive` in a scratch dir, never the
developer's own beads DB. Self-skips when bd is absent (ci-verify's
clean-room has none by design).
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sable_footprint_lib as fp  # noqa: E402

HAVE_BD = shutil.which("bd") is not None
pytestmark = pytest.mark.skipif(
    not HAVE_BD,
    reason="ci-verify clean-room has no bd by design; real-bd integration self-skips",
)

_ENV_LEAKS = ("CLAUDE_AGENT_NAME", "TMUX_PANE", "SABLE_TMUX_SOCKET", "SABLE_MG_BD")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@sable.invalid",
                           "-c", "user.name=SABLE Test", *args],
                          text=True, capture_output=True, check=True)


def _sha(repo, ref="HEAD"):
    return _git(repo, "rev-parse", ref).stdout.strip()


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _sha(repo)


def _robust_bd_init(work, home):
    """Mirrors test_sable_bd_remember_integration.py's helper: `bd init` on
    the embedded-Dolt backend can leave a partial DB on a first-run race (rc
    0 but no .beads/config.yaml) — gate success on that artifact and
    wipe+retry rather than run against a broken DB."""
    env = {k: v for k, v in os.environ.items() if k not in _ENV_LEAKS}
    env["HOME"] = str(home)
    env["BD_NON_INTERACTIVE"] = "1"
    env["CI"] = "true"
    beads = work / ".beads"
    last = None
    for _ in range(4):
        if beads.exists():
            shutil.rmtree(beads)
        last = subprocess.run(["bd", "init", "--non-interactive"], cwd=str(work),
                              env=env, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=180, check=False)
        if last.returncode == 0 and (beads / "config.yaml").is_file():
            return last
    raise AssertionError(f"bd init never produced a clean DB: {last.stdout if last else '<none>'}")


def _bd_create(work, home, description):
    env = {k: v for k, v in os.environ.items() if k not in _ENV_LEAKS}
    env["HOME"] = str(home)
    env["BD_NON_INTERACTIVE"] = "1"
    env["CI"] = "true"
    cp = subprocess.run(
        ["bd", "create", "--title=zx2yv integration bead", "--type=task",
         f"--description={description}"],
        cwd=str(work), env=env, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=60, check=True)
    for tok in cp.stdout.split():
        if tok.startswith("work-"):
            return tok.rstrip(":")
    raise AssertionError(f"could not find created bead id in: {cp.stdout!r}")


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """A repo that is BOTH a real git repo (for mechanical_footprint) and a
    real bd sandbox (for declared_reads) — assess() needs both seams live at
    once, unlike the pure-parser and stubbed-bd suites elsewhere."""
    work = tmp_path / "work"
    home = tmp_path / "home"
    work.mkdir()
    home.mkdir()
    _git(work, "init", "-q", "-b", "trunk")
    # .beads must never be git-add -A'd into a commit: a later `git checkout
    # -b moved <base>` (as every scenario below does, to build a base-move
    # from the pre-branch state) would then delete it from the working tree
    # on checkout, since `base` predates bd init and has no .beads in its
    # tree — silently breaking the sandbox's own beads database mid-test.
    (work / ".gitignore").write_text(".beads/\n")
    (work / "left.py").write_text("l\n")
    (work / "Makefile").write_text("all:\n\techo hi\n")
    (work / "unrelated").mkdir()
    (work / "unrelated" / "other.txt").write_text("o\n")
    _commit(work, "init")
    _robust_bd_init(work, home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BD_NON_INTERACTIVE", "1")
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("SABLE_MG_BD", raising=False)
    return work, home


def test_a_bare_filename_mixed_with_real_paths_is_not_treated_as_disjoint(sandbox):
    """THE concrete defect, end to end (SABLE-zx2yv): a real bd bead declares
    '## File reads' naming 'Makefile' (a bare repo-root filename the old
    tokenizer dropped) alongside a real path. A base-move that edits
    Makefile must NOT be reported disjoint from the branch just because the
    tokenizer silently narrowed the declared read set to exclude the one
    file that actually collides."""
    work, home = sandbox
    bead = _bd_create(work, home, "Story.\n\n## File reads\nMakefile\nbin/foo.py\n")

    base = _sha(work)
    (work / "left.py").write_text("l2\n")
    branch = _commit(work, "branch edits left.py")
    _git(work, "checkout", "-q", "-b", "moved", base)
    (work / "Makefile").write_text("all:\n\techo bye\n")
    new_base = _commit(work, "base-move edits Makefile")

    a = fp.assess(str(work), bead, base, branch, new_base)
    assert a.disjoint is None, (
        f"a bare-filename read declaration must force undetermined/serialize, "
        f"not a false disjoint verdict — got disjoint={a.disjoint}, reason={a.reason!r}")
    assert "undetermined" in a.reason.lower()


def test_two_genuinely_disjoint_beads_still_promote_in_parallel(sandbox):
    """Known-positive control: the fix must not have turned the floor into a
    gate that can never release (SABLE-47try's DO-NOT clause). A bead whose
    '## File reads' section is fully recognisable and genuinely disjoint
    from the base-move must still report disjoint=True."""
    work, home = sandbox
    bead = _bd_create(work, home, "Story.\n\n## File reads\nunrelated/other.txt\n")

    base = _sha(work)
    (work / "left.py").write_text("l2\n")
    branch = _commit(work, "branch edits left.py")
    _git(work, "checkout", "-q", "-b", "moved", base)
    (work / "right.py").write_text("r\n")
    new_base = _commit(work, "base-move adds right.py")

    a = fp.assess(str(work), bead, base, branch, new_base)
    assert a.disjoint is True, a.reason
