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
def _ls_remote(repo, branch):
    """The sha origin ACTUALLY holds for `branch`, or "" — asked of the remote,
    not of a remote-tracking ref, because a stale local ref is exactly what a
    "did --fix push this?" assertion must not be able to read."""
    out = _git(repo, "ls-remote", "origin", branch).stdout.strip()
    return out.split("\t")[0] if out else ""


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
# ============================================================================
# SABLE-su3j3 — DIVERGED, built with real git rather than asserted into being.
# ============================================================================

def _empty_state(tmp_path, frozen_text="# nothing frozen\n"):
    """The two captured-fixture inputs plus a real frozen-branch file, so a scene
    that is not about freezes produces no freeze warnings to read past."""
    beads_file = tmp_path / "beads.json"
    beads_file.write_text("[]")
    panes_file = tmp_path / "panes.txt"
    panes_file.write_text("")
    frozen_file = tmp_path / "frozen.txt"
    frozen_file.write_text(frozen_text)
    return beads_file, panes_file, frozen_file


def _build_divergence_scene(tmp_path):
    """A REAL diverged branch and a REAL merely-ahead one, in one repo.

    Divergence cannot be faked by writing `behind: 1` into a fixture — that is
    the state this bug hid inside, so it is built out of actual commits:

      wk-diverged — pushed, then a PEER CLONE pushed a commit on top, then this
                    side committed without ever reconciling. Both sides now hold
                    a commit the other lacks: 1 ahead / 1 behind.
      wk-ahead    — pushed, then committed on top and left alone. origin's tip is
                    a strict ancestor: 1 ahead / 0 behind. The negative control,
                    without which "never propose a push" passes by doing nothing.
    """
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

    def commit(path, text, msg):
        (path / "f.txt").write_text(text)
        _git(path, "add", "-A")
        _git(path, "commit", "-m", msg)

    # --- wk-diverged
    diverged = add_worktree("wk-diverged")
    commit(diverged, "A\n", "A — shared")
    _git(diverged, "push", "-u", "origin", "wk-diverged")

    # a peer clone advances origin behind this worktree's back
    peer = tmp_path / "peer"
    subprocess.run(["git", "clone", str(origin), str(peer)],
                   capture_output=True, check=True)
    _git(peer, "config", "user.email", "p@p")
    _git(peer, "config", "user.name", "p")
    _git(peer, "checkout", "wk-diverged")
    commit(peer, "A then R\n", "R — only on origin")
    _git(peer, "push", "origin", "wk-diverged")

    # this side learns the remote moved, and commits anyway without reconciling
    _git(main, "fetch", "origin")
    commit(diverged, "A then B\n", "B — only local")

    # --- wk-ahead (control): origin stays a strict ancestor
    ahead = add_worktree("wk-ahead")
    commit(ahead, "A\n", "A")
    _git(ahead, "push", "-u", "origin", "wk-ahead")
    commit(ahead, "A then B\n", "B — local only, fast-forwardable")

    return main, diverged, ahead


def test_real_diverged_worktree_is_reported_diverged_not_ahead(tmp_path):
    main, diverged, ahead = _build_divergence_scene(tmp_path)
    beads_file, panes_file, frozen_file = _empty_state(tmp_path)

    # the fixture proves itself first: assert the scene really is diverged, with
    # the same two commands the bead used to measure the live instance.
    counts = _git(main, "rev-list", "--left-right", "--count",
                  "refs/remotes/origin/wk-diverged...refs/heads/wk-diverged")
    assert counts.stdout.split() == ["1", "1"], counts.stdout
    anc = _git(main, "merge-base", "--is-ancestor",
               "refs/remotes/origin/wk-diverged", "refs/heads/wk-diverged",
               check=False)
    assert anc.returncode != 0, "origin must NOT be an ancestor — scene is not diverged"
    # and that the control genuinely IS fast-forwardable
    anc_ok = _git(main, "merge-base", "--is-ancestor",
                  "refs/remotes/origin/wk-ahead", "refs/heads/wk-ahead", check=False)
    assert anc_ok.returncode == 0

    r = _run_recover(main, beads_file, panes_file,
                     "--frozen-file", str(frozen_file), "--json")
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout)
    rows = {row["branch"]: row for row in report["worktrees"]}

    assert rows["wk-diverged"]["push_state"] == "diverged"
    assert (rows["wk-diverged"]["ahead"], rows["wk-diverged"]["behind"]) == (1, 1)

    # NEGATIVE CONTROL: the fix did not simply stop classifying anything AHEAD
    assert rows["wk-ahead"]["push_state"] == "ahead"
    assert (rows["wk-ahead"]["ahead"], rows["wk-ahead"]["behind"]) == (1, 0)

    pushes = [s["target"] for s in report["plan"] if s["action"] == "push"]
    assert "wk-diverged" not in pushes
    assert "wk-ahead" in pushes          # ...and still recommends the real one

    div_review = [s for s in report["plan"]
                  if s["action"] == "review" and s["target"] == "wk-diverged"]
    assert len(div_review) == 1
    assert "1 ahead" in div_review[0]["detail"]
    assert "1 behind" in div_review[0]["detail"]
    assert "REJECTED" in div_review[0]["detail"]


def test_real_fix_run_pushes_the_ahead_branch_and_never_the_diverged_one(tmp_path):
    """The acceptance criterion `--fix` must satisfy, measured at origin itself."""
    main, diverged, ahead = _build_divergence_scene(tmp_path)
    beads_file, panes_file, frozen_file = _empty_state(tmp_path)

    before_div = _ls_remote(main, "wk-diverged")
    before_ahead = _ls_remote(main, "wk-ahead")
    local_ahead = _git(ahead, "rev-parse", "HEAD").stdout.strip()
    assert before_ahead != local_ahead   # there is genuinely something to push

    r = _run_recover(main, beads_file, panes_file,
                     "--frozen-file", str(frozen_file), "--fix", "--json")
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout)

    assert "wk-diverged" not in report["fix"]["pushed"]
    assert "wk-diverged" not in report["fix"]["failed"]   # never even attempted
    assert "wk-ahead" in report["fix"]["pushed"]

    # origin is the witness, not the report
    assert _ls_remote(main, "wk-diverged") == before_div      # UNCHANGED
    assert _ls_remote(main, "wk-ahead") == local_ahead        # really pushed


# ============================================================================
# SABLE-xgb29 — a real freeze, read from a real file, honoured by a real --fix.
# ============================================================================

def _build_freeze_scene(tmp_path):
    """Two branches that are IDENTICAL in git's eyes — both pushed, both with one
    unpushed commit on top, both clean. The only difference between them is a
    line in a file. That is the point: freeze is not a git property, so the
    control has to be indistinguishable except by the declaration."""
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

    paths = {}
    for branch in ("wk-frozen", "wk-notfrozen"):
        path = tmp_path / branch
        _git(main, "worktree", "add", "-b", branch, str(path), "main")
        _git(path, "config", "user.email", "t@t")
        _git(path, "config", "user.name", "t")
        (path / "f.txt").write_text("published\n")
        _git(path, "add", "-A")
        _git(path, "commit", "-m", "published")
        _git(path, "push", "-u", "origin", branch)
        (path / "f.txt").write_text("unpushed work\n")
        _git(path, "add", "-A")
        _git(path, "commit", "-m", "unpushed work")
        paths[branch] = path
    return main, paths


def test_real_frozen_branch_survives_a_fix_run(tmp_path):
    main, paths = _build_freeze_scene(tmp_path)
    beads_file = tmp_path / "beads.json"
    beads_file.write_text("[]")
    panes_file = tmp_path / "panes.txt"
    panes_file.write_text("")

    # a REAL freeze list, at the REAL default path inside the repo — so this
    # exercises the path resolution an operator gets, not only the test seam.
    state_dir = main / ".claude" / "sable" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "frozen-branches.txt").write_text(
        "# standing cockpit ruling\n"
        "wk-frozen  SABLE-wvxb4  do-not-merge, never force-push, retires unmerged\n"
    )

    before_frozen = _ls_remote(main, "wk-frozen")
    before_free = _ls_remote(main, "wk-notfrozen")
    local_frozen = _git(paths["wk-frozen"], "rev-parse", "HEAD").stdout.strip()
    local_free = _git(paths["wk-notfrozen"], "rev-parse", "HEAD").stdout.strip()
    # both genuinely have something to push — otherwise "unchanged" proves nothing
    assert before_frozen not in ("", local_frozen)
    assert before_free not in ("", local_free)

    r = _run_recover(main, beads_file, panes_file, "--fix", "--json")
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout)

    # no warnings: the list was found at the default path and parsed cleanly
    assert report["warnings"] == [], report["warnings"]

    assert report["fix"]["pushed"] == ["wk-notfrozen"]
    assert "wk-frozen" not in report["fix"]["failed"]

    # *** the assertion the bead asks for: origin's frozen ref is UNTOUCHED ***
    assert _ls_remote(main, "wk-frozen") == before_frozen
    # NEGATIVE CONTROL: --fix still does its job in the same run
    assert _ls_remote(main, "wk-notfrozen") == local_free

    # and the freeze is VISIBLE, citing the ruling that imposed it
    section = {f["branch"]: f for f in report["frozen_branches"]}
    assert section["wk-frozen"]["bead"] == "SABLE-wvxb4"
    assert "do-not-merge" in section["wk-frozen"]["reason"]

    out = _run_recover(main, beads_file, panes_file).stdout
    assert "FROZEN BRANCHES" in out
    assert "SABLE-wvxb4" in out


def test_absent_freeze_list_warns_loudly_on_stderr(tmp_path):
    """An absent list must never be silently equivalent to "nothing is frozen".
    Asserted on the real CLI's real stderr, because "loudly" is a property of
    the output stream, not of a return value."""
    main, _paths = _build_freeze_scene(tmp_path)
    beads_file = tmp_path / "beads.json"
    beads_file.write_text("[]")
    panes_file = tmp_path / "panes.txt"
    panes_file.write_text("")

    r = _run_recover(main, beads_file, panes_file, "--json")   # no list on disk
    assert r.returncode == 0
    assert "WARNING" in r.stderr
    assert "FROZEN-BRANCH LIST NOT FOUND" in r.stderr
    report = json.loads(r.stdout)
    assert report["warnings"] and "NOT FOUND" in report["warnings"][0]
    # stdout stays clean JSON for --json callers
    assert report["frozen_branches"] == []


def test_this_repos_freeze_list_is_tracked_not_merely_present(tmp_path):
    """*** READABILITY ON THIS BOX AND TRACKEDNESS ARE DIFFERENT FACTS AND THIS
    TEST EXISTS TO TELL THEM APART (SABLE-gb5lo). *** .gitignore ignores
    `.claude/sable/state/*`; an untracked freeze list would satisfy every other
    test here while being absent from every fresh checkout, every linked
    worktree, and every clone — which is precisely the post-crash situation the
    consumer runs in."""
    rel = ".claude/sable/state/frozen-branches.txt"
    assert (REPO / rel).exists(), "the freeze list is missing from the working tree"

    tracked = _git(REPO, "ls-files", "--error-unmatch", rel, check=False)
    assert tracked.returncode == 0, f"{rel} is NOT tracked by git: {tracked.stderr}"

    # NOT ignored either — the distinction between a .gitignore NEGATION and a
    # `git add -f` on a still-ignored path. Force-adding tracks THIS copy while
    # leaving the path ignored, so the next author's freeze silently never
    # enters the tree. `check-ignore -q` exits 0 for an ignored path (see the
    # sibling state files one level up, which correctly do) and 1 for this one.
    ignored = _git(REPO, "check-ignore", "-q", rel, check=False)
    assert ignored.returncode != 0, (
        f"{rel} is tracked but still IGNORED — it was force-added rather than "
        f"un-ignored, so future edits to the freeze list can vanish silently"
    )

    # the shipped list must itself parse without warnings — a malformed line in
    # the real file is a freeze nobody can lift on the record
    beads_file = tmp_path / "beads.json"
    beads_file.write_text("[]")
    panes_file = tmp_path / "panes.txt"
    panes_file.write_text("")
    r = subprocess.run(
        [sys.executable, str(RECOVER), "--repo", str(REPO), "--json",
         "--beads-file", str(beads_file), "--panes-file", str(panes_file),
         "--frozen-file", str(REPO / rel)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    report = json.loads(r.stdout)
    assert report["warnings"] == [], report["warnings"]

    # The report must name exactly what the file declares. Read the file
    # INDEPENDENTLY here rather than trusting the tool's own parse: a
    # "every entry cites a bead" loop over a list the tool returned empty is
    # vacuously true, which is how a broken parser passes a freeze-list test.
    # Deliberately NOT asserting the list is non-empty — lifting the last freeze
    # is a legitimate operator ruling, and a test forbidding it is a lock nobody
    # voted for. This compares against the file, so an empty file and an
    # empty-returning parser still cannot be confused.
    declared = [ln.split()[0] for ln in (REPO / rel).read_text().splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]
    assert sorted(e["branch"] for e in report["frozen_branches"]) == sorted(declared)

    for entry in report["frozen_branches"]:
        assert entry["bead"], f"{entry['branch']} cites no ruling bead"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
