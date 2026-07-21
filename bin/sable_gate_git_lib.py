#!/usr/bin/env python3
"""sable_gate_git_lib — the merge gate's SUBPROCESS SEAM (SABLE-jd5fj.3).

The shared plumbing under the classify / preview / promote split: the
env-overridable tool seams (SABLE_MG_GIT / _GH / _BD / _NOTIFY), the git wrapper
every module calls, and the repo-identity resolution the whole flow keys on
(which branch IS this repo's integration branch).

It exists as its own module for one structural reason: preview and promote BOTH
shell out, and a test that stubs git must be able to stub it ONCE for both. Every
module calls `git_lib._git(...)` module-qualified rather than importing the name,
so `monkeypatch.setattr(git_lib, "_git", fake)` reaches the whole gate — which is
what keeps the split from multiplying the number of seams a test has to know
about. This module imports only sable_gate_classify_lib (pure), so the dependency
graph is a DAG: classify <- git <- preview <- promote.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from sable_gate_classify_lib import GateError


def _tool(env_name: str, default: str) -> list[str]:
    return os.environ.get(env_name, default).split()


def _run(argv: list[str], *, cwd: str, check: bool = True,
         timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=cwd, text=True, check=check, timeout=timeout,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


def _git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return _run(_tool("SABLE_MG_GIT", "git") + list(args), cwd=repo, check=check)


def default_mg_timeout(repo: str) -> float:
    """SABLE-cmar4.1 consumption seam: when SABLE_MG_TIMEOUT is not set
    explicitly, the merge_preview tier's duration budget from this repo's
    .github/ci/test-tiers.sh SSOT (if it declares one) is the default Actions
    wait — instead of a value hardcoded here that drifts from the SSOT.
    Falls back to the pre-tier-SSOT default (1800) on any failure: no
    test-tiers.sh, non-bash environment, or a bad/missing budget value. Never
    raises — a missing/broken SSOT must not block the gate."""
    tiers_sh = Path(repo) / ".github" / "ci" / "test-tiers.sh"
    if not tiers_sh.is_file():
        return 1800.0
    try:
        cp = _run(["bash", str(tiers_sh), "--budget", "merge_preview"],
                   cwd=repo, check=False, timeout=10)
        return float(cp.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return 1800.0


def resolve_commit(repo: str, ref: str) -> str:
    """Resolve a fully-qualified ref (or raw SHA) to a commit SHA via
    `git rev-parse --verify <ref>^{commit}`. Raises on ambiguity/absence."""
    cp = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
    if cp.returncode != 0:
        raise GateError(3, f"cannot resolve ref {ref!r}: {cp.stdout.strip()}")
    return cp.stdout.strip()


def remote_ref_commit(repo: str, remote: str, ref: str) -> str | None:
    """Commit at refs/heads/<ref> ON THE REMOTE, or None if the ref is absent /
    unreadable. Read via ls-remote rather than a remote-tracking ref because a
    kick fired seconds ago by ANOTHER process (the post-push hook) has no local
    tracking ref in this repo at all."""
    cp = _git(repo, "ls-remote", "--exit-code", remote, f"refs/heads/{ref}", check=False)
    if cp.returncode != 0:
        return None
    first = cp.stdout.split("\n", 1)[0].split("\t", 1)[0].strip()
    return first or None


def commit_parents(repo: str, sha: str) -> list[str]:
    """Parent SHAs of a commit, or [] if it cannot be read."""
    cp = _git(repo, "rev-list", "--parents", "-n", "1", sha, check=False)
    if cp.returncode != 0:
        return []
    return cp.stdout.split()[1:]


def resolve_integration_branch(repo: str) -> str:
    """Python mirror of hooks/multi-manager/lib-identity.sh's
    sable_resolve_integration_branch (bin/sable-spawn-worker:431 carries the
    same mirror for the spawn path) — <repo>'s OWN integration branch.
    Integration-branch identity is a property of the TARGET REPO, not the
    caller's env, so this is consulted in the same order the pre-push hook
    uses, first match wins:
      1. `git -C <repo> config --get sable.integrationBranch` (repo-local, unshared)
      2. `<repo>/.sable` file, a line `integrationBranch=<name>` (checked in, shared)
      3. $SABLE_INTEGRATION_BRANCH (explicit env override, bare name)
      4. $SABLE_BASE_BRANCH with a leading `origin/` stripped
      5. "main"
    Always returns a bare branch name (SABLE-dtp1 — without this, promote()
    disagreed with the pre-push hook about what the integration branch is)."""
    cfg = _git(repo, "config", "--get", "sable.integrationBranch", check=False)
    val = cfg.stdout.strip() if cfg.returncode == 0 else ""
    if val:
        return val
    sable_file = Path(repo) / ".sable"
    if sable_file.is_file():
        for line in sable_file.read_text().splitlines():
            if line.startswith("integrationBranch="):
                val = line[len("integrationBranch="):].strip()
                if val:
                    return val
    val = os.environ.get("SABLE_INTEGRATION_BRANCH") or os.environ.get("SABLE_BASE_BRANCH") or "main"
    return val[len("origin/"):] if val.startswith("origin/") else val


def resolve_base(explicit_base: str | None, repo: str) -> str:
    """promote()'s --base precedence (SABLE-dtp1): an explicit --base flag
    wins outright; else SABLE_MG_BASE (this tool's own env override); else
    fall through to resolve_integration_branch(repo) instead of a hardcoded
    default — so an unset --base/SABLE_MG_BASE targets the SAME branch the
    pre-push hook already resolved for this repo, not a literal
    'llm-integration' that typically doesn't exist."""
    return explicit_base or os.environ.get("SABLE_MG_BASE") or resolve_integration_branch(repo)
