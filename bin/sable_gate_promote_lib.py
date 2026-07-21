#!/usr/bin/env python3
"""sable_gate_promote_lib — consume a verdict, promote or refuse (SABLE-jd5fj.3).

The PROMOTE half of the merge gate: the single place that writes to the
integration branch, and therefore the single place that has to be right about
BYTE-IDENTICAL PROMOTION — what CI validated is exactly what lands, because the
flow never re-merges after green, it pushes the same commit object it tested.

MODULE BOUNDARY, enforced by bin/test_merge_gate_modules.py: this module does
not construct previews or poll CI. It asks sable_gate_preview_lib for a
classify.Verdict and acts on it. That is the whole point of the split — with the
verdict arriving as a value, promote's body is a decision table over the exit-code
taxonomy rather than a construct-and-wait procedure, and the two beads queued
behind this one land in obvious places: per-tier duration recording
(SABLE-cmar4.4) wraps the acquire_verdict call, and per-promotion implementation
hashing (SABLE-w5ni5) joins the evidence writes below.

IRON RULES this module carries and the split did not touch:
  * the exit-code taxonomy 0/20/21/22/23/24/4, unchanged;
  * the fast-forward integrity assertion (remote base tip == preview SHA, else
    exit 4), preserved verbatim — moved intact, not restructured.
"""
from __future__ import annotations

import sys

import sable_gate_classify_lib as classify
import sable_gate_git_lib as git_lib
import sable_gate_preview_lib as preview
from sable_gate_classify_lib import GateError


def _notify(target: str, message: str) -> None:
    git_lib._run(git_lib._tool("SABLE_MG_NOTIFY", "sable-msg") + [target, message],
                 cwd=".", check=False)


def _append_evidence(repo: str, bead: str, note: str) -> None:
    git_lib._run(git_lib._tool("SABLE_MG_BD", "bd") + ["update", bead, "--append-notes", note],
                 cwd=repo, check=False)


# --------------------------------------------------------------------------
# Post-merge cleanup (SABLE-dn7r) — GREEN path only
# --------------------------------------------------------------------------

def worktree_for_branch(repo: str, branch: str) -> str | None:
    """Path of the registered worktree checked out on refs/heads/<branch>, or
    None if no worktree holds it. Parsed from `git worktree list --porcelain`,
    NEVER inferred from a naming convention: promote() runs against the main
    checkout while worker worktrees live elsewhere, and acting on a
    convention-guessed path was a real bug class here (SABLE-041)."""
    cp = git_lib._git(repo, "worktree", "list", "--porcelain", check=False)
    if cp.returncode != 0:
        return None
    target = f"refs/heads/{branch}"
    path: str | None = None
    for line in cp.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
        elif line.startswith("branch ") and line[len("branch "):].strip() == target:
            return path
        elif not line.strip():
            path = None
    return None


def worktree_is_dirty(worktree_path: str) -> bool:
    """True iff the worktree has uncommitted changes. Runs `git status
    --porcelain` INSIDE the worktree (its own CWD) — the one place this flow is
    meant to read the shell CWD, because that dir IS the tree being cleaned. On
    any error, assume dirty (fail-closed: never destroy under uncertainty)."""
    cp = git_lib._git(worktree_path, "status", "--porcelain", check=False)
    if cp.returncode != 0:
        return True
    return bool(cp.stdout.strip())


def branch_exists_locally(repo: str, branch: str) -> bool:
    cp = git_lib._git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
    return cp.returncode == 0


def all_commits_patch_equivalent(repo: str, base_ref: str, branch: str) -> bool:
    """True iff every commit unique to <branch> is already patch-present in
    <base_ref>. `git cherry` marks each such commit '-' (an equivalent exists in
    upstream) or '+' (genuinely absent); empty output means the branch is a pure
    ancestor. This is the ONLY justification for escalating a refused `branch -d`
    to `-D` — the wk-git-autopush-hunt case (rebased-and-landed: unmerged by
    ancestry yet patch-identical). Any '+' line, or a cherry error, refuses."""
    cp = git_lib._git(repo, "cherry", base_ref, branch, check=False)
    if cp.returncode != 0:
        return False
    lines = [ln for ln in cp.stdout.splitlines() if ln.strip()]
    return all(ln.startswith("-") for ln in lines)


def cleanup_after_merge(repo: str, remote: str, base_ref: str, branch: str) -> None:
    """Reap a merged worker's worktree + local branch + remote branch. GREEN
    PATH ONLY (SABLE-dn7r): once a preview has been promoted byte-identical to
    the integration branch, these are dead weight and re-accumulate at fleet pace
    (58 in one day) without this. Order is load-bearing — the worktree comes off
    FIRST (git refuses to delete a branch checked out in a worktree), then the
    local branch, then the remote.

    Every step is best-effort: failures warn on stderr and the caller swallows
    them so a green merge stays green. A DIRTY worktree aborts the WHOLE cleanup
    (uncommitted work is never destroyed). The remote delete is legal here
    because this runs inside chuck's merge path — the fleet's only push lane — so
    the chuck-only-push convention holds."""
    # (a) worktree — resolved from porcelain, refused if dirty
    worktree = worktree_for_branch(repo, branch)
    if worktree is not None:
        if worktree_is_dirty(worktree):
            print(f"sable-merge-gate cleanup: worktree {worktree} for {branch} is DIRTY — "
                  f"leaving worktree, local branch, and remote branch intact for the operator",
                  file=sys.stderr)
            return
        rm = git_lib._git(repo, "worktree", "remove", worktree, check=False)
        if rm.returncode != 0:
            print(f"sable-merge-gate cleanup: could not remove worktree {worktree} for {branch} "
                  f"({rm.stdout.strip()}) — leaving branches intact", file=sys.stderr)
            return

    # (b) local branch — -d, escalating to -D only on proven patch-equivalence
    if branch_exists_locally(repo, branch):
        d = git_lib._git(repo, "branch", "-d", branch, check=False)
        if d.returncode != 0:
            if all_commits_patch_equivalent(repo, base_ref, branch):
                dd = git_lib._git(repo, "branch", "-D", branch, check=False)
                if dd.returncode != 0:
                    print(f"sable-merge-gate cleanup: guarded -D of {branch} failed: "
                          f"{dd.stdout.strip()}", file=sys.stderr)
            else:
                print(f"sable-merge-gate cleanup: local branch {branch} is neither fully merged "
                      f"nor patch-equivalent to base — NOT deleting local or remote branch "
                      f"(operator ruling needed): {d.stdout.strip()}", file=sys.stderr)
                return

    # (c) remote branch — chuck's merge path is the fleet's only push lane
    push = git_lib._git(repo, "push", remote, "--delete", branch, check=False)
    if push.returncode != 0:
        print(f"sable-merge-gate cleanup: could not delete remote branch {remote}/{branch}: "
              f"{push.stdout.strip()}", file=sys.stderr)


# --------------------------------------------------------------------------
# promote — the only writer to the integration branch
# --------------------------------------------------------------------------

def promote(bead: str, branch: str, base: str, repo: str, remote: str,
            manager: str, override: str | None) -> int:
    base_ref = classify.qualify_remote_ref(remote, base)
    branch_ref = classify.qualify_remote_ref(remote, branch)
    git_lib._git(repo, "fetch", remote, base, branch)
    base_sha = git_lib.resolve_commit(repo, base_ref)
    branch_sha = git_lib.resolve_commit(repo, branch_ref)

    # SABLE-jd5fj.1/.3: adopt the push-time kick for this exact (base, branch)
    # pair if one exists, else build and push a preview the pre-kick way. Raises
    # GateError(22) on conflict BEFORE any ref exists — which is why this sits
    # outside the try/finally below, exactly as the pre-split flow did.
    preview_sha, ref, adopted = preview.materialize_preview(
        repo, remote, branch, base, base_sha, branch_sha, bead)
    try:
        if override:
            # An actions-down human bypass consults no run at all.
            verdict = classify.Verdict("override", override, preview_sha, ref,
                                       source="override")
        else:
            # SABLE-jd5fj.3: read the STORED verdict first and wait only if there
            # isn't one. With previews kicked at push time and running
            # concurrently, the common case here is a completed run and a
            # seconds-long read. (SABLE-cmar4.4's per-tier duration recording
            # belongs around this call — it is where the gate's wall-clock goes.)
            verdict = preview.acquire_verdict(repo, ref, preview_sha)
        conclusion, url = verdict.conclusion, verdict.run_url

        if verdict.outcome == classify.GREEN:
            push_cp = git_lib._git(repo, "push", remote, f"{preview_sha}:refs/heads/{base}", check=False)
            if push_cp.returncode != 0:
                # F1 (tarzan review): the base advanced during the CI wait, so the
                # promote is non-fast-forward. Nothing wrong was shipped — the push
                # was REJECTED — the tested-green ref is simply stale against a base
                # that moved. Exit cleanly and retryably (rebuild the preview on the
                # new base and re-gate) instead of letting CalledProcessError escape
                # as an uncaught traceback. Cleanup still runs via the finally below.
                _notify(manager,
                    f"merge-preview ci-verify gate for {bead} ({branch}): base {base} moved during the CI "
                    f"wait — promote is non-fast-forward, NOT promoted. Rebuild preview + re-gate (ref {ref} was green).")
                _append_evidence(repo, bead,
                    f"merge-preview ci-verify gate BASE-MOVED (retryable): ref {ref}, preview {preview_sha}, "
                    f"non-ff on promote, not promoted: {push_cp.stdout.strip()}")
                raise GateError(23, f"base {base} advanced during CI; preview {preview_sha} is non-ff — rebuild and re-gate")
            git_lib._git(repo, "fetch", remote, base)
            landed = git_lib.resolve_commit(repo, base_ref)
            if landed != preview_sha:
                # F3 (tarzan review): defensive integrity guard. Under chuck's
                # serialized push discipline (single writer to the integration
                # branch) this cannot fire — the fast-forward above is the last write
                # before this read. It is NOT rollback-capable (the object is already
                # pushed); it exists to fail LOUD rather than silently ship a base
                # whose tip is not the exact tested object. If it fires, serialization
                # was violated and a human must reconcile.
                raise GateError(4, f"integrity abort: base {base} tip {landed} != tested preview {preview_sha}")
            if override:
                # F2 (tarzan review + lincoln ruling): --override is an
                # actions-down human bypass ONLY, and must carry a reason (enforced
                # by argparse requiring the value). Using it to bypass a KNOWN-RED is
                # out of contract — documented, recorded, human-owned.
                _append_evidence(repo, bead,
                    f"merge-preview ci-verify gate OVERRIDE (actions-down human bypass): ref {ref}, "
                    f"reason={override!r}, preview {preview_sha}, promoted byte-identical to {base}.")
            else:
                _append_evidence(repo, bead,
                    f"merge-preview ci-verify gate GREEN: ref {ref}, run {url or 'n/a'}, "
                    f"preview {preview_sha}, promoted byte-identical to {base} "
                    f"(verdict {verdict.source}, preview {'adopted' if adopted else 'built'}).")
            # SABLE-dn7r: the promotion landed byte-identical, so the worker's
            # worktree + local branch + remote branch are dead weight — reap them.
            # Wrapped so a cleanup fault can never flip a green merge to non-zero.
            try:
                cleanup_after_merge(repo, remote, base_ref, branch)
            except Exception as exc:  # noqa: BLE001 — a green merge must stay green
                print(f"sable-merge-gate cleanup: skipped after unexpected error: {exc}",
                      file=sys.stderr)
            return 0

        if verdict.outcome == classify.BLOCKED:
            _notify("lincoln",
                f"merge-preview ci-verify gate BLOCKED for {bead} ({branch}): Actions {conclusion}, "
                f"no green result on ref {ref}/{preview_sha[:7]}. No promotion. Needs --override <url> or a recovered Actions.")
            _append_evidence(repo, bead,
                f"merge-preview ci-verify gate BLOCKED: ref {ref}, preview {preview_sha}, Actions {conclusion}, no promotion.")
            return classify.EXIT_BLOCKED

        if verdict.outcome == classify.RETRY:
            # SABLE-sc24: the run was CANCELLED mid-flight, not failed. A
            # cancellation is not a content defect — it happens when a concurrent
            # sweep deletes the in-flight ci-verify ref, a human cancels, or the
            # per-ref concurrency group pre-empts the run. Treating it as RED
            # (exit 20) mis-instructs the author to "fix + re-push" when there is
            # nothing to fix. Instead map it to the SAME retryable contract as
            # BASE-MOVED: no promotion, rebuild the preview + re-gate. The finally
            # below deletes the (possibly already-gone) throwaway ref.
            _notify(manager,
                f"merge-preview ci-verify gate CANCELLED (retryable) for {bead} ({branch}): run {url or 'n/a'} "
                f"was cancelled mid-flight, NOT a test failure — nothing to fix. Rebuild preview + re-gate.")
            _append_evidence(repo, bead,
                f"merge-preview ci-verify gate CANCELLED (retryable): ref {ref}, run {url or 'n/a'}, "
                f"preview {preview_sha}, run cancelled mid-flight, not promoted, no content fix needed.")
            raise GateError(24, f"ci-verify run for {bead} was cancelled mid-flight (not a failure) — rebuild preview + re-gate")

        _notify(manager,
            f"merge-preview ci-verify gate RED for {bead} ({branch}): run {url}, no promotion. Fix + re-push.")
        _append_evidence(repo, bead,
            f"merge-preview ci-verify gate RED: ref {ref}, run {url}, preview {preview_sha}, NOT promoted.")
        return classify.EXIT_RED
    finally:
        # Both-path cleanup: delete the throwaway ref (best-effort).
        preview.delete_ci_ref(repo, remote, ref)
