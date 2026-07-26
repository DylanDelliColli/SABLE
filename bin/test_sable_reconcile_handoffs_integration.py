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
INSTALLER = Path(__file__).resolve().parent / "sable-orchestration-install"
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

# SABLE-4709h: live_pane_bead_ids() shells out to $SABLE_RC_TMUX (default
# "tmux"). This dev host runs a REAL SABLE fleet under a real tmux server —
# stripping TMUX_PANE above does NOT stop `tmux list-panes -a` from finding
# that server's default socket and enumerating its ACTUAL live panes, which
# would couple every rehearsal in this module to whatever real work happens
# to be running at test time. Point at a binary that cannot exist so the
# reconciler's _tmux() raises FileNotFoundError and live_pane_bead_ids fails
# open to an empty set — the exact, hermetic, always-"no live panes" behavior
# every pre-existing rehearsal here already assumes. The one rehearsal that
# WANTS a live pane (test_4709h_live_worker_pane_suppresses_filing) overrides
# this with its own stub tmux script.
_NO_TMUX = "sable-reconcile-tests-hermetic-no-such-tmux-binary"


def _env(home):
    env = {k: v for k, v in os.environ.items() if k not in _ENV_LEAKS}
    env["HOME"] = str(home)
    env["BD_NON_INTERACTIVE"] = "1"
    env["CI"] = "true"
    env["SABLE_RC_TMUX"] = _NO_TMUX
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

def _setup(tmp_path, *, integration_branch=BASE, home=None):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    home = (tmp_path / "home") if home is None else home
    home.mkdir(exist_ok=True)

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
    return _push_named_branch(work, f"wk-{bead_id}", tag=bead_id, merged_into=merged_into)


def _push_named_branch(work, branch, *, tag, merged_into=None):
    """Create + push a branch under an EXPLICIT name (not derived from a bead
    id) with a backdated commit (old push-age). Used by the SABLE-i5739
    metadata-resolution rehearsal, where the branch name deliberately embeds
    NO bead id and the bead's own title/description never mention it either —
    the only thing tying branch to bead is structured metadata."""
    feat = f"{tag}.txt"
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


def _make_work_bead_with_branch_metadata(work, home, branch, *, status="closed"):
    """Create a work bead whose title/description NEVER mention `branch` at
    all (unlike `_make_work_bead`, which relies on the embedded-id `wk-<id>`
    naming convention) and record the branch as STRUCTURED metadata — exactly
    what sable-spawn-worker's tag_branch_metadata writes at dispatch time
    (SABLE-i5739). Under the OLD prose-search resolver this bead would never
    be found for `branch` (mode a: 0 hits)."""
    cp = _bd(work, home, "create", "--sandbox", "--json",
             "--title", "unit of work with a title naming nothing branch-shaped",
             "--type=task", "--priority=2")
    bead_id = json.loads(cp.stdout)["id"]
    _bd(work, home, "update", bead_id, "--sandbox",
        "--set-metadata", f"branch={branch}")
    if status == "closed":
        _bd(work, home, "close", bead_id, "--sandbox")
    return bead_id


def _reconcile(work, home, *, dry_run=False, extra_env=None):
    argv = [sys.executable, str(BIN), "--repo", str(work), "--remote", "origin"]
    if dry_run:
        argv.append("--dry-run")
    return _run(argv, work, home, extra_env=extra_env)


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


# ===========================================================================
# SABLE-7oj5: systemd --user (and cron) run with NO shell PATH — bare 'bd' is
# a FileNotFoundError since this host's bd is nvm-managed, off the default
# PATH. The installer bakes an absolute SABLE_RC_BD into the generated unit;
# these pin that the REAL generated env line is what survives a
# systemd-shaped stripped PATH, and that the bug reproduces without it.
# ===========================================================================

def _generated_service_env(tmp_path, target_repo):
    """Run the real installer against a throwaway scope + this fixture's repo,
    and return the (SABLE_RC_BD, PATH) values it baked into the generated
    .service's Environment= lines."""
    scope = tmp_path / "install-scope"
    env = dict(os.environ)
    env["SABLE_PROJECT_DIR"] = str(scope)
    env["SABLE_RECONCILE_TARGET_REPO"] = str(target_repo)
    cp = subprocess.run([str(INSTALLER), "--project"], cwd=str(tmp_path), env=env,
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert cp.returncode == 0, cp.stdout
    svc = scope / ".claude" / "sable" / "reconcile-timer" / "sable-reconcile-timer.service"
    text = svc.read_text()
    bd_path = svc_path = None
    for line in text.splitlines():
        if line.startswith("Environment=SABLE_RC_BD="):
            bd_path = line.split("=", 2)[2]
        elif line.startswith("Environment=PATH="):
            svc_path = line[len("Environment=PATH="):]
    assert bd_path, f"generated .service has no SABLE_RC_BD env line:\n{text}"
    assert os.access(bd_path, os.X_OK), f"SABLE_RC_BD is not a real executable: {bd_path}"
    assert svc_path, f"generated .service has no fallback PATH env line:\n{text}"
    return bd_path, svc_path


def test_S7oj5_generated_unit_env_survives_systemd_shaped_stripped_path(tmp_path):
    origin, work, home = _setup(tmp_path)
    bead_id = _make_work_bead(work, home, status="closed")
    branch = _push_worker_branch(work, bead_id)
    assert _for_chuck_beads(work, home) == []

    bd_path, svc_path = _generated_service_env(tmp_path, work)

    # simulate the systemd --user (and cron) execution env this bead is about
    # — no shell rc, no nvm, no developer PATH — using EXACTLY the two
    # Environment= lines the real installer baked into the generated unit
    # (bd itself is a `#!/usr/bin/env node` script, so its own dir must be on
    # PATH too, not just resolvable via SABLE_RC_BD's absolute path).
    env = _env(home)
    env["PATH"] = svc_path
    env["SABLE_RC_BD"] = bd_path
    argv = [sys.executable, str(TIMER_BIN), "--once", "--repo", str(work), "--remote", "origin"]
    r = subprocess.run(argv, cwd=str(work), env=env, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    assert r.returncode == 0, r.stdout
    assert "FileNotFoundError" not in r.stdout, r.stdout
    beads = _for_chuck_beads(work, home)
    assert len(beads) == 1, f"expected exactly one for-chuck bead, got {beads}\n{r.stdout}"
    assert branch in beads[0]["title"], beads[0]["title"]


# ===========================================================================
# SABLE-6sdpx: reconcile()'s `git fetch --prune` refresh was check=False and
# never inspected — a real fetch failure (unreachable/misconfigured remote)
# was previously silent, classifying against stale refs with zero signal.
# Real subprocess, real (broken) remote, no mocks: assert the observable
# outcome is a loud warning plus a still-successful, non-crashing sweep — the
# conservative direction (best-effort continue + noise) rather than a swallow.
# ===========================================================================

def test_fetch_failure_against_unconfigured_remote_warns_but_still_sweeps(tmp_path):
    origin, work, home = _setup(tmp_path)
    bead_id = _make_work_bead(work, home, status="closed")
    _push_worker_branch(work, bead_id)

    # a remote name with nothing configured for it -> `git fetch garbage-remote
    # --prune` fails for real (unlike origin, which the fixture wires up).
    argv = [sys.executable, str(BIN), "--repo", str(work), "--remote", "garbage-remote"]
    r = _run(argv, work, home)
    assert r.returncode == 0, ("a failed refresh must not crash the sweep — "
                               f"best-effort continue is the conservative fallback:\n{r.stdout}")
    assert "WARNING" in r.stdout and "fetch" in r.stdout, (
        f"a real fetch failure must be loud, never silent:\n{r.stdout}"
    )


def test_S7oj5_without_sable_rc_bd_the_original_bug_reproduces(tmp_path):
    # Pins the FAILURE this bead fixes: bare 'bd' + a stripped systemd-shaped
    # PATH really does FileNotFoundError — proof the fix above (not some
    # unrelated PATH tweak) is what makes the sweep survive.
    origin, work, home = _setup(tmp_path)
    _make_work_bead(work, home, status="closed")

    env = _env(home)  # _ENV_LEAKS already strips any inherited SABLE_RC_BD
    env["PATH"] = "/usr/bin:/bin"
    argv = [sys.executable, str(TIMER_BIN), "--once", "--repo", str(work), "--remote", "origin"]
    r = subprocess.run(argv, cwd=str(work), env=env, text=True,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    assert r.returncode != 0, r.stdout
    assert "FileNotFoundError" in r.stdout, r.stdout


# ===========================================================================
# SABLE-i5739: STRUCTURED branch->bead resolution replaces prose `bd search`
# as the primary path. Real bd, real git, no mocks. Two-part acceptance:
#   1. a branch whose work bead's text NEVER names it (the OLD resolver's
#      mode-a silent failure: 0 hits, forever un-strandable) IS classified
#      stranded and gets a for-chuck bead filed — because the branch is
#      recorded on the bead as structured metadata, not found by prose.
#   2. filing an UNRELATED bead that happens to mention the branch name in
#      its own title afterward must NOT change the resolution/handoff on a
#      re-run — proving modes (b) wrong-success and (c) drift are closed,
#      not merely that resolution got luckier.
# ===========================================================================

def test_SABLE_i5739_branch_resolves_via_metadata_when_bead_text_never_names_it(tmp_path):
    origin, work, home = _setup(tmp_path)
    branch = "wk-totally-unrelated-slug"
    bead_id = _make_work_bead_with_branch_metadata(work, home, branch, status="closed")
    _push_named_branch(work, branch, tag=bead_id)

    # sanity: the branch name embeds no bead id, and the bead's title/desc
    # never mention the branch — the OLD prose-search resolver finds NOTHING
    # for this exact case (mode a).
    prose = _bd(work, home, "search", branch, "--status", "all", "--json", check=False)
    assert json.loads(prose.stdout) == [], (
        "fixture invariant broken: prose search must find zero hits for this "
        f"branch so the test actually exercises mode (a):\n{prose.stdout}")

    assert _for_chuck_beads(work, home) == []

    r1 = _reconcile(work, home)
    assert r1.returncode == 0, r1.stdout
    beads1 = _for_chuck_beads(work, home)
    assert len(beads1) == 1, (
        f"branch whose bead never names it must still be classified stranded "
        f"via structured metadata, got {beads1}\n{r1.stdout}")
    assert branch in beads1[0]["title"], beads1[0]["title"]

    # --- the acceptance criterion: file an UNRELATED bead that happens to
    # mention this exact branch name, then re-run. Under the OLD resolver
    # this bead would now WIN the prose search (mode b) and/or flip the
    # answer over time (mode c) on later runs as more such beads accumulate.
    _bd(work, home, "create", "--sandbox", "--title",
        f"a completely unrelated bead that mentions {branch} in passing",
        "--type=task", "--priority=2")

    r2 = _reconcile(work, home)
    assert r2.returncode == 0, r2.stdout
    beads2 = _for_chuck_beads(work, home)
    assert len(beads2) == 1, (
        f"an unrelated bead mentioning the branch must not change resolution "
        f"(mode b/c not closed): {beads2}\n{r2.stdout}")
    assert beads2[0]["id"] == beads1[0]["id"], (
        "re-run resolved to a DIFFERENT bead after an unrelated bead "
        f"mentioning the branch appeared — drift (mode c) is not closed: "
        f"{beads1} vs {beads2}")


# ===========================================================================
# SABLE-vif5e: open_for_chuck_beads' query-failure fallback previously folded
# a genuine bd failure into the SAME [] value as "no open handoffs yet" —
# predicate 3's suppression corpus — so a transient bd break during a sweep
# cadence would DUPLICATE a for-chuck bead that already exists. Real bd, real
# git, no mocks: break the REAL beads database out from under the reconciler
# subprocess (rename `.beads` away so bd's real workspace-discovery fails for
# real, matching bd's actual "no beads database found" exit), with a for-chuck
# bead ALREADY open for the branch, and assert the sweep does NOT create a
# duplicate — then restore bd and assert the sweep still correctly suppresses.
# ===========================================================================

def test_SABLE_vif5e_for_chuck_query_failure_does_not_duplicate_handoff(tmp_path):
    origin, work, home = _setup(tmp_path)
    bead_id = _make_work_bead(work, home, status="closed")
    branch = _push_worker_branch(work, bead_id)

    # establish the ALREADY-OPEN for-chuck handoff this sweep must not duplicate
    assert _for_chuck_beads(work, home) == []
    r0 = _reconcile(work, home)
    assert r0.returncode == 0, r0.stdout
    beads0 = _for_chuck_beads(work, home)
    assert len(beads0) == 1, f"fixture invariant broken: expected one pre-existing handoff, got {beads0}\n{r0.stdout}"
    original_id = beads0[0]["id"]

    # break the REAL beads database out from under the reconciler: rename
    # .beads away so `bd list --label for-chuck` genuinely cannot find a
    # workspace and exits nonzero — no stubbing, the real bd binary fails.
    beads_dir = work / ".beads"
    disabled_dir = work / ".beads.disabled"
    beads_dir.rename(disabled_dir)
    try:
        r1 = _reconcile(work, home)
    finally:
        disabled_dir.rename(beads_dir)

    assert r1.returncode == 0, (
        f"a for-chuck query failure must not crash the sweep — best-effort "
        f"continue is the conservative fallback:\n{r1.stdout}")
    assert "WARNING" in r1.stdout, (
        f"a real for-chuck query failure must be loud, never silent:\n{r1.stdout}")

    # the acceptance criterion: bd is restored now — assert NO duplicate was
    # created while it was broken.
    beads_after_break = _for_chuck_beads(work, home)
    assert len(beads_after_break) == 1, (
        f"a transient for-chuck query failure duplicated the handoff: "
        f"{beads_after_break}\n{r1.stdout}")
    assert beads_after_break[0]["id"] == original_id, (
        f"the surviving bead changed identity across the broken run: "
        f"{beads0} vs {beads_after_break}")

    # restore-and-recheck: with bd healthy again, the sweep still correctly
    # suppresses the already-on-record handoff (not just during breakage).
    r2 = _reconcile(work, home)
    assert r2.returncode == 0, r2.stdout
    beads_final = _for_chuck_beads(work, home)
    assert len(beads_final) == 1, (
        f"sweep failed to keep suppressing after bd was restored: "
        f"{beads_final}\n{r2.stdout}")
    assert beads_final[0]["id"] == original_id, beads_final


# ===========================================================================
# SABLE-jejx3: HELD is a first-class third outcome — real bd, real git, no mocks.
#
# The defect (OBSERVED at the merge seat, not theorised): a branch under an
# explicit do-not-merge hold satisfies all four stranded predicates identically
# to an accidentally-unmerged one, so the floor filed a handoff saying "nobody
# merged this — merge it", the EXACT INVERSE of the standing instruction, and
# re-filed it every cadence once Chuck closed it.
#
# The hold is now durable metadata on the WORK BEAD (never the branch name,
# never tmux traffic), so it outlives a pane restart AND a branch rename.
# Every rehearsal below carries its POSITIVE CONTROL: the same sweep, in the
# same run or immediately after, DOES file for an unheld branch — proving the
# sweep was capable of filing and the hold marker is what stopped it.
# ===========================================================================

def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _place_hold(work, home, bead_id, *, reason, by="tarzan",
                since="2026-07-21T00:00:00Z", until="tarzan green-lights a revised tip"):
    """Place a first-class hold on a REAL work bead via real `bd update
    --set-metadata` — the same command the module docstring documents for a
    manager at the merge seat."""
    args = ["update", bead_id, "--sandbox", "--set-metadata", f"hold={reason}"]
    if by is not None:
        args += ["--set-metadata", f"hold_by={by}"]
    if since is not None:
        args += ["--set-metadata", f"hold_since={since}"]
    if until is not None:
        args += ["--set-metadata", f"hold_until={until}"]
    _bd(work, home, *args)


def _lift_hold(work, home, bead_id):
    _bd(work, home, "update", bead_id, "--sandbox",
        "--unset-metadata", "hold", "--unset-metadata", "hold_by",
        "--unset-metadata", "hold_since", "--unset-metadata", "hold_until")


def test_jejx3_held_branch_files_no_handoff_and_is_still_named(tmp_path):
    """(i) NO for-chuck 'merge me' bead is filed for a held branch, and (ii) the
    sweep still NAMES it — a held branch is never silently invisible. Positive
    control at the end: lifting the hold makes the SAME sweep file, so the
    marker is doing the work and this cannot pass vacuously."""
    origin, work, home = _setup(tmp_path)
    branch = "wk-held-target"
    bead_id = _make_work_bead_with_branch_metadata(work, home, branch, status="closed")
    _push_named_branch(work, branch, tag="held")

    # BEFORE the hold: this is a textbook stranded branch (the inverted-handoff
    # case as it fired in production) — assert the sweep would file, in dry-run
    # so the corpus stays clean for the real assertion below.
    pre = _reconcile(work, home, dry_run=True)
    assert pre.returncode == 0, pre.stdout
    assert "STRANDED" in pre.stdout, (
        f"fixture invariant broken: the unheld branch must classify STRANDED "
        f"or the hold assertion below proves nothing:\n{pre.stdout}")

    _place_hold(work, home, bead_id,
                reason="false-negative security regression in the tree-claim gate")

    r1 = _reconcile(work, home)
    assert r1.returncode == 0, r1.stdout

    # (i) NOTHING filed — the inverted 'merge me' handoff is not manufactured
    assert _for_chuck_beads(work, home) == [], (
        f"a HELD branch must never produce a 'merge me' handoff:\n{r1.stdout}")

    # (ii) it is still NAMED, with all four fields, every cadence
    assert branch in r1.stdout, f"held branch went invisible:\n{r1.stdout}"
    assert "HELD" in r1.stdout, r1.stdout
    assert "by=tarzan" in r1.stdout, r1.stdout
    assert "until=tarzan green-lights a revised tip" in r1.stdout, r1.stdout
    assert "false-negative security regression" in r1.stdout, r1.stdout
    summary = [l for l in r1.stdout.splitlines()
               if l.startswith("sable-reconcile-handoffs:")][-1]
    assert "1 held branch(es)" in summary and branch in summary, summary

    # re-running does NOT accumulate anything either (the re-file loop that made
    # the original defect recurring rather than one-shot)
    r2 = _reconcile(work, home)
    assert r2.returncode == 0, r2.stdout
    assert _for_chuck_beads(work, home) == [], (
        f"a second cadence re-filed against a held branch:\n{r2.stdout}")

    # POSITIVE CONTROL: lift the hold; the SAME sweep now files exactly one
    # handoff for the SAME branch — proving the sweep could file all along.
    _lift_hold(work, home, bead_id)
    r3 = _reconcile(work, home)
    assert r3.returncode == 0, r3.stdout
    filed = _for_chuck_beads(work, home)
    assert len(filed) == 1, (
        f"positive control failed — an UNHELD stranded branch must still file: "
        f"{filed}\n{r3.stdout}")
    assert branch in filed[0]["title"], filed[0]["title"]


def test_jejx3_hold_survives_a_branch_rename(tmp_path):
    """The fourth destroyer of the gz3v2 bandage: it keyed suppression on the
    BRANCH NAME appearing in a bead title, so a rename or re-push under a new
    name silently dropped the protection with nothing logged. A hold keyed on
    the WORK BEAD travels with the work: re-point the bead's `branch` metadata
    and the hold still applies under the new name."""
    origin, work, home = _setup(tmp_path)
    old_branch = "wk-renamed-before"
    bead_id = _make_work_bead_with_branch_metadata(work, home, old_branch, status="closed")
    _push_named_branch(work, old_branch, tag="rename")
    _place_hold(work, home, bead_id, reason="rejected tip, revision inbound")

    r0 = _reconcile(work, home)
    assert r0.returncode == 0, r0.stdout
    assert _for_chuck_beads(work, home) == [], r0.stdout

    # RENAME: the work reappears on origin under a brand-new name, the old ref
    # is gone, and the work bead is re-pointed at it (the SABLE-i5739 join).
    new_branch = "wk-renamed-after"
    _git(work, "checkout", old_branch)
    _git(work, "branch", "-m", new_branch)
    _git(work, "push", "origin", new_branch)
    _git(work, "push", "origin", "--delete", old_branch)
    _git(work, "checkout", BASE)
    _bd(work, home, "update", bead_id, "--sandbox",
        "--set-metadata", f"branch={new_branch}")

    r1 = _reconcile(work, home)
    assert r1.returncode == 0, r1.stdout
    assert _for_chuck_beads(work, home) == [], (
        f"the hold did not survive the rename — the floor resumed filing the "
        f"inverted handoff against {new_branch}:\n{r1.stdout}")
    assert new_branch in r1.stdout and "HELD" in r1.stdout, r1.stdout

    # POSITIVE CONTROL, same run shape: a second, UNHELD branch in the same
    # sweep DOES file — the sweep was capable of filing while the renamed held
    # branch was correctly skipped.
    other = "wk-unheld-control"
    _make_work_bead_with_branch_metadata(work, home, other, status="closed")
    _push_named_branch(work, other, tag="control")
    r2 = _reconcile(work, home)
    assert r2.returncode == 0, r2.stdout
    filed = _for_chuck_beads(work, home)
    assert len(filed) == 1, f"positive control failed: {filed}\n{r2.stdout}"
    assert other in filed[0]["title"], filed[0]["title"]
    assert new_branch not in filed[0]["title"], filed[0]["title"]


def test_jejx3_stale_and_incomplete_holds_are_flagged_for_review(tmp_path):
    """A forgotten hold is SELF-SILENCING BY CONSTRUCTION — it suppresses the
    very report that would surface its branch — so age and missing fields must
    escalate into the summary, or 'held' decays into a permanent quiet veto."""
    origin, work, home = _setup(tmp_path)
    branch = "wk-stale-hold"
    bead_id = _make_work_bead_with_branch_metadata(work, home, branch, status="closed")
    _push_named_branch(work, branch, tag="stale")
    # placed long ago, by nobody, with no release condition
    _place_hold(work, home, bead_id, reason="reason lost to a pane restart",
                by=None, since="2001-01-01T00:00:00Z", until=None)

    r = _reconcile(work, home)
    assert r.returncode == 0, r.stdout
    assert _for_chuck_beads(work, home) == [], r.stdout
    assert "STALE(" in r.stdout, r.stdout
    assert "UNOWNED" in r.stdout, r.stdout
    assert "NO-RELEASE-CONDITION" in r.stdout, r.stdout
    summary = [l for l in r.stdout.splitlines()
               if l.startswith("sable-reconcile-handoffs:")][-1]
    assert "1 NEEDING REVIEW" in summary, summary

    # counterpart: a well-formed, fresh hold adds no review noise — so the
    # flag means something when it appears.
    _place_hold(work, home, bead_id, reason="rejected tip, revision inbound",
                since=_now_iso())
    r2 = _reconcile(work, home)
    assert r2.returncode == 0, r2.stdout
    summary2 = [l for l in r2.stdout.splitlines()
                if l.startswith("sable-reconcile-handoffs:")][-1]
    assert "NEEDING REVIEW" not in summary2, summary2
    assert "1 held branch(es)" in summary2, summary2


# ===========================================================================
# SABLE-5xz68 — MULTI-FLEET. This host runs more than one fleet, and the timer
# leg's generated units used to name exactly one repo (a hardcoded one at
# that). A floor that sweeps one fleet and silently ignores the others is the
# vacuous-green shape in infrastructure form: it looks installed, it fires on
# a cadence, and every other fleet's stranded pushes stay stranded.
# Real fixtures, real git, real bd — TWO independent fleets, ONE firing.
# ===========================================================================

def test_5xz68_one_firing_sweeps_every_fleet_on_a_multi_fleet_host(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    alpha = tmp_path / "alpha"; alpha.mkdir()
    beta = tmp_path / "beta"; beta.mkdir()
    _, work_a, _ = _setup(alpha, home=home)
    _, work_b, _ = _setup(beta, home=home)

    bead_a = _make_work_bead(work_a, home, status="closed")
    branch_a = _push_worker_branch(work_a, bead_a)
    bead_b = _make_work_bead(work_b, home, status="closed")
    branch_b = _push_worker_branch(work_b, bead_b)
    assert _for_chuck_beads(work_a, home) == []
    assert _for_chuck_beads(work_b, home) == []

    # ONE invocation, both fleets — the shape a systemd/cron unit generated by
    # sable-orchestration-install with SABLE_RECONCILE_TARGET_REPO=a:b fires.
    argv = [sys.executable, str(TIMER_BIN), "--once", "--remote", "origin",
            "--repo", str(work_a), "--repo", str(work_b)]
    r = _run(argv, tmp_path, home)
    assert r.returncode == 0, r.stdout

    a_beads = _for_chuck_beads(work_a, home)
    b_beads = _for_chuck_beads(work_b, home)
    assert len(a_beads) == 1, f"fleet alpha not reconciled: {a_beads}\n{r.stdout}"
    assert branch_a in a_beads[0]["title"], a_beads[0]["title"]
    assert len(b_beads) == 1, f"fleet beta not reconciled: {b_beads}\n{r.stdout}"
    assert branch_b in b_beads[0]["title"], b_beads[0]["title"]


def test_5xz68_a_broken_fleet_does_not_skip_the_healthy_one(tmp_path):
    """An unswept fleet is an unprotected fleet: a repo that blows up must not
    abort the firing before the remaining repos are swept, and the failure must
    still be visible in the exit code (not swallowed to keep the timer green)."""
    home = tmp_path / "home"
    home.mkdir()
    good = tmp_path / "good"; good.mkdir()
    _, work_good, _ = _setup(good, home=home)
    bead = _make_work_bead(work_good, home, status="closed")
    branch = _push_worker_branch(work_good, bead)

    broken = tmp_path / "does-not-exist"
    argv = [sys.executable, str(TIMER_BIN), "--once", "--remote", "origin",
            "--repo", str(broken), "--repo", str(work_good)]
    r = _run(argv, tmp_path, home)

    beads = _for_chuck_beads(work_good, home)
    assert len(beads) == 1, f"healthy fleet skipped after a broken one: {beads}\n{r.stdout}"
    assert branch in beads[0]["title"], beads[0]["title"]
    assert r.returncode != 0, f"the broken fleet's failure was swallowed:\n{r.stdout}"


# ===========================================================================
# SABLE-z7gue / SABLE-4709h: content-containment, real git + real bd.
#
# z7gue's live instance: a worker branch's tip was NOT an ancestor of the
# integration branch (a rebase changes every sha), yet its content had
# already landed at the spine under a DIFFERENT sha — tip-containment
# (predicate 1) cannot see this, and 'ask both lanes' can't either, because
# every lane truthfully answers 'not mine' when the work is already on the
# spine in nobody's hand. 4709h's own triage record names this as one of
# sixteen [RECONCILE] false-positive firings with zero demonstrated true
# positives across the shift — so this single real-git+real-bd test carries
# BOTH the missing known-positive control (a genuinely unlanded branch DOES
# still file) and the rebase-copy negative (a landed one does NOT), per the
# spec's "at least the rebase-copy negative" plus the true-positive
# requirement. Assertions key on the SPECIFIC branch names this test
# created (SABLE-jd5fj.15 attributable absence), never a global bead count.
# ===========================================================================

def _land_same_content_on_trunk(work, feat_name, content):
    """Commit `content` into `feat_name` directly on BASE and push — models a
    worker branch that was rebased and squash-merged onto the integration
    branch under a NEW sha, without its own branch ref ever being deleted.
    `git cherry` computes patch-id from the diff against a commit's own
    parent, so as long as BASE has not moved since the worker branch forked
    from it, this commit's patch is byte-identical to the worker branch's own
    commit — the exact rebase-copy relation z7gue is about — without this
    fixture needing to run an actual `git rebase`."""
    _git(work, "checkout", BASE)
    (work / feat_name).write_text(content)
    _git(work, "add", feat_name)
    _git(work, "commit", "-m", "worker feature (landed via squash/rebase onto trunk)")
    _git(work, "push", "origin", BASE)


def test_reconcile_against_real_git_and_real_bd(tmp_path):
    origin, work, home = _setup(tmp_path)

    # Leg 1 (true positive, the missing known-positive control): a worker
    # branch pushed, unmerged, work bead closed, no for-chuck bead, and
    # NOTHING landed anywhere else — must still file exactly one bead naming
    # it.
    stranded_bead = _make_work_bead(work, home, status="closed")
    stranded_branch = _push_worker_branch(work, stranded_bead)

    # Leg 2 (rebase-copy negative, SABLE-z7gue): a second worker branch,
    # pushed and unmerged by TIP (git merge-base --is-ancestor reads it as
    # not-an-ancestor), but its content is already on BASE under a different
    # commit/sha — the ref itself is never deleted, modeling the general
    # class this floor's predicate 1 gets wrong (z7gue's own instance had the
    # ref deleted too, but that is a DIFFERENT probe's failure — the ref
    # being live here is what actually exercises THIS reconciler's
    # list_origin_wk_branches + ancestry check against the fix).
    landed_bead = _make_work_bead(work, home, status="closed")
    landed_branch = _push_worker_branch(work, landed_bead)
    _land_same_content_on_trunk(work, f"{landed_bead}.txt", "worker feature\n")
    _git(work, "checkout", BASE)

    cp = _reconcile(work, home)
    assert cp.returncode == 0, cp.stdout
    assert f"{stranded_branch}: STRANDED" in cp.stdout, cp.stdout
    assert f"{landed_branch}: landed-under-different-sha" in cp.stdout, cp.stdout

    beads = _for_chuck_beads(work, home)
    titles = [b.get("title", "") for b in beads]
    assert any(stranded_branch in t for t in titles), \
        f"genuinely unlanded branch {stranded_branch} must file a for-chuck bead: {titles}"
    assert not any(landed_branch in t for t in titles), \
        f"rebased-and-landed branch {landed_branch} must file NO for-chuck bead: {titles}"
    assert len(beads) == 1, \
        f"exactly one bead expected (the genuine strand only): {titles}"


# ===========================================================================
# SABLE-rhsuj / SABLE-q0e3n — real git on disk AND a real sandbox beads DB.
# Nothing mocked on either side: real merges, real branch deletes, a real
# concurrent push moving the spine mid-sweep, real `bd create` / `bd close`.
#
# ASSERT BY ATTRIBUTABLE ABSENCE (SABLE-jd5fj.15): every assertion below names
# the SPECIFIC branch and bead ids this test created. The fleet files beads
# concurrently, so a global open-bead count is a flake generator, not a check.
#
# *** THE REPRODUCTION TRAP (q0e3n §4). Four manual sweeps across two lanes all
# self-reported "0 for-chuck beads filed", against one durable false filing
# from the unattended timer. The attended path is 4-for-4 clean. So A SWEEP RUN
# ON A QUIET REPO PASSES AGAINST THE UNFIXED CODE AND PROVES NOTHING — here
# that is the DEFAULT outcome, not an unlucky one. The race legs below
# therefore SIMULATE the interleaving with a git wrapper that performs a real
# landing at a chosen point inside the sweep, rather than running the tool and
# hoping. ***
# ===========================================================================

def _reconcile_bead_title(branch):
    """Mirror of sable-reconcile-handoffs' own reconcile_bead_title — kept
    literal here so a change to the filed title breaks this rehearsal loudly
    instead of silently matching nothing."""
    return f"[RECONCILE] stranded merge: {branch} — no for-chuck handoff on record"


def _bead_record(work, home, bead_id):
    cp = _bd(work, home, "show", bead_id, "--json", check=False)
    try:
        d = json.loads(cp.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(d, list):
        d = d[0] if d else None
    return d if isinstance(d, dict) else None


def _file_reconcile_bead(work, home, branch, *, branch_sha=None):
    """File a [RECONCILE] bead by hand, in exactly the shape the floor files
    them (same title, same labels, optionally the same branch_sha metadata) —
    so the auto-close leg is exercised against a REAL bd record rather than a
    dict a test author wrote."""
    cp = _bd(work, home, "create", "--sandbox", "--json",
             "--title", _reconcile_bead_title(branch),
             "--type=task", "--priority=2", "--labels=for-chuck,coord",
             "--description", f"Filed by the reconciliation floor.\n\nBranch: {branch}\n")
    bead_id = json.loads(cp.stdout)["id"]
    if branch_sha:
        _bd(work, home, "update", bead_id, "--sandbox",
            "--set-metadata", f"branch_sha={branch_sha}")
    return bead_id


def _land_branch_for_real(work, branch):
    """A REAL landing: merge the branch into BASE, push the spine, delete the
    branch from origin. Returns the landed sha. This is the correct action that
    silently retires a [RECONCILE] bead's premise (SABLE-rhsuj) — no event, no
    failure, no update to the bead."""
    sha = _git(work, "rev-parse", f"origin/{branch}")
    _git(work, "checkout", BASE)
    _git(work, "merge", "--no-ff", "-m", f"merge {branch}", branch)
    _git(work, "push", "origin", BASE)
    _git(work, "push", "origin", "--delete", branch)
    _git(work, "fetch", "origin", "--prune")
    return sha


def test_autoclose_against_real_git_and_real_bd(tmp_path):
    """THE integration named in rhsuj's spec, both polarities against real
    composition.

    LEG 1 (positive): a branch that really merged — its ref really deleted from
    origin afterwards, as a correct promote does — has its [RECONCILE] bead
    AUTO-CLOSED, with the landing sha in the close reason.

    LEG 2 (*** the plant-and-fail control ***): a branch whose origin ref is
    ALSO deleted but which was NEVER merged keeps its bead OPEN. The two
    branches are byte-identical from an absence probe's point of view — both
    gone from origin — so an implementation keyed on the branch being GONE
    closes both and this leg bites. Only the CONTENT tells them apart."""
    origin, work, home = _setup(tmp_path)

    landed_bead = _make_work_bead(work, home, status="closed")
    landed_branch = _push_worker_branch(work, landed_bead)
    stranded_bead = _make_work_bead(work, home, status="closed")
    stranded_branch = _push_worker_branch(work, stranded_bead)

    landed_sha = _git(work, "rev-parse", f"origin/{landed_branch}")
    stranded_sha = _git(work, "rev-parse", f"origin/{stranded_branch}")

    rb_landed = _file_reconcile_bead(work, home, landed_branch, branch_sha=landed_sha)
    rb_stranded = _file_reconcile_bead(work, home, stranded_branch, branch_sha=stranded_sha)

    # LEG 1's correct action: a real merge + push, then the ref really goes.
    _land_branch_for_real(work, landed_branch)
    # LEG 2's confound: the ref goes for a NON-merge reason (cleanup / bad push
    # / rename). The work never landed.
    _git(work, "push", "origin", "--delete", stranded_branch)
    _git(work, "fetch", "origin", "--prune")

    # Both branches are now equally ABSENT from origin — the whole point.
    assert not _origin_has_branch(origin, landed_branch)
    assert not _origin_has_branch(origin, stranded_branch)

    cp = _reconcile(work, home)
    assert cp.returncode == 0, cp.stdout

    landed_rec = _bead_record(work, home, rb_landed)
    stranded_rec = _bead_record(work, home, rb_stranded)
    assert landed_rec is not None and stranded_rec is not None

    assert landed_rec["status"] == "closed", \
        f"a bead whose branch is CONTAINED must be auto-closed: {landed_rec}\n{cp.stdout}"
    reason = json.dumps(landed_rec)
    assert landed_sha in reason, \
        f"the close reason must carry the landing SHA {landed_sha}: {reason}"

    assert stranded_rec["status"] != "closed", (
        "*** ABSENCE PROBE ***: a branch deleted from origin WITHOUT merging must "
        f"keep its bead OPEN — {rb_stranded} was closed anyway:\n{cp.stdout}")


def test_autoclose_leaves_an_unmerged_live_branch_alone(tmp_path):
    # The ordinary case, kept explicit so the auto-close cannot pass by simply
    # never closing anything: a live, unmerged branch's bead stays open AND the
    # sweep still reports it.
    origin, work, home = _setup(tmp_path)
    bead = _make_work_bead(work, home, status="closed")
    branch = _push_worker_branch(work, bead)
    sha = _git(work, "rev-parse", f"origin/{branch}")
    rb = _file_reconcile_bead(work, home, branch, branch_sha=sha)

    cp = _reconcile(work, home)
    assert cp.returncode == 0, cp.stdout
    rec = _bead_record(work, home, rb)
    assert rec["status"] != "closed", f"{rb} closed for a live unmerged branch:\n{cp.stdout}"


def test_filed_bead_carries_a_branch_sha_that_its_own_autoclose_can_probe(tmp_path):
    """END-TO-END for the rhsuj remedy: the floor FILES a handoff, the branch
    then really merges and its ref is really deleted, and the NEXT sweep
    retires the floor's own bead. Nobody looked at it. That whole chain is the
    fix, and it only works because the filed bead recorded the sha — after the
    ref is gone there is nothing else left to probe that is not an absence."""
    origin, work, home = _setup(tmp_path)
    bead = _make_work_bead(work, home, status="closed")
    branch = _push_worker_branch(work, bead)

    r1 = _reconcile(work, home)
    assert r1.returncode == 0, r1.stdout
    filed = [b for b in _for_chuck_beads(work, home) if branch in b.get("title", "")]
    assert len(filed) == 1, f"{branch} should have filed exactly one handoff: {r1.stdout}"
    rb = filed[0]["id"]

    rec = _bead_record(work, home, rb)
    recorded = (rec.get("metadata") or {}).get("branch_sha")
    assert recorded, f"the filed bead must record its branch sha: {rec}"

    landed_sha = _land_branch_for_real(work, branch)
    assert recorded == landed_sha, (recorded, landed_sha)

    r2 = _reconcile(work, home)
    assert r2.returncode == 0, r2.stdout
    rec2 = _bead_record(work, home, rb)
    assert rec2["status"] == "closed", \
        f"the floor's own bead must be retired once its branch lands:\n{r2.stdout}"
    assert landed_sha in json.dumps(rec2), rec2


# --- the race legs (q0e3n §4): SIMULATE the interleaving, never wait for it --

def _racing_git(tmp_path, *, trigger_arg, script_lines):
    """A real-git wrapper that performs a REAL landing at a chosen point inside
    the sweep. It forwards every call to the real git; the first time it sees
    `trigger_arg` in its argv it first runs `script_lines` (arbitrary real git
    against the same repos), then forwards. That is a genuine concurrent push
    moving the base mid-run — the same shape as a promote landing while the
    unattended timer sweeps, which is the only path that has ever produced the
    defect. Returns the wrapper path, for $SABLE_RC_GIT."""
    marker = tmp_path / "raced.marker"
    land = tmp_path / "land.sh"
    land.write_text("set -e\n" + "\n".join(script_lines) + "\n")
    wrapper = tmp_path / "git-racer"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        f'if [ ! -e "{marker}" ]; then\n'
        f'  for a in "$@"; do\n'
        f'    if [ "$a" = "{trigger_arg}" ]; then\n'
        f'      touch "{marker}"\n'
        f'      bash "{land}" >"{tmp_path}/land.log" 2>&1 || true\n'
        f"      break\n"
        f"    fi\n"
        f"  done\n"
        f"fi\n"
        'exec git "$@"\n'
    )
    wrapper.chmod(0o755)
    return wrapper, marker


def test_q0e3n_a_sweep_racing_a_spine_advancing_push_files_zero_beads(tmp_path):
    """*** THE kiem2 REHEARSAL. *** The landing happens AFTER the sweep's own
    `git fetch --prune` — the window in which the sweep's refs are stale and it
    classifies an already-landed branch as STRANDED. Unfixed, that is a DURABLE
    false work item in the merge seat's inbox naming a branch that no longer
    exists, i.e. an UNSATISFIABLE instruction whose evidence is self-erasing.

    Fixed, the pre-create containment re-verify re-fetches and refuses: a
    contained branch NEVER produces a stranded bead, whatever the
    classification said earlier in the sweep.

    Attributable absence: asserted about THIS branch's title only."""
    origin, work, home = _setup(tmp_path)
    bead = _make_work_bead(work, home, status="closed")
    branch = _push_worker_branch(work, bead)

    # A second clone is the "other process" (the promote) — the sweep's own
    # repo must not be mutated by the racer, or the race would be fictional.
    racer_clone = tmp_path / "promote"
    _git(tmp_path, "clone", str(origin), str(racer_clone))
    wrapper, marker = _racing_git(
        tmp_path,
        trigger_arg="for-each-ref",   # fires right AFTER reconcile's fetch --prune
        script_lines=[
            f'cd "{racer_clone}"',
            f"git -c user.email=t@t -c user.name=t checkout {BASE}",
            f"git -c user.email=t@t -c user.name=t merge --no-ff -m land origin/{branch}",
            f"git push origin {BASE}",
            f"git push origin --delete {branch}",
        ])

    cp = _reconcile(work, home, extra_env={"SABLE_RC_GIT": str(wrapper)})
    assert cp.returncode == 0, cp.stdout
    assert marker.exists(), f"the race never fired — this test proves nothing:\n{cp.stdout}"

    # GROUND TRUTH, checked at the object with the mandated presence probe
    # (never "the branch is gone"): the work really is contained in the spine.
    _git(work, "fetch", "origin", "--prune")
    branch_sha = _git(work, "rev-parse", f"{BASE}@{{u}}^2")
    _git(work, "merge-base", "--is-ancestor", branch_sha, f"origin/{BASE}")

    named = [b for b in _for_chuck_beads(work, home) if branch in b.get("title", "")]
    assert named == [], (
        f"a sweep racing a spine-advancing push must file ZERO for-chuck beads for "
        f"{branch} — it landed mid-sweep and the filed instruction would be "
        f"unsatisfiable (SABLE-kiem2):\n{cp.stdout}")


def test_q0e3n_a_merged_branch_is_never_reported_held_or_queued_under_a_race(tmp_path):
    """*** THE STRADDLE REHEARSAL. *** The measured incident: run 2 straddled a
    landing and reclassified three ALREADY-MERGED branches as HELD /
    QUEUED-AT-SEAT, and kicked previews for them. Here the local
    remote-tracking ref really moves UNDER the classification loop (a real
    `git fetch` from another process mid-sweep, which is what a promote's
    pre-push hook does), while a branch that merged BEFORE the sweep began is
    also under a real do-not-merge hold.

    A HELD label on an already-merged branch is incoherent AND self-
    perpetuating — held branches are re-reported every cadence by design. With
    containment pinned and decided ahead of the hold join, the merged branch
    can never reach the hold path at all."""
    origin, work, home = _setup(tmp_path)

    # merged BEFORE the sweep, and deliberately held — the incoherent pair
    merged_bead = _make_work_bead(work, home, status="closed")
    merged_branch = _push_worker_branch(work, merged_bead, merged_into=BASE)
    _place_hold(work, home, merged_bead, reason="do not merge, superseded")

    # THE CASE THAT ACTUALLY DECIDES THE ORDERING, and the one the branch above
    # cannot reach: unmerged BY TIP (a rebase changed every sha, so
    # `merge-base --is-ancestor` says not-an-ancestor) but fully CONTAINED BY
    # CONTENT — and also held. Tip-ancestry short-circuits the merged branch at
    # predicate 1 whatever the order is, so only this one proves the hold join
    # is gated on containment rather than merely losing to it.
    rebased_bead = _make_work_bead(work, home, status="closed")
    rebased_branch = _push_worker_branch(work, rebased_bead)
    _land_same_content_on_trunk(work, f"{rebased_bead}.txt", "worker feature\n")
    _git(work, "checkout", BASE)
    _place_hold(work, home, rebased_bead, reason="do not merge, superseded")

    # a genuinely unmerged branch, so the sweep still has real work to do
    live_bead = _make_work_bead(work, home, status="closed")
    live_branch = _push_worker_branch(work, live_bead)

    # the racer advances the spine on origin AND fetches into the SWEEP's repo,
    # so the tracking ref moves mid-loop exactly as a concurrent promote does
    racer_clone = tmp_path / "promote"
    _git(tmp_path, "clone", str(origin), str(racer_clone))
    (racer_clone / "spine.txt").write_text("spine advanced\n")
    wrapper, marker = _racing_git(
        tmp_path,
        trigger_arg="cherry",   # fires mid-classification, after branch 1
        script_lines=[
            f'cd "{racer_clone}"',
            f"git -c user.email=t@t -c user.name=t checkout {BASE}",
            "git -c user.email=t@t -c user.name=t add spine.txt",
            "git -c user.email=t@t -c user.name=t commit -m 'spine advanced mid-sweep'",
            f"git push origin {BASE}",
            f'cd "{work}"',
            "git fetch origin --prune",
        ])

    cp = _reconcile(work, home, extra_env={"SABLE_RC_GIT": str(wrapper)})
    assert cp.returncode == 0, cp.stdout
    assert marker.exists(), f"the race never fired — this test proves nothing:\n{cp.stdout}"

    out = cp.stdout
    assert f"HELD {merged_branch}" not in out, (
        f"an already-merged branch was labelled HELD under a straddling sweep — "
        f"incoherent, and held branches are re-reported every cadence:\n{out}")
    assert f"QUEUED {merged_branch}" not in out, (
        f"an already-merged branch was labelled QUEUED-AT-SEAT — that tells the "
        f"seat it owns work it does not:\n{out}")
    assert f"{merged_branch}: merged-or-unresolvable" in out, out
    assert f"{merged_branch}: preview-kick" not in out, \
        f"CI was burned on an already-merged branch:\n{out}"

    # ...and the rebase-landed one, which is where the ordering is load-bearing
    assert f"HELD {rebased_branch}" not in out, (
        f"a branch whose CONTENT is already at the spine was labelled HELD — the "
        f"hold join must be gated on containment, not merely lose to it:\n{out}")
    assert f"QUEUED {rebased_branch}" not in out, out
    assert f"{rebased_branch}: landed-under-different-sha" in out, out
    assert f"{rebased_branch}: preview-kick" not in out, \
        f"CI was burned on a rebased-and-landed branch:\n{out}"
    assert [b for b in _for_chuck_beads(work, home)
            if rebased_branch in b.get("title", "")] == [], out

    # attributable absence: no handoff for the merged branch...
    assert [b for b in _for_chuck_beads(work, home)
            if merged_branch in b.get("title", "")] == [], out
    # ...and the POSITIVE control, so this cannot pass by the floor having
    # stopped filing altogether under the race.
    assert [b for b in _for_chuck_beads(work, home)
            if live_branch in b.get("title", "")] != [], \
        f"the genuinely stranded branch {live_branch} must still file:\n{out}"


def test_tseoz_a_land_together_branch_files_no_handoff(tmp_path):
    """SABLE-tseoz's ruling against real bd: the discriminating input for
    HELD-ON-MATCHED-PAIR is ALREADY structured, symmetric, queryable metadata
    on the work bead — and the reconciler filed a stranded bead for it anyway.
    Positive and negative, so the metadata is provably what does the work."""
    origin, work, home = _setup(tmp_path)

    paired_bead = _make_work_bead(work, home, status="closed")
    paired_branch = _push_worker_branch(work, paired_bead)
    _bd(work, home, "update", paired_bead, "--sandbox",
        "--set-metadata", "serialize_kind=land-together",
        "--set-metadata", "serialize_with=SABLE-cmar4.9",
        "--set-metadata", "serialize_reason=twin-hardcodes-same-file",
        "--set-metadata", "serialize_ruling=lincoln-2026-07-22")

    plain_bead = _make_work_bead(work, home, status="closed")
    plain_branch = _push_worker_branch(work, plain_bead)

    cp = _reconcile(work, home)
    assert cp.returncode == 0, cp.stdout
    out = cp.stdout

    assert f"MATCHED-PAIR {paired_branch}" in out, out
    assert "SABLE-cmar4.9" in out and "lincoln-2026-07-22" in out, out
    titles = [b.get("title", "") for b in _for_chuck_beads(work, home)]
    assert not any(paired_branch in t for t in titles), \
        f"a land-together branch must file no stranded handoff: {titles}\n{out}"
    assert any(plain_branch in t for t in titles), \
        f"the unpaired control must still file: {titles}\n{out}"
