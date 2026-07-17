#!/usr/bin/env python3
"""Integration rehearsals for bin/sable-merge-gate (SABLE-o9aa acceptance).

Real git end-to-end against scratch repos (a bare "origin" + a working clone);
only the GitHub Actions verdict is injected, via a fake `gh` wired through the
SABLE_MG_GH seam that reports a run whose headSha is the real pushed preview SHA.
This exercises the load-bearing paths for real: merge-tree/commit-tree preview,
ci-verify push, byte-identical fast-forward promotion + SHA-equality assertion,
red/actions-down blocking, conflict delegation, and orphan sweep.

The three o9aa rehearsals:
  * real-green   -> promotion, base tip == tested preview SHA, ci-verify deleted
  * deliberate-red -> NO promotion, base unchanged, ci-verify deleted
  * actions-down -> BLOCK (exit 21), base unchanged; --override promotes
"""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parent / "sable-merge-gate"
BASE = "trunk"
WORKER = "wk-x"


def _run(argv, cwd=None, env=None):
    return subprocess.run(argv, cwd=cwd, env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _git(cwd, *args, check=True):
    cp = _run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd)
    if check and cp.returncode != 0:
        raise AssertionError(f"git {args} failed: {cp.stdout}")
    return cp.stdout.strip()


def _write_fake_gh(tmp_path, origin_gitdir, conclusion):
    """A fake `gh` that answers `run list --branch <ref>` with a run whose
    headSha is the REAL tip of that ci-verify ref (so wait_for_ci's SHA match is
    faithful). conclusion=='empty' -> no runs (actions-down)."""
    gh = tmp_path / "fake-gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import os,sys,json,subprocess\n"
        "a=sys.argv[1:]\n"
        f"c={conclusion!r}\n"
        "if c=='empty':\n"
        "    print('[]'); sys.exit(0)\n"
        "ref=a[a.index('--branch')+1]\n"
        f"od={str(origin_gitdir)!r}\n"
        "sha=subprocess.run(['git','--git-dir='+od,'rev-parse','refs/heads/'+ref],"
        "text=True,capture_output=True).stdout.strip()\n"
        "print(json.dumps([{'databaseId':1,'headSha':sha,'status':'completed',"
        "'conclusion':c,'url':'http://fake/run/1'}]))\n"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return gh


def _setup(tmp_path, *, conflict=False):
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    _run(["git", "init", "--bare", "-b", BASE, str(origin)])
    _run(["git", "clone", str(origin), str(work)])
    (work / "shared.txt").write_text("l1\nl2\nl3\n")
    (work / "README.md").write_text("base\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "init")
    _git(work, "push", "origin", BASE)

    _git(work, "checkout", "-b", WORKER)
    if conflict:
        (work / "shared.txt").write_text("WORKER\nl2\nl3\n")
    else:
        (work / "feature.txt").write_text("feature\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "feature")
    _git(work, "push", "origin", WORKER)

    if conflict:
        _git(work, "checkout", BASE)
        (work / "shared.txt").write_text("BASE\nl2\nl3\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-m", "base change")
        _git(work, "push", "origin", BASE)

    _git(work, "checkout", BASE)
    _git(work, "fetch", "origin")
    return origin, work


def _env(gh_path):
    e = dict(os.environ)
    e.update({
        "SABLE_MG_GH": str(gh_path),
        "SABLE_MG_BD": "true",       # no-op evidence recorder
        "SABLE_MG_NOTIFY": "true",   # no-op notifier
        "SABLE_MG_POLL": "0",
        "SABLE_MG_GRACE": "0",
        "SABLE_MG_TIMEOUT": "0",
    })
    return e


def _promote(work, gh_path, *, extra=()):
    return _run([sys.executable, str(BIN), "promote", "--bead", "TEST-1",
                 "--branch", WORKER, "--base", BASE, "--repo", str(work),
                 "--remote", "origin", *extra], cwd=str(work), env=_env(gh_path))


def _origin_base_sha(origin):
    return _git(None, "--git-dir=" + str(origin), "rev-parse", "refs/heads/" + BASE)


def _ci_verify_refs(origin):
    out = _git(None, "--git-dir=" + str(origin), "for-each-ref",
               "--format=%(refname)", "refs/heads/ci-verify/", check=False)
    return [r for r in out.splitlines() if r.strip()]


# --- Rehearsal 1: real-green -> byte-identical promotion ----------------------

def test_rehearsal_green_promotes_byte_identical(tmp_path):
    origin, work = _setup(tmp_path)
    gh = _write_fake_gh(tmp_path, origin, "success")
    before = _origin_base_sha(origin)

    cp = _promote(work, gh)
    assert cp.returncode == 0, cp.stdout

    after = _origin_base_sha(origin)
    assert after != before, "base did not advance"
    # the promoted commit is a real merge (two parents) — the tested object itself
    parents = _git(None, "--git-dir=" + str(origin), "rev-list", "--parents", "-n", "1", after).split()
    assert len(parents) == 3, "promoted commit is not the two-parent merge preview"
    assert _ci_verify_refs(origin) == [], "ci-verify ref not cleaned up on green"


# --- Rehearsal 2: deliberate-red -> no promotion ------------------------------

def test_rehearsal_red_no_promotion(tmp_path):
    origin, work = _setup(tmp_path)
    gh = _write_fake_gh(tmp_path, origin, "failure")
    before = _origin_base_sha(origin)

    cp = _promote(work, gh)
    assert cp.returncode == 20, cp.stdout
    assert _origin_base_sha(origin) == before, "base advanced on a red run"
    assert _ci_verify_refs(origin) == [], "ci-verify ref not cleaned up on red"


# --- Rehearsal 3: actions-down -> block; override -> promote -------------------

def test_rehearsal_actions_down_blocks(tmp_path):
    origin, work = _setup(tmp_path)
    gh = _write_fake_gh(tmp_path, origin, "empty")
    before = _origin_base_sha(origin)

    cp = _promote(work, gh)
    assert cp.returncode == 21, cp.stdout
    assert _origin_base_sha(origin) == before, "base advanced while Actions down"
    assert _ci_verify_refs(origin) == [], "ci-verify ref not cleaned up on block"


# --- SABLE-7wyl: sustained-503-class outage where `gh` itself hangs -----------

def _write_hanging_gh(tmp_path):
    """A fake `gh` that never returns — the 2026-07-16 muw0 incident shape: the
    Actions API was down hard enough that the `gh` CLI's own retry/connect
    behavior blocked indefinitely, not just erroring fast. Proves
    SABLE_MG_GH_TIMEOUT bounds each poll call so the gate still parks cleanly
    instead of needing a manual kill+requeue."""
    gh = tmp_path / "hanging-gh"
    gh.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(600)\n")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return gh


def test_rehearsal_actions_api_hang_bounded_by_gh_timeout(tmp_path):
    origin, work = _setup(tmp_path)
    gh = _write_hanging_gh(tmp_path)
    before = _origin_base_sha(origin)

    env = _env(gh)
    env["SABLE_MG_GH_TIMEOUT"] = "1"
    env["SABLE_MG_TIMEOUT"] = "2"
    env["SABLE_MG_GRACE"] = "0"

    cp = subprocess.run(
        [sys.executable, str(BIN), "promote", "--bead", "TEST-1",
         "--branch", WORKER, "--base", BASE, "--repo", str(work), "--remote", "origin"],
        cwd=str(work), env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=30,  # safety net: fail loud rather than hang pytest if the fix regresses
    )
    assert cp.returncode == 21, cp.stdout
    assert _origin_base_sha(origin) == before, "base advanced while Actions API was hung"
    assert _ci_verify_refs(origin) == [], "ci-verify ref not cleaned up after hang-bounded block"


def test_rehearsal_override_promotes_without_ci(tmp_path):
    origin, work = _setup(tmp_path)
    gh = _write_fake_gh(tmp_path, origin, "empty")  # CI down, but override supplied
    before = _origin_base_sha(origin)

    cp = _promote(work, gh, extra=("--override", "http://human/approval"))
    assert cp.returncode == 0, cp.stdout
    assert _origin_base_sha(origin) != before, "override did not promote"
    assert _ci_verify_refs(origin) == []


# --- SABLE-sc24: a cancelled preview run re-gates (retryable), never RED --------

def test_rehearsal_cancelled_run_is_retryable_not_red(tmp_path):
    # A run cancelled mid-flight (conclusion=='cancelled') is not a test failure.
    # The gate must exit RETRYABLE (24, rebuild preview + re-gate), NOT the red
    # path's exit 20 with a fix-and-re-push instruction — and must not promote.
    origin, work = _setup(tmp_path)
    gh = _write_fake_gh(tmp_path, origin, "cancelled")
    before = _origin_base_sha(origin)

    cp = _promote(work, gh)
    assert cp.returncode == 24, cp.stdout
    assert cp.returncode != 20, "cancellation must not take the RED path"
    assert _origin_base_sha(origin) == before, "base advanced on a cancelled run"
    assert _ci_verify_refs(origin) == [], "ci-verify ref not cleaned up on cancelled"


# --- Conflict delegation + sweep ---------------------------------------------

def test_conflict_delegates_exit_22_no_ref(tmp_path):
    origin, work = _setup(tmp_path, conflict=True)
    gh = _write_fake_gh(tmp_path, origin, "success")
    before = _origin_base_sha(origin)

    cp = _promote(work, gh)
    assert cp.returncode == 22, cp.stdout
    assert _origin_base_sha(origin) == before, "base advanced despite a preview conflict"
    assert _ci_verify_refs(origin) == [], "conflict must not leave a ci-verify ref"


# --- SABLE-dn7r: post-merge worktree/branch cleanup on the real green path -----

def _add_worktree(work, tmp_path, *, dirty=False):
    """Register a real linked worktree checked out on WORKER, mirroring the fleet
    layout (worktree lives outside the --repo main checkout)."""
    wt = tmp_path / "wt-wk-x"
    _git(work, "worktree", "add", str(wt), WORKER)
    if dirty:
        (wt / "uncommitted.txt").write_text("work in progress\n")
    return wt


def _worktree_paths(work):
    out = _git(work, "worktree", "list", "--porcelain", check=False)
    return [ln[len("worktree "):] for ln in out.splitlines() if ln.startswith("worktree ")]


def _local_branch_exists(work, branch):
    cp = _run(["git", "-C", str(work), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
    return cp.returncode == 0


def _origin_branch_exists(origin, branch):
    out = _git(None, "--git-dir=" + str(origin), "for-each-ref", "--format=%(refname)",
               f"refs/heads/{branch}", check=False)
    return bool(out.strip())


def test_green_promote_cleans_up_worktree_and_branches(tmp_path):
    origin, work = _setup(tmp_path)
    wt = _add_worktree(work, tmp_path)
    gh = _write_fake_gh(tmp_path, origin, "success")

    cp = _promote(work, gh)
    assert cp.returncode == 0, cp.stdout

    assert str(wt) not in _worktree_paths(work), "merged worktree not unregistered"
    assert not _local_branch_exists(work, WORKER), "merged local branch not deleted"
    assert not _origin_branch_exists(origin, WORKER), "merged remote branch not deleted"


def test_green_promote_dirty_worktree_left_intact(tmp_path):
    origin, work = _setup(tmp_path)
    wt = _add_worktree(work, tmp_path, dirty=True)
    gh = _write_fake_gh(tmp_path, origin, "success")
    before = _origin_base_sha(origin)

    cp = _promote(work, gh)
    assert cp.returncode == 0, cp.stdout
    assert _origin_base_sha(origin) != before, "dirty worktree must not block the merge itself"

    assert str(wt) in _worktree_paths(work), "dirty worktree wrongly removed"
    assert _local_branch_exists(work, WORKER), "branch of dirty worktree wrongly deleted"
    assert _origin_branch_exists(origin, WORKER), "remote branch wrongly deleted for dirty worktree"
    assert "DIRTY" in cp.stdout, "refusal not surfaced on stderr"


def test_red_gate_no_cleanup(tmp_path):
    # REGRESSION iron rule: a red gate leaves the worktree and both branches
    # untouched — the branch is still needed for the fix + re-push.
    origin, work = _setup(tmp_path)
    wt = _add_worktree(work, tmp_path)
    gh = _write_fake_gh(tmp_path, origin, "failure")

    cp = _promote(work, gh)
    assert cp.returncode == 20, cp.stdout

    assert str(wt) in _worktree_paths(work), "red gate removed the worktree"
    assert _local_branch_exists(work, WORKER), "red gate deleted the local branch"
    assert _origin_branch_exists(origin, WORKER), "red gate deleted the remote branch"


# --- SABLE-dtp1: unset --base/SABLE_MG_BASE resolves from the SAME source the
# pre-push hook uses (git config sable.integrationBranch > .sable file), not
# the hardcoded 'llm-integration' literal, which doesn't exist in these
# fixture repos — pre-fix this would fail with exit 3 (cannot resolve ref).

def _env_no_base(gh_path):
    """_env(), but with SABLE_MG_BASE/SABLE_INTEGRATION_BRANCH/SABLE_BASE_BRANCH
    stripped so resolution is driven purely by repo config, never by
    dev-machine env leakage."""
    e = _env(gh_path)
    for k in ("SABLE_MG_BASE", "SABLE_INTEGRATION_BRANCH", "SABLE_BASE_BRANCH"):
        e.pop(k, None)
    return e


def _promote_no_base(work, gh_path):
    return _run([sys.executable, str(BIN), "promote", "--bead", "TEST-1",
                 "--branch", WORKER, "--repo", str(work),
                 "--remote", "origin"], cwd=str(work), env=_env_no_base(gh_path))


def test_promote_resolves_base_from_git_config_when_unset(tmp_path):
    origin, work = _setup(tmp_path)
    _git(work, "config", "sable.integrationBranch", BASE)
    gh = _write_fake_gh(tmp_path, origin, "success")
    before = _origin_base_sha(origin)

    cp = _promote_no_base(work, gh)
    assert cp.returncode == 0, cp.stdout
    assert _origin_base_sha(origin) != before, "git-config-resolved base did not advance"


def test_promote_resolves_base_from_sable_file_when_unset(tmp_path):
    origin, work = _setup(tmp_path)
    (work / ".sable").write_text(f"integrationBranch={BASE}\n")
    gh = _write_fake_gh(tmp_path, origin, "success")
    before = _origin_base_sha(origin)

    cp = _promote_no_base(work, gh)
    assert cp.returncode == 0, cp.stdout
    assert _origin_base_sha(origin) != before, ".sable-file-resolved base did not advance"


def test_promote_with_neither_base_nor_config_fails_closed_not_llm_integration(tmp_path):
    # regression guard for the bug itself: with no --base, no SABLE_MG_BASE, and
    # no repo config, resolution falls through to "main" (SABLE-dtp1) — which
    # does not exist in this fixture (its integration branch is BASE="trunk") —
    # so the gate fails (git can't fetch a nonexistent ref) rather than
    # silently targeting a stale hardcoded 'llm-integration' literal that
    # ALSO doesn't exist here (pre-fix this failed the exact same way, just
    # against 'llm-integration' instead of 'main' — this pins the resolved
    # name, not just failure).
    origin, work = _setup(tmp_path)
    gh = _write_fake_gh(tmp_path, origin, "success")
    before = _origin_base_sha(origin)

    cp = _promote_no_base(work, gh)
    assert cp.returncode != 0, cp.stdout
    assert "'main'" in cp.stdout or "main'" in cp.stdout, \
        f"expected the resolved default 'main' (not 'llm-integration') in the failure output: {cp.stdout}"
    assert "llm-integration" not in cp.stdout
    assert _origin_base_sha(origin) == before, "base must not advance when resolution has no configured source"


def test_sweep_deletes_only_aged_orphans(tmp_path):
    origin, work = _setup(tmp_path)
    base_sha = _origin_base_sha(origin)
    base_tree = _git(work, "rev-parse", base_sha + "^{tree}")
    # a genuinely backdated preview commit (year 2000) for the "old" ref — the
    # sweep keys on committerdate, so the ref must point at an old COMMIT, not
    # merely be pushed with a stale env (which does not re-date an existing commit).
    old_env = dict(os.environ,
                   GIT_COMMITTER_DATE="2000-01-01T00:00:00 +0000",
                   GIT_AUTHOR_DATE="2000-01-01T00:00:00 +0000")
    old_sha = _run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit-tree", base_tree, "-m", "old-preview"], cwd=str(work), env=old_env).stdout.strip()
    _git(work, "push", "origin", f"{old_sha}:refs/heads/ci-verify/old-aaaaaaa")
    _git(work, "push", "origin", f"{base_sha}:refs/heads/ci-verify/new-bbbbbbb")

    cp = _run([sys.executable, str(BIN), "sweep", "--max-age-hours", "6",
               "--repo", str(work), "--remote", "origin"], cwd=str(work), env=_env(_write_fake_gh(tmp_path, origin, "empty")))
    assert cp.returncode == 0, cp.stdout
    remaining = _ci_verify_refs(origin)
    assert "refs/heads/ci-verify/old-aaaaaaa" not in remaining, "aged orphan not swept"
    assert "refs/heads/ci-verify/new-bbbbbbb" in remaining, "fresh ref wrongly swept"


def _write_fake_gh_status(tmp_path, status):
    """A fake gh whose `run list` reports one run with the given status (and null
    conclusion) for ANY --branch — exercises the sweep in-flight guard (SABLE-sc24)."""
    gh = tmp_path / "fake-gh-status"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import sys,json\n"
        f"s={status!r}\n"
        "print(json.dumps([{'databaseId':1,'status':s,'conclusion':None,'url':'http://fake/run/1'}]))\n"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return gh


def test_sweep_spares_aged_ref_with_inflight_run(tmp_path):
    # SABLE-sc24: an aged ci-verify ref whose Actions run is still in-flight must
    # NOT be reaped — deleting it cancels the run (the spurious-RED failure).
    origin, work = _setup(tmp_path)
    base_sha = _origin_base_sha(origin)
    base_tree = _git(work, "rev-parse", base_sha + "^{tree}")
    old_env = dict(os.environ,
                   GIT_COMMITTER_DATE="2000-01-01T00:00:00 +0000",
                   GIT_AUTHOR_DATE="2000-01-01T00:00:00 +0000")
    old_sha = _run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit-tree", base_tree, "-m", "old-preview"], cwd=str(work), env=old_env).stdout.strip()
    _git(work, "push", "origin", f"{old_sha}:refs/heads/ci-verify/inflight-aaaaaaa")

    gh = _write_fake_gh_status(tmp_path, "in_progress")
    cp = _run([sys.executable, str(BIN), "sweep", "--max-age-hours", "6",
               "--repo", str(work), "--remote", "origin"], cwd=str(work), env=_env(gh))
    assert cp.returncode == 0, cp.stdout
    assert "refs/heads/ci-verify/inflight-aaaaaaa" in _ci_verify_refs(origin), \
        "aged ref with an in-flight run was wrongly swept (would cancel the run)"
