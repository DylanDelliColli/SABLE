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
import os
import signal
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RECOVER = REPO / "bin" / "sable-recover"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git not available"
)
HAVE_BD = shutil.which("bd") is not None
HAVE_TMUX = shutil.which("tmux") is not None


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


def _private_env(home, *, store=None, socket=None):
    env = dict(os.environ)
    for name in (
        "BEADS_DB", "BEADS_DIR", "TMUX", "TMUX_PANE",
        "SABLE_TMUX_SOCKET", "SABLE_TMUX_SESSION",
    ):
        env.pop(name, None)
    env["HOME"] = str(home)
    env["BD_NON_INTERACTIVE"] = "1"
    if store is not None:
        env["BEADS_DIR"] = str(store)
    if socket is not None:
        env["SABLE_TMUX_SOCKET"] = socket
    return env


def _robust_bd_init(repo, home):
    beads = repo / ".beads"
    last = None
    for _ in range(4):
        if beads.exists():
            shutil.rmtree(beads)
        last = subprocess.run(
            [
                "bd", "init", "--prefix=RCV", "--non-interactive",
                "--skip-agents", "--skip-hooks", "--quiet",
            ],
            cwd=repo,
            env=_private_env(home),
            capture_output=True,
            text=True,
        )
        if last.returncode == 0 and (beads / "config.yaml").is_file():
            return beads
    raise AssertionError(
        "bd init never produced a complete private store: "
        + ((last.stdout + last.stderr) if last else "no attempt")
    )


def _tmux(socket, *args, check=True):
    env = dict(os.environ)
    for name in ("TMUX", "TMUX_PANE", "SABLE_TMUX_SOCKET", "SABLE_TMUX_SESSION"):
        env.pop(name, None)
    return subprocess.run(
        ["tmux", "-L", socket, *args],
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )


def _run_production_recover(main, home, store, socket):
    result = subprocess.run(
        [
            sys.executable, str(RECOVER), "--repo", str(main),
            "--base", "main", "--json",
        ],
        cwd=main,
        env=_private_env(home, store=store, socket=socket),
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _binding(report, bead):
    return next(row for row in report["pane_bindings"] if row["bead"] == bead)


def _durable_worktree_picture(report):
    """The git-owned fields that survive independently of pane annotations."""
    keys = ("path", "branch", "detached", "dirty", "push_state")
    return [{key: row[key] for key in keys} for row in report["worktrees"]]


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


@pytest.mark.skipif(
    not (HAVE_BD and HAVE_TMUX),
    reason="real recovery-world proof requires bd/dolt and tmux",
)
def test_production_collectors_distinguish_manager_clear_from_host_crash(tmp_path):
    """The involuntary-end contract through the production collectors.

    One private world drives both mode arms without --beads-file or
    --panes-file: first SIGKILL only the manager while its worker survives,
    then destroy the whole named-socket server. The durable git/bd picture must
    survive both; worker-owned topology survives only the first.
    """
    main, _, _ = _build_crash_scene(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    store = _robust_bd_init(main, home)
    created = subprocess.run(
        [
            "bd", "create", "--title=Recovery world in-flight work",
            "--type=task", "--json",
        ],
        cwd=main,
        env=_private_env(home, store=store),
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(created.stdout)
    if isinstance(payload, list):
        payload = payload[0]
    bead = payload["id"]
    subprocess.run(
        ["bd", "update", bead, "--claim"],
        cwd=main,
        env=_private_env(home, store=store),
        capture_output=True,
        text=True,
        check=True,
    )

    socket = f"recover-{uuid.uuid4().hex[:10]}"
    worker_path = tmp_path / "wk-stranded"
    try:
        _tmux(
            socket, "new-session", "-d", "-s", "recovery", "-n", "lincoln",
            "-c", str(main), "bash --noprofile --norc",
        )
        manager = _tmux(
            socket, "display-message", "-p", "-t", "recovery:lincoln",
            "#{pane_id}\t#{pane_pid}",
        ).stdout.strip().split("\t")
        manager_pane, manager_pid = manager[0], int(manager[1])
        _tmux(socket, "set-option", "-p", "-t", manager_pane, "@sable_role", "lincoln")
        _tmux(socket, "set-option", "-p", "-t", manager_pane, "@sable_status", "running")
        _tmux(socket, "set-option", "-p", "-t", manager_pane, "@sable_repo", str(main))

        _tmux(
            socket, "new-window", "-d", "-t", "recovery", "-n", "worker",
            "-c", str(worker_path), "bash --noprofile --norc",
        )
        worker_pane = _tmux(
            socket, "display-message", "-p", "-t", "recovery:worker",
            "#{pane_id}",
        ).stdout.strip()
        for option, value in (
            ("@sable_role", "worker"),
            ("@sable_bead", bead),
            ("@sable_status", "running"),
            ("@sable_lane", "optimus"),
            ("@sable_repo", str(main)),
        ):
            _tmux(socket, "set-option", "-p", "-t", worker_pane, option, value)

        before = _run_production_recover(main, home, store, socket)
        assert before["recovery"] == {
            "observed_mode": "tmux-reachable",
            "tmux_socket": socket,
            "pane_source": "tmux",
            "missing_facts": [],
        }
        before_worker = _binding(before, bead)
        assert before_worker["lane"] == "optimus"
        assert before_worker["pane"] == worker_pane
        assert before_worker["session"] == "recovery"
        assert before_worker["repo_in_worktree_census"] is True

        # Involuntary manager end: SIGKILL, never a graceful quit.
        os.kill(manager_pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            listed = _tmux(
                socket, "list-panes", "-a", "-F", "#{pane_id}", check=False,
            ).stdout.splitlines()
            if manager_pane not in listed:
                break
            time.sleep(0.05)
        assert manager_pane not in listed, "SIGKILLed manager pane remained live"

        manager_clear = _run_production_recover(main, home, store, socket)
        assert manager_clear["recovery"]["observed_mode"] == "tmux-reachable"
        assert manager_clear["recovery"]["tmux_socket"] == socket
        assert manager_clear["worktrees"] == before["worktrees"]
        assert manager_clear["in_progress_beads"] == before["in_progress_beads"]
        assert _binding(manager_clear, bead) == before_worker

        # Whole-host analogue: the configured socket is now unreachable.
        _tmux(socket, "kill-server")
        host_crash = _run_production_recover(main, home, store, socket)
        assert host_crash["recovery"]["observed_mode"] == "tmux-unreachable"
        assert host_crash["recovery"]["tmux_socket"] == socket
        assert host_crash["recovery"]["missing_facts"] == [
            "owning lane", "pane/bead binding", "tmux session identity",
        ]
        assert _durable_worktree_picture(host_crash) == _durable_worktree_picture(before)
        assert host_crash["in_progress_beads"] == before["in_progress_beads"]
        assert host_crash["pane_bindings"] == []
        crashed_worker = next(
            row for row in host_crash["worktrees"] if row["path"] == str(worker_path)
        )
        assert crashed_worker["bead"] is None
        assert crashed_worker["bead_source"] is None
    finally:
        _tmux(socket, "kill-server", check=False)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
