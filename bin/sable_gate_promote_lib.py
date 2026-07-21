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

import os
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


def run_impact_tier(repo: str, tree_sha: str, paths: list[str]) -> tuple[str, str]:
    """Run the cmar4 impact tier against the REAL COMBINED TREE and report
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
                warm = Path(repo) / ".testmondata"
                if warm.is_file():
                    shutil.copy2(warm, Path(worktree) / ".testmondata")
                cp = git_lib._run([sys.executable, str(selector)], cwd=worktree, check=False,
                                  timeout=_impact_timeout())
                ran.append("bin/ pytest impact tier")
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

def _stale_base(bead: str, branch: str, base: str, repo: str, remote: str,
                manager: str, base_ref: str, ref: str, preview_sha: str,
                base_sha: str, branch_sha: str, current_base: str) -> int:
    """The base moved out from under a GREEN preview. Decide what that costs.

    Reached from two places that mean the same thing: the pre-push check (the
    common case — the base moved during the CI wait, and we notice before
    pushing) and the non-fast-forward push rejection (the narrow race where it
    moved between that check and the push). Both funnel here so there is ONE
    stale-base decision path to reason about, which is what makes invariant I1
    checkable at all.

    Returns an exit code, or raises GateError(23) for the full-re-preview
    outcome — the pre-jd5fj.4 contract, byte for byte, including the notify and
    the evidence line."""
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


def promote(bead: str, branch: str, base: str, repo: str, remote: str,
            manager: str, override: str | None) -> int:
    # FIRST, before any git work: is the fleet frozen? (SABLE-jd5fj.5)
    assert_not_frozen(repo)
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
