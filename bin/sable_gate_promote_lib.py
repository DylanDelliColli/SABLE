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
(SABLE-cmar4.4, landed — see the acquire_verdict call below and
sable_gate_budget_lib.check_and_file) wraps the acquire_verdict call, and
per-promotion implementation hashing (SABLE-w5ni5) joins the evidence writes
below.

IRON RULES this module carries and the split did not touch:
  * the exit-code taxonomy 0/20/21/22/23/24/4, unchanged;
  * the fast-forward integrity assertion (remote base tip == preview SHA, else
    exit 4), preserved verbatim — moved intact, not restructured.

OPTIMISTIC DISJOINT PROMOTION (SABLE-jd5fj.4) adds ONE path to the decision
table and relaxes none of the existing ones. Before jd5fj.4, a base that moved
between the preview and the promote always meant exit 23: rebuild the preview
and re-gate through a full CI cycle. Now a stale base whose change-set is
DISJOINT from the branch's (sable_footprint_lib) earns a cheaper re-verification
— the cmar4 impact tier, run on the REAL COMBINED TREE — instead of a full
re-preview. Green promotes that same combined object; red ejects on the existing
exit-20 path; anything else falls back to exit 23.

SABLE-kzi1a WIDENS THE ENTRY to that path and changes nothing else about it.
jd5fj.4 reached the table only when the base moved during the gate's own CI wait
— which, under a serial merge lane with a single writer to the integration
branch, cannot happen: nothing else can move the base while chuck is inside a
promote. The situation the lane DOES produce is the queued branch whose push-time
preview went green against a base an earlier merge has since moved past. Same
facts, one step earlier, so it reaches the same _stale_base and the same table.
The one behavioural difference is what a non-disjoint answer costs: on that entry
nothing has been pushed, so instead of exit 23 the promote just builds a preview
the pre-kick way — the status quo ante, and the only thing that keeps a queued
branch from refusing forever on every retry.

  This is the one change in the epic that makes the system LESS safe by design.
  Everything else added verification; this removes some. What it removes is the
  structural guarantee that the exact object CI tested is the object that lands
  — so the replacement guarantee has to be carried explicitly, and it is:

  PROPERTY INVARIANT I1  No reachable promote path where the base moved and the
                         footprints are not proven disjoint and no
                         re-verification ran. decide_promotion() is total over
                         its input space and bin/test_promote_decision.py
                         enumerates that space exhaustively rather than by
                         example.
  PROPERTY INVARIANT I2  Every promotion pushes exactly the object that was
                         verified — the CI-green preview when the base held
                         still, the impact-tier-green combined commit when it
                         did not. Byte-identical promotion survives; what
                         changed is WHICH verifier attests the object, never
                         whether one did.

Do not read SABLE-nueh3's 0/126 semantic-break rate as support for this: that
number was measured under the regime this bead removes, where the failure class
was structurally impossible rather than rare. The usable prior is the
rule-of-three bound, <=2.4%, which is why the impact tier is mandatory on every
optimistic path and why an unavailable tier degrades to exit 23.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import sable_footprint_lib as footprint_lib
import sable_gate_budget_lib as budget_lib
import sable_gate_classify_lib as classify
import sable_gate_git_lib as git_lib
import sable_gate_preview_lib as preview
import sable_snapshot_lib as snapshot_lib
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


def _report_identifier_decay(repo: str, identifier: str) -> None:
    """Print any OPEN bead whose INSTRUCTIONS still name `identifier`, on the eve
    of that identifier being retired (SABLE-x9vby, promote-time seam).

    Fail-open on the decision, loud on the report (standing discipline 7): this
    NEVER affects whether the branch is deleted, and a sweep that could not run
    prints its could-not-assess notice rather than the silence a clean sweep
    prints. The sweeper's own exit code 3 already carries that text."""
    argv = git_lib._tool("SABLE_MG_IDDECAY", "sable-identifier-decay") + [
        "--branch", "-C", repo, identifier,
    ]
    try:
        cp = git_lib._run(argv, cwd=repo, check=False, timeout=30)
    except Exception as exc:  # sweeper absent / unrunnable — say so, don't die
        print(f"⚠ identifier-decay: COULD NOT ASSESS branch {identifier} ({exc}). "
              f"This is NOT a clean result: nothing was checked.", file=sys.stderr)
        return
    out = (getattr(cp, "stdout", "") or "").strip()
    if out:
        print(out, file=sys.stderr)


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

    # (c) remote branch — chuck's merge path is the fleet's only push lane.
    # Deleting it RETIRES the branch name as an identifier, so sweep first:
    # instructions keyed to a branch name (a HOLD that reads "do not merge
    # wk-foo") go stale the instant the name stops resolving, and they go stale
    # SILENTLY, still reading as satisfiable (SABLE-x9vby, instance 1). Advisory
    # only — never gates the delete.
    _report_identifier_decay(repo, branch)
    push = git_lib._git(repo, "push", remote, "--delete", branch, check=False)
    if push.returncode != 0:
        print(f"sable-merge-gate cleanup: could not delete remote branch {remote}/{branch}: "
              f"{push.stdout.strip()}", file=sys.stderr)


# --------------------------------------------------------------------------
# Optimistic disjoint promotion (SABLE-jd5fj.4)
# --------------------------------------------------------------------------

# What the impact tier concluded about the real combined tree.
IMPACT_GREEN = "green"    # the tier ran and every selected suite passed
IMPACT_RED = "red"        # the tier ran and something failed — a real defect
IMPACT_ERROR = "error"    # the tier could NOT run (absent, broke, timed out)

# Actions the decision table can produce.
ACTION_PROMOTE = "promote"        # push `verified_sha` to the integration branch
ACTION_REVERIFY = "reverify"      # run the impact tier on the combined tree, then re-decide
ACTION_REPREVIEW = "repreview"    # exit 23 — rebuild the preview and re-gate (status quo ante)
ACTION_REFUSE = "refuse"          # do not promote; exit per the taxonomy


@dataclass(frozen=True)
class PromoteDecision:
    action: str
    exit_code: int | None       # None for ACTION_REVERIFY: not a terminal state
    verified_sha: str | None    # the object attested by a verifier AND pushed (I2)
    reverified: bool            # True iff a re-verification ran on the combined tree
    reason: str


def decide_promotion(outcome: str, base_moved: bool, disjoint: bool | None,
                     impact: str | None, preview_sha: str = "",
                     combined_sha: str = "") -> PromoteDecision:
    """THE DECISION TABLE. Pure, total, and the sole authority on whether an
    object may be promoted — so that invariants I1 and I2 can be proven by
    ENUMERATION over its inputs rather than argued from the call sites.

    Inputs, and why each is tri- or bi-valued:
      outcome       the Actions verdict on the ORIGINAL preview (classify's
                    GREEN/RED/BLOCKED/RETRY). Anything but GREEN refuses here
                    exactly as it always did; the rows exist so the enumeration
                    covers the whole space, not just the interesting corner.
      base_moved    the integration branch tip is no longer the commit the green
                    preview was built on.
      disjoint      True / False / None, where None is UNDETERMINED (a footprint
                    could not be computed). None and False are treated
                    IDENTICALLY — the tri-state exists so the evidence can say
                    which one happened, never so they can act differently.
      impact        None = the tier has not run yet; else IMPACT_GREEN/RED/ERROR.

    Note the deliberate redundancy: an impact result is only honoured when
    disjoint is True. A caller that somehow arrives with impact=IMPACT_GREEN and
    disjoint=False still gets a refusal. Combinations that look unreachable are
    the ones that turn out reachable after a refactor, and a silent bad merge
    does not announce itself."""
    if outcome != classify.GREEN:
        return PromoteDecision(ACTION_REFUSE, classify.OUTCOME_EXIT[outcome], None, False,
                               f"verdict is {outcome}, not green")

    if not base_moved:
        # The pre-jd5fj.4 happy path, untouched: CI tested this exact object
        # against this exact base, and this exact object fast-forwards.
        return PromoteDecision(ACTION_PROMOTE, classify.EXIT_OK, preview_sha or None, False,
                               "base held still — promoting the CI-verified preview byte-identical")

    if disjoint is not True:
        # I1's load-bearing row. Not-disjoint AND undetermined both land here.
        return PromoteDecision(ACTION_REPREVIEW, classify.EXIT_BASE_MOVED, None, False,
                               "base moved and footprints are not proven disjoint — full re-preview")

    if impact is None:
        return PromoteDecision(ACTION_REVERIFY, None, None, False,
                               "base moved but footprints are disjoint — re-verify the combined tree")

    if impact == IMPACT_GREEN:
        if not combined_sha:
            # A green tier with nothing to point at cannot satisfy I2: there is
            # no attested object to push. Refuse rather than invent one.
            return PromoteDecision(ACTION_REPREVIEW, classify.EXIT_BASE_MOVED, None, False,
                                   "impact tier is green but no combined object was built — full re-preview")
        return PromoteDecision(ACTION_PROMOTE, classify.EXIT_OK, combined_sha, True,
                               "impact tier green on the real combined tree — promoting that same object")

    if impact == IMPACT_RED:
        return PromoteDecision(ACTION_REFUSE, classify.EXIT_RED, None, False,
                               "impact tier RED on the real combined tree — the merge is broken, not promoted")

    # IMPACT_ERROR and anything unrecognized: the tier did not answer, so the
    # optimism is unfunded. Fall back to the behaviour that needs no optimism.
    return PromoteDecision(ACTION_REPREVIEW, classify.EXIT_BASE_MOVED, None, False,
                           "impact tier could not answer — full re-preview")


def optimistic_promotion_enabled() -> bool:
    """SABLE_MG_OPTIMISTIC=0 restores the pre-jd5fj.4 behaviour exactly (every
    base-move is a full re-preview). An operator kill switch for the one
    relaxation in this epic, deliberately checked at the top of the stale-base
    path so turning it off cannot leave a half-taken decision behind."""
    return os.environ.get("SABLE_MG_OPTIMISTIC", "1") not in ("0", "false", "no")


def _impact_timeout() -> float:
    return float(os.environ.get("SABLE_MG_IMPACT_TIMEOUT", "900"))


def _selected_suites(worktree: str, paths: list[str]) -> list[str]:
    """Suites the shell impact manifest selects for these changed paths
    (SABLE-cmar4.2). Its own contract already handles the dangerous direction:
    a path it cannot map selects the FULL allow-list rather than nothing."""
    sel = git_lib._run(["bash", ".github/ci/impact-manifest.sh", "--select", *paths],
                       cwd=worktree, check=False, timeout=_impact_timeout())
    if sel.returncode != 0:
        raise RuntimeError(f"impact selection failed: {sel.stdout.strip()[:400]}")
    return [ln.strip() for ln in sel.stdout.splitlines()
            if ln.strip() and not ln.startswith("::")]


# --------------------------------------------------------------------------
# The bin/ pytest half's warm .testmondata (SABLE-jd5fj.8)
# --------------------------------------------------------------------------
#
# tier_selection.build_impact_tier_plan falls back to a conservative FULL
# bin/ run whenever ITS repo (the throwaway combined-tree worktree) has no
# .testmondata -- and a fresh `git worktree add` never does. The pytest half
# below best-effort copies one in before invoking the selector, from either
# of two sources, in priority order:
#
#   1. `repo`'s OWN root .testmondata -- CI's copy, when the checkout running
#      the gate happens to carry one (ci-verify's testmon-cache-warm.sh warms
#      exactly this file on the runner).
#   2. WARM_TESTMON_FILE under this repo's gate-owned state dir
#      (snapshot_lib.state_dir -- shared by every worktree of this repo,
#      resolved through git-common-dir). Chuck's own checkout is NOT
#      guaranteed to carry (1) either -- it is the runner's artifact, never
#      fetched down -- so (2) is this bead's actual fix for that gap: a
#      LOCAL, gate-owned copy that survives a `git clean`, a fresh worktree,
#      or a checkout that never itself ran a cache-warm pytest pass. It is
#      refreshed by `sable-merge-gate warm-testmon-cache` (warm_gate_testmon_cache
#      below), meant to be run periodically/by an operator -- NOT
#      automatically after every impact-tier run, because neither tier mode
#      (build_impact_tier_plan's "selected" or "full") passes pytest-testmon's
#      own --testmon/--testmon-noselect flags, so a real impact-tier pytest
#      invocation never updates .testmondata itself. Only an explicit
#      --testmon-noselect full run (what warm_gate_testmon_cache and CI's
#      testmon-cache-warm.sh both do) does.
#
# Neither existing is the genuinely-cold case, reported honestly below.

WARM_TESTMON_FILE = "testmondata-warm"

# tier_selection.py's own stderr line ("tier_selection: <mode> -- <reason>"),
# folded into stdout by git_lib._run's stderr=STDOUT. Parsing THIS instead of
# inferring warm/cold purely from file presence is the actual FAIL-VISIBLE
# fix: a warm map that is present but STALE or CORRUPT still gets handed to
# the selector, and build_impact_tier_plan already detects that (a broken
# pytest-testmon collector exits outside {0, 5}) and falls back to a full run
# -- but only THIS line says so. Reporting "warm testmon map" from file
# presence alone, as the prior revision of this bead did, would silently
# under-report that internal fallback as a scoped success: exactly the
# under-selection the bead's ownership notes require to fail visible.
_TIER_SELECTION_LINE = re.compile(r"^tier_selection: (\w+) -- (.+)$", re.MULTILINE)


def _warm_testmondata_path(repo: str) -> Path:
    """Where the gate persists its own warm .testmondata, independent of
    whether `repo`'s own root carries one. See the module comment above."""
    return snapshot_lib.ensure_state_dir(repo) / WARM_TESTMON_FILE


def _warm_testmondata_source(repo: str) -> tuple[Path | None, str]:
    """Which .testmondata (if any) the pytest half should copy into the
    throwaway worktree, and the label to report if tier_selection.py's own
    reason line (see _tier_selection_reason) is unavailable for some reason.
    Priority: repo's own root, then the gate's persisted cache, then none."""
    own = Path(repo) / ".testmondata"
    if own.is_file():
        return own, "warm testmon map"
    persisted = _warm_testmondata_path(repo)
    if persisted.is_file():
        return persisted, "warm testmon map (gate cache)"
    return None, "no warm .testmondata -- full run"


def _tier_selection_reason(output: str) -> str | None:
    """Pull tier_selection.py's own mode/reason line out of its captured
    output, or None if the line is missing (an override, or a selector too
    old to print it) so the caller falls back to its own warm/cold label
    instead of a blank detail."""
    m = _TIER_SELECTION_LINE.search(output)
    return m.group(2) if m else None


def _refresh_warm_testmondata(repo: str, updated: Path) -> None:
    """Copy `updated` (a .testmondata that a REAL --testmon/--testmon-noselect
    run just wrote) into the gate's persisted cache. Best-effort: a failed
    refresh only costs the NEXT promote's tier scoping, never its
    correctness -- the next run degrades to whatever the old cache (or none)
    reports, and that degradation is itself named in ITS OWN detail string."""
    try:
        if updated.is_file():
            shutil.copy2(updated, _warm_testmondata_path(repo))
    except OSError:
        pass


def warm_gate_testmon_cache(repo: str) -> int:
    """Refresh the gate's persisted warm .testmondata (see the module
    comment above) by running tier_selection.py's own --cache-warm directly
    against `repo` -- the SAME full bin/ suite + tolerant classification of
    the known pytest-testmon extensionless-file crash that
    .github/ci/testmon-cache-warm.sh runs on CI's ephemeral runner, just run
    locally so a checkout that never fetches CI's own copy still gets one.
    Meant to be invoked periodically or by an operator
    (`sable-merge-gate warm-testmon-cache`) -- it pays the full bin/ suite
    itself, which is exactly the cost this bead exists to keep OFF the
    promote path, so it must never run automatically inside run_impact_tier.
    Returns tier_selection.py's own exit code (0 = warm, non-zero = a real
    failure -- see classify_cache_warm_outcome for what it tolerates)."""
    selector = Path(repo) / "bin" / "tier_selection.py"
    if not selector.is_file():
        print(f"sable-merge-gate: {repo} has no bin/tier_selection.py — nothing to warm",
              file=sys.stderr)
        return 1
    cp = git_lib._run([sys.executable, str(selector), "--cache-warm"], cwd=repo,
                      check=False, timeout=1800)
    print(cp.stdout, end="")
    if cp.returncode == 0:
        _refresh_warm_testmondata(repo, Path(repo) / ".testmondata")
        print(f"sable-merge-gate: gate-owned warm .testmondata refreshed at "
              f"{_warm_testmondata_path(repo)}", file=sys.stderr)
    return cp.returncode


# --------------------------------------------------------------------------
# Impact-tier serialization (SABLE-jd5fj.13) — the MECHANICAL one-at-a-time rule
# --------------------------------------------------------------------------

IMPACT_LOCK_FILE = "impact-tier.lock"
IMPACT_WINDOW_FILE = "impact-tier-windows.jsonl"


class ImpactLockTimeout(RuntimeError):
    """Waited longer than SABLE_MG_IMPACT_LOCK_TIMEOUT for the tier lock. Never
    an exit code: run_impact_tier converts it to IMPACT_ERROR, which the decision
    table already routes to a full re-preview. A tier we could not START taught
    us nothing, exactly like a tier that could not run."""


def impact_lock_path(repo: str | os.PathLike = ".") -> Path:
    """Where the one-at-a-time lock lives. Per-REPO, in the merge-gate state dir
    that sable_snapshot_lib already resolves from git-common-dir — so every
    worktree of the same repo contends on the same file (which is the whole
    point: the promotes that collide are chuck's, all against one seat's repo)
    and another repo's promotes contend on their own.

    SABLE_MG_IMPACT_LOCK overrides the path outright. That seam exists for tests
    — including the negative control that proves the instrument can see overlap —
    and NOT as a bypass: pointing it somewhere private still takes a lock, it
    just takes a different one."""
    override = os.environ.get("SABLE_MG_IMPACT_LOCK")
    if override:
        return Path(override)
    return snapshot_lib.ensure_state_dir(repo) / IMPACT_LOCK_FILE


def impact_serialization_enabled() -> bool:
    """SABLE_MG_IMPACT_SERIALIZE=0 restores the pre-jd5fj.13 free-for-all.

    Unlike assert_not_frozen, this DOES get a kill switch, for one reason: the
    integration test's negative control has to observe real overlap to prove the
    instrument is not measuring nothing. Its off-state is the state chuck was
    already policing by hand, so turning it off loses a control rather than
    disabling a safety assertion — but it does lose one, so it is documented
    here and nowhere in the operator-facing flow."""
    return os.environ.get("SABLE_MG_IMPACT_SERIALIZE", "1") not in ("0", "false", "no")


def _impact_lock_timeout() -> float:
    """How long a queued promote will wait for its turn. Deliberately MUCH larger
    than _impact_timeout(): the expected wait is one whole tier ahead of us, and
    under a burst it is several. A promote that gives up here degrades to a full
    re-preview, which is correct but wasteful, so the bound is a runaway-holder
    backstop rather than a queueing policy."""
    return float(os.environ.get("SABLE_MG_IMPACT_LOCK_TIMEOUT", "3600"))


# --------------------------------------------------------------------------
# The derivable promote budget (SABLE-w0zjm)
# --------------------------------------------------------------------------

BUDGET_HEADROOM = 1.2
"""Multiplier applied to the worst case to get a RECOMMENDED enclosing timeout.

Deliberately modest and NOT an env knob. The headroom exists to cover the gate's
non-tier work (fetch, preview read, push) and clock slop, not to paper over a
mis-sized budget: if a wrapper needs materially more than this, the honest fix is
to raise SABLE_MG_IMPACT_TIMEOUT / SABLE_MG_IMPACT_LOCK_TIMEOUT so the number the
wrapper derives is the number the gate actually intends to spend."""


def impact_budget() -> dict:
    """The gate's own worst-case promote wall-clock, so an ENCLOSING wrapper can
    DERIVE its timeout instead of hardcoding one (SABLE-w0zjm).

    WHY THIS IS A FUNCTION AND NOT A DOC LINE. Chuck ran every promote inside a
    900s wrapper — the SAME number as the default SABLE_MG_IMPACT_TIMEOUT. That
    was harmless only while the impact tier essentially never ran (0 optimistic
    paths in 157 promotions). SABLE-jd5fj.4 deliberately moved cost from GitHub's
    CI into the local promote, and SABLE-jd5fj.13 then added a queue in front of
    it. A CHANGE THAT MOVES COST ACROSS A PROCESS BOUNDARY INVALIDATES EVERY
    TIMEOUT SIZED AGAINST THE OLD BEHAVIOUR — and those timeouts live OUTSIDE this
    repo, in operator wrappers, where no repo-side test can ever see them. The
    only structural fix is to stop them being a second copy of the number.

    THE WORST CASE IS A SUM, NOT THE TIER BUDGET. The lock wait is deliberately
    EXCLUDED from the tier's own budget (see run_impact_tier — a queued promote
    still gets its full tier budget, which is correct and must not be "fixed"), so
    a promote can spend LOCK WAIT + TIER, i.e. ~4500s on stock defaults. A wrapper
    sized to the tier budget alone is MORE wrong after jd5fj.13, not less: under a
    burst the queue wait alone can exceed it.

    Reported in seconds:
      tier_timeout_s  SABLE_MG_IMPACT_TIMEOUT — the tier's own run budget.
      lock_timeout_s  SABLE_MG_IMPACT_LOCK_TIMEOUT — how long a promote will
                      queue for the seat. 0 when serialization is off, because
                      then there is no queue to wait in.
      worst_case_s    their sum: the longest a promote may legitimately take.
      recommended_wrapper_timeout_s   worst_case_s * BUDGET_HEADROOM, rounded up.
                      This is the number a wrapper should use.
      serialized      whether the queue is in play at all."""
    tier = _impact_timeout()
    lock = _impact_lock_timeout() if impact_serialization_enabled() else 0.0
    worst = tier + lock
    return {
        "tier_timeout_s": tier,
        "lock_timeout_s": lock,
        "worst_case_s": worst,
        "recommended_wrapper_timeout_s": int(math.ceil(worst * BUDGET_HEADROOM)),
        "serialized": impact_serialization_enabled(),
    }


def format_impact_budget(budget: dict) -> str:
    """Human-readable breakdown. Says which number is which, because the failure
    this prevents is a wrapper sized against the wrong one."""
    q = (f"queue wait   {budget['lock_timeout_s']:.0f}s  (SABLE_MG_IMPACT_LOCK_TIMEOUT)"
         if budget["serialized"] else
         "queue wait     0s  (serialization OFF — SABLE_MG_IMPACT_SERIALIZE=0)")
    return "\n".join([
        "sable-merge-gate promote budget:",
        f"  {q}",
        f"  impact tier  {budget['tier_timeout_s']:.0f}s  (SABLE_MG_IMPACT_TIMEOUT, "
        f"starts AFTER the queue wait — it is not charged the wait)",
        f"  worst case   {budget['worst_case_s']:.0f}s  (queue + tier)",
        f"  RECOMMENDED enclosing wrapper timeout: "
        f"{budget['recommended_wrapper_timeout_s']}s",
        "",
        "Any timeout wrapping `sable-merge-gate promote` MUST exceed the worst case.",
        "Derive it — `timeout \"$(sable-merge-gate promote-budget --seconds)\"",
        "sable-merge-gate promote ...` — do not copy the number, or the two drift",
        "apart (SABLE-w0zjm).",
        "A wrapper kill mid-tier is SAFE (nothing is pushed before a green verdict)",
        "but reads as a path malfunction, so it is misdiagnosed rather than noticed.",
    ])


@contextlib.contextmanager
def impact_tier_lock(repo: str | os.PathLike = "."):
    """Hold the exclusive impact-tier lock for the duration of the block; yield
    the seconds spent waiting for it.

    WHY A LOCK AND NOT HERMETICIZATION (SABLE-jd5fj.13, fix direction 1). The
    iron-rule suites this tier selects are deliberately NON-HERMETIC — real bd,
    real sable-spawn-worker, live ~/.claude/settings.json — and they both read
    and write that shared live state. Two tiers running at once race on it and
    false-RED each other: under a 6+ promote pile-up at the merge seat,
    test-dep-merge-state.sh's WIRING subtest and test-overlap-dispatch-e2e.sh's
    serialize_grant subtest went red repeatedly while passing 18/18 and 5/5
    standalone on the same clean HEAD. Six branches were ejected that had nothing
    wrong with them. Hermeticizing the whole iron-rule set kills the class rather
    than queueing around it and remains the better end state; this queues, cheaply
    and today, and preserves every suite's current semantics exactly.

    It replaces a MANAGER DISCIPLINE with a CONTROL. Chuck's interim rule — "let
    promotes reach the local impact tier one at a time" — worked and is the same
    guidance-as-control shape SABLE-rkc3o refuted for the pinning suites: it holds
    exactly as long as everyone remembers it, and the moment it matters most is a
    burst, which is exactly when nobody is counting.

    flock, not a pidfile: the kernel releases it when the holder dies, so a
    crashed or killed promote cannot wedge the seat. Acquisition polls a
    non-blocking flock rather than blocking in the kernel so the wait is bounded
    and the give-up is a value (IMPACT_ERROR) rather than a hang."""
    if not impact_serialization_enabled():
        yield 0.0
        return
    path = impact_lock_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    deadline = started + _impact_lock_timeout()
    with open(path, "a+") as fh:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                waited = time.monotonic() - started
                if time.monotonic() >= deadline:
                    raise ImpactLockTimeout(
                        f"waited {waited:.0f}s for the impact-tier lock {path} and it is "
                        f"still held (SABLE_MG_IMPACT_LOCK_TIMEOUT={_impact_lock_timeout():.0f}s)"
                    ) from None
                time.sleep(0.05)
        try:
            yield time.monotonic() - started
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _stamp_impact_window(repo: str | os.PathLike, event: str, tree_sha: str,
                         waited: float) -> None:
    """Append one start/end record for this tier run. Best-effort and never
    load-bearing on the verdict — it exists so a human (and
    hooks/test/test-impact-tier-serialization.sh) can see whether two tier
    WINDOWS overlapped, which is the only direct evidence that the lock is doing
    its job. Overlap is invisible from suite results alone: that is precisely why
    the pile-up read as six broken branches instead of one broken control."""
    try:
        path = Path(os.environ.get("SABLE_MG_IMPACT_WINDOW_LOG")
                    or (snapshot_lib.ensure_state_dir(repo) / IMPACT_WINDOW_FILE))
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps({"event": event, "pid": os.getpid(), "at": time.time(),
                                 "tree": tree_sha[:12], "waited": round(waited, 3)}) + "\n")
    except OSError:
        pass


def run_impact_tier(repo: str, tree_sha: str, paths: list[str]) -> tuple[str, str]:
    """Run the cmar4 impact tier against the REAL COMBINED TREE, ONE AT A TIME
    PER SEAT (SABLE-jd5fj.13), and report (GREEN|RED|ERROR, detail).

    The serialization wraps this whole function rather than living inside
    _run_impact_tier_locked, so that the LOCK WAIT IS OUTSIDE THE TIER'S TIMEOUT
    BUDGET. That ordering is load-bearing, not incidental: _impact_timeout() is
    read fresh for each subprocess AFTER the lock is held, so a promote that
    queued behind two others still gets its full SABLE_MG_IMPACT_TIMEOUT to run
    in. Charging queue time to the tier budget would time out a queued promote
    through no fault of its own — turning the fix for false-REDs into a new
    source of them.

    See impact_tier_lock for why the answer here is a lock and not hermetic
    isolation, and _run_impact_tier_locked for the tier's own contract."""
    try:
        with impact_tier_lock(repo) as waited:
            if waited >= 1.0:
                print(f"sable-merge-gate: waited {waited:.0f}s for the impact-tier lock "
                      f"(another promote held the seat) — the tier's own "
                      f"{_impact_timeout():.0f}s budget starts now, unspent",
                      flush=True)
            # SABLE-w0zjm (c): an UNCONDITIONAL in-tier marker, so a promote killed
            # from OUTSIDE is diagnosable after the fact instead of mysterious. The
            # jd5fj.13 line above only fires on a wait of 1s or more, which is
            # exactly the uncontended case where a wrapper kill looks most like the
            # optimistic path malfunctioning. Naming the budget here means the last
            # line before the silence says how long the silence was entitled to be.
            #
            # flush=True IS THE FEATURE, not tidiness. stdout is block-buffered
            # whenever the gate is piped or captured — which is how every operator
            # wrapper runs it — so an unflushed marker dies in the buffer with the
            # process that a wrapper timeout kills, i.e. it is absent from exactly
            # the one transcript it exists to explain. C7 in
            # hooks/test/test-optimistic-promotion.sh caught this: the assertion
            # failed with EMPTY output while the kill itself behaved correctly.
            print(f"sable-merge-gate: ENTERING IMPACT TIER (budget "
                  f"{_impact_timeout():.0f}s) — if this promote dies without a "
                  f"verdict line, suspect an enclosing wrapper timeout before the "
                  f"tier itself (see `sable-merge-gate promote-budget`)",
                  flush=True)
            _stamp_impact_window(repo, "start", tree_sha, waited)
            try:
                return _run_impact_tier_locked(repo, tree_sha, paths)
            finally:
                _stamp_impact_window(repo, "end", tree_sha, waited)
    except ImpactLockTimeout as exc:
        return (IMPACT_ERROR, f"impact tier never started: {exc}")


def _run_impact_tier_locked(repo: str, tree_sha: str, paths: list[str]) -> tuple[str, str]:
    """The tier itself, with the seat's serialization lock already held. Reports
    (IMPACT_GREEN|IMPACT_RED|IMPACT_ERROR, detail).

    "Real" is the whole point, and it is why this checks the combined commit out
    into a throwaway detached worktree instead of reasoning about trees: the
    thing that has to be exercised is the code as it will exist AFTER the merge,
    with both changes present at once. A semantic break between two file-disjoint
    changes exists only in that combined state — no diff of either side can show
    it, which is exactly why disjointness alone was ruled unsound (SABLE-djopw).

    Scoped, not full: the shell half runs only the suites .github/ci/impact-
    manifest.sh selects for the union footprint, and the pytest half defers to
    bin/tier_selection.py (which itself falls back to a full bin/ run on a cold
    testmon cache). Both halves inherit their conservative-default behaviour from
    cmar4 rather than re-deriving a narrower one here.

    ERROR, not RED, whenever the tier could not RUN — an absent manifest, a
    missing suite file, a timeout, a broken worktree. That distinction is
    load-bearing: RED tells an author to fix a real defect and ejects on exit 20,
    while ERROR means we learned nothing and must fall back to exit 23. Reporting
    a non-answer as either green or red would be the silent-green class this epic
    exists to eliminate."""
    override = os.environ.get("SABLE_MG_IMPACT", "").split()
    parent = tempfile.mkdtemp(prefix="sable-impact-")
    worktree = str(Path(parent) / "tree")
    try:
        add = git_lib._git(repo, "worktree", "add", "--detach", worktree, tree_sha, check=False)
        if add.returncode != 0:
            return (IMPACT_ERROR, f"could not check out the combined tree: {add.stdout.strip()[:400]}")
        try:
            if override:
                cp = git_lib._run(override + list(paths), cwd=worktree, check=False,
                                  timeout=_impact_timeout())
                return ((IMPACT_GREEN, "impact tier override reported green") if cp.returncode == 0
                        else (IMPACT_RED, f"impact tier override failed (rc={cp.returncode}): "
                                          f"{cp.stdout.strip()[:400]}"))

            if not (Path(worktree) / ".github" / "ci" / "impact-manifest.sh").is_file():
                return (IMPACT_ERROR, "this repo has no .github/ci/impact-manifest.sh — "
                                      "no impact tier to run on the combined tree")
            suites = _selected_suites(worktree, list(paths))
            ran: list[str] = []
            for suite in suites:
                suite_path = Path(worktree) / "hooks" / "test" / suite
                if not suite_path.is_file():
                    return (IMPACT_ERROR, f"impact tier selected {suite} but it is missing "
                                          f"from the combined tree")
                cp = git_lib._run(["bash", str(suite_path)], cwd=worktree, check=False,
                                  timeout=_impact_timeout())
                ran.append(suite)
                if cp.returncode != 0:
                    return (IMPACT_RED, f"{suite} FAILED on the combined tree (rc={cp.returncode}): "
                                        f"{cp.stdout.strip()[-800:]}")

            # The pytest half, only when the footprint reaches bin/ at all.
            selector = Path(worktree) / "bin" / "tier_selection.py"
            if selector.is_file() and any(p.startswith("bin/") for p in paths):
                warm_source, warm_label = _warm_testmondata_source(repo)
                if warm_source is not None:
                    shutil.copy2(warm_source, Path(worktree) / ".testmondata")
                cp = git_lib._run([sys.executable, str(selector)], cwd=worktree, check=False,
                                  timeout=_impact_timeout())
                # SABLE-jd5fj.8: name whichever path tier_selection.py ITSELF
                # actually took (its own stderr line, folded into cp.stdout by
                # git_lib._run), not just whether a file was handed to it — a
                # STALE or CORRUPT warm map is still handed in, and only the
                # selector's own reason distinguishes a genuinely-used warm
                # map from one that triggered ITS OWN internal collector-
                # failure fallback to a full run. A silent cold/broken
                # fallback reported as "warm" is the exact under-selection the
                # ownership notes on this bead require to fail visible.
                detail_reason = _tier_selection_reason(cp.stdout) or warm_label
                ran.append(f"bin/ pytest impact tier ({detail_reason})")
                if cp.returncode != 0:
                    return (IMPACT_RED, f"bin/ pytest impact tier FAILED on the combined tree "
                                        f"(rc={cp.returncode}): {cp.stdout.strip()[-800:]}")

            if not ran:
                # Nothing selected and nothing to select from is not a pass —
                # it is a tier that told us nothing about the combined tree.
                return (IMPACT_ERROR, "impact tier selected no suites at all — no evidence about "
                                      "the combined tree")
            return (IMPACT_GREEN, f"impact tier GREEN on the combined tree: {', '.join(ran)}")
        finally:
            git_lib._git(repo, "worktree", "remove", "--force", worktree, check=False)
            git_lib._git(repo, "worktree", "prune", check=False)
    except subprocess.TimeoutExpired as exc:
        return (IMPACT_ERROR, f"impact tier timed out after {_impact_timeout()}s: {exc}")
    except (OSError, RuntimeError) as exc:
        return (IMPACT_ERROR, f"impact tier could not run: {exc}")
    finally:
        shutil.rmtree(parent, ignore_errors=True)


# --------------------------------------------------------------------------
# Green-snapshot freeze (SABLE-jd5fj.5) — the MECHANICAL deny path
# --------------------------------------------------------------------------

def assert_not_frozen(repo: str) -> None:
    """Refuse to promote while the green snapshot has the fleet FROZEN.

    MECHANICAL, not conventional, and that distinction is the whole bead. A
    freeze that lives in a manager's role text, a checklist, or a notify message
    is a freeze that holds exactly as long as everyone remembers it — and the
    moment it matters most (a broken integration branch, work queued behind it,
    pressure to land) is precisely when someone reasons their way past it. So
    the refusal is a code path in the only function that writes to the
    integration branch, and it is the FIRST thing that function does: before the
    fetch, before the preview, before any verdict is read. There is no state a
    promote can reach where the freeze has been checked and then unchecked.

    Deliberately NO environment kill switch, unlike SABLE_MG_OPTIMISTIC. That
    switch disables an OPTIMIZATION and its off-state is the safer one; this
    would disable a SAFETY MECHANISM and its off-state is the dangerous one — an
    env var is exactly the kind of bypass that leaves no name attached. The two
    ways out are a GREEN SNAPSHOT (the machine observing that the branch is
    healthy again) or `sable-snapshot unfreeze --reason "..."` (a human saying
    so, on the record).

    Reads fail-closed: an unreadable freeze file denies. See
    sable_snapshot_lib.read_freeze."""
    frozen = snapshot_lib.read_freeze(repo)
    if not frozen:
        return
    suites = ", ".join(frozen.get("suites") or []) or "unrecorded"
    bead = frozen.get("bead") or "unfiled"
    raise GateError(
        classify.EXIT_FROZEN,
        f"PROMOTION FROZEN by the green snapshot (SABLE-jd5fj.5) since "
        f"{frozen.get('since') or 'unknown'}: the integration branch is "
        f"deterministically red. Suites: {suites}. Bisect bead: {bead}. "
        f"Reason: {frozen.get('reason', '')}. No promotion until a green snapshot "
        f"clears the freeze, or an operator runs `sable-snapshot unfreeze "
        f"--reason \"...\"` on the record.")


# --------------------------------------------------------------------------
# promote — the only writer to the integration branch
# --------------------------------------------------------------------------

class _OptimisticNotApplicable(Exception):
    """The WIDENED (adoption-miss) entry looked at a queued green preview and the
    optimistic path did not apply to it. Internal to this module and never an
    exit code: on that entry there is nothing in flight to be non-fast-forward,
    so "rebuild and re-gate" is not a terminal state — it is simply the flow the
    caller runs next. See _adoption_miss_optimistic."""


def _stale_base(bead: str, branch: str, base: str, repo: str, remote: str,
                manager: str, base_ref: str, ref: str, preview_sha: str,
                base_sha: str, branch_sha: str, current_base: str,
                *, on_adoption_miss: bool = False) -> int:
    """The base moved out from under a GREEN preview. Decide what that costs.

    Reached from three places that mean the same thing — a green verdict for a
    preview whose base is no longer the base that would be merged onto:
      * the pre-push check (the base moved during the gate's own CI wait, and we
        notice before pushing);
      * the non-fast-forward push rejection (the narrow race where it moved
        between that check and the push);
      * the ADOPTION MISS (SABLE-kzi1a): the branch sat in the serial merge queue
        and an EARLIER promote moved the base past the one its push-time preview
        was kicked against. Structurally identical, and the only one of the three
        that can actually happen under a single-writer serial merge lane.
    All three funnel here so there is ONE stale-base decision path to reason
    about, which is what makes invariant I1 checkable at all — and so the
    widening cannot acquire its own, weaker, notion of what re-verification is.

    Returns an exit code, or raises GateError(23) for the full-re-preview
    outcome — the pre-jd5fj.4 contract, byte for byte, including the notify and
    the evidence line. `on_adoption_miss` changes exactly one thing: that
    re-preview outcome is raised as _OptimisticNotApplicable instead, because on
    that entry nothing has been pushed and the caller simply continues into the
    ordinary build-and-gate flow. Every other row — the tier, the invariants, the
    integrity assertion, the RED eject — is the same code."""
    if not optimistic_promotion_enabled():
        assessment = footprint_lib.Assessment(
            None, "optimistic disjoint promotion is disabled (SABLE_MG_OPTIMISTIC=0)")
    else:
        assessment = footprint_lib.assess(repo, bead, base_sha, branch_sha, current_base)

    decision = decide_promotion(classify.GREEN, base_moved=True, disjoint=assessment.disjoint,
                                impact=None, preview_sha=preview_sha)
    combined_sha, impact, impact_detail = "", None, ""
    if decision.action == ACTION_REVERIFY:
        print(f"sable-merge-gate: base {base} moved to {current_base[:7]} but the footprints are "
              f"disjoint — re-verifying the REAL combined tree with the impact tier "
              f"({len(assessment.paths)} changed path(s))")
        combined_sha = preview.build_preview(
            repo, current_base, branch_sha,
            f"ci-verify merge-preview: {branch} onto {base} ({bead}, disjoint re-verify)")
        impact, impact_detail = run_impact_tier(repo, combined_sha, list(assessment.paths))
        print(f"sable-merge-gate: impact tier on combined tree {combined_sha[:7]}: "
              f"{impact} — {impact_detail}")
        decision = decide_promotion(classify.GREEN, base_moved=True, disjoint=assessment.disjoint,
                                    impact=impact, preview_sha=preview_sha,
                                    combined_sha=combined_sha)

    if decision.action == ACTION_PROMOTE:
        # I2, asserted rather than assumed: the object pushed here must be the
        # object the impact tier just attested, not the stale CI-green preview.
        if not decision.reverified or decision.verified_sha != combined_sha:
            raise GateError(4, f"integrity abort: stale-base promotion would push "
                               f"{decision.verified_sha} which is not the re-verified "
                               f"combined object {combined_sha}")
        push_cp = git_lib._git(repo, "push", remote, f"{combined_sha}:refs/heads/{base}", check=False)
        if push_cp.returncode != 0:
            # The base moved AGAIN, during the re-verification. One optimistic
            # attempt per promote, by construction — no loop, no second window.
            _notify(manager,
                f"merge-preview ci-verify gate for {bead} ({branch}): base {base} moved a second time "
                f"during the disjoint re-verification — NOT promoted. Rebuild preview + re-gate.")
            _append_evidence(repo, bead,
                f"merge-preview ci-verify gate BASE-MOVED-TWICE (retryable): combined {combined_sha}, "
                f"non-ff on promote: {push_cp.stdout.strip()}")
            raise GateError(23, f"base {base} advanced again during the disjoint re-verification — "
                                f"rebuild and re-gate")
        git_lib._git(repo, "fetch", remote, base)
        landed = git_lib.resolve_commit(repo, base_ref)
        if landed != combined_sha:
            raise GateError(4, f"integrity abort: base {base} tip {landed} != re-verified combined "
                               f"tree {combined_sha}")
        _append_evidence(repo, bead,
            f"merge-preview ci-verify gate GREEN via OPTIMISTIC DISJOINT PROMOTION (SABLE-jd5fj.4): "
            f"ref {ref}, CI-green preview {preview_sha} was built on base {base_sha[:7]}, which moved "
            f"to {current_base[:7]}. Footprints disjoint ({assessment.reason}). The combined tree "
            f"{combined_sha} was RE-VERIFIED on the real merge: {impact_detail}. Promoted that same "
            f"combined object byte-identical to {base} — NOT the stale preview.")
        try:
            cleanup_after_merge(repo, remote, base_ref, branch)
        except Exception as exc:  # noqa: BLE001 — a green merge must stay green
            print(f"sable-merge-gate cleanup: skipped after unexpected error: {exc}",
                  file=sys.stderr)
        return classify.EXIT_OK

    if decision.action == ACTION_REFUSE:
        # Impact-tier RED on the real combined tree: two changes that were each
        # green alone break together. Ejects on the SAME exit-20 path a red CI
        # run takes, because it is the same kind of fact — a real defect with a
        # named author to fix it.
        _notify(manager,
            f"merge-preview ci-verify gate RED for {bead} ({branch}) on the COMBINED TREE: the branch "
            f"was CI-green on base {base_sha[:7]}, but base {base} has moved to {current_base[:7]} and "
            f"the impact tier FAILS on the real merge. No promotion. {impact_detail}")
        _append_evidence(repo, bead,
            f"merge-preview ci-verify gate RED on the combined tree (optimistic disjoint re-verify, "
            f"SABLE-jd5fj.4): preview {preview_sha} was green on {base_sha[:7]}; combined "
            f"{combined_sha} with base {current_base[:7]} is RED: {impact_detail}. NOT promoted.")
        return decision.exit_code or classify.EXIT_RED

    # ACTION_REPREVIEW — the pre-jd5fj.4 contract, unchanged.
    detail = assessment.reason if impact is None else f"{assessment.reason}; {impact_detail}"
    if on_adoption_miss:
        # SABLE-kzi1a: nothing is in flight on this entry, so there is no
        # non-fast-forward promote to report and nothing to rebuild — the caller
        # has simply learned that the queued preview is not usable and goes on to
        # build one the pre-kick way, which is what it would have done anyway.
        # Exiting 23 here instead would STRAND the branch: every retry finds the
        # same queued preview and refuses again, forever.
        raise _OptimisticNotApplicable(f"{decision.reason}: {detail}")
    _notify(manager,
        f"merge-preview ci-verify gate for {bead} ({branch}): base {base} moved during the CI "
        f"wait — promote is non-fast-forward, NOT promoted. Rebuild preview + re-gate (ref {ref} was green). "
        f"[{decision.reason}: {detail}]")
    _append_evidence(repo, bead,
        f"merge-preview ci-verify gate BASE-MOVED (retryable): ref {ref}, preview {preview_sha}, "
        f"base moved {base_sha[:7]} -> {current_base[:7]}, not promoted. {decision.reason}: {detail}")
    # The reason travels on the EXCEPTION, not only into the notify/evidence
    # seams: a refusal a reader cannot audit from the gate's own output is a
    # bare exit code, and this is the path an operator investigates most.
    raise GateError(23, f"base {base} advanced during CI; preview {preview_sha} is non-ff — "
                        f"{decision.reason} [{detail}] — rebuild and re-gate")


def _adoption_miss_optimistic(bead: str, branch: str, base: str, repo: str, remote: str,
                              manager: str, base_ref: str, base_sha: str,
                              branch_sha: str) -> int | None:
    """THE WIDENED ENTRY to the optimistic disjoint path (SABLE-kzi1a). An exit
    code if this promote was decided here, or None to run the ordinary flow.

    WHY IT EXISTS. jd5fj.4 wired the optimistic path to one caller: the base
    moving during the gate's OWN CI wait. Chuck promotes SERIALLY and is the only
    writer to the integration branch, so while he is inside a promote nothing can
    move the base under it — that caller is unreachable by construction in the
    operating model, and the telemetry agreed: 0 optimistic promotions in 157,
    and 0 across the 15-worker burst that queued 11 branches and was supposed to
    be its first real test. Meanwhile the branches WAITING in that queue were in
    the exact situation the path was built for. Each merge invalidated their
    push-time previews, and each subsequent promote threw away a COMPLETED GREEN
    CI run and paid for a fresh one — the optimization degrading precisely when
    it was needed most.

    WHAT IT DOES NOT CHANGE. It reaches decide_promotion with the same inputs the
    mid-gate caller does, by calling the same _stale_base: the footprint
    assessment is the same computation, and the IMPACT TIER STILL RE-VERIFIES THE
    REAL COMBINED TREE before anything is promoted. A wider entry to a path whose
    verification was weakened would be worse than no fix at all, so the entry is
    the only thing that widens.

    Not taken when the operator has closed the optimistic path
    (SABLE_MG_OPTIMISTIC=0) — the kill switch must restore the pre-jd5fj.4
    behaviour exactly, and that includes never going looking.

    No cmar4.4 budget check here, deliberately: that measurement IS the gate's
    wall-clock for the merge_preview tier, and this path spends none of it — the
    verdict was already stored. Recording a near-zero sample would deflate the
    tier's own statistics with time it did not spend. What this path DOES cost is
    the impact tier, which is a different tier with a different budget. The cost
    that stays unmeasured either way is push->LANDED, which is SABLE-q4rn4."""
    if not optimistic_promotion_enabled():
        return None
    stale = preview.find_stale_green_preview(repo, remote, branch, base_sha, branch_sha)
    if stale is None:
        return None
    print(f"sable-merge-gate: {branch} already has a GREEN preview ({stale.preview_sha[:7]} "
          f"on ref {stale.ref}) built on base {stale.base_sha[:7]}, which the queue has moved "
          f"past to {base_sha[:7]} — assessing it instead of discarding it (SABLE-kzi1a)")
    try:
        code = _stale_base(bead, branch, base, repo, remote, manager, base_ref,
                           stale.ref, stale.preview_sha, stale.base_sha, branch_sha,
                           base_sha, on_adoption_miss=True)
    except _OptimisticNotApplicable as exc:
        print(f"sable-merge-gate: the queued preview for {branch} is not usable against the "
              f"current base ({exc}) — building a fresh preview the pre-kick way")
        return None
    # Consumed: this ref's run is complete and its object has been decided on, so
    # the throwaway ref is now what the ordinary flow's finally-block would make
    # it. Best-effort, exactly like that one. NOT reached when _stale_base raises
    # (a second base move, an integrity abort, a conflict) — there the preview is
    # still the best evidence the next attempt has.
    preview.delete_ci_ref(repo, remote, stale.ref)
    return code


def promote(bead: str, branch: str, base: str, repo: str, remote: str,
            manager: str, override: str | None) -> int:
    # FIRST, before any git work: is the fleet frozen? (SABLE-jd5fj.5)
    assert_not_frozen(repo)
    base_ref = classify.qualify_remote_ref(remote, base)
    branch_ref = classify.qualify_remote_ref(remote, branch)
    git_lib._git(repo, "fetch", remote, base, branch)
    base_sha = git_lib.resolve_commit(repo, base_ref)
    branch_sha = git_lib.resolve_commit(repo, branch_ref)

    # SABLE-kzi1a: before building anything, is this an ADOPTION MISS with a
    # green preview already sitting behind it — the queued-branch case a serial
    # merge lane manufactures on every promote after the first? Asked HERE,
    # before materialize_preview, because that call is what would push a second
    # ci-verify ref and start the redundant CI run this bead exists to stop.
    # Skipped under --override: that bypass consults no run at all by contract,
    # and this entry is entirely about consuming a run that already exists.
    widened = None if override else _adoption_miss_optimistic(
        bead, branch, base, repo, remote, manager, base_ref, base_sha, branch_sha)
    if widened is not None:
        return widened

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
            # seconds-long read.
            #
            # SABLE-cmar4.4: this call IS the gate's own wall-clock for the
            # merge_preview tier — whatever acquire_verdict spends (a fast
            # precomputed read, or a fall-through wait_for_ci poll) is exactly
            # what a human waiting on this promote experiences. Measured here,
            # never inside acquire_verdict/wait_for_ci themselves, so the
            # budget check stays a promote-only concern the preview module
            # never has to know about. check_and_file never raises and never
            # changes the promotion outcome — a breach only WARNs + auto-files
            # (idempotently) a suite-optimization bead.
            t0 = time.monotonic()
            verdict = preview.acquire_verdict(repo, ref, preview_sha)
            budget_lib.check_and_file(repo, "merge_preview", time.monotonic() - t0,
                                      context=f"bead={bead} branch={branch} ref={ref}")
        conclusion, url = verdict.conclusion, verdict.run_url

        if verdict.outcome == classify.GREEN:
            # SABLE-jd5fj.4: is the base still the commit this preview was built
            # on? Asked BEFORE the push, so the stale-base decision is made from
            # an observation rather than inferred from a rejection — the
            # optimistic path needs the new base SHA to compute a footprint
            # against, and a rejection message does not carry one.
            git_lib._git(repo, "fetch", remote, base, check=False)
            current_base = git_lib.resolve_commit(repo, base_ref)
            if current_base != base_sha:
                return _stale_base(bead, branch, base, repo, remote, manager, base_ref,
                                   ref, preview_sha, base_sha, branch_sha, current_base)
            push_cp = git_lib._git(repo, "push", remote, f"{preview_sha}:refs/heads/{base}", check=False)
            if push_cp.returncode != 0:
                # F1 (tarzan review): the base advanced during the CI wait, so the
                # promote is non-fast-forward. Nothing wrong was shipped — the push
                # was REJECTED — the tested-green ref is simply stale against a base
                # that moved. Exit cleanly and retryably instead of letting
                # CalledProcessError escape as an uncaught traceback. Cleanup still
                # runs via the finally below.
                #
                # jd5fj.4 narrowed this to the RACE it always described: the base
                # moved between the check above and this push. It funnels into the
                # same stale-base decision, so there is no second, divergent
                # base-moved path (which is how invariant I1 stays checkable).
                git_lib._git(repo, "fetch", remote, base, check=False)
                current_base = git_lib.resolve_commit(repo, base_ref)
                if current_base != base_sha:
                    return _stale_base(bead, branch, base, repo, remote, manager, base_ref,
                                       ref, preview_sha, base_sha, branch_sha, current_base)
                _notify(manager,
                    f"merge-preview ci-verify gate for {bead} ({branch}): promote to {base} was rejected "
                    f"though the base tip still reads {base_sha[:7]} — NOT promoted. Rebuild preview + re-gate "
                    f"(ref {ref} was green).")
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
