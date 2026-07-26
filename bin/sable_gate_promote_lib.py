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

import ast
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import sable_batch_admission_lib as admission
import sable_batch_fold_lib as fold_lib
import sable_batch_key_lib as batch_key
import sable_coverage_floor_lib as coverage_floor_lib
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


def _impact_timeout(repo: str | None = None) -> float:
    """SABLE-jd5fj.9: the impact tier's own run budget, derived from the
    merge_preview tier's SSOT (.github/ci/test-tiers.sh, via
    sable_gate_budget_lib.tier_budget_sec) instead of a hand-copied literal —
    the same duplicated-list class SABLE-cmar4.1 closed for tier membership,
    reintroduced one level down as this function's old body.

    SABLE_MG_IMPACT_TIMEOUT is an explicit override and always wins over the
    SSOT. `repo` defaults to the current working directory so callers with no
    repo path yet (sable-merge-gate promote-budget is deliberately --repo-less
    — see its own parser help) still get an answer: tier_budget_sec fails
    closed (returns None) when cwd has no test-tiers.sh, and this falls back
    to the pre-fix constant (900) for that case — never raises, mirroring
    git_lib.default_mg_timeout's never-raises contract."""
    override = os.environ.get("SABLE_MG_IMPACT_TIMEOUT")
    if override is not None:
        return float(override)
    budget = budget_lib.tier_budget_sec(repo or os.getcwd(), "merge_preview")
    return budget if budget is not None else 900.0


def _selected_suites(repo: str, worktree: str, paths: list[str]) -> list[str]:
    """Suites the shell impact manifest selects for these changed paths
    (SABLE-cmar4.2). Its own contract already handles the dangerous direction:
    a path it cannot map selects the FULL allow-list rather than nothing."""
    sel = git_lib._run(["bash", ".github/ci/impact-manifest.sh", "--select", *paths],
                       cwd=worktree, check=False, timeout=_impact_timeout(repo))
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
#      automatically after every impact-tier run, because an impact-tier run
#      never refreshes the coverage map itself. See below for WHY, which is
#      not what an earlier revision of this comment claimed (SABLE-jd5fj.19).
#
# WHY AN IMPACT-TIER RUN NEVER REFRESHES THE MAP (SABLE-jd5fj.19) -- and the
# invariant a maintainer must not break:
#
#   build_impact_tier_plan DOES pass --testmon (tier_selection.py, in the
#   cache-hit branch: `collector(repo_root, ["--testmon"])`). An earlier
#   revision of this comment asserted the opposite -- "neither tier mode
#   passes pytest-testmon's own --testmon/--testmon-noselect flags" -- and
#   that is simply false. What makes the tier safe is narrower and load-
#   bearing: --testmon is only ever handed to the --COLLECT-ONLY collector
#   (tier_selection._pytest_collect_only shells out to
#   `pytest bin/ --collect-only -q <extra_args>`), which never executes a
#   test body. The tier's one EXECUTING pytest call, in run_impact_tier,
#   runs plan.argv -- node ids, or the full-run argv -- and carries no
#   testmon flag at all.
#
#   So the true invariant is "--testmon only ever reaches a --collect-only
#   invocation", NOT "--testmon is never passed". The distinction matters
#   because pytest-testmon 2.2.0's extensionless-file crash
#   (testmon_core.py:93 `filename.rsplit(".", 1)[1]` -> IndexError) is raised
#   from pytest_runtest_logreport (pytest_testmon.py:416) -- a PER-TEST-
#   EXECUTION hook. Under --collect-only no test executes, so the hook never
#   fires and the crash site is structurally unreachable; this repo's bin/
#   carries 44 extensionless files, 34 of them python by shebang (counted
#   2026-07-26), so an executing --testmon run here does reach it -- and
#   reproducing it needs only ONE such file that a test loads in-process
#   (measured; see test_tier_selection_integration.extensionless_repo)
#   (see tier_selection.classify_cache_warm_outcome, which exists
#   solely to tolerate that crash on the one path that deliberately takes
#   it). A maintainer who believed the old blanket claim and "restored"
#   --testmon to a genuine, non-collect-only call would reintroduce the crash
#   into the merge seat's local impact tier as innocent REDs on unrelated
#   branches. That is now pinned mechanically rather than by this prose:
#   bin/test_tier_selection.py's "--testmon implies --collect-only" section
#   fails on exactly that change.
#
#   Refreshing the map is a separate consequence of the same fact. A
#   collect-only --testmon run does write to .testmondata -- it records the
#   node ids it collected and a checksum of each test FILE (measured) -- but
#   it can never record which files a test EXECUTES, since nothing executed.
#   Only a genuinely-executing --testmon/--testmon-noselect run produces
#   those coverage fingerprints, which is what warm_gate_testmon_cache and
#   CI's testmon-cache-warm.sh are for.
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

# SABLE-mbkbm: bumped when "end" records started carrying a "phases" list.
# Records written before this bead (jd5fj.13's original five keys: event, pid,
# at, tree, waited) carry no "schema" key at all -- readers must treat a
# missing key as schema 1 and EXCLUDE it from phase aggregates rather than
# reading a missing "phases" as zero phases. A fabricated 0.0 duration is
# indistinguishable from "ran instantly" and would poison the aggregate the
# same way an inferred split would (see impact_tier_phase_report).
IMPACT_WINDOW_SCHEMA_VERSION = 2


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


def impact_budget(repo: str | None = None) -> dict:
    """The gate's own worst-case promote wall-clock, so an ENCLOSING wrapper can
    DERIVE its timeout instead of hardcoding one (SABLE-w0zjm).

    `repo` defaults to the current working directory (see _impact_timeout) so
    the --repo-less `promote-budget` CLI command keeps working unchanged.

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
    a promote can spend LOCK WAIT + TIER + COVERAGE FLOOR, i.e. ~5400s on stock
    defaults. A wrapper sized to the tier budget alone is MORE wrong after
    jd5fj.13, not less: under a burst the queue wait alone can exceed it.

    THE COVERAGE FLOOR IS A THIRD TERM, NOT A FOOTNOTE (SABLE-5v3d5). cmar4.5
    put assert_coverage_floor()'s subprocess check (run_coverage_floor_check,
    bounded by _coverage_floor_timeout) INSIDE promote(), deliberately early —
    before any preview/CI work — so a promote may legitimately spend that
    ceiling too, on top of the queue wait and the impact tier. Before this fix
    that ceiling was NOT in this sum: cmar4.9 later re-pinned
    _coverage_floor_timeout to borrow the same SSOT the tier budget does (900s
    in place of a hardcoded 600s) and worst_case_s did not move, because the
    changed term was never a summand — an instrument that cannot see its own
    blind spot, pointed at the number that bounds every promote.

    Reported in seconds:
      tier_timeout_s           SABLE_MG_IMPACT_TIMEOUT — the tier's own run
                                budget.
      lock_timeout_s           SABLE_MG_IMPACT_LOCK_TIMEOUT — how long a
                                promote will queue for the seat. 0 when
                                serialization is off, because then there is no
                                queue to wait in.
      coverage_floor_timeout_s SABLE_MG_COVERAGE_FLOOR_TIMEOUT (or its SSOT
                                default, see _coverage_floor_timeout) — the
                                coverage-delta check's own run budget, paid on
                                every promote of a pruning diff, BEFORE the
                                queue wait or the tier even start.
      worst_case_s              the sum of all three: the longest a promote
                                may legitimately take.
      recommended_wrapper_timeout_s   worst_case_s * BUDGET_HEADROOM, rounded up.
                      This is the number a wrapper should use.
      serialized      whether the queue is in play at all."""
    tier = _impact_timeout(repo)
    lock = _impact_lock_timeout() if impact_serialization_enabled() else 0.0
    coverage_floor = _coverage_floor_timeout(repo)
    worst = tier + lock + coverage_floor
    return {
        "tier_timeout_s": tier,
        "lock_timeout_s": lock,
        "coverage_floor_timeout_s": coverage_floor,
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
        f"  coverage floor {budget['coverage_floor_timeout_s']:.0f}s  "
        f"(SABLE_MG_COVERAGE_FLOOR_TIMEOUT, paid on a pruning diff BEFORE the "
        f"queue wait or the tier even start)",
        f"  worst case   {budget['worst_case_s']:.0f}s  (queue + tier + coverage floor)",
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


# --------------------------------------------------------------------------
# The combined-tree BATCH budget (SABLE-be4lo.6) — a NEW named field, never
# an overload of recommended_wrapper_timeout_s
# --------------------------------------------------------------------------

BISECTION_RESERVE_RUNS = 3
"""Architecture decision 4 of the SABLE-be4lo epic: a red n=4 batch bisects
into at most 3 EXTRA combined runs before a member is isolated or the batch
reports COULD-NOT-ATTRIBUTE. Each bisection round re-runs only the impact
tier on a re-formed fold chain — it does not re-queue for the seat lock or
re-pay the coverage floor, both of which are paid once per batch (decision 7:
a batch is one promote). The reserve is therefore RESERVE_RUNS extra
tier_timeout_s terms, not extra worst_case_s terms."""


def combined_tree_budget(member_footprints: list, repo: str | None = None) -> dict:
    """SABLE-be4lo.6: price a BATCH's combined tree — union of member
    footprints, member impact tiers on that tree, plus the bisection reserve
    — as its OWN named field, `recommended_batch_wrapper_timeout_s`, never by
    overloading impact_budget()'s recommended_wrapper_timeout_s (the 5v3d5
    lesson: one name carrying two quantities is how a fix straddles both
    readings of a number at once). This function is purely additive:
    impact_budget() itself is untouched, so the single-branch report and its
    recommended_wrapper_timeout_s stay byte-identical to before this bead.

    `member_footprints` is one entry (an iterable of changed paths) per batch
    member. Every member currently prices against the SAME repo-wide tier
    SSOT — impact_budget() has no per-path tier selection yet (the fleet
    still runs a single flat "merge_preview" tier, SABLE-cmar4.1) — so today
    every member's tier_timeout_s is identical and the max() below is a
    no-op. The max()-over-members shape is kept anyway so a future
    footprint-sensitive tier slots in here without a second combined-budget
    function needing to be built to receive it; a caller may already
    monkeypatch/stub impact_budget() per member to exercise that shape ahead
    of time.

    Governing precedent: no hand-carried terms. A batch wrapper derives its
    timeout from THIS field at run time (`sable-merge-gate promote-budget
    --member-footprint ... --seconds`); copying the number forfeits the
    derivation, the same retired +900 pattern impact_budget()'s own docstring
    already warns against.

    Raises ValueError on an empty batch — a zero-member request is a caller
    bug, not a valid "no reserve needed" answer, so it must never return a
    number (SABLE-p9n7k vacuous-pass discipline)."""
    if not member_footprints:
        raise ValueError("combined_tree_budget requires at least one member footprint")

    member_budgets = [impact_budget(repo) for _ in member_footprints]
    tier_timeout_s = max(m["tier_timeout_s"] for m in member_budgets)
    # Paid ONCE per batch (decision 7: a batch is one promote), so any
    # member's own value is the same as any other's under the current
    # repo-wide (not per-member) lock/coverage-floor knobs.
    lock_timeout_s = member_budgets[0]["lock_timeout_s"]
    coverage_floor_timeout_s = member_budgets[0]["coverage_floor_timeout_s"]
    bisection_reserve_s = BISECTION_RESERVE_RUNS * tier_timeout_s
    worst_case_s = tier_timeout_s + lock_timeout_s + coverage_floor_timeout_s + bisection_reserve_s

    union_footprint_paths = sorted({path for fp in member_footprints for path in fp})

    return {
        "member_count": len(member_footprints),
        "union_footprint_paths": union_footprint_paths,
        "tier_timeout_s": tier_timeout_s,
        "lock_timeout_s": lock_timeout_s,
        "coverage_floor_timeout_s": coverage_floor_timeout_s,
        "bisection_reserve_runs": BISECTION_RESERVE_RUNS,
        "bisection_reserve_s": bisection_reserve_s,
        "worst_case_s": worst_case_s,
        "recommended_batch_wrapper_timeout_s": int(math.ceil(worst_case_s * BUDGET_HEADROOM)),
        "serialized": member_budgets[0]["serialized"],
    }


def format_combined_tree_budget(budget: dict) -> str:
    """Human-readable breakdown, mirroring format_impact_budget's shape and
    the same "say which number is which" discipline."""
    return "\n".join([
        f"sable-merge-gate combined-tree batch budget ({budget['member_count']} members):",
        f"  impact tier  {budget['tier_timeout_s']:.0f}s  (worst tier required by any member)",
        f"  queue wait   {budget['lock_timeout_s']:.0f}s",
        f"  coverage floor {budget['coverage_floor_timeout_s']:.0f}s",
        f"  bisection reserve {budget['bisection_reserve_s']:.0f}s  "
        f"({budget['bisection_reserve_runs']} extra combined runs reserved, "
        f"architecture decision 4)",
        f"  worst case   {budget['worst_case_s']:.0f}s",
        f"  RECOMMENDED enclosing batch wrapper timeout: "
        f"{budget['recommended_batch_wrapper_timeout_s']}s",
        "",
        "A NEW named field — never recommended_wrapper_timeout_s, which stays the",
        "single-branch number unchanged (SABLE-be4lo.6, the 5v3d5 lesson: one name,",
        "one quantity). Any wrapper enclosing a batched `sable-merge-gate promote`",
        "MUST derive from recommended_batch_wrapper_timeout_s, not copy it.",
    ])


def render_promote_budget_report(member_footprints: list | None, seconds: bool,
                                 as_json: bool) -> str:
    """The `promote-budget` CLI command's dispatch, factored out of
    bin/sable-merge-gate so the single-branch/combined-tree-batch branch this
    bead adds, plus the seconds/json/text format selection, both stay inside
    the thin-CLI budget test_merge_gate_modules.py::test_cli_is_thin enforces.

    `member_footprints` is the raw repeated --member-footprint values (each
    entry one member's comma-separated changed paths); empty/None reports the
    pre-existing single-branch budget, unchanged."""
    if member_footprints:
        members = [fp.split(",") for fp in member_footprints]
        budget = combined_tree_budget(members)
        if seconds:
            return str(budget["recommended_batch_wrapper_timeout_s"])
        if as_json:
            return json.dumps(budget, sort_keys=True)
        return format_combined_tree_budget(budget)
    budget = impact_budget()
    if seconds:
        return str(budget["recommended_wrapper_timeout_s"])
    if as_json:
        return json.dumps(budget, sort_keys=True)
    return format_impact_budget(budget)


# --------------------------------------------------------------------------
# Promote-path timeout completeness (SABLE-5v3d5) — catches the NEXT omission
# --------------------------------------------------------------------------
#
# impact_budget()'s coverage-floor omission happened because the coupling
# between "a bounded subprocess call lives in promote()'s path" and "its
# ceiling is a summand in impact_budget()" is maintained by memory: nothing
# breaks when the two drift apart, which is exactly how they drifted apart.
# This is the structural half of the fix — a check that FAILS the moment a
# new `timeout=`-bearing call joins promote()'s reachable call graph without
# a registered budget term, in the spirit of cmar4.2's manifest fan-out check
# (.github/ci/impact-manifest.sh) erroring on an unmapped sourced lib rather
# than silently under-selecting.

_BUDGETED_TIMEOUT_SOURCES = {
    "_impact_timeout": "tier_timeout_s",
    "_impact_lock_timeout": "lock_timeout_s",
    "_coverage_floor_timeout": "coverage_floor_timeout_s",
}
"""Maps the helper a `timeout=` keyword argument calls to get its value, to
the impact_budget() key that accounts for it. A `timeout=` call reachable
from promote() whose source is neither a key here nor in
_KNOWN_UNBUDGETED_PROMOTE_TIMEOUTS below is an unbudgeted term —
unbudgeted_promote_timeouts() reports it."""

_KNOWN_UNBUDGETED_PROMOTE_TIMEOUTS = {
    ("_report_identifier_decay", "30"),
    ("_impact_isolated_env", "60"),
}
"""(function, literal-source) pairs already reachable from promote() at the
time this check was written that are deliberately NOT folded into
worst_case_s: both are small (<=60s), run at most once per promote, and are
independently exception/return-guarded so they cannot hang past their own
bound (see _report_identifier_decay's broad except and
_impact_isolated_env's caller, whose worktree-setup failures already return
IMPACT_ERROR rather than hang). Flagged HERE, visibly, rather than silently
passing the check — SABLE-5v3d5 filed the follow-up to fold them in or
re-justify the exclusion (bd q). This set exists so a THIRD, unreviewed one
cannot join them unnoticed: it is matched by EXACT (function, literal) pair,
not by function name alone, so a literal that changes (the way the coverage
floor's 600 once did) still falls through as a fresh gap."""


def _reachable_function_defs(tree: ast.Module, entry: str) -> dict[str, ast.FunctionDef]:
    """Every module-level function def reachable from `entry` by NAME-based
    call references, transitively. Derived from the AST rather than
    hand-listed, so a new function promote() starts calling is automatically
    in scope — the completeness property this check exists for must not
    itself depend on someone remembering to extend a list."""
    defs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    seen: dict[str, ast.FunctionDef] = {}
    stack = [entry]
    while stack:
        name = stack.pop()
        if name in seen or name not in defs:
            continue
        node = defs[name]
        seen[name] = node
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                stack.append(call.func.id)
    return seen


def _timeout_kwarg_sources(node: ast.AST):
    """Yields the source of every `timeout=` keyword argument in any call
    within `node`: the callee's name when the value is itself a call (e.g.
    `_impact_timeout(repo)` -> "_impact_timeout"), else the literal's own
    text (so two different literal ceilings, e.g. 30 vs 60, are never
    conflated into one entry)."""
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        for kw in call.keywords:
            if kw.arg != "timeout":
                continue
            if isinstance(kw.value, ast.Call) and isinstance(kw.value.func, ast.Name):
                yield kw.value.func.id
            elif isinstance(kw.value, ast.Constant):
                yield str(kw.value.value)
            else:
                yield ast.dump(kw.value)


def unbudgeted_promote_timeouts(tree: ast.Module, entry: str = "promote",
                                 budgeted: set | None = None,
                                 known_unbudgeted: set | None = None) -> list:
    """THE COMPLETENESS CHECK (SABLE-5v3d5). Returns (function, source) pairs
    for every `timeout=`-bearing call reachable from `entry` whose source is
    neither a registered budget term (`budgeted`, defaulting to
    _BUDGETED_TIMEOUT_SOURCES) nor a documented exclusion (`known_unbudgeted`,
    defaulting to _KNOWN_UNBUDGETED_PROMOTE_TIMEOUTS). Empty means every
    bounded call in the promote path is accounted for by impact_budget() or
    explicitly, visibly excused from it.

    Takes an already-parsed `tree` (rather than reading this module's own
    __file__ internally) so a test can hand it a synthetic snippet and prove
    the check actually fires on a planted gap, without touching this file."""
    budgeted = budgeted if budgeted is not None else set(_BUDGETED_TIMEOUT_SOURCES)
    known_unbudgeted = (known_unbudgeted if known_unbudgeted is not None
                        else _KNOWN_UNBUDGETED_PROMOTE_TIMEOUTS)
    gaps = []
    for name, node in _reachable_function_defs(tree, entry).items():
        for source in _timeout_kwarg_sources(node):
            if source in budgeted or (name, source) in known_unbudgeted:
                continue
            gaps.append((name, source))
    return gaps


def promote_module_ast() -> ast.Module:
    """This module's own AST, freshly re-read from disk — see
    unbudgeted_promote_timeouts()."""
    return ast.parse(Path(__file__).read_text(), filename=__file__)


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
    and the give-up is a value (IMPACT_ERROR) rather than a hang.

    HERMETICIZATION LANDED (SABLE-jd5fj.15): _impact_isolated_env now gives
    every suite/override invocation its own BEADS_DB/HOME/TMPDIR, so the
    underlying collision this lock queues around is gone, not merely
    serialized — hooks/test/test-impact-tier-serialization.sh's S7 proves two
    REAL concurrent tiers with this lock DISABLED (SABLE_MG_IMPACT_SERIALIZE=0)
    neither observe nor corrupt each other's state. This lock stays ON by
    default anyway: it is now a belt-and-suspenders mechanical rule rather than
    the only thing standing between a promote burst and a false-RED, and
    dropping a control that is still free is a separate, deliberately
    unbundled decision, not a corollary of this one landing."""
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
                         waited: float, phases: list[dict] | None = None) -> None:
    """Append one start/end record for this tier run. Best-effort and never
    load-bearing on the verdict — it exists so a human (and
    hooks/test/test-impact-tier-serialization.sh) can see whether two tier
    WINDOWS overlapped, which is the only direct evidence that the lock is doing
    its job. Overlap is invisible from suite results alone: that is precisely why
    the pile-up read as six broken branches instead of one broken control.

    SABLE-mbkbm: `phases` (only ever passed on the "end" event, once
    run_impact_tier knows what it ran) is the per-phase wall-clock breakdown —
    setup, each shell suite by name, the pytest half — that answers "where did
    the tier's own time go" instead of only "how long did it take in total".
    Every record still carries the original five keys unchanged, so a mixed-
    vintage journal stays readable by old consumers; "schema" and "phases" are
    additive."""
    try:
        path = Path(os.environ.get("SABLE_MG_IMPACT_WINDOW_LOG")
                    or (snapshot_lib.ensure_state_dir(repo) / IMPACT_WINDOW_FILE))
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"schema": IMPACT_WINDOW_SCHEMA_VERSION, "event": event, "pid": os.getpid(),
                  "at": time.time(), "tree": tree_sha[:12], "waited": round(waited, 3)}
        if phases is not None:
            record["phases"] = phases
        with open(path, "a") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


class TierWriterIdentity(str, Enum):
    """WHO/WHAT produced a given impact-tier journal window — the tier
    journal's additive 'writer_identity' field (SABLE-21rug.1). Typed so a
    producer discriminator cannot be typo'd into an unenumerated value the
    way a bare string could (Primitive Obsession guard).

    GATE     — the merge gate's own automated re-verification. Every call
               site in this module produces this identity today.
    HAND_RUN — a human explicitly ran the tier outside the gate's own flow.
               Reserved: no caller sets this yet — this epic's runner-offload
               sibling is what will ever produce it, so the schema does not
               need a second migration when that caller lands.
    """
    GATE = "gate"
    HAND_RUN = "hand_run"


class TierVerdict(str, Enum):
    """Typed mirror of IMPACT_GREEN/IMPACT_RED/IMPACT_ERROR for the tier
    journal's additive 'verdict' field (SABLE-21rug.1) — the same enumerated
    vocabulary the decision table already uses, wrapped here so the journal
    write validates against it instead of accepting an arbitrary string."""
    GREEN = IMPACT_GREEN
    RED = IMPACT_RED
    ERROR = IMPACT_ERROR


def _stamp_impact_verdict(repo: str | os.PathLike, tree_sha: str, verdict: str,
                          writer_identity: TierWriterIdentity = TierWriterIdentity.GATE) -> None:
    """Fold the tier's typed outcome + producer identity into the JUST-WRITTEN
    'end' record of impact-tier-windows.jsonl, as ADDITIVE keys on that SAME
    line (SABLE-21rug.1) — never a new line/event, and never a change to
    _stamp_impact_window's own call signature.

    Why an in-place augment and not a new call argument on the existing
    writes: run_impact_tier's two calls to _stamp_impact_window are exercised
    via monkeypatch with a FIXED signature by bin/test_promote_decision.py (a
    suite outside this bead's declared footprint) — adding a kwarg there
    would break it. Why not a separate 'verdict' event instead: a sibling
    test in that same file (test_the_tier_window_log_records_both_edges)
    asserts the window log's event sequence is EXACTLY ["start", "end"] per
    run — a third event breaks that literal check too. Rewriting the 'end'
    line's own dict in place is the shape compatible with both constraints,
    and it is exactly what 'additive fields' means at the record level.

    Safe under concurrency: this runs inside run_impact_tier's SAME
    impact_tier_lock critical section that guards every write to this file,
    so no other writer can be mid-append while this reads/rewrites it.

    Best-effort like its sibling: any failure here (file absent because
    _stamp_impact_window was itself stubbed out by a caller, an unenumerated
    verdict, a disk error) leaves the 'end' line exactly as
    _stamp_impact_window wrote it and must never affect a promote's real
    outcome."""
    try:
        path = Path(os.environ.get("SABLE_MG_IMPACT_WINDOW_LOG")
                    or (snapshot_lib.ensure_state_dir(repo) / IMPACT_WINDOW_FILE))
        if not path.is_file():
            return
        lines = path.read_text().splitlines()
        if not lines:
            return
        last = json.loads(lines[-1])
        if (last.get("event") != "end" or last.get("tree") != tree_sha[:12]
                or last.get("pid") != os.getpid()):
            return
        last["verdict"] = TierVerdict(verdict).value
        last["writer_identity"] = TierWriterIdentity(writer_identity).value
        lines[-1] = json.dumps(last)
        path.write_text("\n".join(lines) + "\n")
    except (OSError, ValueError, json.JSONDecodeError):
        pass


def _record_phase(phases: list[dict], name: str, started: float) -> None:
    """Append one measured (not inferred) phase span to `phases`, timed from
    `started` (a prior time.monotonic() call) to now. Multiple entries can share
    a `name` (e.g. worktree setup and teardown both report "setup") — the phase-2
    reader (impact_tier_phase_report) sums by name, so this stays a direct
    measurement of each real span rather than a derived split of a total."""
    phases.append({"name": name, "seconds": round(time.monotonic() - started, 3)})


_SUITE_STRICT_FAIL_RE = re.compile(r'(?im)^FAIL(?:ED|URE)?\b.*$')
_SUITE_LOOSE_FAIL_RE = re.compile(r'(?im)^.*\bFAIL(?:ED|URE)?\b.*$')


def _anchor_failure_region(text: str) -> tuple[int, str]:
    """Pick where a bounded failure excerpt should start, and NAME the rule
    that picked it (SABLE-1u6dr).

    A single loose regex — any line CONTAINING FAIL/FAILED/FAILURE anywhere —
    anchors just as readily on a PASSING line that merely mentions the word
    in its message (e.g. "PASS: SABLE-mji: bd failure fails open (rc=0,
    silent allow)", a real line in hooks/test/test-pre-dispatch-preempt.sh)
    as on the real failure. Try the fail() convention's actual shape first —
    a line BEGINNING with FAIL/FAILED/FAILURE, which is what hooks/test/
    lib-require-all.sh's pass/fail/skip idiom always emits for a real
    failure and a PASS/SKIP line never does — and fall back to the loose
    match only for suites that don't follow that convention, so the
    stricter default never regresses a suite the loose rule used to find.
    Naming which rule matched turns a bad anchor into something diagnosable
    instead of merely wrong."""
    match = _SUITE_STRICT_FAIL_RE.search(text)
    if match:
        return match.start(), "strict-fail-line"
    match = _SUITE_LOOSE_FAIL_RE.search(text)
    if match:
        return match.start(), "loose-failure-mention"
    return 0, "no-marker-found"


def _bounded_failure_detail(stdout: str, limit: int = 4000) -> str:
    """Bound a FAILED suite's stdout for the gate's RED report WITHOUT a
    positional tail (SABLE-twpe2).

    cp.stdout.strip()[-800:] kept whatever happened to be LAST, regardless of
    where the failure actually was. On a real suite (test-ci-bd-coverage-
    gap.sh) that cut every inline "FAIL: <name>" line and its detail while a
    trailing summary survived by accident of layout — not because it was more
    useful than the lines it displaced. A suite with no trailing epilogue
    would have propagated NOTHING usable on a red at all, and nothing about
    the gate would have looked wrong.

    Anchor on the first line naming a failure instead (via
    _anchor_failure_region, SABLE-1u6dr), so the excerpt always starts at the
    region that explains the red rather than wherever the output happened to
    stop, or wherever a PASS line's own wording happened to mention the word
    "failure". If a bound still applies, announce it — a truncated report
    that reads as complete is the exact hazard SABLE-np1nx's no-tail rule
    exists to forbid, now applied to the gate's own reporting of that rule's
    own violations — and name which anchor rule was used, so a future bad
    anchor is diagnosable rather than merely wrong."""
    text = stdout.strip()
    if len(text) <= limit:
        return text
    start, anchor = _anchor_failure_region(text)
    prefix = f"[anchor: {anchor}]"
    if start:
        prefix += f" [{start} leading char(s) elided]"
    prefix += "\n"
    excerpt = text[start:]
    if len(excerpt) <= limit:
        return f"{prefix}{excerpt}"
    dropped = len(excerpt) - limit
    return f"{prefix}{excerpt[:limit]}\n[...truncated, {dropped} more char(s) omitted...]"


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
                      f"{_impact_timeout(repo):.0f}s budget starts now, unspent",
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
                  f"{_impact_timeout(repo):.0f}s) — if this promote dies without a "
                  f"verdict line, suspect an enclosing wrapper timeout before the "
                  f"tier itself (see `sable-merge-gate promote-budget`)",
                  flush=True)
            _stamp_impact_window(repo, "start", tree_sha, waited)
            # SABLE-mbkbm: a mutable OUTPUT list, not a return value — so even a
            # tier that dies partway (an exception inside _run_impact_tier_locked
            # that its own except clauses don't catch) still stamps whatever
            # phases genuinely ran before that point, instead of losing every
            # phase because the function never reached its return.
            phases: list[dict] = []
            outcome: str | None = None
            try:
                outcome, detail = _run_impact_tier_locked(repo, tree_sha, paths, phases)
                return outcome, detail
            finally:
                _stamp_impact_window(repo, "end", tree_sha, waited, phases=phases)
                # SABLE-21rug.1: folds the additive verdict/writer_identity
                # fields onto the "end" line the call above just wrote. Kept
                # as a separate call (not a new kwarg on the call above) so
                # that call's monkeypatched stand-ins in
                # bin/test_promote_decision.py stay untouched.
                if outcome is not None:
                    _stamp_impact_verdict(repo, tree_sha, outcome)
    except ImpactLockTimeout as exc:
        return (IMPACT_ERROR, f"impact tier never started: {exc}")


def _impact_isolated_env(parent: str | os.PathLike) -> dict[str, str]:
    """Build the per-run env every suite/override invocation in
    _run_impact_tier_locked runs under (SABLE-jd5fj.15): an isolated bd DB, an
    isolated HOME carrying a read-only VIEW of the live ~/.claude/settings.json,
    and TMPDIR scoped to this run's own scratch parent.

    WHY THIS KILLS THE CLASS jd5fj.13 ONLY QUEUED. The iron-rule suites this
    tier selects (test-dep-merge-state.sh, test-overlap-dispatch-e2e.sh, and
    anything else the manifest picks) are deliberately non-hermetic: real bd
    (sandbox-scoped beads, dep graphs, the DB lock), the real
    sable-spawn-worker binary, live ~/.claude/settings.json reachability
    checks. They read AND WRITE that shared live state, which is exactly what
    let two concurrent tiers false-RED each other under a pile-up. jd5fj.13
    queued around that with an flock; this makes the underlying collision
    impossible instead, by giving each run its own copy of every piece of
    state the suites touch. `parent` is the caller's own tempfile.mkdtemp,
    already unique per run — nesting the sandbox under it is what makes two
    concurrent run_impact_tier calls land in DIFFERENT scratch parents.

    A FRESH bd DB, NOT A COPY of the real one. The suites create their own
    scratch beads (blocker/dependent, A/B) and never assert anything about
    pre-existing real bead content or the real issue prefix (checked: both
    suites extract IDs by regex from their OWN `bd create` output, never a
    hardcoded prefix) — so a fresh, freshly-initialized DB satisfies every
    assertion they make while guaranteeing no real bead can leak in or out.

    BD-ABSENT DEGRADES, IT DOES NOT FAIL THE TIER. If bd is not on PATH at all
    (the ci-verify clean-room, SABLE-59zu, ships none), there is nothing to
    isolate — the suites' own `command -v bd` guard already self-skips their
    real-bd legs exactly as before, so BEADS_DB is simply left unset rather
    than pointed at a DB this env can never build. The audit this bead's
    WHERE note asked for landed here: the risk was never bd's ABSENCE (the
    suites already handle that loudly), it was a REDIRECT to an uninitialized
    DB — `bd create` against a bare directory fails outright and several
    call sites read that failure as "could not create the scratch bead" and
    silently `exit 0` (skip), which would have looked like a pass. Only
    surface an error when bd IS present and the isolated DB still could not
    be built — that really did not happen before and deserves an IMPACT_ERROR
    (full re-preview), not a silent downgrade to the old shared-state path.

    A COPY, not a symlink or a live pointer, of ~/.claude/settings.json — a
    VIEW, so the live-matcher reachability check in test-dep-merge-state.sh
    still exercises the real registration instead of skipping for want of a
    settings file, chmod'd read-only so an accidental write inside the sandbox
    fails loud instead of silently diverging from the file every other
    concurrent run is also reading."""
    parent = Path(parent)
    home = parent / "home"
    scratch_tmp = parent / "tmp"
    home.mkdir(parents=True, exist_ok=True)
    scratch_tmp.mkdir(parents=True, exist_ok=True)

    real_settings = Path(os.environ.get("CLAUDE_SETTINGS")
                          or (Path.home() / ".claude" / "settings.json"))
    if real_settings.is_file():
        dest_dir = home / ".claude"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "settings.json"
        shutil.copy2(real_settings, dest)
        dest.chmod(0o444)

    env = dict(os.environ)
    env["HOME"] = str(home)
    env["TMPDIR"] = str(scratch_tmp)

    if shutil.which("bd") is None:
        return env

    beads_root = parent / "beads"
    beads_root.mkdir(parents=True, exist_ok=True)
    init = subprocess.run(
        ["bd", "init", "--prefix=impacttier"], cwd=str(beads_root),
        env={**os.environ, "BD_NON_INTERACTIVE": "1"},
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
    )
    if init.returncode != 0:
        raise RuntimeError(
            f"bd is on PATH but the isolated impact-tier bd DB failed to initialize "
            f"at {beads_root}: {init.stdout.strip()[:400]}")
    env["BEADS_DB"] = str(beads_root / ".beads")
    return env


def _run_impact_tier_locked(repo: str, tree_sha: str, paths: list[str],
                            phases: list[dict] | None = None) -> tuple[str, str]:
    """The tier itself, with the seat's serialization lock already held. Reports
    (IMPACT_GREEN|IMPACT_RED|IMPACT_ERROR, detail).

    SABLE-mbkbm: `phases`, when given, is appended to in place with each real
    span this function times — worktree add/remove and the isolated-env build
    (all folded into "setup", SABLE-np1nx), each shell suite by its own name
    ("shell:<suite>"), and the pytest half ("pytest") — using time.monotonic()
    around subprocess boundaries that already exist here. No phase is invented
    for work that didn't run: a footprint that never reaches bin/ never
    appends a "pytest" entry, which is the difference between an absent
    measurement and a fabricated zero.

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
    if phases is None:
        phases = []
    override = os.environ.get("SABLE_MG_IMPACT", "").split()
    parent = tempfile.mkdtemp(prefix="sable-impact-")
    worktree = str(Path(parent) / "tree")
    try:
        _t0 = time.monotonic()
        try:
            add = git_lib._git(repo, "worktree", "add", "--detach", worktree, tree_sha, check=False)
        finally:
            _record_phase(phases, "setup", _t0)
        if add.returncode != 0:
            return (IMPACT_ERROR, f"could not check out the combined tree: {add.stdout.strip()[:400]}")
        try:
            # SABLE-jd5fj.15: every suite/override invocation below runs under
            # its own isolated BEADS_DB/HOME/TMPDIR, nested under this run's
            # OWN scratch parent — see _impact_isolated_env for why that is
            # what kills the false-RED class jd5fj.13 could only queue around.
            #
            # SABLE-np1nx: this call was the phase journal's actual blind spot.
            # When bd is on PATH it runs a real `bd init` subprocess (fresh DB,
            # real disk I/O) that this span never timed, so under host
            # contention it could eat several real seconds attributed to no
            # phase at all — the "reconcile" check (this bucket is folded into
            # "setup", same as the worktree add/remove either side of it) then
            # failed on total >> phase_sum, which was never scheduling noise
            # around an already-measured block, only a genuinely-unmeasured one.
            _t0 = time.monotonic()
            try:
                env = _impact_isolated_env(parent)
            finally:
                _record_phase(phases, "setup", _t0)
            if override:
                _t0 = time.monotonic()
                try:
                    cp = git_lib._run(override + list(paths), cwd=worktree, check=False,
                                      timeout=_impact_timeout(repo), env=env)
                finally:
                    _record_phase(phases, "override", _t0)
                return ((IMPACT_GREEN, "impact tier override reported green") if cp.returncode == 0
                        else (IMPACT_RED, f"impact tier override failed (rc={cp.returncode}): "
                                          f"{cp.stdout.strip()[:400]}"))

            if not (Path(worktree) / ".github" / "ci" / "impact-manifest.sh").is_file():
                return (IMPACT_ERROR, "this repo has no .github/ci/impact-manifest.sh — "
                                      "no impact tier to run on the combined tree")
            _t0 = time.monotonic()
            try:
                suites = _selected_suites(repo, worktree, list(paths))
            finally:
                _record_phase(phases, "setup", _t0)
            ran: list[str] = []
            for suite in suites:
                suite_path = Path(worktree) / "hooks" / "test" / suite
                if not suite_path.is_file():
                    return (IMPACT_ERROR, f"impact tier selected {suite} but it is missing "
                                          f"from the combined tree")
                _t0 = time.monotonic()
                try:
                    cp = git_lib._run(["bash", str(suite_path)], cwd=worktree, check=False,
                                      timeout=_impact_timeout(repo), env=env)
                finally:
                    # SABLE-mbkbm: EACH suite gets its OWN phase name — a single
                    # "shell" bucket would hide exactly the question this bead
                    # exists to answer (which suite dominates).
                    _record_phase(phases, f"shell:{suite}", _t0)
                ran.append(suite)
                if cp.returncode != 0:
                    return (IMPACT_RED, f"{suite} FAILED on the combined tree (rc={cp.returncode}): "
                                        f"{_bounded_failure_detail(cp.stdout)}")

            # The pytest half, only when the footprint reaches bin/ at all.
            selector = Path(worktree) / "bin" / "tier_selection.py"
            if selector.is_file() and any(p.startswith("bin/") for p in paths):
                warm_source, warm_label = _warm_testmondata_source(repo)
                if warm_source is not None:
                    shutil.copy2(warm_source, Path(worktree) / ".testmondata")
                _t0 = time.monotonic()
                try:
                    cp = git_lib._run([sys.executable, str(selector)], cwd=worktree, check=False,
                                      timeout=_impact_timeout(repo), env=env)
                finally:
                    _record_phase(phases, "pytest", _t0)
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
                                        f"(rc={cp.returncode}): {_bounded_failure_detail(cp.stdout)}")

            if not ran:
                # Nothing selected and nothing to select from is not a pass —
                # it is a tier that told us nothing about the combined tree.
                return (IMPACT_ERROR, "impact tier selected no suites at all — no evidence about "
                                      "the combined tree")
            return (IMPACT_GREEN, f"impact tier GREEN on the combined tree: {', '.join(ran)}")
        finally:
            _t0 = time.monotonic()
            git_lib._git(repo, "worktree", "remove", "--force", worktree, check=False)
            git_lib._git(repo, "worktree", "prune", check=False)
            _record_phase(phases, "setup", _t0)
    except subprocess.TimeoutExpired as exc:
        return (IMPACT_ERROR, f"impact tier timed out after {_impact_timeout(repo)}s: {exc}")
    except (OSError, RuntimeError) as exc:
        return (IMPACT_ERROR, f"impact tier could not run: {exc}")
    finally:
        shutil.rmtree(parent, ignore_errors=True)


# --------------------------------------------------------------------------
# Phase-2: decomposing the tier's own wall-clock (SABLE-mbkbm)
# --------------------------------------------------------------------------
#
# THE PREMISE: this is a DECISION INPUT, not a curiosity. jd5fj.8 was dispatched
# on the ASSUMPTION that the cold-testmon pytest fallback was a first-order tier
# cost; measured on a real footprint the warm cache saved ~2%. The instrumentation
# above (run_impact_tier / _run_impact_tier_locked) is what makes the next such
# decision a measurement instead of another assumption -- this is the reader for
# the journal it writes.
#
# NO INFERRED SPLITS. Every number below is either a directly-recorded phase
# span or a directly-recorded start/end pair from the SAME journal; nothing here
# derives a phase duration from a total. Records written before this bead (no
# "schema" key, schema < 2, or an "end" event with no "phases" key) are counted
# in `legacy_records_excluded` and EXCLUDED from every phase statistic -- folding
# them in as zero-duration phases would be indistinguishable from "ran instantly"
# and would silently understate whichever phase actually dominates.

def _phase_stats(values: list[float]) -> dict | None:
    """n / median / p90 / total for one phase's observed durations, or None for
    an empty sample -- callers must not report statistics for zero records."""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    median = s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2
    p90 = s[min(n - 1, math.ceil(0.9 * n) - 1)]
    return {"n": n, "median_s": round(median, 3), "p90_s": round(p90, 3),
            "total_s": round(sum(s), 3)}


def impact_tier_phase_report(repo: str | os.PathLike = ".") -> dict:
    """Decompose the impact tier's own wall-clock by phase — setup, each shell
    suite, the pytest half — from the journal _stamp_impact_window writes.

    Returns a dict with:
      * total_tier_wall_clock: _phase_stats over each PAIRED start/end window's
        (end.at - start.at), the same total the pre-mbkbm journal could already
        report, recomputed here so a caller can see phase totals alongside it.
      * phases: {name: _phase_stats(...) | share_of_total} for every phase name
        seen across schema>=2 "end" records that carry a "phases" list.
      * tiers_with_phase_data: how many "end" records actually had phase data —
        THE n THE BEAD REQUIRES A CALLER TO STATE. 0 means "no measurement yet",
        not "the split is even".
      * legacy_records_excluded: "end" records with no usable phase data
        (pre-mbkbm schema, or a schema>=2 record whose phases list is absent —
        both are "we don't know", never zero).
    """
    path = Path(os.environ.get("SABLE_MG_IMPACT_WINDOW_LOG")
                or (snapshot_lib.ensure_state_dir(repo) / IMPACT_WINDOW_FILE))
    by_phase: dict[str, list[float]] = defaultdict(list)
    total_windows: list[float] = []
    legacy_excluded = 0
    tiers_with_phases = 0
    starts: dict[tuple, float] = {}

    if path.is_file():
        for raw in path.read_text().splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            key = (rec.get("pid"), rec.get("tree"))
            event = rec.get("event")
            if event == "start":
                at = rec.get("at")
                if at is not None:
                    starts[key] = at
                continue
            if event != "end":
                continue
            start_at = starts.pop(key, None)
            end_at = rec.get("at")
            if start_at is not None and end_at is not None:
                total_windows.append(end_at - start_at)
            schema = rec.get("schema", 1)
            phases = rec.get("phases")
            if schema < IMPACT_WINDOW_SCHEMA_VERSION or phases is None:
                legacy_excluded += 1
                continue
            if phases:
                tiers_with_phases += 1
            for p in phases:
                name, seconds = p.get("name"), p.get("seconds")
                if name is not None and seconds is not None:
                    by_phase[name].append(float(seconds))

    phase_report = {}
    grand_total = sum(sum(v) for v in by_phase.values())
    for name, values in by_phase.items():
        stat = _phase_stats(values)
        stat["share_of_total"] = round(stat["total_s"] / grand_total, 3) if grand_total else None
        phase_report[name] = stat

    return {
        "tiers_with_phase_data": tiers_with_phases,
        "legacy_records_excluded": legacy_excluded,
        "total_tier_wall_clock": _phase_stats(total_windows),
        "phases": phase_report,
    }


def format_impact_tier_phase_report(report: dict) -> str:
    """Human rendering that names n explicitly and refuses to present a split
    computed from zero or a handful of records as a finding — DO NOT SKIP TO
    PHASE 2 ON THIN DATA is the bead's own instruction, not a formality."""
    n = report["tiers_with_phase_data"]
    lines = [f"impact tier phase breakdown: n={n} tier run(s) with per-phase data "
             f"({report['legacy_records_excluded']} legacy/unphased record(s) excluded)"]
    if n == 0:
        lines.append("  no per-phase measurements yet — this instrument (SABLE-mbkbm) only "
                     "started recording phases from this landing forward; re-run after real "
                     "promotes accumulate under it. A split from zero records is a guess, "
                     "not a finding.")
        return "\n".join(lines)
    if n < 5:
        lines.append(f"  THIN DATA (n={n}): treat the shares below as a hypothesis, not a "
                     f"decision input, until more promotes accumulate.")
    total = report["total_tier_wall_clock"]
    if total:
        lines.append(f"  total tier wall-clock: n={total['n']} median={total['median_s']}s "
                     f"p90={total['p90_s']}s")
    for name, stat in sorted(report["phases"].items(), key=lambda kv: -kv[1]["total_s"]):
        share = f"{stat['share_of_total'] * 100:.0f}%" if stat["share_of_total"] is not None else "n/a"
        lines.append(f"  {name}: n={stat['n']} median={stat['median_s']}s p90={stat['p90_s']}s "
                     f"share={share}")
    return "\n".join(lines)


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
# MUST-LAND-TOGETHER pairing (SABLE-rzkw7) — the MECHANICAL deny path
# --------------------------------------------------------------------------
#
# Before this bead, a "these two must land in the same promote" ruling lived
# in a bead NOTE or a manager's working memory — nowhere the seat's own
# promote path ever reads. The near-miss that named this bead: chuck was
# holding one half of a deliberately-paired change and said, in his own
# words, he would have promoted it on the other lane's sign-off alone,
# because nothing told him the pairing existed. Splitting a matched pair
# PROMOTES SUCCESSFULLY — both halves are individually green, which is
# exactly what makes them individually signable — so there is no failure
# event at the promote; the damage is an invariant that only holds jointly
# now half-landed.
#
# The fix is a metadata field ON THE BEAD ITSELF (`landing_pair`, set the
# same way chuck.md's `hold` metadata already is: `bd update <id>
# --set-metadata landing_pair=<counterpart-id>[,<counterpart-id>...]`) so the
# declaration lives where THIS function already looks, instead of somewhere
# it never reads. A solo promote of either half is refused, naming the
# counterpart, unless the counterpart is explicitly named on THIS SAME
# promote call via --with-pair (the operator's mechanical acknowledgement
# that both halves are being promoted together) or the counterpart has
# ALREADY landed.
#
# "Landed" is deliberately NOT bead status. SABLE-d5iku (chuck.md) already
# established that a CLOSED bead is not a MERGED bead — status flips at the
# worker's push, landing happens only at chuck's promote — so reusing status
# here would silently reopen the exact gap that bead documents. Instead this
# reads for the literal marker every successful path through THIS module's
# own promote() already writes via _append_evidence: "promoted
# byte-identical to" appears in a bead's notes if and only if some earlier
# promote() call actually landed it.

_LANDING_PAIR_KEY = "landing_pair"
_LANDED_MARKER = "promoted byte-identical to"


class LandingPairRefused(Exception):
    """A bead's declared `landing_pair` counterpart is neither landed nor
    named via --with-pair on this promote call."""


def _bd_show(repo: str, bead_id: str) -> subprocess.CompletedProcess:
    """bd show <bead_id> --json, tolerant of a repo path that does not exist
    or isn't executable-from at all (a fixture/test double, or a promote()
    call whose bd-only checks run before any real checkout is guaranteed) —
    those degrade to the same 'could not read' outcome as a non-zero bd exit,
    rather than raising OSError out of a check every promote() call makes
    unconditionally."""
    try:
        return git_lib._run(git_lib._tool("SABLE_MG_BD", "bd") + ["show", bead_id, "--json"],
                            cwd=repo, check=False)
    except OSError as exc:
        return subprocess.CompletedProcess([], 1, stdout=str(exc))


def _bd_show_json(repo: str, bead_id: str) -> dict:
    """bd show <bead_id> --json, parsed defensively. Returns {} on any
    failure (unreadable bd, unknown bead, unparseable output) — callers below
    treat that as 'no pairing declared' when reading a bead's OWN metadata
    (fail toward the pre-existing default of an unconstrained promote) and as
    'not landed' when checking a COUNTERPART's status (fail toward refusing
    the promote — an unresolvable counterpart cannot be proven landed)."""
    cp = _bd_show(repo, bead_id)
    if cp.returncode != 0:
        return {}
    try:
        data = json.loads(cp.stdout)
    except (json.JSONDecodeError, ValueError):
        return {}
    if isinstance(data, list):
        data = data[0] if data else {}
    return data if isinstance(data, dict) else {}


def parse_with_pair(values: list[str]) -> frozenset[str]:
    """--with-pair CLI values (repeatable, or comma-separated) -> a flat id
    set. Lives here, not in the CLI, so bin/sable-merge-gate stays thin."""
    return frozenset(tok.strip() for raw in values for tok in raw.split(",") if tok.strip())


def declared_landing_pair(repo: str, bead: str) -> frozenset[str]:
    """The bead ids <bead> itself declares via `metadata.landing_pair`
    (comma/whitespace-separated). Empty when unset, unreadable, or bead is
    falsy — "no pairing declared" is the correct default for every bead this
    mechanism does not apply to, which is almost all of them."""
    if not bead:
        return frozenset()
    raw = (_bd_show_json(repo, bead).get("metadata") or {}).get(_LANDING_PAIR_KEY) or ""
    return frozenset(tok.strip() for tok in re.split(r"[,\s]+", raw) if tok.strip())


def _bead_landed(repo: str, bead_id: str) -> bool:
    notes = _bd_show_json(repo, bead_id).get("notes") or ""
    return _LANDED_MARKER in notes


def assert_landing_pair_satisfied(repo: str, bead: str,
                                  with_pair: frozenset[str] = frozenset()) -> None:
    """Refuse a solo promote of <bead> when it declares a `landing_pair`
    counterpart that is neither already landed nor named in <with_pair> (the
    --with-pair CLI flag, i.e. this SAME promote call). Two unpaired beads —
    no `landing_pair` metadata at all — are never touched by this check,
    however similar their footprints: it discriminates on the declared
    relation, not on any file-level property."""
    for counterpart in declared_landing_pair(repo, bead):
        if counterpart in with_pair:
            continue
        if _bead_landed(repo, counterpart):
            continue
        raise LandingPairRefused(
            f"{bead} declares metadata.{_LANDING_PAIR_KEY}={counterpart!r} (SABLE-rzkw7: "
            f"MUST-LAND-TOGETHER) and {counterpart} is neither landed nor named on this "
            f"promote call. Refusing a solo promote of {bead}. Promote both together — "
            f"sable-merge-gate promote --bead {bead} --branch <branch> --with-pair {counterpart} "
            f"— or wait for {counterpart} to land first.")


# --------------------------------------------------------------------------
# Coverage floor on pruning passes (SABLE-cmar4.5) — the MECHANICAL deny path
# --------------------------------------------------------------------------

def _coverage_floor_timeout(repo: str | None = None) -> float:
    """SABLE-cmar4.9: the coverage-floor check's run budget, derived from the
    merge_preview tier's SSOT (.github/ci/test-tiers.sh, via
    sable_gate_budget_lib.tier_budget_sec) instead of a fresh hand-picked
    literal — the same duplicated-list class SABLE-jd5fj.9 just closed for
    _impact_timeout, and that SABLE-w0zjm's promote-budget mechanism exists to
    eliminate generally.

    Borrows merge_preview's budget rather than declaring the coverage floor
    its own tier entry, mirroring jd5fj.9's choice for the same reason: it is
    faithful to today's behaviour (600 sits under merge_preview's 900 today)
    and adding a tier entry here would be scope creep this bead explicitly
    disclaims. The coverage floor runs inside the promote path rather than
    inside merge_preview's own suite list, so if its runtime characteristics
    ever prove to need a different ceiling than merge_preview's, the SSOT
    should grow a dedicated entry instead of a second borrow — flagged here,
    not decided.

    SABLE_MG_COVERAGE_FLOOR_TIMEOUT is an explicit override and always wins
    over the SSOT, exactly as before. `repo` defaults to the current working
    directory. A missing or broken SSOT (or an unparseable override) falls
    back to the pre-fix constant (600) — never raises, mirroring
    _impact_timeout's and git_lib.default_mg_timeout's never-raises
    contract."""
    override = os.environ.get("SABLE_MG_COVERAGE_FLOOR_TIMEOUT")
    if override is not None:
        try:
            return float(override)
        except ValueError:
            return 600.0
    budget = budget_lib.tier_budget_sec(repo or os.getcwd(), "merge_preview")
    return budget if budget is not None else 600.0


# --- Phase attribution for the coverage floor (SABLE-9yjt5) ---------------
#
# The floor used to answer ONE question ("did the script exit 0?") and print
# ONE sentence ("coverage regressed"), for THREE unrelated causes: a red
# suite, a budget overrun, and a genuine patch-coverage miss. The named cause
# was frequently the wrong one, and the remedy it named was actively harmful:
# SABLE-21rug.4's worker read "coverage regressed" literally and added 115
# lines of tests to a branch whose real problem was that the run was blowing
# through its 900s budget — which LENGTHENS the very run that is overrunning.
#
# So the runner now returns an attributable result. The DECISION AXIS DOES NOT
# MOVE — every phase below still denies, fail-closed, and `passed` is still
# derived from `rc == 0` exactly as before. Only the REPORT axis gains
# resolution (standing discipline 7).
#
# The three phases map to THREE DIFFERENT OPERATOR ACTIONS, which is the whole
# reason collapsing them was expensive:
#   pytest-failed          -> fix the suite   (adding tests does not help)
#   timed-out              -> fix the budget  (adding tests makes it WORSE)
#   diff-cover-under-floor -> add tests       (the only case that advice fits)
PHASE_OK = "ok"
PHASE_PYTEST_FAILED = "pytest-failed"
PHASE_DIFF_COVER_UNDER_FLOOR = "diff-cover-under-floor"
PHASE_CANNOT_ASSESS = "cannot-assess"
PHASE_TIMED_OUT = "timed-out"
PHASE_NO_SCRIPT = "no-script"
PHASE_NO_WORKTREE = "no-worktree"
PHASE_OS_ERROR = "os-error"
PHASE_UNATTRIBUTED = "unattributed-exit"

# Script exit codes -> phase. Kept in lockstep with the table in
# .github/ci/diff-cover-gate.sh's header; anything not listed here is
# UNATTRIBUTED (an older branch's set -e script, or a shell/bash failure),
# which is a could-not-assess, NOT a coverage verdict.
_COVERAGE_FLOOR_EXIT_PHASES = {
    0: PHASE_OK,
    10: PHASE_PYTEST_FAILED,
    11: PHASE_DIFF_COVER_UNDER_FLOOR,
    12: PHASE_CANNOT_ASSESS,
}

# The script's machine-readable self-report. Preferred over the exit code for
# ATTRIBUTION (never for the decision), so a script that grows a new marker is
# readable by an older runner as "unattributed" rather than mis-attributed.
_COVERAGE_PHASE_MARKER_RE = re.compile(
    r'^SABLE-COVERAGE-FLOOR-PHASE:[ \t]+(\S+)', re.MULTILINE)
_MARKER_PHASES = {
    "ok": PHASE_OK,
    "pytest-failed": PHASE_PYTEST_FAILED,
    "diff-cover-under-floor": PHASE_DIFF_COVER_UNDER_FLOOR,
    "diff-cover-unavailable": PHASE_CANNOT_ASSESS,
    "coverage-xml-missing": PHASE_CANNOT_ASSESS,
}

_PYTEST_FAILED_TEST_RE = re.compile(r'^FAILED[ \t]+(\S+)', re.MULTILINE)


@dataclass
class CoverageFloorRun:
    """What the coverage-delta check actually did.

    `passed` is the UNCHANGED tri-state fed to
    coverage_floor_lib.evaluate_coverage_floor (True cleared / False measured-
    and-under-floor / None could-not-assess) — the decision axis. `phase` and
    `detail` are the report axis: which of the causes fired, and the sentence
    an operator can act on. They never influence the decision."""
    passed: bool | None
    phase: str
    detail: str


def _failing_tests_from(output: str, limit: int = 5) -> str:
    """The `FAILED <nodeid>` lines pytest -q prints in its short summary, so a
    pytest-phase deny NAMES the tests instead of sending the reader back to a
    log they may no longer have."""
    names = _PYTEST_FAILED_TEST_RE.findall(output or "")
    if not names:
        return "no FAILED lines parsed from the run's output"
    shown = "; ".join(names[:limit])
    if len(names) > limit:
        shown += f"; (+{len(names) - limit} more)"
    return shown


def run_coverage_floor_check(repo: str, base_sha: str, branch_sha: str) -> CoverageFloorRun:
    """Actually run the coverage-delta check — .github/ci/diff-cover-gate.sh,
    real pytest + coverage.py + diff-cover, no mocks — against the branch's
    checked-out tree, comparing to base_sha. Same pattern as
    _run_impact_tier_locked: a throwaway detached worktree, because the thing
    being measured is the code AS IT WILL EXIST on the branch, not a diff of
    trees.

    Returns a CoverageFloorRun (SABLE-9yjt5). Its `.passed` carries the same
    tri-state this function used to return directly — True (patch coverage
    cleared --fail-under), False (diff-cover ran and failed it), None (could
    not assess: no script, no worktree, timeout, or an exit this runner cannot
    attribute) — and None is still a FAIL-CLOSED read, same as
    assert_not_frozen's unreadable-freeze-file contract: "we could not prove
    it's covered" denies, exactly like "we proved it's not".

    What is NEW is that `.phase`/`.detail` say WHICH of those happened. Note
    that a red suite is now None rather than False: the pytest phase failing
    (scoped or full — SABLE-hauwa's selection does not change this) means
    diff-cover never ran, so there is no coverage number and claiming one
    ("coverage regressed") was a false statement about the branch. It still
    denies."""
    parent = tempfile.mkdtemp(prefix="sable-coverage-floor-")
    worktree = str(Path(parent) / "tree")
    started = time.monotonic()
    try:
        add = git_lib._git(repo, "worktree", "add", "--detach", worktree, branch_sha, check=False)
        if add.returncode != 0:
            return CoverageFloorRun(None, PHASE_NO_WORKTREE,
                "the throwaway worktree for the check could not be built, so the check "
                "NEVER RAN — this is gate infrastructure, not a property of the branch")
        try:
            script = Path(worktree) / ".github" / "ci" / "diff-cover-gate.sh"
            if not script.is_file():
                return CoverageFloorRun(None, PHASE_NO_SCRIPT,
                    "the branch does not carry .github/ci/diff-cover-gate.sh at all — add "
                    "the check, or record a 'Coverage override: <reason>' line")
            # The budget term stays SPELLED AS THE CALL here, not hoisted into a
            # local: unbudgeted_promote_timeouts() reads `timeout=`'s callee name
            # out of the AST, and a local would read as an unregistered source
            # (SABLE-5v3d5's completeness check caught exactly that during this
            # bead's own work). The message path below reads the budget back off
            # the exception instead.
            cp = git_lib._run(["bash", str(script), base_sha], cwd=worktree, check=False,
                              timeout=_coverage_floor_timeout(repo))
            return _attribute_coverage_run(cp.returncode, cp.stdout or "")
        finally:
            git_lib._git(repo, "worktree", "remove", "--force", worktree, check=False)
            git_lib._git(repo, "worktree", "prune", check=False)
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - started
        limit = exc.timeout if exc.timeout is not None else _coverage_floor_timeout(repo)
        return CoverageFloorRun(None, PHASE_TIMED_OUT,
            f"the check RAN but did not finish: killed after {elapsed:.0f}s against a "
            f"{float(limit):.0f}s budget (SABLE_MG_COVERAGE_FLOOR_TIMEOUT / the "
            f"merge_preview tier), so NO coverage number was produced. The branch DOES "
            f"carry the check. Adding tests LENGTHENS this run and makes it strictly "
            f"worse — raise the budget or shorten the suite")
    except OSError as exc:
        return CoverageFloorRun(None, PHASE_OS_ERROR,
            f"the check could not be executed ({exc.__class__.__name__}: {exc}), so it "
            f"NEVER RAN — gate infrastructure, not a property of the branch")
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def _attribute_coverage_run(returncode: int, output: str) -> CoverageFloorRun:
    """Map a completed diff-cover-gate.sh run to its phase.

    DECISION AXIS, unchanged and deliberately derived from the exit code alone:
    `passed is True` iff rc == 0. The marker is consulted only to say WHICH
    non-zero this was, so a branch carrying a marker this runner does not know
    reads as could-not-assess rather than as a measured coverage failure."""
    markers = _COVERAGE_PHASE_MARKER_RE.findall(output)
    phase = None
    if markers:
        phase = _MARKER_PHASES.get(markers[-1])
    if phase is None:
        phase = _COVERAGE_FLOOR_EXIT_PHASES.get(returncode, PHASE_UNATTRIBUTED)

    if returncode == 0:
        return CoverageFloorRun(True, PHASE_OK,
            "diff-cover ran and the patch cleared --fail-under")

    # rc != 0 below. `ok` from a non-zero run is self-contradictory: trust the
    # exit code and refuse to attribute.
    if phase == PHASE_OK:
        phase = PHASE_UNATTRIBUTED

    if phase == PHASE_DIFF_COVER_UNDER_FLOOR:
        return CoverageFloorRun(False, PHASE_DIFF_COVER_UNDER_FLOOR,
            "diff-cover RAN and produced a real patch-coverage number, and that number "
            "was under --fail-under")

    if phase == PHASE_PYTEST_FAILED:
        return CoverageFloorRun(None, PHASE_PYTEST_FAILED,
            f"the branch's own pytest suite FAILED (scoped or full — SABLE-hauwa's "
            f"selection does not change the attribution), so diff-cover NEVER RAN and NO "
            f"patch-coverage number exists. Fix the suite — adding tests does not clear "
            f"this. Failing test(s): {_failing_tests_from(output)}")

    if phase == PHASE_CANNOT_ASSESS:
        return CoverageFloorRun(None, PHASE_CANNOT_ASSESS,
            "the suite passed but no patch-coverage number could be produced (diff-cover "
            "missing, or coverage.py emitted no XML) — a tooling gap, not a measurement")

    return CoverageFloorRun(None, PHASE_UNATTRIBUTED,
        f"the check exited {returncode} without naming a phase, so which of "
        f"suite-failure / budget-overrun / coverage-miss occurred is UNKNOWN — no "
        f"coverage number can be claimed either way")


def assert_coverage_floor(repo: str, bead: str, base_sha: str, branch_sha: str,
                          coverage_override: str | None) -> None:
    """Refuse to promote a PRUNING diff (removed test function, newly-added
    skip marker, or deleted test file — sable_coverage_floor_lib.detect_pruning)
    unless it carries a real, passing coverage-delta check, or a human recorded
    a named 'Coverage override: <reason>' line.

    A non-pruning diff is unaffected — this is a floor on PRUNING passes only
    (the locked S3 scope), never a general coverage gate. Overridden diffs
    consult no run at all, by contract (mirrors promote()'s own --override:
    "An actions-down human bypass consults no run at all"), so an override
    reason is checked BEFORE the (real, potentially slow) check ever runs."""
    diff_text = git_lib._git(repo, "diff", f"{base_sha}...{branch_sha}").stdout
    signal = coverage_floor_lib.detect_pruning(diff_text)
    override_reason = coverage_floor_lib.parse_named_override(coverage_override or "")

    run = None
    if signal.is_pruning and not override_reason:
        run = run_coverage_floor_check(repo, base_sha, branch_sha)

    passed = run.passed if run is not None else None
    decision = coverage_floor_lib.evaluate_coverage_floor(signal, passed, override_reason)
    reason = _coverage_floor_report(signal, run, decision)

    if decision.action == coverage_floor_lib.ACTION_DENY:
        raise GateError(
            classify.EXIT_COVERAGE_FLOOR,
            f"COVERAGE FLOOR (SABLE-cmar4.5): {reason} — bead {bead}, "
            f"branch {branch_sha[:7]} onto {base_sha[:7]}. Not promoted.")

    if signal.is_pruning:
        _append_evidence(repo, bead, f"coverage-floor: {reason}.")


def _coverage_floor_report(signal, run, decision) -> str:
    """The REPORT axis of the coverage floor (SABLE-9yjt5), composed HERE
    rather than in coverage_floor_lib.evaluate_coverage_floor because only
    this layer knows which phase ran — evaluate_coverage_floor sees the
    tri-state and nothing else, and stays the single source of the DECISION.

    Two of the stock texts are wrong once you know the phase, and the fix is
    not symmetric between them:

      * the False arm ("coverage regressed on the removed/skipped test's
        lines") is TRUE exactly when diff-cover produced a number, which after
        this bead is the only way False is reachable. It is kept verbatim and
        merely stamped with the phase.
      * the None arm ("no coverage-delta check ... was carried on this
        branch") is AFFIRMATIVELY FALSE for every None cause except a
        genuinely absent script: on a timeout the branch carries the check and
        it ran for fifteen minutes. A reader acting on that sentence goes and
        adds a check that is already there (SABLE-be4lo.7's denial, measured).
        So it is REPLACED with the phase's own sentence — except in the one
        case where it happens to be true, where it is kept and stamped.

    The decision is not consulted for any of this and cannot be changed by it:
    every branch below is reached with `decision` already fixed."""
    if run is None:
        # No check was consulted at all (non-pruning, or a named override).
        return decision.reason

    if run.passed is True or run.phase == PHASE_NO_SCRIPT:
        return f"{decision.reason} [phase: {run.phase}]"

    if run.passed is False:
        return f"{decision.reason} [phase: {run.phase}] — {run.detail}"

    return (f"pruning diff ({'; '.join(signal.reasons)}) and the coverage-delta check "
            f"COULD NOT ASSESS this branch [phase: {run.phase}] — {run.detail}. DENIED "
            f"(fail-closed): a check that produced no number cannot clear the floor. "
            f"Record a 'Coverage override: <reason>' line if it should land anyway.")


# --------------------------------------------------------------------------
# AUTO-PROMOTE (SABLE-21rug.4) — the gated deny/allow assert
# --------------------------------------------------------------------------
#
# The last member of the assert_* family, and the only one whose ALLOW means
# "no human turn is owed here". Everything above it refuses a promote a human
# asked for; this one decides whether a promote may happen with no human
# asking at all. That inversion is why it is built as a DISQUALIFIER TABLE
# rather than a boolean: an unattended landing that turns out to be wrong is
# only debuggable if the decision named, in a durable record, exactly which
# row it turned on — and a row that has never been SEEN to fire is untested by
# construction, so every row below has a unit case that observes it fire BY
# NAME (this bead's whole test spec).
#
# WHERE THE MECHANICAL-CLASS JUDGEMENT COMES FROM, and where it does not.
# Five of the rows are clauses of the SHARED per-member predicate
# sable_batch_admission_lib.is_this_branch_mechanical (SABLE-be4lo.3, which
# this bead is dep-blocked on). This module CONSUMES that verdict and
# re-derives NO clause of it: it never computes a footprint, never resolves
# the gate-class roster, never reads a hold, never runs a fold check. A clause
# appearing in two places is precisely the drift the factoring exists to
# prevent, and bin/test_sable_gate_promote_lib.py asserts the absence
# structurally (test_the_auto_promote_assert_re_derives_no_mechanical_clause)
# so the property is checked rather than promised.
#
# The remaining rows are this bead's OWN, and they are a different question
# from "is this branch mechanical":
#   * THE EVIDENCE CHAIN — a provenance record (a typed producer for the
#     verdict) and a self-hash (which implementation decided), each bound to
#     the EXACT preview SHA under decision, refusing on mismatch rather than
#     adapting. SABLE-21rug.6 replays unattended landings from durable records
#     alone; a record that cannot say who produced the verdict, or which code
#     read it, is not replayable.
#   * BASE-UNCHANGED-SINCE-VERDICT — the base ref, re-observed live, still the
#     commit this decision was made against. Distinct from adoption: adoption
#     proves a preview exists for the (base, branch) pair the CALLER named,
#     which says nothing about whether that pair is still current.
#   * THE THREE VACUOUS-GREEN PRECONDITIONS — see below. These are the rows
#     that make the other rows mean something.
#
# WHY THE VACUOUS-GREEN PRECONDITIONS ARE ROWS AND NOT COMMENTS. `mechanical =
# all(c.passed for c in clauses)` is True over an EMPTY clause tuple. That is
# not a hypothetical: it is SABLE-p9n7k's exact shape (a summary that cannot
# distinguish checked-and-passed from had-nothing-to-check), sitting in the
# one predicate this gate trusts most, and an auto-promote is the worst
# possible consumer of it because there is no human between the vacuous green
# and the landing. So the assert proves its own inputs were non-vacuous before
# it believes them:
#   p9n7k  NON-VACUOUS      the clause set is non-empty — something was checked.
#   x2n8a  COMPLETE         the clause set covers every clause the table maps,
#                           so a TRUNCATED verdict cannot pass as a small one.
#   52aym  BOUNDED READS    every enumerating bd read in the deciding source
#                           carries an explicit --limit, so no absence-shaped
#                           input to this decision can be a silent prefix of
#                           the real answer.
# All three deny FAIL-CLOSED, in the same direction as assert_not_frozen's
# unreadable-freeze-file contract: "we could not prove it" denies exactly like
# "we proved it does not hold".

class AutoPromoteDisqualifier(str, Enum):
    """Every reason an auto-promote may be refused, enumerated so a decline
    NAMES its row instead of carrying a free-text reason a replay cannot
    group on (Primitive Obsession guard, same as VerdictSource/TierVerdict).

    The twelve rows this bead's test spec enumerates, plus the two remaining
    clauses of the shared predicate (NOT_INDIVIDUALLY_GREEN,
    NO_CLEAN_FF_ADOPTION). Those two are deliberately included even though the
    spec's table did not name them: without them a branch whose verdict is RED
    would be refused by an unnamed catch-all, and an unnamed refusal is the
    thing this enum exists to make impossible. The table is COMPLETE over the
    predicate's clause set — every clause has a row — which is what lets
    x2n8a's completeness precondition below be stated as a set comparison.
    """
    # --- clauses of the shared predicate (consumed, never re-derived) ------
    GATE_CLASS_FILE = "gate-class-file"
    TIER_MECHANISM_FILE = "tier-mechanism-file"
    CLASSIFICATION_AMBIGUITY = "classification-ambiguity"
    NOT_INDIVIDUALLY_GREEN = "not-individually-green"
    LIVE_HOLD = "live-hold"
    NO_CLEAN_FF_ADOPTION = "no-clean-ff-adoption"
    CONFLICT = "conflict"
    # --- the evidence chain (this bead's own) -----------------------------
    MISSING_PROVENANCE = "missing-provenance"
    MISSING_SELF_HASH = "missing-self-hash"
    VERDICT_SHA_MISMATCH = "verdict-sha-mismatch"
    BASE_MOVED = "base-moved-since-verdict"
    # --- the three vacuous-green preconditions ----------------------------
    NON_VACUOUS_PROOF_ABSENT = "non-vacuous-proof-absent"        # SABLE-p9n7k
    SELECTION_COMPLETENESS_ABSENT = "selection-completeness-absent"  # SABLE-x2n8a
    TRUNCATED_BD_READ = "truncated-bd-read"                      # SABLE-52aym


# The clause names is_this_branch_mechanical is contracted to return, in its
# own documented order. Stated here as the COMPLETENESS TARGET for x2n8a's
# precondition: a verdict missing any of these is a truncated selection, not a
# small one, and must not be believed. Kept in sync by
# test_the_completeness_target_matches_the_real_predicates_clause_set, which
# reads the names off a REAL verdict rather than trusting this literal.
REQUIRED_MECHANICAL_CLAUSES = (
    "non_gate_class", "individually_green", "zero_holds",
    "clean_ff_adoption", "zero_conflicts",
)

# Which row a failing clause of the shared predicate maps to. non_gate_class
# is the one clause that fans out to three rows, because "your footprint
# touches gate tooling", "your footprint touches the tier mechanism" and "your
# footprint could not be determined at all" are three different things for the
# human who has to act on the decline, and the predicate reports them through
# one clause. The fan-out reads the clause's OWN reason text — it does not
# recompute the footprint (see _row_for_non_gate_class).
_CLAUSE_ROWS = {
    "individually_green": AutoPromoteDisqualifier.NOT_INDIVIDUALLY_GREEN,
    "zero_holds": AutoPromoteDisqualifier.LIVE_HOLD,
    "clean_ff_adoption": AutoPromoteDisqualifier.NO_CLEAN_FF_ADOPTION,
    "zero_conflicts": AutoPromoteDisqualifier.CONFLICT,
}

# The marker sable_batch_admission_lib._non_gate_class puts on the clause when
# sable_footprint_lib raised FootprintUndetermined — i.e. the footprint is not
# WRONG, it is UNKNOWN, which is a materially different decline. Matched
# loosely (substring, case-folded) so a reword of that reason degrades to the
# generic gate-class row rather than to a crash, and pinned against the real
# producer by test_classification_ambiguity_maps_from_a_real_undetermined_clause.
_UNDETERMINED_MARKER = "undetermined"

# bd subcommands that ENUMERATE and therefore truncate at bd's default limit.
# `show` and `update` are absent on purpose: they address one bead by id, so
# there is no result set to silently cut (SABLE-52aym's defect is specifically
# that a truncated LIST is indistinguishable from a complete one).
_BD_ENUMERATING_SUBCOMMANDS = frozenset({"list", "ready", "query"})


class AutoPromoteRefused(Exception):
    """An auto-promote evaluation DENIED. Carries the evaluation itself, so a
    caller (and SABLE-21rug.5's shadow mode, which logs a verdict instead of
    acting on it) has the whole disqualifier table rather than a message."""

    def __init__(self, evaluation: "AutoPromoteEvaluation") -> None:
        super().__init__(evaluation.reason)
        self.evaluation = evaluation


@dataclass(frozen=True)
class Disqualification:
    """One row of the table, seen to fire: which row, and the specific detail
    that fired it (the failing clause's own reason, or this module's own
    observation). `detail` is for the human; `disqualifier` is what a replay
    groups on."""
    disqualifier: AutoPromoteDisqualifier
    detail: str

    def to_dict(self) -> dict:
        return {"disqualifier": self.disqualifier.value, "detail": self.detail}


AUTO_PROMOTE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class AutoPromoteEvaluation:
    """The whole auto-promote story for one (bead, branch, base) decision —
    durable, replayable, and complete whether it allowed or denied.

    Complete on ALLOW too, deliberately: SABLE-21rug.6 audits unattended
    landings by replaying recorded evaluations, and a record written only on
    denial makes the landings that actually happened the unobservable ones.

    self_hash / provenance / preview_sha are the evidence chain: WHICH
    implementation decided, WHO produced the verdict it decided on, and WHICH
    object both are bound to.
    """
    bead: str
    branch: str
    base_sha: str
    branch_sha: str
    preview_sha: str
    self_hash: str
    provenance: str
    disqualifications: tuple[Disqualification, ...]

    @property
    def allowed(self) -> bool:
        return not self.disqualifications

    @property
    def rows(self) -> tuple[AutoPromoteDisqualifier, ...]:
        """Every row that fired, in table order — what a decline NAMES."""
        return tuple(d.disqualifier for d in self.disqualifications)

    @property
    def reason(self) -> str:
        if self.allowed:
            return ("auto-promote ALLOWED: every disqualifier cleared, evidence chain "
                    f"bound to preview {self.preview_sha[:7]} "
                    f"(provenance={self.provenance}, self-hash={self.self_hash[:12]})")
        named = "; ".join(f"{d.disqualifier.value}: {d.detail}" for d in self.disqualifications)
        return f"auto-promote DENIED ({len(self.disqualifications)} disqualifier(s)) — {named}"

    def to_dict(self) -> dict:
        return {
            "schema": AUTO_PROMOTE_SCHEMA_VERSION,
            "bead": self.bead,
            "branch": self.branch,
            "base_sha": self.base_sha,
            "branch_sha": self.branch_sha,
            "preview_sha": self.preview_sha,
            "self_hash": self.self_hash,
            "provenance": self.provenance,
            "allowed": self.allowed,
            "disqualifiers": [d.to_dict() for d in self.disqualifications],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AutoPromoteEvaluation":
        """Reconstruct from a durable record, TOLERATING unknown keys — the
        same additive discipline AttentionRecord.from_dict carries, so a field
        a later sibling adds never breaks SABLE-21rug.6's replay of an older
        record."""
        return cls(
            bead=str(data.get("bead", "")),
            branch=str(data.get("branch", "")),
            base_sha=str(data.get("base_sha", "")),
            branch_sha=str(data.get("branch_sha", "")),
            preview_sha=str(data.get("preview_sha", "")),
            self_hash=str(data.get("self_hash", "")),
            provenance=str(data.get("provenance", "")),
            disqualifications=tuple(
                Disqualification(AutoPromoteDisqualifier(d["disqualifier"]),
                                 str(d.get("detail", "")))
                for d in (data.get("disqualifiers") or [])),
        )


def _deciding_sources() -> list[Path]:
    """The files that JOINTLY decide an auto-promote: this module (the table)
    and the shared predicate's module (the five clauses). The self-hash covers
    both, because "which implementation decided" is not answered by either one
    alone — a swap of the predicate underneath an unchanged table changes the
    decision just as completely."""
    return [Path(__file__), Path(admission.__file__)]


def gate_self_hash(sources: list[Path] | None = None) -> str:
    """sha256 over the deciding implementation's own source — the evidence
    chain's answer to "which code made this decision", which SABLE-21rug.6's
    replay needs to tell a re-derivation from a re-implementation.

    Returns "" when any source cannot be read. That empty string is the
    MISSING_SELF_HASH row: an unreadable implementation is not a self-hash of
    zero, it is the absence of one, and the assert denies on it (fail-closed,
    same contract as run_coverage_floor_check's None).

    Hashes (path-basename, digest) pairs rather than the concatenated bytes so
    the digest is stable under a checkout at a different absolute path — a
    worktree and its origin must produce the same self-hash for the same
    code."""
    digests = []
    for path in (sources if sources is not None else _deciding_sources()):
        try:
            digests.append(f"{Path(path).name}:"
                           f"{hashlib.sha256(Path(path).read_bytes()).hexdigest()}")
        except OSError:
            return ""
    if not digests:
        # p9n7k discipline applied to this function's own inputs: a hash over
        # zero sources is a well-formed digest of nothing, which would read as
        # a present self-hash. Absence, not a digest.
        return ""
    return hashlib.sha256("\n".join(sorted(digests)).encode()).hexdigest()


def unlimited_bd_reads(tree: ast.Module) -> list[str]:
    """SABLE-52aym's precondition, checked STRUCTURALLY. Returns the argv of
    every bd invocation in `tree` that enumerates (`list`/`ready`/`query`)
    without an explicit `--limit`/`-n` — each one a read whose result is a
    silent prefix of the real answer whenever the store holds more than bd's
    default 50, and therefore a source of absence-shaped conclusions that fail
    in the RELEASING direction.

    Recognises the argv shape this codebase's bd seam actually uses — a list
    literal of string constants passed to (or concatenated onto)
    git_lib._tool("SABLE_MG_BD", "bd") — rather than trying to model bd
    invocation in general. A shape it does not recognise is simply not
    reported: this is a floor on the known seam, not a proof of universal
    boundedness, and saying so here is cheaper than a future reader inferring
    a guarantee that was never made.

    Takes an already-parsed `tree` (never reading a file itself) for the same
    reason unbudgeted_promote_timeouts does: a test can hand it a planted
    snippet and prove the check fires, without editing a real module."""
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List) or not node.elts:
            continue
        first = node.elts[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        if first.value not in _BD_ENUMERATING_SUBCOMMANDS:
            continue
        argv = [e.value for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if not any(a == "--limit" or a == "-n" or a.startswith("--limit=") for a in argv):
            offenders.append(" ".join(argv))
    return offenders


def _row_for_non_gate_class(reason: str) -> AutoPromoteDisqualifier:
    """Which of the three non_gate_class rows a failing clause fired.

    Reads the clause's OWN reason string. This is a projection of the shared
    predicate's output, NOT a second derivation of it: nothing here computes a
    footprint, resolves the roster, or re-asks whether the sets intersect —
    the predicate already answered that, and this only decides how to NAME the
    answer it gave."""
    folded = reason.casefold()
    if _UNDETERMINED_MARKER in folded:
        return AutoPromoteDisqualifier.CLASSIFICATION_AMBIGUITY
    if any(tier_file.casefold() in folded for tier_file in admission.GATE_TIER_FILES):
        return AutoPromoteDisqualifier.TIER_MECHANISM_FILE
    return AutoPromoteDisqualifier.GATE_CLASS_FILE


def _preconditions(mechanical, module_asts: list[ast.Module]) -> list[Disqualification]:
    """The three vacuous-green preconditions, in bead order (p9n7k, x2n8a,
    52aym). Evaluated BEFORE `mechanical.mechanical` is believed, and every
    one of them is evaluated — never short-circuited — so a decline names
    every precondition that failed rather than only the first."""
    found: list[Disqualification] = []

    # p9n7k — all(...) over an empty clause tuple is True. Something must have
    # been checked before "everything passed" can mean anything.
    present = tuple(c.name for c in mechanical.clauses)
    if not present:
        found.append(Disqualification(
            AutoPromoteDisqualifier.NON_VACUOUS_PROOF_ABSENT,
            "the mechanical verdict carries ZERO clauses — 'all clauses passed' over an "
            "empty set is a vacuous green (SABLE-p9n7k), not a proof that anything was "
            "checked. Refusing to auto-promote on it."))

    # x2n8a — non-empty but SHORT reads exactly like non-empty and complete.
    missing = [name for name in REQUIRED_MECHANICAL_CLAUSES if name not in present]
    if present and missing:
        found.append(Disqualification(
            AutoPromoteDisqualifier.SELECTION_COMPLETENESS_ABSENT,
            f"the mechanical verdict is missing clause(s) {', '.join(missing)} — a "
            f"TRUNCATED clause set is indistinguishable from a genuinely small one "
            f"(SABLE-x2n8a). Got {', '.join(present)}."))

    # 52aym — an unbounded enumerating bd read anywhere in the deciding source
    # can turn a real result into a silent prefix, in the releasing direction.
    unbounded = [argv for tree in module_asts for argv in unlimited_bd_reads(tree)]
    if unbounded:
        found.append(Disqualification(
            AutoPromoteDisqualifier.TRUNCATED_BD_READ,
            f"the deciding source carries {len(unbounded)} enumerating bd read(s) with no "
            f"explicit --limit, which bd silently truncates at 50 (SABLE-52aym): "
            f"{'; '.join(f'bd {a}' for a in unbounded)}"))

    return found


def _evidence_chain(repo: str, remote: str, branch: str, base: str, base_ref: str,
                    base_sha: str, branch_sha: str,
                    self_hash: str) -> tuple[str, str, list[Disqualification]]:
    """The evidence-chain rows. Returns (preview_sha, provenance, rows).

    The preview/verdict READ here is not a re-derivation of the
    individually_green clause: that clause already answered "is the verdict
    GREEN", and this asks the separate questions the clause does not carry out
    of the predicate — WHICH object the verdict names, and WHO produced it.
    Both are needed because a verdict is a value, and a value with no producer
    and no binding is exactly the artifact an unattended landing must not
    trust. read_verdict (not acquire_verdict) on purpose: an auto-promote
    consults what already exists and NEVER waits for a run."""
    rows: list[Disqualification] = []

    if not self_hash:
        rows.append(Disqualification(
            AutoPromoteDisqualifier.MISSING_SELF_HASH,
            "the deciding implementation could not be hashed, so this landing's record "
            "cannot say WHICH code decided it — unreplayable by SABLE-21rug.6, refused "
            "fail-closed rather than landed unattributed."))

    # BASE-UNCHANGED-SINCE-VERDICT. Re-observed LIVE from the remote, not
    # taken from the caller's argument, because the whole question is whether
    # the caller's argument is still true.
    # Narrowed to the base branch, mirroring promote()'s own stale-base fetch
    # exactly — a bare `fetch <remote>` would pull every ref in the repo on
    # every auto evaluation, and the only ref this question is about is the base.
    git_lib._git(repo, "fetch", remote, base, check=False)
    try:
        current_base = git_lib.resolve_commit(repo, base_ref)
    except GateError as exc:
        # An unresolvable base is not "unchanged" — it is UNKNOWN, and this
        # function must return a row rather than raise one (the whole table is
        # always evaluated). Fail closed onto the same row.
        current_base = ""
        rows.append(Disqualification(
            AutoPromoteDisqualifier.BASE_MOVED,
            f"the base ref {base_ref} could not be resolved, so 'unchanged since the "
            f"verdict' is unprovable: {exc}"))
    if current_base and current_base != base_sha:
        rows.append(Disqualification(
            AutoPromoteDisqualifier.BASE_MOVED,
            f"the base moved since this decision's inputs were taken: {base_ref} now reads "
            f"{current_base[:7]}, the verdict is bound to {base_sha[:7]}. An auto-promote "
            f"never adapts a verdict to a base it was not produced against."))

    adopted = preview.adopt_kicked_preview(repo, remote, branch, base_sha, branch_sha)
    if adopted is None:
        rows.append(Disqualification(
            AutoPromoteDisqualifier.MISSING_PROVENANCE,
            f"no kicked preview exists for the (base={base_sha[:7]}, branch={branch_sha[:7]}) "
            f"pair, so there is no verdict to attribute a producer to."))
        return "", "", rows

    preview_sha, ref = adopted
    verdict = preview.read_verdict(repo, ref, preview_sha)

    try:
        provenance = VerdictSource(verdict.source).value
    except ValueError:
        provenance = ""
        rows.append(Disqualification(
            AutoPromoteDisqualifier.MISSING_PROVENANCE,
            f"the verdict for preview {preview_sha[:7]} carries producer {verdict.source!r}, "
            f"which is not one of the typed producers "
            f"({', '.join(s.value for s in VerdictSource)}). An unenumerated producer is "
            f"refused, never passed through."))

    # Bound to the EXACT preview SHA, refuse-on-mismatch — never adapted.
    if verdict.preview_sha != preview_sha:
        rows.append(Disqualification(
            AutoPromoteDisqualifier.VERDICT_SHA_MISMATCH,
            f"the verdict names object {verdict.preview_sha[:7] or '<none>'} but the object "
            f"under decision is {preview_sha[:7]}. A verdict for a different SHA is REFUSED, "
            f"never re-pointed at the object in hand."))

    return preview_sha, provenance, rows


def evaluate_auto_promote(bead: str, branch: str, base: str, repo: str, remote: str,
                          base_sha: str, branch_sha: str,
                          mechanical=None,
                          module_asts: list[ast.Module] | None = None
                          ) -> AutoPromoteEvaluation:
    """Evaluate the full disqualifier table for one candidate landing and
    return the record. NEVER raises on a disqualifier and never writes
    anything — the whole table is always evaluated, so the record names EVERY
    row that fired rather than the first (mirroring
    is_this_branch_mechanical's own never-short-circuit contract, and for the
    same reason: one decline that hides four others costs four more round
    trips at the seat).

    `mechanical` and `module_asts` are injection seams for the tests that must
    observe rows the real inputs cannot produce on demand — a verdict with a
    truncated clause set (x2n8a) has no natural fixture, and planting an
    unbounded bd read (52aym) must not require editing a real module. Default
    None means: ask the real shared predicate, and read the real deciding
    sources."""
    if mechanical is None:
        mechanical = admission.is_this_branch_mechanical(
            repo, remote, bead, branch, base_sha, branch_sha)
    if module_asts is None:
        module_asts = []
        for path in _deciding_sources():
            try:
                module_asts.append(ast.parse(path.read_text(), filename=str(path)))
            except (OSError, SyntaxError, UnicodeDecodeError):
                # An unparseable deciding source cannot be PROVEN to carry only
                # bounded reads. Fail closed with a synthetic offender rather
                # than silently checking nothing (a scan that could not run and
                # a scan that found nothing must not read the same — the same
                # bounded-search failure 52aym itself is an instance of).
                module_asts.append(ast.parse(f'_unreadable = ["list", "{path.name}"]'))

    self_hash = gate_self_hash()
    base_ref = classify.qualify_remote_ref(remote, base)

    rows: list[Disqualification] = []
    rows.extend(_preconditions(mechanical, module_asts))
    preview_sha, provenance, evidence_rows = _evidence_chain(
        repo, remote, branch, base, base_ref, base_sha, branch_sha, self_hash)
    rows.extend(evidence_rows)
    for clause in mechanical.clauses:
        if clause.passed:
            continue
        row = (_row_for_non_gate_class(clause.reason) if clause.name == "non_gate_class"
               else _CLAUSE_ROWS.get(clause.name))
        if row is not None:
            rows.append(Disqualification(row, clause.reason))

    # Table order, not evaluation order, so two records for the same facts are
    # byte-identical however the evaluation happened to be scheduled — a
    # property SABLE-21rug.6's replay compares on.
    order = list(AutoPromoteDisqualifier)
    rows.sort(key=lambda d: order.index(d.disqualifier))

    return AutoPromoteEvaluation(
        bead=bead, branch=branch, base_sha=base_sha, branch_sha=branch_sha,
        preview_sha=preview_sha, self_hash=self_hash, provenance=provenance,
        disqualifications=tuple(rows))


def assert_auto_promote_allowed(bead: str, branch: str, base: str, repo: str,
                                remote: str, base_sha: str, branch_sha: str
                                ) -> AutoPromoteEvaluation:
    """The assert. ALLOW returns the evaluation and the caller's existing
    mechanical decision tree proceeds with no human turn; DENY raises
    AutoPromoteRefused, whose message NAMES every row that fired, and the
    branch goes back to the seat's ordinary queue exactly as it does today.

    A DENY is not an error state and must not read like one: nothing has been
    built, nothing pushed, nothing consumed. It means "this one wants a human",
    which is the status quo for every branch in the fleet."""
    evaluation = evaluate_auto_promote(bead, branch, base, repo, remote,
                                       base_sha, branch_sha)
    if not evaluation.allowed:
        raise AutoPromoteRefused(evaluation)
    return evaluation


# --------------------------------------------------------------------------
# Seat-attention instrumentation (SABLE-21rug.1) — the metric's mandatory
# baseline. No sibling in the merge-seat epic may claim a reduction in
# attended time without this: an AttentionRecord per landing, joined to the
# promote evidence notes, plus the baseline computation over a set of them.
# The 18-promote night that motivated this epic ran zero local impact tiers,
# so no baseline is derivable retroactively — every landing from here on
# writes one of these before any offload/auto-promote sibling may close.
# --------------------------------------------------------------------------

class VerdictSource(str, Enum):
    """Typed mirror of classify.Verdict.source's three legal values
    (SABLE-jd5fj.3) — enumerated here rather than carried across the module
    boundary as a bare string, so a value classify_lib doesn't recognize
    surfaces as a construction error instead of a silently-stored typo
    (Primitive Obsession guard)."""
    PRECOMPUTED = "precomputed"
    WAITED = "waited"
    OVERRIDE = "override"


ATTENTION_RECORD_FILE = "attention-records.jsonl"
ATTENTION_RECORD_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class AttentionRecord:
    """One per-landing record of the human attention a promote consumed.

    bead / branch           — which landing this record describes.
    verdict_source          — classify.Verdict.source, typed (VerdictSource).
    hand_run_suites         — suite names an impact tier actually ran on the
                              real combined tree for THIS landing (empty when
                              the optimistic disjoint path never fired for it
                              — no re-verification means nothing to name, not
                              zero suites available).
    red_triage_events       — labels for what this landing passed through
                              before reaching GREEN (e.g. "base_moved",
                              "override") — empty for a landing that went
                              straight to GREEN with no intervention.
    attention_span_seconds  — measured wall-clock this promote call spent
                              waiting on a verdict or a hand-run tier for this
                              landing. Never inferred; zero is a legitimate,
                              measured value (e.g. an override consults no
                              run and waits on nothing) — it is only the
                              BASELINE's empty-INPUT case that must never
                              read as zero (see EmptyLandingSetError below).
    """
    bead: str
    branch: str
    verdict_source: VerdictSource
    hand_run_suites: tuple[str, ...] = ()
    red_triage_events: tuple[str, ...] = ()
    attention_span_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "schema": ATTENTION_RECORD_SCHEMA_VERSION,
            "bead": self.bead,
            "branch": self.branch,
            "verdict_source": self.verdict_source.value,
            "hand_run_suites": list(self.hand_run_suites),
            "red_triage_events": list(self.red_triage_events),
            "attention_span_seconds": round(self.attention_span_seconds, 3),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AttentionRecord":
        """Reconstruct from a dict, TOLERATING unknown keys — a future
        additive field an older reader doesn't recognize must never break
        this round trip, the same additive discipline the tier journal's
        schema carries."""
        return cls(
            bead=str(data.get("bead", "")),
            branch=str(data.get("branch", "")),
            verdict_source=VerdictSource(data.get("verdict_source", VerdictSource.WAITED.value)),
            hand_run_suites=tuple(data.get("hand_run_suites") or ()),
            red_triage_events=tuple(data.get("red_triage_events") or ()),
            attention_span_seconds=float(data.get("attention_span_seconds", 0.0)),
        )


def _suites_from_impact_detail(detail: str) -> tuple[str, ...]:
    """Which suite names an impact tier's own GREEN report named as having
    run, parsed from run_impact_tier's existing detail format ('...on the
    combined tree: <suite1>, <suite2>'). Informational only, for the
    attention record's hand_run_suites — a format this can't recognize (an
    override's 'impact tier override reported green', or any non-GREEN
    detail) degrades to an empty tuple rather than guessing."""
    marker = "on the combined tree: "
    idx = detail.find(marker)
    if idx == -1:
        return ()
    tail = detail[idx + len(marker):]
    return tuple(s.strip() for s in tail.split(",") if s.strip())


def _stamp_attention_record(repo: str | os.PathLike, record: AttentionRecord) -> None:
    """Append one AttentionRecord to the durable per-landing log — the
    baseline computation's only input, and what the promote evidence note's
    'attention-record:' line mirrors. Best-effort: a write failure here must
    never flip a green promote red.

    Skips silently (no write, no directory created) when `repo` is not a
    real, existing directory and no SABLE_MG_ATTENTION_LOG override is set.
    Several unit tests elsewhere in this module's sibling suites drive
    promote()/_stale_base with a synthetic, non-existent repo path — without
    this guard, snapshot_lib.ensure_state_dir's own last-resort fallback
    would resolve to the real ~/.claude/sable/state and write there on every
    such test run, which is a real-filesystem side effect no test asked for."""
    override = os.environ.get("SABLE_MG_ATTENTION_LOG")
    if not override and not Path(repo).is_dir():
        return
    try:
        path = Path(override) if override else (
            snapshot_lib.ensure_state_dir(repo) / ATTENTION_RECORD_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps(record.to_dict()) + "\n")
    except OSError:
        pass


def read_attention_records(repo: str | os.PathLike = ".") -> list[AttentionRecord]:
    """Read every durable attention record back — the read half of the round
    trip _stamp_attention_record writes. Tolerates malformed or unreadable
    lines (skipped, never fatal); mirrors the same repo-existence guard so a
    synthetic repo path reads as 'no records' rather than reaching into
    ~/.claude/sable/state."""
    override = os.environ.get("SABLE_MG_ATTENTION_LOG")
    if not override and not Path(repo).is_dir():
        return []
    path = Path(override) if override else (
        snapshot_lib.ensure_state_dir(repo) / ATTENTION_RECORD_FILE)
    records: list[AttentionRecord] = []
    if not path.is_file():
        return records
    for raw in path.read_text().splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        try:
            records.append(AttentionRecord.from_dict(data))
        except ValueError:
            continue
    return records


class EmptyLandingSetError(ValueError):
    """Raised by compute_attention_baseline on an empty landing set. A
    baseline computed over zero records is not a baseline — it is
    indistinguishable from 'we measured nothing', which is the exact failure
    this epic exists to fix. NEVER return 0.0 for no data."""


def compute_attention_baseline(records: list[AttentionRecord]) -> dict:
    """Attended-minutes-per-landing baseline: n / mean / median / p90 / total,
    computed only from each record's measured attention_span_seconds — never
    inferred (mirrors _phase_stats' own discipline). This is the number no
    offload/auto-promote sibling in this epic may claim a reduction against
    until it exists.

    Raises EmptyLandingSetError on an empty `records` — the vacuous-guard
    contract this bead is required to honour literally."""
    if not records:
        raise EmptyLandingSetError(
            "cannot compute an attention baseline over zero landing records — an "
            "empty set means 'we measured nothing', not '0 attended minutes'")
    minutes = sorted(r.attention_span_seconds / 60.0 for r in records)
    n = len(minutes)
    mid = n // 2
    median = minutes[mid] if n % 2 else (minutes[mid - 1] + minutes[mid]) / 2
    p90 = minutes[min(n - 1, math.ceil(0.9 * n) - 1)]
    return {
        "n": n,
        "mean_attended_minutes": round(sum(minutes) / n, 3),
        "median_attended_minutes": round(median, 3),
        "p90_attended_minutes": round(p90, 3),
        "total_attended_minutes": round(sum(minutes), 3),
    }


# --------------------------------------------------------------------------
# BatchRecord — the typed, durable manifest for a batched landing
# (SABLE-be4lo.2, architecture decision 5)
# --------------------------------------------------------------------------
#
# S2 of the SABLE-be4lo epic: "the seat holds no state a recycle could lose —
# the record is reconstructible from git plus the promote log alone." Two
# durable artifacts carry the manifest, independently:
#   1. the PROMOTE RECORD — one BatchRecord.to_dict() appended to
#      batch-records.jsonl per batched landing (_stamp_batch_record writes it;
#      read_batch_records / find_batch_record read it back), mirroring
#      AttentionRecord's own durable-log precedent immediately above.
#   2. the FOLD COMMIT MESSAGES — each two-parent commit in the batch's fold
#      chain names exactly the ONE member it folds in (fold_commit_message /
#      parse_fold_commit_message). Building the fold chain itself is
#      SABLE-be4lo.4's bead; this module owns the message FORMAT both that
#      builder and this manifest's reconstruction agree on, so the contract
#      exists before either side needs it.
#
# Primitive Obsession guard (architecture smell_risks): a batch's member set
# is NEVER passed across a module boundary as a loose tuple/list of branch
# names — BatchMember/BatchRecord own it, typed, so no caller-supplied input
# order can corrupt the manifest or its setkey. BatchRecord.from_members is
# the only constructor that matters here: it canonicalizes (sorts by
# tip_sha) before anything is stored or hashed, which is what makes the
# ordering-safety property true rather than merely asserted.


class EmptyBatchError(ValueError):
    """Raised by BatchRecord.from_members on a zero-member batch. A batch
    manifest with no members is a caller bug, never a valid vacuous record
    (SABLE-p9n7k discipline) — must never construct a record."""


@dataclass(frozen=True)
class BatchMember:
    """One member of a batched landing — the typed unit BatchRecord.members
    holds instead of a loose (branch, bead, footprint) tuple crossing a
    module boundary.

    branch           — the member's own branch name.
    tip_sha          — the member branch's tip commit; the value setkey()
                       and the canonical (sorted) member ordering are keyed
                       on.
    bead_ids         — every bead this member branch closes (usually one;
                       typed as a tuple so a multi-bead branch is not a
                       caller-side special case).
    footprint_paths  — this member's OWN declared footprint
                       (sable_footprint_lib) at admission time — half of the
                       disjointness evidence BatchRecord carries; the other
                       half is BatchRecord.fold_disjoint, the mechanical fold
                       result."""
    branch: str
    tip_sha: str
    bead_ids: tuple[str, ...]
    footprint_paths: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "branch": self.branch,
            "tip_sha": self.tip_sha,
            "bead_ids": list(self.bead_ids),
            "footprint_paths": list(self.footprint_paths),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BatchMember":
        return cls(
            branch=str(data.get("branch", "")),
            tip_sha=str(data.get("tip_sha", "")),
            bead_ids=tuple(data.get("bead_ids") or ()),
            footprint_paths=tuple(data.get("footprint_paths") or ()),
        )


BATCH_RECORD_FILE = "batch-records.jsonl"
BATCH_RECORD_SCHEMA_VERSION = 1

# Outcome constants, mirroring the ACTION_* / IMPACT_* module-level-constant
# style above rather than inventing an enum this bead's siblings (the
# admission/bisection children, be4lo.3/.7/.8) don't own yet. Additive: those
# children may define further outcome strings without touching this one.
BATCH_OUTCOME_LANDED = "landed"
BATCH_OUTCOME_FELL_BACK_SERIAL = "fell_back_serial"
# The two terminal outcomes of the bisection child (SABLE-be4lo.8) — added
# here, beside their siblings, exactly as this block's comment anticipated
# ("those children may define further outcome strings"). Neither can ever
# accompany a landing: both describe a batch that was RED and stayed unlanded.
BATCH_OUTCOME_BISECTED_CULPRIT = "bisected_culprit"
BATCH_OUTCOME_COULD_NOT_ATTRIBUTE = "could_not_attribute"


@dataclass(frozen=True)
class BatchRecord:
    """The typed, durable manifest for one batched landing (S2, architecture
    decision 5) — the promote record for a batch. Carries everything the S2
    reconstruct-without-asking acceptance requires: member branches, member
    bead ids, the disjointness evidence used (declared footprints per member
    AND the mechanical fold result), the combined ref, the setkey, and the
    outcome.

    members is ALWAYS in canonical order (sorted by tip_sha) regardless of
    the order from_members was called with — the ordering-safety contract:
    the same member set, admitted in any order, serializes identically and
    keys identically. Construct via from_members(); the bare dataclass
    constructor exists for from_dict()'s round trip, which reproduces a
    persisted (already-canonicalized) record rather than re-deriving one.

    culprit / report_lines are ADDITIVE fields for the red-batch bisection
    child (SABLE-be4lo.8): a batch that went red and was bisected still
    produces a promote record — it just records WHO was named (culprit) and
    the verbatim report the seat and the fleet saw (report_lines), rather than
    a landing. They default to absent so every pre-bisection caller, and every
    record already persisted, reconstructs unchanged."""
    base_sha: str
    setkey: str
    combined_ref: str
    outcome: str
    fold_disjoint: bool
    members: tuple[BatchMember, ...]
    culprit: str = ""
    report_lines: tuple[str, ...] = ()

    @classmethod
    def from_members(cls, base_sha: str, members: list[BatchMember], *,
                     combined_ref: str, outcome: str, fold_disjoint: bool,
                     culprit: str = "", report_lines: tuple[str, ...] = ()) -> "BatchRecord":
        """The only constructor that canonicalizes: members is re-sorted by
        tip_sha (the same key setkey() sorts on) before anything is stored or
        hashed, so a caller's admission order can never leak into the
        manifest or the setkey. Raises EmptyBatchError on an empty batch —
        never a vacuous zero-member record."""
        if not members:
            raise EmptyBatchError(
                "BatchRecord requires at least one member — an empty batch is "
                "a caller bug, never a valid vacuous record")
        canonical = tuple(sorted(members, key=lambda m: m.tip_sha))
        key = batch_key.setkey(base_sha, [m.tip_sha for m in canonical])
        return cls(base_sha=base_sha, setkey=key, combined_ref=combined_ref,
                   outcome=outcome, fold_disjoint=fold_disjoint, members=canonical,
                   culprit=culprit, report_lines=tuple(report_lines))

    def member_branches(self) -> tuple[str, ...]:
        return tuple(m.branch for m in self.members)

    def member_bead_ids(self) -> tuple[tuple[str, ...], ...]:
        return tuple(m.bead_ids for m in self.members)

    def declared_footprint_paths(self) -> tuple[str, ...]:
        """The declared-footprint half of the disjointness evidence: the
        UNION of every member's own declared footprint, sorted — a set
        union, so it is order-independent regardless of canonical ordering."""
        return tuple(sorted({p for m in self.members for p in m.footprint_paths}))

    def to_dict(self) -> dict:
        return {
            "schema": BATCH_RECORD_SCHEMA_VERSION,
            "base_sha": self.base_sha,
            "setkey": self.setkey,
            "combined_ref": self.combined_ref,
            "outcome": self.outcome,
            "fold_disjoint": self.fold_disjoint,
            "members": [m.to_dict() for m in self.members],
            "culprit": self.culprit,
            "report_lines": list(self.report_lines),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BatchRecord":
        """Reconstruct from a dict, TOLERATING unknown keys — mirrors
        AttentionRecord.from_dict's additive-field discipline. Does NOT
        re-canonicalize members: a persisted record was already canonicalized
        by from_members at write time, and from_dict's job is to reproduce
        exactly what was written, not to re-derive it."""
        members = tuple(BatchMember.from_dict(m) for m in (data.get("members") or ()))
        return cls(
            base_sha=str(data.get("base_sha", "")),
            setkey=str(data.get("setkey", "")),
            combined_ref=str(data.get("combined_ref", "")),
            outcome=str(data.get("outcome", "")),
            fold_disjoint=bool(data.get("fold_disjoint", False)),
            members=members,
            culprit=str(data.get("culprit", "")),
            report_lines=tuple(data.get("report_lines") or ()),
        )


def _stamp_batch_record(repo: str | os.PathLike, record: BatchRecord) -> None:
    """Append one BatchRecord to the durable per-batch log — the promote
    record half of the S2 manifest. Mirrors _stamp_attention_record's guard
    exactly: best-effort (a write failure here must never flip a green
    promote red), and skips silently (no write, no directory created) on a
    synthetic/non-existent repo path so unit tests never leak into the real
    ~/.claude/sable/state."""
    override = os.environ.get("SABLE_MG_BATCH_RECORD_LOG")
    if not override and not Path(repo).is_dir():
        return
    try:
        path = Path(override) if override else (
            snapshot_lib.ensure_state_dir(repo) / BATCH_RECORD_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps(record.to_dict()) + "\n")
    except OSError:
        pass


def read_batch_records(repo: str | os.PathLike = ".") -> list[BatchRecord]:
    """Read every durable batch record back — the read half of the round
    trip _stamp_batch_record writes. Tolerates malformed/unreadable lines
    (skipped, never fatal); mirrors read_attention_records' repo-existence
    guard so a synthetic repo path reads as 'no records'."""
    override = os.environ.get("SABLE_MG_BATCH_RECORD_LOG")
    if not override and not Path(repo).is_dir():
        return []
    path = Path(override) if override else (
        snapshot_lib.ensure_state_dir(repo) / BATCH_RECORD_FILE)
    records: list[BatchRecord] = []
    if not path.is_file():
        return records
    for raw in path.read_text().splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        records.append(BatchRecord.from_dict(data))
    return records


def find_batch_record(repo: str | os.PathLike, combined_ref: str) -> BatchRecord | None:
    """The most recent durable batch record for `combined_ref`, or None —
    the lookup the S2 manifest-reconstruction path uses instead of scanning
    read_batch_records() by hand at every call site."""
    matches = [r for r in read_batch_records(repo) if r.combined_ref == combined_ref]
    return matches[-1] if matches else None


# The fold-commit-message CONTRACT — S2's other durable artifact: every
# fold-chain commit for a batched landing names exactly the ONE member it
# folds in. SABLE-be4lo.4 (the fold builder) is this format's WRITER once it
# lands; this module owns the format itself plus the reader, so the manifest
# is reconstructible from fold commit messages ALONE even with the promote
# record log unavailable.
FOLD_COMMIT_MESSAGE_PREFIX = "batch-fold: "


def fold_commit_message(branch: str, bead_ids: tuple[str, ...]) -> str:
    """The commit message a fold-chain commit for `branch` MUST carry."""
    return f"{FOLD_COMMIT_MESSAGE_PREFIX}{branch} ({','.join(bead_ids)})"


def parse_fold_commit_message(message: str) -> tuple[str, tuple[str, ...]] | None:
    """Inverse of fold_commit_message. None if `message` is not a fold-chain
    commit message at all — e.g. the batch's own base commit, or an
    unrelated commit swept up by a broad `git log` range."""
    if not message.startswith(FOLD_COMMIT_MESSAGE_PREFIX):
        return None
    body = message[len(FOLD_COMMIT_MESSAGE_PREFIX):]
    m = re.match(r"^(.*) \(([^)]*)\)$", body)
    if not m:
        return None
    beads = tuple(b for b in m.group(2).split(",") if b)
    return m.group(1), beads


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
                *, on_adoption_miss: bool = False,
                verdict_source: str = VerdictSource.WAITED.value) -> int:
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
    integrity assertion, the RED eject — is the same code.

    `verdict_source` (SABLE-21rug.1) carries the ORIGINAL verdict's provenance
    into this landing's AttentionRecord — 'waited'/'precomputed' from the
    caller's own classify.Verdict, or 'precomputed' from
    _adoption_miss_optimistic (a stale GREEN preview is consumed, never
    waited on). Purely for that record; it changes no promotion decision."""
    if not optimistic_promotion_enabled():
        assessment = footprint_lib.Assessment(
            None, "optimistic disjoint promotion is disabled (SABLE_MG_OPTIMISTIC=0)")
    else:
        assessment = footprint_lib.assess(repo, bead, base_sha, branch_sha, current_base)

    decision = decide_promotion(classify.GREEN, base_moved=True, disjoint=assessment.disjoint,
                                impact=None, preview_sha=preview_sha)
    combined_sha, impact, impact_detail = "", None, ""
    impact_wall_clock = 0.0
    if decision.action == ACTION_REVERIFY:
        print(f"sable-merge-gate: base {base} moved to {current_base[:7]} but the footprints are "
              f"disjoint — re-verifying the REAL combined tree with the impact tier "
              f"({len(assessment.paths)} changed path(s))")
        combined_sha = preview.build_preview(
            repo, current_base, branch_sha,
            f"ci-verify merge-preview: {branch} onto {base} ({bead}, disjoint re-verify)")
        # SABLE-21rug.1: this call IS the hand-run attention this landing
        # spends — measured here (never inferred) for the AttentionRecord
        # written on the ACTION_PROMOTE branch below.
        _t0 = time.monotonic()
        impact, impact_detail = run_impact_tier(repo, combined_sha, list(assessment.paths))
        impact_wall_clock = time.monotonic() - _t0
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
            record = AttentionRecord(
                bead=bead, branch=branch,
                verdict_source=VerdictSource(verdict_source),
                hand_run_suites=_suites_from_impact_detail(impact_detail),
                red_triage_events=("base_moved",),
                attention_span_seconds=impact_wall_clock)
            _stamp_attention_record(repo, record)
            _append_evidence(repo, bead, f"attention-record: {json.dumps(record.to_dict())}")
        except Exception as exc:  # noqa: BLE001 — observability must never block a green promote
            print(f"sable-merge-gate: attention record skipped after unexpected error: {exc}",
                  file=sys.stderr)
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
                           base_sha, on_adoption_miss=True,
                           # SABLE-21rug.1: a stale GREEN preview is CONSUMED
                           # here, never waited on — the same 'precomputed'
                           # semantics jd5fj.3 gives a stored-run read.
                           verdict_source=VerdictSource.PRECOMPUTED.value)
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
            manager: str, override: str | None,
            coverage_override: str | None = None,
            with_pair: frozenset[str] = frozenset(),
            auto: bool = False) -> int:
    # FIRST, before any git work: is the fleet frozen? (SABLE-jd5fj.5)
    assert_not_frozen(repo)
    # SECOND, still before any git work — a bd-only check, cheap like the
    # freeze read above: does <bead> declare a MUST-LAND-TOGETHER counterpart
    # that this solo promote would silently split? (SABLE-rzkw7)
    try:
        assert_landing_pair_satisfied(repo, bead, with_pair)
    except LandingPairRefused as exc:
        raise GateError(classify.EXIT_PAIR_REFUSED, str(exc)) from exc
    base_ref = classify.qualify_remote_ref(remote, base)
    branch_ref = classify.qualify_remote_ref(remote, branch)
    git_lib._git(repo, "fetch", remote, base, branch)
    base_sha = git_lib.resolve_commit(repo, base_ref)
    branch_sha = git_lib.resolve_commit(repo, branch_ref)

    # SABLE-21rug.4: THE AUTO-PROMOTE GATE. Reached only when this promote is
    # running with NO HUMAN ASKING FOR IT (`auto`); a seat promote — every
    # caller today — takes the identical path it always has, which is what
    # keeps this bead's landing behaviour-neutral until SABLE-21rug.5's shadow
    # mode and computed activation decide when `auto` may be set.
    #
    # Placed HERE, in the assert_* prologue, because it must sit IN FRONT OF
    # BOTH integration-branch writers with nothing built and nothing pushed
    # behind it: a decline costs a preview build, a CI ref, or an impact tier
    # only if it happens after them. A decline routes to the seat's ordinary
    # queue — the status quo for the branch, not a failure of it.
    if auto:
        try:
            allowed = assert_auto_promote_allowed(bead, branch, base, repo, remote,
                                                  base_sha, branch_sha)
            # The ALLOW is recorded just as durably as the DENY. Writing only
            # declines would make the landings that ACTUALLY HAPPENED the
            # unobservable ones — and an unattended landing nobody recorded is
            # precisely what SABLE-21rug.6's audit ("zero input on a landing
            # day is RED") is built to catch.
            _append_evidence(repo, bead,
                f"auto-promote ALLOWED (SABLE-21rug.4): {json.dumps(allowed.to_dict())}")
        except AutoPromoteRefused as exc:
            _append_evidence(repo, bead,
                f"auto-promote DECLINED (SABLE-21rug.4): {json.dumps(exc.evaluation.to_dict())}")
            raise GateError(
                classify.EXIT_PRECONDITION,
                f"AUTO-PROMOTE DECLINED for {bead} ({branch}) — {exc.evaluation.reason}. "
                f"Nothing was built and nothing landed; {branch} stays in the seat's "
                f"ordinary queue for a human to promote.") from exc

    # SABLE-cmar4.5: next, before building anything — is this a PRUNING diff
    # (removed test fn / newly-skipped test / deleted test file) without a
    # carried, passing coverage-delta check? Asked here, right after
    # assert_not_frozen and before any preview/CI work, for the same reason
    # the freeze check goes first: this is a property of the (base, branch)
    # diff itself, not of what CI concludes about it, so there is no reason to
    # spend a preview build on a diff this refuses outright.
    assert_coverage_floor(repo, bead, base_sha, branch_sha, coverage_override)

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
    # SABLE-21rug.1: this landing's attention span — the measured wall-clock
    # this promote call spends waiting on a verdict. Zero is the honest value
    # for an override (no run is consulted, nothing is waited on), never an
    # inferred one.
    attention_span = 0.0
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
            attention_span = time.monotonic() - t0
            budget_lib.check_and_file(repo, "merge_preview", attention_span,
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
                                   ref, preview_sha, base_sha, branch_sha, current_base,
                                   verdict_source=verdict.source)
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
                                       ref, preview_sha, base_sha, branch_sha, current_base,
                                       verdict_source=verdict.source)
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
            if not batch_key.tip_matches(landed, preview_sha):
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
            # SABLE-21rug.1: a per-landing attention record joins the evidence
            # notes above — the epic's mandatory-first baseline input. Wrapped
            # like the cleanup below: observability must never block a green
            # promote.
            try:
                record = AttentionRecord(
                    bead=bead, branch=branch,
                    verdict_source=VerdictSource(verdict.source),
                    red_triage_events=("override",) if override else (),
                    attention_span_seconds=attention_span)
                _stamp_attention_record(repo, record)
                _append_evidence(repo, bead, f"attention-record: {json.dumps(record.to_dict())}")
            except Exception as exc:  # noqa: BLE001 — observability must never block a green promote
                print(f"sable-merge-gate: attention record skipped after unexpected error: {exc}",
                      file=sys.stderr)
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


# --------------------------------------------------------------------------
# The BATCH LANDING PATH (SABLE-be4lo.7, architecture decisions 6-8)
# --------------------------------------------------------------------------
#
# GATE-CLASS by construction: this path lands a whole fold chain onto the
# integration branch in exactly ONE fast-forward, or lands NOTHING. It is the
# THIRD (and, by test_the_module_has_exactly_two_writers_to_the_integration_
# branch's successor, last-enumerated) writer to the integration branch — and
# like the two single-branch writers it is preceded by a guard the caller
# cannot skip: the two single-branch writers are guarded by decide_promotion;
# this one by assert_batch_budget_present + the per-batch stale-base check +
# the built-on-this-base ancestry precondition + the post-push integrity
# assertion. Four behaviors, in the order the epic locked them:
#
#   1. BUDGET REFUSAL (decision 6) — before ANY git work, refuse unless the
#      promote-budget artifact carries the be4lo.6 combined-tree field, naming
#      it. Extend-tool-first enforcement made mechanical: a pre-extension
#      budget tool cannot silently drive a batch on hand-carried terms.
#   2. PER-BATCH STALE-BASE (decision 7) — the fold chain was formed on
#      base_sha; if the integration tip has moved off base_sha by land time,
#      RE-FORM (land nothing) rather than land onto a base the chain, and the
#      CI run that verified it, never saw.
#   3. NO-HALF-LANDING (decision 8, absolute) — the land is exactly ONE
#      `git push <fold_tip>:refs/heads/<base>` fast-forward. git moves the ref
#      to the chain tip or not at all; there is no intermediate state to
#      observe or recover from. On ANY push failure nothing landed and every
#      member falls back to the serial queue AS ITSELF, its own preview ref
#      untouched (this path never writes a member ref, so "previews intact" is
#      structural, not best-effort).
#   4. ONE-PROMOTE SEAT POLICY (decision 7) — enforced by 2+3, NOT by a seat
#      mutex: a serial promote that interleaves moves the base, so this land's
#      stale-base check (2) refuses it before the push, or its no-force
#      fast-forward is rejected non-ff (3); symmetrically, a landed batch makes
#      an interleaved serial promote's own push non-ff. At most one of the two
#      lands against a given base; the other re-forms. The ordering invariant
#      lives in the flow, so no seat needs to hold state a recycle could lose.

BATCH_BUDGET_FIELD = "recommended_batch_wrapper_timeout_s"
"""The combined-tree field combined_tree_budget() adds (SABLE-be4lo.6). Its
PRESENCE in a promote-budget artifact is the mechanical proof the budget tool
was extended before this batch runs — decision 6's blocking dependency."""

# Land dispositions — the three terminal shapes a batch land can take. Named
# next to (and reusing) the BATCH_OUTCOME_* record constants above: a LANDED
# result stamps BATCH_OUTCOME_LANDED, a fell-back result BATCH_OUTCOME_
# FELL_BACK_SERIAL; REFORM is transient (the caller rebuilds + re-gates) so it
# stamps no durable record.
BATCH_OUTCOME_REFORM = "reform"


def assert_batch_budget_present(budget: dict) -> None:
    """BUDGET REFUSAL (behavior 1, decision 6). Raise LOUDLY — naming the
    missing field — unless `budget` carries the be4lo.6 combined-tree field.
    Returns None (proceeds silently) when it is present.

    This is the batch path's before-anything guard, the same placement
    assert_not_frozen / assert_coverage_floor occupy in promote(): a budget
    artifact that predates the be4lo.6 extension (e.g. impact_budget()'s
    single-branch shape, which carries recommended_wrapper_timeout_s but not
    this field) must stop the batch here, not deep in the land where a
    hand-carried interim number could paper over the gap (the retired +900
    pattern; the 5v3d5 precedent). Exit code is EXIT_PRECONDITION — a property
    of the inputs, decided before any git work, exactly like the freeze and
    coverage-floor refusals."""
    if not isinstance(budget, dict) or BATCH_BUDGET_FIELD not in budget:
        raise GateError(
            classify.EXIT_PRECONDITION,
            f"batch landing REFUSED: the promote-budget artifact is missing the "
            f"combined-tree field {BATCH_BUDGET_FIELD!r}. The SABLE-be4lo.6 "
            f"promote-budget extension is a BLOCKING DEPENDENCY of any batched "
            f"landing (architecture decision 6, extend-tool-first): derive the "
            f"budget from `sable-merge-gate promote-budget --member-footprint "
            f"<paths> [--member-footprint ...] --json`, which produces "
            f"{BATCH_BUDGET_FIELD!r}. A batch must never run on a budget that "
            f"does not name it.")


@dataclass(frozen=True)
class BatchLandResult:
    """The terminal shape of one batch land. `outcome` is one of
    BATCH_OUTCOME_LANDED / BATCH_OUTCOME_REFORM / BATCH_OUTCOME_FELL_BACK_SERIAL;
    `landed_tip` is the integration tip after a successful land and None
    otherwise; `fallback_branches` is every member branch returning to the
    serial queue as itself (empty only on a clean land) so the caller never has
    to re-derive the member set from the outcome string."""
    outcome: str
    landed_tip: str | None
    reason: str
    fallback_branches: tuple[str, ...]

    @property
    def landed(self) -> bool:
        return self.outcome == BATCH_OUTCOME_LANDED

    @property
    def exit_code(self) -> int:
        if self.outcome == BATCH_OUTCOME_LANDED:
            return classify.EXIT_OK
        # REFORM and FELL_BACK_SERIAL are both retry-safe "rebuild + re-gate"
        # states — nothing landed, nothing to fix — so they share the same
        # retryable taxonomy code the single-branch base-moved path uses.
        return classify.EXIT_BASE_MOVED


def _fold_tip_built_on(repo: str, base_sha: str, fold_tip: str, member_count: int) -> bool:
    """True iff `base_sha` is the fold chain's BASE PARENT — the object-level
    proof the tested combined tip was actually folded ONTO this base. A
    sable_batch_fold_lib fold chain is exactly `member_count` two-parent
    commits deep, each commit's FIRST parent being the previous fold commit and
    the deepest commit's first parent being base_sha — so walking first parents
    `member_count` times from the tip lands EXACTLY on base_sha. This is
    stronger than plain ancestry: a member branch may itself descend from
    base_sha, which would make base_sha a general ancestor of the tip even when
    the chain was folded onto a DIFFERENT base. Only the first-parent-depth
    check distinguishes a genuine stale base from a mismatched (base_sha,
    fold_tip) pair (a precondition bug, not a stale base)."""
    cp = git_lib._git(repo, "rev-parse", "--verify",
                      f"{fold_tip}~{member_count}^{{commit}}", check=False)
    return cp.returncode == 0 and cp.stdout.strip() == base_sha


def land_batch(repo: str, remote: str, base: str, base_sha: str, fold_tip: str,
               members: list, *, budget: dict, combined_ref: str = "",
               manager: str = "lincoln") -> BatchLandResult:
    """Land a verified batch fold chain onto the integration branch, whole or
    not at all. `fold_tip` is the SAME combined object CI verified on
    ci-verify/batch-<setkey7> (byte-identical promotion — never re-folded here,
    which would produce an untested SHA); `base_sha` is the integration tip the
    chain was formed on; `members` are the typed BatchMember set. See this
    section's header for the four behaviors and their ordering.

    Never raises on a stale base or a losing push race — those are RETURNED as
    REFORM / FELL_BACK_SERIAL results so the caller can act on the disposition.
    Raises only on a true precondition breach (budget absent → GateError 3;
    empty batch → EmptyBatchError; a fold_tip not built on base_sha →
    GateError 3) or the post-push integrity failure (GateError 4)."""
    # 0. Freeze first — the same before-any-git-work read promote() opens with.
    assert_not_frozen(repo)
    # 1. BUDGET REFUSAL — before any git work.
    assert_batch_budget_present(budget)
    if not members:
        raise EmptyBatchError(
            "land_batch requires at least one member — an empty batch is a "
            "caller bug, never a vacuous no-op land (SABLE-p9n7k)")
    fallback = tuple(m.branch for m in members)
    base_ref = classify.qualify_remote_ref(remote, base)

    # Object-level precondition: the tested tip must actually have been built
    # ON base_sha. A tip that was not is a mismatched (base_sha, fold_tip) pair
    # — a caller bug — NOT a stale base, so it must fail loud here rather than
    # masquerade as a re-form.
    resolved_tip = git_lib.resolve_commit(repo, fold_tip)
    if not _fold_tip_built_on(repo, base_sha, resolved_tip, len(members)):
        raise GateError(
            classify.EXIT_PRECONDITION,
            f"batch fold tip {resolved_tip[:7]} was not built on base {base_sha[:7]} "
            f"— the (base_sha, fold_tip) pair does not correspond; not a stale base.")

    # 2. PER-BATCH STALE-BASE — is the integration tip still the commit this
    # chain (and its CI run) was formed on? Asked from an OBSERVATION before the
    # push, mirroring promote()'s jd5fj.4 pre-push check. A move → RE-FORM: land
    # nothing, touch no member ref, hand the members back to be rebuilt.
    git_lib._git(repo, "fetch", remote, base, check=False)
    current_base = git_lib.resolve_commit(repo, base_ref)
    if not batch_key.tip_matches(current_base, base_sha):
        reason = (f"per-batch stale base: integration tip {current_base[:7]} != "
                  f"{base_sha[:7]} the fold chain was formed on — RE-FORM, nothing "
                  f"landed; member previews intact.")
        _notify(manager, f"batch land RE-FORM ({combined_ref or 'batch'}): {reason}")
        return BatchLandResult(BATCH_OUTCOME_REFORM, None, reason, fallback)

    # 3. NO-HALF-LANDING — ONE fast-forward. No --force, so git rejects a
    # non-ff push (the narrow race where the base moved between the check above
    # and here) rather than clobbering: the ref advances to fold_tip atomically
    # or does not move. On any failure NOTHING landed and every member falls
    # back to the serial queue as itself.
    push_cp = git_lib._git(repo, "push", git_lib.resolve_remote_url(repo, remote),
                           f"{fold_tip}:refs/heads/{base}", check=False)
    if push_cp.returncode != 0:
        reason = (f"NO-HALF-LANDING: the single fast-forward of {base} to fold tip "
                  f"{resolved_tip[:7]} failed (non-ff or push error) — nothing landed; "
                  f"{len(fallback)} member(s) fall back to the serial queue as "
                  f"themselves: {push_cp.stdout.strip()[:300]}")
        _notify(manager, f"batch land FELL BACK to serial ({combined_ref or 'batch'}): {reason}")
        return BatchLandResult(BATCH_OUTCOME_FELL_BACK_SERIAL, None, reason, fallback)

    # 4. Integrity: the integration tip must now be EXACTLY the tested object.
    # Not rollback-capable (the object is already pushed) — it fails LOUD if
    # chuck's single-writer discipline was violated, the batch analogue of
    # promote()'s F3 assertion.
    git_lib._git(repo, "fetch", remote, base)
    landed = git_lib.resolve_commit(repo, base_ref)
    if not batch_key.tip_matches(landed, resolved_tip):
        raise GateError(
            classify.EXIT_INTEGRITY,
            f"integrity abort: base {base} tip {landed} != tested fold tip "
            f"{resolved_tip} after the batch fast-forward")

    # Landed. Stamp the durable manifest (best-effort — observability must never
    # flip a landed batch), append per-member evidence, and hand back the tip.
    reason = (f"BATCH LANDED: {len(members)} member(s) fast-forwarded {base} to "
              f"fold tip {resolved_tip[:7]} in ONE cycle (combined_ref "
              f"{combined_ref or 'n/a'}).")
    try:
        record = BatchRecord.from_members(
            base_sha, list(members), combined_ref=combined_ref,
            outcome=BATCH_OUTCOME_LANDED, fold_disjoint=True)
        _stamp_batch_record(repo, record)
    except Exception as exc:  # noqa: BLE001 — a landed batch must stay landed
        print(f"sable-merge-gate: batch record skipped after unexpected error: {exc}",
              file=sys.stderr)
    for m in members:
        for bead_id in m.bead_ids:
            try:
                _append_evidence(repo, bead_id, reason)
            except Exception:  # noqa: BLE001 — evidence is best-effort
                pass
    _notify(manager, reason)
    return BatchLandResult(BATCH_OUTCOME_LANDED, landed, reason, ())


def resolve_batch_members(repo: str, remote: str, base_sha: str, member_specs: list,
                          command: str) -> list[BatchMember]:
    """Turn raw `branch` / `branch:BEAD[,BEAD...]` CLI specs into the typed
    BatchMember set both batch entrypoints (land-batch, bisect-batch) need.

    ONE resolver rather than one per subcommand: a bisection that derived its
    member tips or footprints even slightly differently from the land it feeds
    would be bisecting a different batch than the one that went red — the
    Shotgun Surgery risk the epic's architecture review flagged on the
    (base, branch)-pair sites, in its member-set form. Raises EXIT_USAGE on an
    empty spec list: a batch command with no members is a usage error, never a
    vacuous no-op (SABLE-p9n7k)."""
    members: list[BatchMember] = []
    for spec in member_specs:
        branch, _, bead_part = spec.partition(":")
        bead_ids = tuple(b for b in bead_part.split(",") if b)
        git_lib._git(repo, "fetch", remote, branch, check=False)
        tip = git_lib.resolve_commit(repo, classify.qualify_remote_ref(remote, branch))
        paths = tuple(sorted(footprint_lib.changed_paths(repo, base_sha, tip)))
        members.append(BatchMember(branch, tip, bead_ids, paths))
    if not members:
        raise GateError(classify.EXIT_USAGE, f"{command} requires at least one --member")
    return members


def land_batch_from_refs(base: str, member_specs: list, repo: str, remote: str,
                         manager: str = "lincoln") -> int:
    """CLI orchestrator for `sable-merge-gate land-batch`: resolve each member
    branch's tip and changed-path footprint, DERIVE the combined-tree budget
    (so the be4lo.6 field is present by construction on the sanctioned path),
    resolve the already-verified combined object from its ci-verify/batch-
    <setkey7> ref, and land it via land_batch. Returns the land result's exit
    code. Kept in the lib so bin/sable-merge-gate stays argparse + one call
    (test_cli_is_thin).

    `base` is the raw --base value (None → resolved here via resolve_base, the
    same precedence promote() uses); `member_specs` are raw `branch` or
    `branch:BEAD[,BEAD...]` strings."""
    base = git_lib.resolve_base(base, repo)
    base_ref = classify.qualify_remote_ref(remote, base)
    git_lib._git(repo, "fetch", remote, base, check=False)
    base_sha = git_lib.resolve_commit(repo, base_ref)

    members = resolve_batch_members(repo, remote, base_sha, member_specs, "land-batch")

    budget = combined_tree_budget([list(m.footprint_paths) for m in members])
    setkey = batch_key.setkey(base_sha, [m.tip_sha for m in members])
    combined_ref = classify.preview_ref_name("batch", setkey)
    fold_tip = git_lib.remote_ref_commit(repo, remote, combined_ref)
    if not fold_tip:
        raise GateError(
            classify.EXIT_PRECONDITION,
            f"no verified combined object to land: {combined_ref} is absent on "
            f"{remote}. Form + CI-verify the batch (sable_batch_fold_lib.push_batch_ref) "
            f"before landing it.")
    result = land_batch(repo, remote, base, base_sha, fold_tip, members,
                        budget=budget, combined_ref=combined_ref, manager=manager)
    print(result.reason)
    return result.exit_code


def register_land_batch(sub) -> None:
    """Register the `land-batch` subcommand on an argparse subparsers object.
    Lives here rather than in bin/sable-merge-gate so the CLI stays argparse-
    dispatch-thin (test_cli_is_thin) even as this GATE-CLASS path adds a
    subcommand — the subparser is declaration, not logic, and the logic it
    dispatches to (land_batch_from_refs) is all in this module."""
    lb = sub.add_parser("land-batch", help="land a CI-verified batch fold chain "
                        "onto the integration branch in ONE fast-forward, or nothing")
    lb.add_argument("--base", default=None, help="integration branch to land onto; "
                    "same resolution as promote")
    lb.add_argument("--member", action="append", default=[], dest="members", required=True,
                    metavar="BRANCH[:BEAD[,BEAD...]]", help="repeatable; one per batch member")
    lb.add_argument("--repo", default=os.environ.get("SABLE_MG_REPO", os.getcwd()))
    lb.add_argument("--remote", default=os.environ.get("SABLE_MG_REMOTE", "origin"))
    lb.add_argument("--manager", default="lincoln")


# --------------------------------------------------------------------------
# THE RED-BATCH BISECTION STATE MACHINE (SABLE-be4lo.8, architecture decision 4)
# --------------------------------------------------------------------------
#
# The other half of the batched gate: what happens when the combined run comes
# back RED. GATE-CLASS, like the landing path above, and — this is the whole
# safety argument — a NON-LANDING path. Nothing in this section pushes to the
# integration branch. It only re-folds subsets, re-verifies them, and reports.
# "Zero members land from the batched path across every red node of the
# bisection tree" is therefore STRUCTURAL, not a discipline this code has to
# keep: land_batch is the only writer, and nothing here calls it.
#
# THE STATE MACHINE. A node is a member subset PRESUMED to carry the failure.
# Starting at the whole (verified-red) batch:
#
#   |S| == 1   verify S alone. RED -> S's one member IS the culprit, and it was
#              seen red BY ITSELF, never inferred. GREEN -> this branch of the
#              tree attributes nothing.
#   |S| >= 2   split into halves L, R (left takes the extra on an odd count).
#              Verify L.
#                RED   -> descend into L. R is untouched and re-batchable.
#                GREEN -> descend into R WITHOUT spending a run on R itself:
#                         the culprit, if there is a single one, is in R, and
#                         if R's own descent finds nothing then no single member
#                         is at fault and the redness is an INTERACTION.
#
# Two properties fall out of that shape, and both are the point:
#
#  1. A NAMED CULPRIT IS ALWAYS BACKED BY A SOLO RED RUN. The machine never
#     concludes "it must be the other one" from a green sibling — that
#     inference is unsound precisely when the failure is an interaction, which
#     is the case this whole section exists for. Descending on a green half is
#     a cheap SEARCH heuristic; the naming itself is always a measurement.
#  2. THE <=3-RUNS-AT-n=4 BOUND (architecture decision 4) is arithmetic, not
#     aspiration — max_bisection_rounds() states it and bisect_red_batch()
#     ASSERTS its own run count against it, so a future edit that widens the
#     search reds the gate here rather than silently costing the fleet CI
#     cycles. Verifying BOTH halves at every level (the obvious alternative)
#     costs 4 at n=4 and misses the bound.
#
# COULD-NOT-ATTRIBUTE is the interaction verdict: the combined object is red
# and every subset the descent verified came back green. No member-level
# disjointness check can catch that — two members can touch disjoint files and
# still break each other, which is exactly why the epic verifies the COMBINED
# tree rather than trusting disjointness (locked contract SABLE-djopw:
# disjointness is necessary, never sufficient). The safe degradation is
# ALL-SERIAL: every member goes back to the serial queue and lands as itself,
# where the interacting pair surfaces LATE BUT LOUD — as a red on the second
# member's own serial gate cycle, against a base that by then contains the
# first. Late-but-loud beats fast-and-silent; the alternative (land the batch
# anyway, or drop members quietly) is the failure this bead exists to prevent.


BISECT_REPORT_COULD_NOT_ATTRIBUTE = "COULD-NOT-ATTRIBUTE"
"""The interaction verdict's token, verbatim. Named as a constant so the
acceptance, the report and any downstream reader agree on ONE spelling."""


class VacuousBisectionError(ValueError):
    """Raised when a bisection result would carry ZERO verification rounds.

    A red batch that produced no bisection rounds has not been bisected — it
    has been asserted about. Every property this section reports (a culprit, a
    COULD-NOT-ATTRIBUTE, a bound) is a statement ABOUT the round set, so an
    empty round set makes all of them vacuously true at once, which is the
    exact shape SABLE-p9n7k exists to refuse. Constructing the result raises
    instead, so neither the caller nor a test can pass over nothing."""


@dataclass(frozen=True)
class BisectionRound:
    """ONE combined verification run performed during a bisection — a node of
    the bisection tree, recorded whether it came back green or red.

    branches    — the member subset this round verified, in canonical order.
    combined_ref— the ci-verify/batch-<setkey7> ref this round's object was
                  pushed to; one ref per round is the observable proof of how
                  many extra combined runs the bisection actually cost.
    fold_tip    — the folded object that ref carries.
    green       — the verdict. `green is False` is a RED NODE, and the
                  no-silent-landing sweep is defined over exactly those.
    adopted     — True when this round ADOPTED an object already standing on
                  its ref rather than folding and pushing a fresh one (see
                  _batch_round_object)."""
    branches: tuple[str, ...]
    combined_ref: str
    fold_tip: str
    green: bool
    adopted: bool = False

    def to_dict(self) -> dict:
        return {"branches": list(self.branches), "combined_ref": self.combined_ref,
                "fold_tip": self.fold_tip, "green": self.green, "adopted": self.adopted}


@dataclass(frozen=True)
class BisectionResult:
    """The terminal shape of one red-batch bisection.

    outcome      — BATCH_OUTCOME_BISECTED_CULPRIT or
                   BATCH_OUTCOME_COULD_NOT_ATTRIBUTE. Never a landing outcome:
                   this path lands nothing.
    culprit      — the member branch verified RED ALONE, or "" when the
                   bisection could not attribute.
    rounds       — every combined run the bisection spent, in the order spent.
    serial_queue — the members returning to the SERIAL queue as themselves:
                   just the culprit on an attribution, ALL members on a
                   COULD-NOT-ATTRIBUTE (the all-serial fallback).
    rebatch      — the members that may be re-batched together (empty on a
                   COULD-NOT-ATTRIBUTE — an unattributed interaction makes the
                   whole set suspect, so re-batching any of it would re-run the
                   same unanswered question).
    report_lines — the loud, observable report, verbatim as printed, notified,
                   and stamped into the promote record."""
    outcome: str
    culprit: str
    rounds: tuple[BisectionRound, ...]
    serial_queue: tuple[str, ...]
    rebatch: tuple[str, ...]
    report_lines: tuple[str, ...]

    def __post_init__(self):
        if not self.rounds:
            raise VacuousBisectionError(
                "a bisection result must carry at least one verification round — "
                "a red batch that produced zero rounds was never bisected, and "
                "every assertion over an empty round set passes vacuously "
                "(SABLE-p9n7k)")

    @property
    def attributed(self) -> bool:
        return self.outcome == BATCH_OUTCOME_BISECTED_CULPRIT

    @property
    def red_rounds(self) -> tuple[BisectionRound, ...]:
        """Every RED node of the bisection tree — the set the no-silent-landing
        sweep is defined over. Non-empty on an attribution (the culprit's own
        solo run is red); empty on a COULD-NOT-ATTRIBUTE, which is what that
        verdict MEANS."""
        return tuple(r for r in self.rounds if not r.green)

    @property
    def exit_code(self) -> int:
        """EXIT_RED for both outcomes. The batch was red and nothing landed —
        the same thing a red single-branch promote reports, so a caller that
        already branches on the taxonomy needs no new case to stay correct."""
        return classify.EXIT_RED


def max_bisection_rounds(n: int) -> int:
    """The WORST-CASE number of extra combined runs bisect_red_batch can spend
    on an n-member red batch. Pure arithmetic over the state machine's own
    shape: one run to verify the left half, then the worst of the two halves.

        f(1) = 1                      (the solo verification that names a culprit)
        f(n) = 1 + max(f(|L|), f(|R|))

    f(4) == 3, which IS architecture decision 4's "<=3 extra combined runs at
    n=4" — stated here as a computation so the bound is checkable at any n and
    asserted by the machine against itself, rather than being a sentence in a
    bead that only a hand-written n=4 test ever compares against."""
    if n < 1:
        raise ValueError(f"a bisection needs at least one member, got n={n}")
    if n == 1:
        return 1
    left = (n + 1) // 2
    return 1 + max(max_bisection_rounds(left), max_bisection_rounds(n - left))


def bisection_split(members: list) -> tuple[list, list]:
    """Split a bisection node into halves; the LEFT half takes the extra member
    on an odd count. Both halves are non-empty by construction, so the descent
    always makes progress and cannot loop."""
    if len(members) < 2:
        raise ValueError("bisection_split needs at least two members to split")
    mid = (len(members) + 1) // 2
    return list(members[:mid]), list(members[mid:])


def _bisection_report(outcome: str, culprit: str, members: tuple, rounds: list,
                      bound: int, serial_queue: tuple, rebatch: tuple) -> tuple[str, ...]:
    """The report's exact words. Kept in ONE function because the vocabulary is
    the deliverable — 'the report NAMES the member' and the literal token
    COULD-NOT-ATTRIBUTE are what the acceptance reads, and a second site
    phrasing either of them slightly differently is how a verbatim contract
    stops being verbatim. Note what is NOT here: the attributed report never
    contains the COULD-NOT-ATTRIBUTE token, which is what makes that token a
    discriminating signal rather than decoration."""
    n = len(members)
    held = (f"ZERO members landed from the batched path: all {n} member(s) are held "
            f"at the batch and land only through the serial queue.")
    if outcome == BATCH_OUTCOME_BISECTED_CULPRIT:
        return (
            f"BATCH RED — CULPRIT ISOLATED: {culprit}. Verified RED ALONE (never "
            f"inferred from a green sibling) in {len(rounds)} extra combined run(s), "
            f"bound {bound} at n={n}.",
            f"SERIAL QUEUE: {culprit} returns to the serial queue as itself.",
            "RE-BATCH: " + (", ".join(rebatch) if rebatch else
                            "(nothing — the culprit was the only member)"),
            held,
        )
    return (
        f"{BISECT_REPORT_COULD_NOT_ATTRIBUTE}: the combined object is RED and every "
        f"bisected half verified GREEN. This is an INTERACTION-ONLY failure — no "
        f"member is individually at fault — and it is the case member disjointness "
        f"cannot catch, which is why the combined run exists at all.",
        f"ALL-SERIAL FALLBACK: all {n} member(s) return to the serial queue and land "
        f"as themselves, in canonical order: {', '.join(serial_queue)}. The "
        f"interacting pair surfaces LATE BUT LOUD, as a red on the second member's "
        f"own serial gate cycle against a base that by then carries the first.",
        held,
    )


def _batch_round_object(repo: str, remote: str, base_sha: str, subset: list) -> tuple[str, str, bool]:
    """The combined object for ONE bisection round, as (tip, ref, adopted).

    ADOPT BEFORE RE-FOLDING, for the same reason promote() adopts a kicked
    preview instead of rebuilding one. The round's ref name is a pure function
    of (base_sha, member tips) — batch_key.setkey — but the FOLDED COMMIT is
    not: commit-tree stamps a committer date, so re-folding the same subset a
    second later produces a different SHA for the same ref. Pushing that is a
    NON-FAST-FORWARD (git refuses it, and a --force would cancel any run
    already in flight on that ref — the SABLE-sc24 spurious-RED failure). So
    when the ref already stands, and the object standing on it is verifiably a
    fold chain of exactly this many members built on exactly this base
    (_fold_tip_built_on — the ref name alone is not taken as proof), that
    object IS this round's object: same base, same member tips, already
    triggered. Anything else — ref absent, unfetchable, or not built on this
    base — falls through to a normal fold-and-push, so adoption can never
    change a verdict, only skip a duplicate.

    This is what makes a bisection RESUMABLE: a seat that crashed mid-bisection
    re-runs it and pays only for the rounds it had not yet triggered."""
    fold_members = [fold_lib.FoldMember(m.branch, m.tip_sha,
                                        m.bead_ids[0] if m.bead_ids else "")
                    for m in subset]
    ref = classify.preview_ref_name(
        "batch", batch_key.setkey(base_sha, [m.tip_sha for m in subset]))
    standing = git_lib.remote_ref_commit(repo, remote, ref)
    if standing:
        git_lib._git(repo, "fetch", remote, ref, check=False)
        if _fold_tip_built_on(repo, base_sha, standing, len(subset)):
            return standing, ref, True
        raise GateError(
            classify.EXIT_PRECONDITION,
            f"bisection round REFUSED: {ref} already stands at {standing[:7]}, which is "
            f"NOT a {len(subset)}-member fold chain built on base {base_sha[:7]} (or is "
            f"not fetchable, so it cannot be shown to be one). The ref name is derived "
            f"from exactly that base and member set, so this is a foreign or corrupt "
            f"ref, not this round's object. Refusing rather than force-pushing over it: "
            f"a force would cancel any run in flight on that ref (SABLE-sc24). Delete "
            f"the ref and re-run.")
    tip, pushed_ref = fold_lib.push_batch_ref(repo, remote, base_sha, fold_members)
    return tip, pushed_ref, False


def bisect_red_batch(repo: str, remote: str, base_sha: str, members: list, verify,
                     *, combined_ref: str = "", manager: str = "lincoln") -> BisectionResult:
    """Bisect a RED batch: name the culprit, or say COULD-NOT-ATTRIBUTE — and
    land nothing either way. See this section's header for the state machine.

    `verify(subset, fold_tip, combined_ref) -> bool` is the combined-run seam:
    True means that subset's folded object verified GREEN. ci_batch_verify()
    is the production implementation (read the Actions verdict for the ref this
    function just pushed); a caller with its own verifier — a local impact tier,
    an integration sandbox — passes one in. Every subset's object comes from
    sable_batch_fold_lib, the SAME builder that produced the object which went
    red (or is adopted from that subset's standing ref — see
    _batch_round_object), so a bisection round tests what a landing of that
    subset would land.

    Raises EmptyBatchError on an empty member list (never a vacuous no-op
    bisection) and GateError(EXIT_PRECONDITION) if the machine ever spends more
    runs than max_bisection_rounds() allows."""
    if not members:
        raise EmptyBatchError(
            "bisect_red_batch requires at least one member — an empty batch cannot "
            "be red, and bisecting nothing is a caller bug (SABLE-p9n7k)")
    # Canonical order, by the SAME key BatchRecord.from_members sorts on: the
    # bisection tree — and therefore which culprit a multi-culprit batch names
    # first — must be a function of the member SET, not of the caller's
    # admission order.
    canonical = tuple(sorted(members, key=lambda m: m.tip_sha))
    rounds: list[BisectionRound] = []
    seen: dict[tuple[str, ...], bool] = {}

    def _verify_subset(subset: list) -> bool:
        key = tuple(m.branch for m in subset)
        if key in seen:
            # Already measured this exact subset (the |L|==1 red case descends
            # into a half it just verified). Re-running it would spend a real CI
            # cycle to re-learn a known fact and would inflate the round count
            # past the bound for no information.
            return seen[key]
        tip, ref, adopted = _batch_round_object(repo, remote, base_sha, subset)
        green = bool(verify(list(subset), tip, ref))
        rounds.append(BisectionRound(key, ref, tip, green, adopted))
        seen[key] = green
        return green

    def _descend(subset: list) -> str:
        if len(subset) == 1:
            # A culprit is named ONLY here, and only on a solo RED run.
            return "" if _verify_subset(subset) else subset[0].branch
        left, right = bisection_split(subset)
        if not _verify_subset(left):
            return _descend(left)
        return _descend(right)

    culprit = _descend(list(canonical))

    bound = max_bisection_rounds(len(canonical))
    if len(rounds) > bound:
        raise GateError(
            classify.EXIT_PRECONDITION,
            f"bisection spent {len(rounds)} combined runs on n={len(canonical)}, past "
            f"its own bound of {bound} (architecture decision 4). The search widened "
            f"without the bound moving with it — a batch that costs more CI cycles "
            f"than it saves is the failure this check exists to catch.")

    branches = tuple(m.branch for m in canonical)
    if culprit:
        outcome = BATCH_OUTCOME_BISECTED_CULPRIT
        serial_queue = (culprit,)
        rebatch = tuple(b for b in branches if b != culprit)
    else:
        outcome = BATCH_OUTCOME_COULD_NOT_ATTRIBUTE
        serial_queue = branches
        rebatch = ()
    report = _bisection_report(outcome, culprit, canonical, rounds, bound,
                               serial_queue, rebatch)
    result = BisectionResult(outcome, culprit, tuple(rounds), serial_queue, rebatch, report)

    # LOUD AND OBSERVABLE, on all three channels a red batch is read through:
    # the seat's own stdout, the lane manager's cockpit, and the durable promote
    # record. A degradation only one of the three can see is the silent kind.
    rendered = render_bisection_report(result)
    print(rendered)
    _notify(manager, f"batch RED ({combined_ref or 'batch'}):\n{rendered}")
    _stamp_batch_record(repo, BatchRecord.from_members(
        base_sha, list(canonical), combined_ref=combined_ref, outcome=outcome,
        fold_disjoint=True, culprit=culprit, report_lines=report))
    return result


def render_bisection_report(result: BisectionResult) -> str:
    """The report as one printable block — the report LINES, verbatim, never a
    re-derivation. Every channel renders through here so stdout, the cockpit
    notification and the promote record cannot drift apart."""
    return "\n".join(result.report_lines)


def ci_batch_verify(repo: str, remote: str):
    """The PRODUCTION verify seam: a bisection round's object has just been
    pushed to its own ci-verify/batch-<setkey7> ref, so the verdict for that
    round is the Actions verdict for that ref — read, never recomputed.

    A verdict that is neither green nor red (Actions down, a cancelled run)
    RAISES with that verdict's own taxonomy code rather than being coerced to
    either. A bisection that guessed 'red' on an unobtainable verdict would
    name an innocent member; one that guessed 'green' would clear a guilty
    one.

    The ref is deleted once its verdict is IN HAND — the same cleanup-on-both-
    polarities discipline promote() keeps, and the complement of
    _batch_round_object's adoption rather than a contradiction of it: a seat
    that dies BEFORE the verdict leaves the ref standing, so the re-run adopts
    it and re-triggers nothing; one that dies AFTER has already consumed it,
    so the re-run correctly folds and pushes afresh."""
    def _verify(subset: list, fold_tip: str, combined_ref: str) -> bool:
        verdict = preview.acquire_verdict(repo, combined_ref, fold_tip)
        preview.delete_ci_ref(repo, remote, combined_ref)
        if verdict.outcome == classify.GREEN:
            return True
        if verdict.outcome == classify.RED:
            return False
        raise GateError(
            verdict.exit_code,
            f"bisection round {combined_ref} got no usable verdict "
            f"({verdict.outcome}/{verdict.conclusion}) — a bisection may not guess a "
            f"verdict it could not read: guessing red names an innocent member, "
            f"guessing green clears a guilty one.")
    return _verify


def bisect_batch_from_refs(base: str, member_specs: list, repo: str, remote: str,
                           manager: str = "lincoln") -> int:
    """CLI orchestrator for `sable-merge-gate bisect-batch`: resolve the member
    set exactly as land-batch does (resolve_batch_members — one resolver, so the
    bisected batch is the batch that went red), then run the state machine with
    the production CI verifier. Returns the result's exit code (always
    EXIT_RED — the batch was red and nothing landed)."""
    base = git_lib.resolve_base(base, repo)
    git_lib._git(repo, "fetch", remote, base, check=False)
    base_sha = git_lib.resolve_commit(repo, classify.qualify_remote_ref(remote, base))
    members = resolve_batch_members(repo, remote, base_sha, member_specs, "bisect-batch")
    setkey = batch_key.setkey(base_sha, [m.tip_sha for m in members])
    result = bisect_red_batch(repo, remote, base_sha, members,
                              ci_batch_verify(repo, remote),
                              combined_ref=classify.preview_ref_name("batch", setkey),
                              manager=manager)
    return result.exit_code


def register_bisect_batch(sub) -> None:
    """Register the `bisect-batch` subcommand — same declaration-not-logic
    placement as register_land_batch above, for the same test_cli_is_thin
    reason."""
    bb = sub.add_parser("bisect-batch", help="bisect a RED batch: name the culprit in "
                        "<=3 extra combined runs at n=4, or report COULD-NOT-ATTRIBUTE. "
                        "Lands nothing.")
    bb.add_argument("--base", default=None, help="integration branch the batch was "
                    "formed on; same resolution as promote")
    bb.add_argument("--member", action="append", default=[], dest="members", required=True,
                    metavar="BRANCH[:BEAD[,BEAD...]]", help="repeatable; one per batch member")
    bb.add_argument("--repo", default=os.environ.get("SABLE_MG_REPO", os.getcwd()))
    bb.add_argument("--remote", default=os.environ.get("SABLE_MG_REMOTE", "origin"))
    bb.add_argument("--manager", default="lincoln")


def register_batch_subcommands(sub) -> None:
    """Register BOTH batch subcommands in one call, and dispatch them through
    dispatch_batch_command below. One registrar and one dispatch line rather
    than a pair per subcommand: bin/sable-merge-gate sits one line under
    test_cli_is_thin's 150-line budget (SABLE-ehx8u), so a second subcommand
    could not be added the obvious way without breaching a guard that exists
    to stop exactly this file from regrowing its logic."""
    register_land_batch(sub)
    register_bisect_batch(sub)


def dispatch_batch_command(args) -> int:
    """Dispatch either batch subcommand from the parsed args. Both take the
    identical flag set, so this is a two-way branch, not a table."""
    if args.command == "land-batch":
        return land_batch_from_refs(args.base, args.members, args.repo, args.remote,
                                    args.manager)
    return bisect_batch_from_refs(args.base, args.members, args.repo, args.remote,
                                  args.manager)
