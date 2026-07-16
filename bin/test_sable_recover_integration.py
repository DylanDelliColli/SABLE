#!/usr/bin/env python3
"""Integration tests for bin/sable-recover (SABLE-wwxd).

Real composition: the actual `sable-recover` binary run as a subprocess against
a REAL git repository with REAL linked worktrees on disk — real `git worktree
list`, real `git status --porcelain` for the dirty check, real
`git rev-list`/`for-each-ref` for the push/merge classification, and a real
bare "origin" the branches are (or aren't) pushed to. This is the exact
substrate the tool exists to sweep: worktrees + branch/push state that survive a
crash on disk.

Two dependencies are supplied as captured fixtures rather than live services,
and this is deliberate, not a mock of the system under test:

  - the bead pool (`--beads-file`) — a `bd list --status in_progress --json`
    snapshot. bd is a durable store that outlived the crash; a point-in-time
    snapshot of it IS the real input to the forensics, and the clean-room CI
    runner has NO bd (bd/dolt suites self-skip there — see ci-verify.yml). The
    tool's job is git forensics, and THAT runs fully live here.
  - the tmux pane set (`--panes-file`) — post-crash there are literally zero
    live panes, which is precisely the state a captured/empty dump models. The
    stranded-claim case here uses a genuinely paneless world.

The headline case builds the exact trio the bead names: one pushed worktree, one
unpushed-dirty worktree, and one stranded claim — and asserts the report names
all three correctly and orders the resume steps. git is required (universally
present in CI); the suite self-skips only if git is somehow absent.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RECOVER = REPO / "bin" / "sable-recover"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)


def _git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=check)


def _build_crash_scene(tmp_path):
    """A real repo + a bare origin + three linked worktrees modelling the
    post-crash state:

      wk-pushed      — committed and pushed to origin, clean          (PUSHED)
      wk-dirty       — committed but NEVER pushed, uncommitted edits  (UNPUSHED+DIRTY)
      wk-stranded    — its worktree/branch, but its claim has no pane (STRANDED)

    Returns (main_repo_path, beads_file_path, panes_file_path)."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   capture_output=True, check=True)

    main = tmp_path / "main"
    subprocess.run(["git", "init", "-b", "main", str(main)],
                   capture_output=True, check=True)
    _git(main, "config", "user.email", "t@t")
    _git(main, "config", "user.name", "t")
    _git(main, "remote", "add", "origin", str(origin))
    (main / "README").write_text("base\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-m", "base")
    _git(main, "push", "-u", "origin", "main")

    def add_worktree(branch):
        path = tmp_path / branch
        _git(main, "worktree", "add", "-b", branch, str(path), "main")
        _git(path, "config", "user.email", "t@t")
        _git(path, "config", "user.name", "t")
        return path

    # wk-pushed: a real commit, pushed to origin, clean tree
    pushed = add_worktree("wk-pushed")
    (pushed / "f.txt").write_text("pushed work\n")
    _git(pushed, "add", "-A")
    _git(pushed, "commit", "-m", "pushed work")
    _git(pushed, "push", "-u", "origin", "wk-pushed")

    # wk-dirty: a real commit that NEVER left disk, plus uncommitted edits
    dirty = add_worktree("wk-dirty")
    (dirty / "f.txt").write_text("committed but unpushed\n")
    _git(dirty, "add", "-A")
    _git(dirty, "commit", "-m", "local only")
    (dirty / "f.txt").write_text("uncommitted edit on top\n")  # dirty tree

    # wk-stranded: exists on disk; its claim (below) has no pane
    add_worktree("wk-stranded")

    # bead snapshot: two in_progress claims. wk-dirty's has a live pane;
    # wk-stranded's does NOT -> stranded.
    beads = [
        {"id": "SABLE-dirty", "title": "the dirty worker", "status": "in_progress"},
        {"id": "SABLE-stranded", "title": "crashed claim", "status": "in_progress"},
    ]
    beads_file = tmp_path / "beads.json"
    beads_file.write_text(json.dumps(beads))

    # pane dump: only the dirty worker has a live pane; branch names carry the
    # bead id (wk-sable-dirty style) so branch-fallback resolution is exercised
    # too. Path points at the dirty worktree so pane->worktree binding is real.
    panes_file = tmp_path / "panes.txt"
    panes_file.write_text(f"{dirty}\tSABLE-dirty\trunning\n")

    return main, beads_file, panes_file


def _run_recover(main, beads_file, panes_file, *extra):
    return subprocess.run(
        [sys.executable, str(RECOVER), "--repo", str(main),
         "--base", "main", "--beads-file", str(beads_file),
         "--panes-file", str(panes_file), *extra],
        capture_output=True, text=True,
    )


def test_report_names_all_three_states(tmp_path):
    main, beads_file, panes_file = _build_crash_scene(tmp_path)
    r = _run_recover(main, beads_file, panes_file, "--json")
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout)

    rows = {row["branch"]: row for row in report["worktrees"]}

    # (1) pushed worktree — on origin, in sync
    assert rows["wk-pushed"]["push_state"] == "pushed"
    assert rows["wk-pushed"]["dirty"] is False

    # (2) unpushed + dirty worktree
    assert rows["wk-dirty"]["push_state"] == "unpushed"
    assert rows["wk-dirty"]["dirty"] is True
    assert rows["wk-dirty"]["bead"] == "SABLE-dirty"  # resolved via live pane

    # (3) stranded claim — in_progress bead with no live pane
    stranded_ids = [b["id"] for b in report["stranded_claims"]]
    assert stranded_ids == ["SABLE-stranded"]


def test_resume_plan_is_ordered(tmp_path):
    main, beads_file, panes_file = _build_crash_scene(tmp_path)
    r = _run_recover(main, beads_file, panes_file, "--json")
    report = json.loads(r.stdout)

    actions = [s["action"] for s in report["plan"]]
    # dirty tree with unpushed commits -> review (never auto-pushed)
    assert "review" in actions
    # stranded claim -> redispatch
    assert "redispatch" in actions
    # a redispatch step never precedes the dirty-tree review (group order)
    orders = [s["order"] for s in report["plan"]]
    assert orders == sorted(orders)


def test_unmerged_branch_appears_when_pushed_but_not_merged(tmp_path):
    main, beads_file, panes_file = _build_crash_scene(tmp_path)
    r = _run_recover(main, beads_file, panes_file, "--json")
    report = json.loads(r.stdout)
    # wk-pushed is on origin but was never merged into main -> Chuck's list
    assert "origin/wk-pushed" in report["unmerged_branches"]
    merge_steps = [s for s in report["plan"] if s["action"] == "merge"]
    assert merge_steps and "origin/wk-pushed" in merge_steps[0]["detail"]


def test_text_report_names_the_three(tmp_path):
    main, beads_file, panes_file = _build_crash_scene(tmp_path)
    r = _run_recover(main, beads_file, panes_file)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "wk-pushed" in out and "wk-dirty" in out
    assert "SABLE-stranded" in out
    assert "RESUME PLAN" in out


def test_fix_pushes_clean_unpushed_branch_but_not_dirty(tmp_path):
    """--fix applies only the safe step. A separate CLEAN unpushed worktree gets
    pushed for real; the DIRTY unpushed one is left alone (review, not push)."""
    main, beads_file, panes_file = _build_crash_scene(tmp_path)
    tmp = Path(main).parent

    # add a CLEAN unpushed worktree: committed, never pushed, no dirt
    clean = tmp / "wk-clean"
    _git(main, "worktree", "add", "-b", "wk-clean", str(clean), "main")
    _git(clean, "config", "user.email", "t@t")
    _git(clean, "config", "user.name", "t")
    (clean / "c.txt").write_text("clean unpushed\n")
    _git(clean, "add", "-A")
    _git(clean, "commit", "-m", "clean unpushed")

    # before: origin has no wk-clean
    before = _git(main, "for-each-ref", "--format=%(refname:short)",
                  "refs/remotes/origin/wk-clean")
    assert before.stdout.strip() == ""

    r = _run_recover(main, beads_file, panes_file, "--fix", "--json")
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout)
    assert "wk-clean" in report["fix"]["pushed"]
    assert "wk-dirty" not in report["fix"]["pushed"]  # dirty is review, never pushed

    # after: origin now HAS wk-clean (a real push happened)
    _git(main, "fetch", "origin")
    after = _git(main, "for-each-ref", "--format=%(refname:short)",
                 "refs/remotes/origin/wk-clean")
    assert after.stdout.strip() == "origin/wk-clean"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
