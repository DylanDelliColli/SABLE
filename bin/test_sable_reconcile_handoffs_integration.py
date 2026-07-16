#!/usr/bin/env python3
"""Integration rehearsals for bin/sable-reconcile-handoffs (SABLE-jfg6.3 / D3).

Real end-to-end: scratch `origin.git` + a working clone (the merge-gate
origin.git+work fixture shape) AND a REAL sandbox beads DB (`bd init
--non-interactive` under a throwaway HOME). Nothing is mocked — the reconciler
runs as a subprocess, queries origin with real git and the beads DB with real
`bd`, and files its for-chuck handoff with a real `bd create --sandbox`.

What these pin (the contract's acceptance):
  * injected NOTIFY-MISS — a worker-shaped push (branch on origin, work bead
    closed, NO for-chuck handoff) is caught within ONE invocation.
  * run TWICE => exactly ONE for-chuck bead (idempotency: the run-1 bead names
    the branch, so run-2 finds it via predicate 3 and skips).
  * the `.sable integrationBranch=<name>` file drives ancestry through the reused
    SABLE-dtp1 resolver — a branch merged INTO that named branch reads as merged
    and is NOT reconciled.
  * --dry-run files ZERO.
  * BOUNDARIES: the reconciler never advances the integration branch and never
    deletes the worker branch — it files beads only.

Fixture discipline: sandbox bd (own HOME, own DB), hermetic + headless (env
leaks stripped, BD_NON_INTERACTIVE), all git ops on tmp_path scratch repos
(tripwire-clean: no real-repo mutation).
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent / "sable-reconcile-handoffs"
TIMER_BIN = Path(__file__).resolve().parent / "sable-reconcile-timer"
BASE = "trunk"

# The ci-verify clean-room is tmux+pytest only — no bd/dolt by design. These
# rehearsals drive a REAL sandbox beads DB (`bd init` under a throwaway HOME),
# so the whole module self-skips when bd is absent, matching the
# bd/dolt-suites-self-skip contract stated in ci-verify.yml. (Passed reviewer-run
# because the dev env HAS bd; this guard is what keeps the clean-room gate green.)
HAVE_BD = shutil.which("bd") is not None
pytestmark = pytest.mark.skipif(
    not HAVE_BD,
    reason="ci-verify clean-room has no bd/dolt by design; real-bd integration self-skips",
)

# env this test's HOME/bd must not inherit from the developer session
_ENV_LEAKS = (
    "SABLE_RC_REPO", "SABLE_RC_REMOTE", "SABLE_RC_GIT", "SABLE_RC_BD",
    "SABLE_RECONCILE_AGE_MIN", "SABLE_INTEGRATION_BRANCH", "SABLE_BASE_BRANCH",
    "CLAUDE_AGENT_NAME", "TMUX_PANE",
    "SABLE_RECONCILE_REPO", "SABLE_RECONCILE_INTERVAL_MIN",
)

# a committer date old enough that push-age >> the 10-minute settle threshold
_OLD_DATE = "2001-01-01T00:00:00 +0000"


def _env(home):
    env = {k: v for k, v in os.environ.items() if k not in _ENV_LEAKS}
    env["HOME"] = str(home)
    env["BD_NON_INTERACTIVE"] = "1"
    env["CI"] = "true"
    return env


def _run(argv, cwd, home, extra_env=None):
    env = _env(home)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(argv, cwd=str(cwd), env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)


def _git(cwd, *args, check=True, extra_env=None):
    env = None
    if extra_env:
        env = dict(os.environ)
        env.update(extra_env)
    cp = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                        cwd=str(cwd), env=env, text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if check and cp.returncode != 0:
        raise AssertionError(f"git {args} failed: {cp.stdout}")
    return cp.stdout.strip()


def _bd(cwd, home, *args, check=True):
    cp = _run(["bd", *args], cwd, home)
    if check and cp.returncode != 0:
        raise AssertionError(f"bd {args} failed: {cp.stdout}")
    return cp


def _robust_bd_init(work, home):
    """`bd init` on the embedded-Dolt backend can leave a PARTIAL database on a
    first-run repo_state.json race (rc 0 but no .beads/config.yaml, and stray
    databases) — the exact flakiness the fleet hits cold. A clean init always
    writes config.yaml, so gate success on that artifact and wipe+retry the
    half-init rather than run the whole test against a broken DB."""
    import shutil
    beads = work / ".beads"
    last = None
    for _ in range(4):
        if beads.exists():
            shutil.rmtree(beads)
        last = _run(["bd", "init", "--non-interactive"], work, home)
        if last.returncode == 0 and (beads / "config.yaml").is_file():
            return last
    raise AssertionError(f"bd init never produced a clean DB: {last.stdout if last else '<none>'}")


def _bare_git(origin, *args, check=True):
    cp = subprocess.run(["git", "--git-dir=" + str(origin), *args], text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if check and cp.returncode != 0:
        raise AssertionError(f"git {args} on origin failed: {cp.stdout}")
    return cp.stdout.strip()


# --------------------------------------------------------------------------
# Fixture: origin.git + work clone with a .sable integration-branch contract,
# a real sandbox beads DB, and a worker branch that models a completed-but-
# unhanded-off push.
# --------------------------------------------------------------------------

def _setup(tmp_path, *, integration_branch=BASE):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    home = tmp_path / "home"
    home.mkdir()

    _git(tmp_path, "init", "--bare", "-b", BASE, str(origin))
    _git(tmp_path, "clone", str(origin), str(work))

    # Fixture hygiene for a REAL bd on the embedded-Dolt backend (learned the
    # hard way): (1) NEVER let git track the .beads store — a `git add -A` stages
    # it, and a later branch switch then DELETES .beads/config.yaml and corrupts
    # the embedded Dolt DB; exclude it and stage only named files below.
    # (2) point core.hooksPath at an empty dir so bd's installed git hooks
    # (pre-push/post-checkout) never fire mid-fixture and race the same store.
    exclude = work / ".git" / "info" / "exclude"
    exclude.write_text(exclude.read_text() + ".beads/\n")
    nohooks = tmp_path / "nohooks"
    nohooks.mkdir()
    _git(work, "config", "--local", "core.hooksPath", str(nohooks))

    # the .sable contract the SABLE-dtp1 resolver reads to pick the integration branch
    (work / ".sable").write_text(f"integrationBranch={integration_branch}\n")
    (work / "README.md").write_text("base\n")
    _git(work, "add", "README.md", ".sable")
    _git(work, "commit", "-m", "init")
    _git(work, "push", "origin", BASE)

    # a real sandbox beads DB in the work tree
    _robust_bd_init(work, home)
    return origin, work, home


def _make_work_bead(work, home, *, status="closed"):
    """Create a work bead (real, --sandbox), optionally close it, and return the
    id. The worker branch is named `wk-<id>` so the reconciler resolves the bead
    via the embedded-id path (parse_bead_id -> bd show)."""
    cp = _bd(work, home, "create", "--sandbox", "--json",
             "--title", "worker unit of work", "--type=task", "--priority=2")
    bead_id = json.loads(cp.stdout)["id"]
    if status == "closed":
        _bd(work, home, "close", bead_id, "--sandbox")
    return bead_id


def _push_worker_branch(work, bead_id, *, merged_into=None):
    """Create + push a worker branch `wk-<bead_id>` with a backdated commit
    (old push-age). If merged_into is set, also fast-forward that branch to the
    worker tip and push it (models an already-merged branch)."""
    branch = f"wk-{bead_id}"
    feat = f"{bead_id}.txt"
    _git(work, "checkout", "-b", branch, BASE)
    (work / feat).write_text("worker feature\n")
    _git(work, "add", feat)  # named file only — NEVER -A (would stage .beads)
    _git(work, "commit", "-m", "worker feature",
         extra_env={"GIT_COMMITTER_DATE": _OLD_DATE, "GIT_AUTHOR_DATE": _OLD_DATE})
    _git(work, "push", "origin", branch)
    if merged_into:
        _git(work, "checkout", merged_into)
        _git(work, "merge", "--ff-only", branch)
        _git(work, "push", "origin", merged_into)
    _git(work, "checkout", BASE)
    return branch


def _reconcile(work, home, *, dry_run=False):
    argv = [sys.executable, str(BIN), "--repo", str(work), "--remote", "origin"]
    if dry_run:
        argv.append("--dry-run")
    return _run(argv, work, home)


def _reconcile_via_timer(work, home, *, repo_via="cli", dry_run=False, extra_env=None):
    """Drive the SABLE-jfg6.5 timer entrypoint's --once mode instead of calling
    sable-reconcile-handoffs directly — the S5 walk-away path. `repo_via`
    selects which of the timer's repo-resolution legs supplies the repo
    ('cli' -> --repo, 'env' -> $SABLE_RECONCILE_REPO); either way the process
    runs with NO tmux server and NO live pane (the whole _env() helper already
    strips TMUX_PANE / CLAUDE_AGENT_NAME for every subprocess in this module)."""
    argv = [sys.executable, str(TIMER_BIN), "--once", "--remote", "origin"]
    env = dict(extra_env or {})
    if repo_via == "cli":
        argv += ["--repo", str(work)]
    elif repo_via == "env":
        env["SABLE_RECONCILE_REPO"] = str(work)
    else:
        raise ValueError(repo_via)
    if dry_run:
        argv.append("--dry-run")
    return _run(argv, work, home, extra_env=env)


def _for_chuck_beads(work, home):
    cp = _bd(work, home, "list", "--status", "open,in_progress",
             "--label", "for-chuck", "--json", check=False)
    try:
        d = json.loads(cp.stdout)
    except (json.JSONDecodeError, ValueError):
        return []
    return d if isinstance(d, list) else []


def _origin_has_branch(origin, branch):
    out = _bare_git(origin, "for-each-ref", "--format=%(refname)",
                    f"refs/heads/{branch}", check=False)
    return bool(out.strip())


# ===========================================================================
# S1-E1: injected notify-miss caught in ONE invocation + run-twice idempotency
# ===========================================================================

def test_notify_miss_caught_in_one_invocation_and_idempotent(tmp_path):
    origin, work, home = _setup(tmp_path)
    bead_id = _make_work_bead(work, home, status="closed")
    branch = _push_worker_branch(work, bead_id)
    base_before = _bare_git(origin, "rev-parse", "refs/heads/" + BASE)

    # NO for-chuck bead exists yet (the notify-miss); one invocation must catch it
    assert _for_chuck_beads(work, home) == []

    r1 = _reconcile(work, home)
    assert r1.returncode == 0, r1.stdout
    beads1 = _for_chuck_beads(work, home)
    assert len(beads1) == 1, f"expected exactly one for-chuck bead, got {beads1}\n{r1.stdout}"
    assert branch in beads1[0]["title"], beads1[0]["title"]
    assert "for-chuck" in beads1[0].get("labels", [])

    # run TWICE -> still exactly one (predicate-3 idempotency)
    r2 = _reconcile(work, home)
    assert r2.returncode == 0, r2.stdout
    beads2 = _for_chuck_beads(work, home)
    assert len(beads2) == 1, f"re-run filed a duplicate: {beads2}\n{r2.stdout}"
    assert beads2[0]["id"] == beads1[0]["id"]

    # BOUNDARIES: the integration branch never advanced and the worker branch
    # was never deleted — this floor files beads only.
    assert _bare_git(origin, "rev-parse", "refs/heads/" + BASE) == base_before, \
        "reconciler advanced the integration branch"
    assert _origin_has_branch(origin, branch), "reconciler deleted the worker branch"


# ===========================================================================
# S1-E2: --dry-run classifies + prints but files ZERO
# ===========================================================================

def test_dry_run_files_zero(tmp_path):
    origin, work, home = _setup(tmp_path)
    bead_id = _make_work_bead(work, home, status="closed")
    branch = _push_worker_branch(work, bead_id)

    r = _reconcile(work, home, dry_run=True)
    assert r.returncode == 0, r.stdout
    assert "DRY-RUN" in r.stdout
    assert branch in r.stdout  # it classified the branch as stranded...
    assert _for_chuck_beads(work, home) == [], "dry-run filed a bead"


# ===========================================================================
# S1-E3: the .sable resolver drives ancestry — a branch merged into the
# resolved integration branch reads as merged and is NOT reconciled.
# ===========================================================================

def test_sable_resolver_drives_ancestry_merged_branch_not_filed(tmp_path):
    origin, work, home = _setup(tmp_path, integration_branch=BASE)
    bead_id = _make_work_bead(work, home, status="closed")
    # push the worker AND fast-forward trunk (the .sable-named branch) to it:
    # origin/wk-<id> is now an ancestor of origin/trunk -> merged.
    _push_worker_branch(work, bead_id, merged_into=BASE)

    r = _reconcile(work, home)
    assert r.returncode == 0, r.stdout
    assert _for_chuck_beads(work, home) == [], \
        "a branch merged into the .sable integration branch must not be reconciled"


# ===========================================================================
# S1-E3b: a still-open work bead is NOT merge work — no reconciliation even
# though the branch is unmerged and un-handed-off.
# ===========================================================================

def test_open_work_bead_not_reconciled(tmp_path):
    origin, work, home = _setup(tmp_path)
    bead_id = _make_work_bead(work, home, status="open")  # worker NOT done
    _push_worker_branch(work, bead_id)

    r = _reconcile(work, home)
    assert r.returncode == 0, r.stdout
    assert _for_chuck_beads(work, home) == [], \
        "an open (unfinished) work bead must not trigger a merge handoff"


# ===========================================================================
# S5-U1: the timer entrypoint (sable-reconcile-timer --once), invoked with NO
# tmux server and NO live panes, still scans and files a notify-miss handoff.
# SABLE-jfg6.5 / D3 TIMER LEG.
# ===========================================================================

def test_S5_U1_timer_once_catches_notify_miss_without_tmux_or_pane_context(tmp_path):
    origin, work, home = _setup(tmp_path)
    bead_id = _make_work_bead(work, home, status="closed")
    branch = _push_worker_branch(work, bead_id)
    assert _for_chuck_beads(work, home) == []

    # --repo leg of the resolution chain
    r = _reconcile_via_timer(work, home, repo_via="cli")
    assert r.returncode == 0, r.stdout
    beads = _for_chuck_beads(work, home)
    assert len(beads) == 1, f"expected exactly one for-chuck bead, got {beads}\n{r.stdout}"
    assert branch in beads[0]["title"], beads[0]["title"]


def test_S5_U1b_timer_once_resolves_repo_via_env_not_pane_context(tmp_path):
    # $SABLE_RECONCILE_REPO leg of the resolution chain — still no --repo flag,
    # no TMUX_PANE, no pane-shaped fallback to the caller's cwd.
    origin, work, home = _setup(tmp_path)
    bead_id = _make_work_bead(work, home, status="closed")
    branch = _push_worker_branch(work, bead_id)

    r = _reconcile_via_timer(work, home, repo_via="env")
    assert r.returncode == 0, r.stdout
    beads = _for_chuck_beads(work, home)
    assert len(beads) == 1, f"expected exactly one for-chuck bead via env resolution, got {beads}\n{r.stdout}"
    assert branch in beads[0]["title"], beads[0]["title"]


# ===========================================================================
# S5-E1: walk-away simulation. The push-based notify hook is never wired (it
# never runs anywhere in this fixture — modeling the operator having walked
# away with every manager/worker pane asleep). MULTIPLE stranded branches
# accumulate; the timer entrypoint ALONE, in a SINGLE cadence firing (one
# --once invocation), catches every one of them — the automated stand-in that
# clears bldh.7's walk-away blocker.
# ===========================================================================

def test_S5_E1_walkaway_simulation_timer_alone_catches_all_strands_in_one_cadence(tmp_path):
    origin, work, home = _setup(tmp_path)

    bead_a = _make_work_bead(work, home, status="closed")
    branch_a = _push_worker_branch(work, bead_a)
    bead_b = _make_work_bead(work, home, status="closed")
    branch_b = _push_worker_branch(work, bead_b)
    # a third branch that must NOT be caught: work still open (worker not done)
    bead_open = _make_work_bead(work, home, status="open")
    branch_open = _push_worker_branch(work, bead_open)

    # notify unwired the whole way through this fixture: no hook process ever
    # ran, so there is NOTHING on record before the timer's single sweep.
    assert _for_chuck_beads(work, home) == []

    r = _reconcile_via_timer(work, home, repo_via="cli")
    assert r.returncode == 0, r.stdout

    beads = _for_chuck_beads(work, home)
    titles = [b["title"] for b in beads]
    assert len(beads) == 2, f"expected exactly the two closed-work strands, got {beads}\n{r.stdout}"
    assert any(branch_a in t for t in titles), titles
    assert any(branch_b in t for t in titles), titles
    assert not any(branch_open in t for t in titles), \
        "the open-work branch must not be swept up by the walk-away catch-all"

    # BOUNDARIES hold even through the timer wrapper: files beads only.
    assert _origin_has_branch(origin, branch_a) and _origin_has_branch(origin, branch_b)
