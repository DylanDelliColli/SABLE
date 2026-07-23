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


def run_coverage_floor_check(repo: str, base_sha: str, branch_sha: str):
    """Actually run the coverage-delta check — .github/ci/diff-cover-gate.sh,
    real pytest + coverage.py + diff-cover, no mocks — against the branch's
    checked-out tree, comparing to base_sha. Same pattern as
    _run_impact_tier_locked: a throwaway detached worktree, because the thing
    being measured is the code AS IT WILL EXIST on the branch, not a diff of
    trees.

    Returns True (patch coverage cleared --fail-under), False (diff-cover ran
    and failed it), or None (the branch does not carry the script at all, the
    worktree could not be built, or the check timed out) — None is a FAIL-
    CLOSED read, same as assert_not_frozen's unreadable-freeze-file contract:
    "we could not prove it's covered" denies, exactly like "we proved it's
    not"."""
    parent = tempfile.mkdtemp(prefix="sable-coverage-floor-")
    worktree = str(Path(parent) / "tree")
    try:
        add = git_lib._git(repo, "worktree", "add", "--detach", worktree, branch_sha, check=False)
        if add.returncode != 0:
            return None
        try:
            script = Path(worktree) / ".github" / "ci" / "diff-cover-gate.sh"
            if not script.is_file():
                return None
            cp = git_lib._run(["bash", str(script), base_sha], cwd=worktree, check=False,
                              timeout=_coverage_floor_timeout(repo))
            return cp.returncode == 0
        finally:
            git_lib._git(repo, "worktree", "remove", "--force", worktree, check=False)
            git_lib._git(repo, "worktree", "prune", check=False)
    except subprocess.TimeoutExpired:
        return None
    except OSError:
        return None
    finally:
        shutil.rmtree(parent, ignore_errors=True)


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

    passed = None
    if signal.is_pruning and not override_reason:
        passed = run_coverage_floor_check(repo, base_sha, branch_sha)

    decision = coverage_floor_lib.evaluate_coverage_floor(signal, passed, override_reason)

    if decision.action == coverage_floor_lib.ACTION_DENY:
        raise GateError(
            classify.EXIT_COVERAGE_FLOOR,
            f"COVERAGE FLOOR (SABLE-cmar4.5): {decision.reason} — bead {bead}, "
            f"branch {branch_sha[:7]} onto {base_sha[:7]}. Not promoted.")

    if signal.is_pruning:
        _append_evidence(repo, bead, f"coverage-floor: {decision.reason}.")


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
    persisted (already-canonicalized) record rather than re-deriving one."""
    base_sha: str
    setkey: str
    combined_ref: str
    outcome: str
    fold_disjoint: bool
    members: tuple[BatchMember, ...]

    @classmethod
    def from_members(cls, base_sha: str, members: list[BatchMember], *,
                     combined_ref: str, outcome: str, fold_disjoint: bool) -> "BatchRecord":
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
                   outcome=outcome, fold_disjoint=fold_disjoint, members=canonical)

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
            with_pair: frozenset[str] = frozenset()) -> int:
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
