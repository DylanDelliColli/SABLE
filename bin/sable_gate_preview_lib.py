#!/usr/bin/env python3
"""sable_gate_preview_lib — build previews, read their verdicts (SABLE-jd5fj.3).

The PREVIEW half of the merge gate: everything that constructs a merge-preview
commit, pushes its ci-verify ref (the CI trigger), discovers what Actions
concluded about it, and reaps the throwaway refs afterwards.

MODULE BOUNDARY, enforced by bin/test_merge_gate_modules.py: nothing here
promotes. This module never pushes to an integration branch, never asserts the
byte-identical fast-forward, never writes bead evidence, never notifies a
manager, and never reaps a worker's branches. It answers exactly one question —
"what does CI say about this merge?" — and hands the answer to the promote
module as a classify.Verdict. That is the seam the split exists to create: a
verdict is a VALUE that can be computed at push time and consumed later, rather
than a side effect only reachable by running promote end to end.

PARALLEL PREVIEWS (the jd5fj.3 property). N previews on N distinct
ci-verify/<bead>-<sha7> refs run CONCURRENTLY. ci-verify.yml's concurrency group
is `ci-verify-${{ github.ref }}` — keyed on the ref, so distinct refs are
distinct groups and cancel-in-progress never reaches across them. The ref naming
in classify.preview_ref_name / preview_kick_ref is therefore load-bearing for
concurrency, not just for collision-avoidance, and
hooks/test/test-parallel-previews.sh pins it as a regression case.

CEILING: GitHub Actions allows 20 concurrent jobs per account on the free tier.
Previews past that queue rather than fail, so exceeding it costs latency, not
correctness — but a fleet wider than ~20 simultaneous in-flight previews is
running against a queue, and the merge-latency telemetry (SABLE-jd5fj.7) is
where that would show up.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass

import sable_batch_key_lib as batch_key
import sable_gate_classify_lib as classify
import sable_gate_git_lib as git_lib
from sable_gate_classify_lib import GateError


# --------------------------------------------------------------------------
# Preview construction
# --------------------------------------------------------------------------

def build_preview(repo: str, base_sha: str, branch_sha: str, message: str) -> str:
    """Build the merge-preview commit with NO working tree. Conflict -> exit 22."""
    mt = git_lib._git(repo, "merge-tree", "--write-tree", base_sha, branch_sha, check=False)
    if mt.returncode == 1:
        raise GateError(22, f"merge-preview conflict between base and branch:\n{mt.stdout.strip()}")
    if mt.returncode != 0:
        raise GateError(3, f"merge-tree failed: {mt.stdout.strip()}")
    tree = mt.stdout.splitlines()[0].strip()
    parent1, parent2 = batch_key.pair_parents(base_sha, branch_sha)
    ct = git_lib._git(repo, "commit-tree", tree, "-p", parent1, "-p", parent2, "-m", message)
    return ct.stdout.strip()


def adopt_kicked_preview(repo: str, remote: str, branch: str,
                         base_sha: str, branch_sha: str) -> tuple[str, str] | None:
    """(preview_sha, ref) of an ALREADY-KICKED preview for exactly this
    (base, branch) pair, or None to build a fresh one (SABLE-jd5fj.1).

    Adoption is what makes the push-time kick pay off: the CI run it started is
    the run promote then waits on, instead of a second run on a second ref. It
    is a strict optimization and FAILS SAFE in every direction — the kicked
    commit is adopted ONLY if it is fetchable and its parents are exactly the
    base/branch SHAs promote just resolved. Any absence, error, or drift (the
    base moved after the kick) returns None and the caller builds the preview
    the pre-kick way, so no outcome and no exit code depends on a kick having
    happened."""
    try:
        ref = classify.preview_kick_ref(branch, base_sha, branch_sha)
        remote_sha = git_lib.remote_ref_commit(repo, remote, ref)
        if not remote_sha:
            return None
        # Fetch the object itself — the kick ran in another process (and often
        # another worktree), so this repo may not have the commit locally.
        if git_lib._git(repo, "fetch", remote, f"refs/heads/{ref}", check=False).returncode != 0:
            return None
        if git_lib.commit_parents(repo, remote_sha) != batch_key.pair_parents(base_sha, branch_sha):
            return None
        return (remote_sha, ref)
    except Exception:  # noqa: BLE001 — adoption must never break the promote flow
        return None


@dataclass(frozen=True)
class StalePreview:
    """A completed-GREEN preview of the CURRENT branch tip onto an OLDER base."""
    preview_sha: str
    ref: str
    base_sha: str        # the base it was built on — NOT the current one
    run_url: str = ""


def _stale_candidates(repo: str, remote: str, branch: str,
                      base_sha: str, branch_sha: str) -> list[StalePreview]:
    """Kicked previews of THIS branch tip onto a base the integration branch has
    since advanced PAST. Verified by parents, never by ref name: the kick ref is
    keyed on a hash of the parent pair, so the name says nothing about which base
    a preview was built on and only the commit itself can be believed."""
    listing = git_lib._git(repo, "ls-remote", "--heads", remote,
                           f"refs/heads/{classify.preview_ref_prefix(branch)}*", check=False)
    if listing.returncode != 0:
        return []
    found: list[StalePreview] = []
    for line in listing.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2 or not parts[1].strip().startswith("refs/heads/"):
            continue
        sha, ref = parts[0].strip(), parts[1].strip()[len("refs/heads/"):]
        if git_lib._git(repo, "fetch", remote, f"refs/heads/{ref}", check=False).returncode != 0:
            continue
        parents = git_lib.commit_parents(repo, sha)
        if len(parents) != 2 or parents[1] != branch_sha:
            # Either not a merge preview at all, or a preview of a branch tip
            # that has since been re-pushed. A verdict about an object no longer
            # on the branch is not evidence about this promote.
            continue
        if parents[0] == base_sha:
            continue        # not stale — that is adopt_kicked_preview's case
        # The base must have moved FORWARD from this preview's base. Anything
        # else (a reset, a force-push, an unrelated history) is not the
        # queued-branch case this exists for, and gets the pre-kzi1a treatment.
        if git_lib._git(repo, "merge-base", "--is-ancestor", parents[0], base_sha,
                        check=False).returncode != 0:
            continue
        found.append(StalePreview(sha, ref, parents[0]))
    return found


def find_stale_green_preview(repo: str, remote: str, branch: str,
                             base_sha: str, branch_sha: str) -> StalePreview | None:
    """The completed-GREEN preview a QUEUED branch already paid for, built on a
    base that has since moved (SABLE-kzi1a). None when there is none, or when
    ordinary adoption already has an answer.

    THE PROBLEM THIS READS FOR. adopt_kicked_preview asks "is there a preview for
    this EXACT (base, branch) pair?", and under a serial merge lane the answer is
    no for every branch after the first: each merge advances the base and
    invalidates the push-time preview of everything still queued behind it. The
    green run those previews already completed was then discarded and a fresh one
    started — so the push-time kick's payoff INVERTED exactly under the burst it
    was built for (measured: 0 optimistic promotions in 157, and 0 across a
    15-worker burst that queued 11 branches).

    Only a COMPLETED GREEN counts. A red preview says nothing about the new base;
    a PENDING one is a wait, and paying a wait on a stale object is the opposite
    of the point. Reads only — whether a stale green may be promoted is the
    promote module's question, and the answer there still runs the impact tier on
    the real combined tree.

    A strict optimization with the same fail-safe contract as
    adopt_kicked_preview: every absence, error, or drift returns None and the
    caller does what it did before, so no outcome and no exit code depends on a
    queued preview having been found."""
    try:
        if adopt_kicked_preview(repo, remote, branch, base_sha, branch_sha) is not None:
            return None
        greens = []
        for cand in _stale_candidates(repo, remote, branch, base_sha, branch_sha):
            verdict = read_verdict(repo, cand.ref, cand.preview_sha)
            if verdict.complete and verdict.outcome == classify.GREEN:
                greens.append(StalePreview(cand.preview_sha, cand.ref, cand.base_sha,
                                           verdict.run_url))
        if not greens:
            return None
        # Prefer the preview built on the LATEST base: its base-move footprint is
        # the smallest of the candidates', and a narrower footprint is the one
        # more likely to be provably disjoint. Ancestry, not ref order — the
        # listing is alphabetical by a hash and says nothing about time.
        best = greens[0]
        for cand in greens[1:]:
            if git_lib._git(repo, "merge-base", "--is-ancestor", best.base_sha,
                            cand.base_sha, check=False).returncode == 0:
                best = cand
        return best
    except Exception:  # noqa: BLE001 — discovery must never break the promote flow
        return None


def materialize_preview(repo: str, remote: str, branch: str, base: str,
                        base_sha: str, branch_sha: str, bead: str) -> tuple[str, str, bool]:
    """The commit promote will verify and (if green) promote byte-identical:
    (preview_sha, ref, adopted).

    Adopt a push-time kick for this exact (base, branch) pair if one exists;
    otherwise build the preview and push its ci-verify ref exactly as the
    pre-split flow did. When adopted, the ref is ALREADY on the remote at this
    SHA, so no push is made — re-pushing the same object is a network round-trip
    that changes nothing, and skipping it is part of what makes an adopted
    promote a seconds-long read instead of a construct-and-wait.

    Raises GateError(22) on conflict, from build_preview — unchanged, and still
    before any ref is pushed."""
    adopted = adopt_kicked_preview(repo, remote, branch, base_sha, branch_sha)
    if adopted is not None:
        preview_sha, ref = adopted
        print(f"sable-merge-gate: adopting push-time preview {ref} ({preview_sha[:7]}) "
              f"— its ci-verify run is already underway")
        return (preview_sha, ref, True)
    msg = f"ci-verify merge-preview: {branch} onto {base} ({bead})"
    preview_sha = build_preview(repo, base_sha, branch_sha, msg)
    ref = classify.preview_ref_name(bead, preview_sha)
    # Push by EXPLICIT resolved path, never the CWD-sensitive remote NAME, so a
    # working-dir escape can't redirect this ci-verify ref to the real upstream
    # (SABLE-ck05). Fetch above stays name-based — it only reads.
    git_lib._git(repo, "push", git_lib.resolve_remote_url(repo, remote),
                 f"{preview_sha}:refs/heads/{ref}")
    return (preview_sha, ref, False)


def kick_preview(branch: str, base: str, repo: str, remote: str) -> int:
    """Build the merge-preview for <branch> onto <base> and push its ci-verify
    ref — then RETURN, without waiting for CI (SABLE-jd5fj.1).

    This is the push-time half of the gate. The ref push is the CI trigger
    (.github/workflows/ci-verify.yml fires on ci-verify/**), so returning here
    leaves a run computing in the background; promote picks that run up later via
    adopt_kicked_preview. Deliberately does NOT poll, notify, or write bead
    evidence — a kick is a speculative warm-up with no verdict to report, and it
    runs in a hook's shadow where those side effects would be unattributable.

    Idempotent by the shared key: a ref that already exists for this exact
    (base, branch) pair means the merge is already building or built, so the kick
    is a no-op instead of a re-push (which would re-trigger CI and, under the
    workflow's cancel-in-progress concurrency group, CANCEL the run already
    underway — the SABLE-sc24 spurious-cancel shape).

    Exit codes: 0 kicked or already-kicked, 22 merge conflict (nothing to
    verify — the author must resolve it), 3 precondition failed."""
    base_ref = classify.qualify_remote_ref(remote, base)
    branch_ref = classify.qualify_remote_ref(remote, branch)
    git_lib._git(repo, "fetch", remote, base, branch)
    base_sha = git_lib.resolve_commit(repo, base_ref)
    branch_sha = git_lib.resolve_commit(repo, branch_ref)

    ref = classify.preview_kick_ref(branch, base_sha, branch_sha)
    existing = git_lib.remote_ref_commit(repo, remote, ref)
    if existing:
        print(f"sable-merge-gate preview: {ref} already exists ({existing[:7]}) — "
              f"CI is already running for {branch} onto {base}; not re-pushing")
        return 0

    msg = f"ci-verify merge-preview: {branch} onto {base} (push-time kick)"
    preview_sha = build_preview(repo, base_sha, branch_sha, msg)
    # Push by EXPLICIT resolved path, never the CWD-sensitive remote NAME
    # (SABLE-ck05): this is the leg the post-push hook fires, and it ran under a
    # CWD escape that redirected every name-based kick to the operator's real
    # upstream. Fetch above stays name-based — it only reads.
    git_lib._git(repo, "push", git_lib.resolve_remote_url(repo, remote),
                 f"{preview_sha}:refs/heads/{ref}")
    print(f"sable-merge-gate preview: kicked {ref} (preview {preview_sha}) for {branch} "
          f"onto {base} — NOT waiting for CI")
    return 0


# --------------------------------------------------------------------------
# Actions polling / verdict reading
# --------------------------------------------------------------------------

def _gh_runs(repo: str, ref_branch: str, fields: str) -> list | None:
    """`gh run list` for one ref, decoded — or None on ANY failure (non-zero gh,
    unparseable JSON, or a hung call). One place for the SABLE-7wyl bound: a dead
    Actions API can make the gh subprocess HANG rather than error fast, and an
    unbounded call blocks the caller forever regardless of the wall-clock timeout
    math around it. Callers distinguish None (no answer) from [] (an answer: no
    runs) — conflating those is how a non-answer becomes a false verdict."""
    gh = git_lib._tool("SABLE_MG_GH", "gh")
    gh_timeout = float(os.environ.get("SABLE_MG_GH_TIMEOUT", "30"))
    try:
        cp = git_lib._run(gh + ["run", "list", "--branch", ref_branch, "--limit", "10",
                                "--json", fields],
                          cwd=repo, check=False, timeout=gh_timeout)
    except (subprocess.TimeoutExpired, OSError):
        # OSError covers `gh` not being installed at all. That is a NON-ANSWER,
        # not a verdict — the same bucket as a hang or a 503 — so it converges on
        # actions_down/BLOCKED rather than being mistaken for "no runs exist".
        return None
    if cp.returncode != 0:
        return None
    try:
        return json.loads(cp.stdout or "[]")
    except json.JSONDecodeError:
        return None


def read_verdict(repo: str, ref_branch: str, preview_sha: str) -> classify.Verdict:
    """NON-BLOCKING peek at the verdict already stored on GitHub for this exact
    preview SHA (SABLE-jd5fj.3). Exactly one API call; never sleeps, never
    retries, never waits for a run to finish.

    This is the read half of "Chuck reads precomputed verdicts". The push-time
    kick (jd5fj.1) starts the run at push; by the time Chuck sequences merges,
    the run has usually long since completed, and the whole cost of knowing its
    outcome is this one call. Returns a Verdict with complete=False when no
    COMPLETED run for this SHA is visible yet — pending, or unreadable — which is
    the caller's cue to wait (promote) or report 'pending' (the verdict CLI).
    A non-answer is never turned into a conclusion here. Non-answers (gh
    missing/erroring/timing out inside _gh_runs) also carry answered=False, so
    report_verdict — which has no wait loop to converge through — can print a
    distinct 'unknown' instead of a 'pending' indistinguishable from a
    genuinely in-flight run (SABLE-fewih)."""
    runs = _gh_runs(repo, ref_branch, "databaseId,headSha,status,conclusion,url")
    if runs is None:
        return classify.Verdict("pending", "", preview_sha, ref_branch,
                                source="precomputed", complete=False, answered=False)
    for r in runs:
        if r.get("headSha") != preview_sha:
            continue
        if r.get("status") != "completed":
            return classify.Verdict("pending", r.get("url") or "", preview_sha, ref_branch,
                                    source="precomputed", complete=False)
        return classify.Verdict(r.get("conclusion") or "unknown", r.get("url") or "",
                                preview_sha, ref_branch, source="precomputed", complete=True)
    return classify.Verdict("pending", "", preview_sha, ref_branch,
                            source="precomputed", complete=False)


def wait_for_ci(repo: str, ref_branch: str, preview_sha: str) -> tuple[str, str]:
    """Poll the Actions run keyed to preview_sha on ref_branch until it completes.
    Returns (conclusion, run_url). conclusion == 'actions_down' if no run appears
    within SABLE_MG_GRACE. Blocks up to SABLE_MG_TIMEOUT for completion.

    SABLE-7wyl: the 2026-07-16 sustained-503 outage stalled the gate mid-verify
    despite SABLE_MG_TIMEOUT/GRACE already existing, because those are wall-clock
    bookkeeping around each `gh run list` call — they only get to fire if the call
    itself returns. A dead Actions API can make the `gh` subprocess hang (not just
    error fast), and a hung call blocks this loop forever regardless of the
    timeout math. SABLE_MG_GH_TIMEOUT bounds each individual poll call; a hang is
    treated exactly like a fast API error (run stays undiscovered this iteration)
    so the existing grace/timeout logic still converges to a clean actions_down/
    timeout park instead of an unbounded hang needing a manual kill+requeue."""
    poll = float(os.environ.get("SABLE_MG_POLL", "20"))
    timeout = float(os.environ.get("SABLE_MG_TIMEOUT") or git_lib.default_mg_timeout(repo))
    grace = float(os.environ.get("SABLE_MG_GRACE", "300"))
    gh_timeout = float(os.environ.get("SABLE_MG_GH_TIMEOUT", "30"))
    waited = 0.0
    seen_run = False
    while True:
        runs = _gh_runs(repo, ref_branch, "databaseId,headSha,status,conclusion,url")
        run = None
        if runs is not None:
            for r in runs:
                if r.get("headSha") == preview_sha:
                    run = r
                    break
        if run is not None:
            seen_run = True
            if run.get("status") == "completed":
                return (run.get("conclusion") or "unknown", run.get("url") or "")
        elif not seen_run and waited >= grace:
            return ("actions_down", "")
        if waited >= timeout:
            return ("actions_down" if not seen_run else "timeout", "")
        if runs is None:
            waited += gh_timeout
        else:
            time.sleep(poll)
            waited += poll


def acquire_verdict(repo: str, ref: str, preview_sha: str) -> classify.Verdict:
    """The verdict promote acts on — READ FIRST, wait only if it has to.

    This is the jd5fj.3 inversion. Before the split, promote's only way to learn
    an outcome was wait_for_ci: a polling loop that sleeps SABLE_MG_POLL between
    calls and is written to tolerate a run that has not started yet. But by the
    time Chuck merges, the push-time kick has usually had minutes-to-hours of
    head start and the run is DONE — so the loop's first call already had the
    answer, and everything else it is built to handle was dead weight on the
    merge path.

    So: one non-blocking read_verdict first. If a completed run for this exact
    preview SHA exists, promote consumes it and returns in seconds
    (source='precomputed'). Only when no verdict is stored yet does this fall
    through to wait_for_ci (source='waited') — the pre-split path, byte for byte,
    with the same grace/timeout/actions_down semantics. Neither branch can invent
    a conclusion the other would not have produced: read_verdict reports only
    COMPLETED runs matching this SHA, which is exactly wait_for_ci's own
    completion condition."""
    stored = read_verdict(repo, ref, preview_sha)
    if stored.complete:
        print(f"sable-merge-gate: consuming precomputed verdict for {ref} "
              f"({preview_sha[:7]}): {stored.conclusion} — no CI wait")
        return stored
    conclusion, url = wait_for_ci(repo, ref, preview_sha)
    return classify.Verdict(conclusion, url, preview_sha, ref, source="waited", complete=True)


def ref_has_inflight_run(repo: str, ref_branch: str) -> bool:
    """True iff Actions shows a not-yet-completed run on ref_branch. Deleting a
    ci-verify ref cancels its in-progress GitHub run — the SABLE-sc24 root cause —
    so `sweep` must never reap a ref whose run is still live, no matter how old the
    ref's commit date is. A run is live unless its status is 'completed'
    (queued/in_progress/waiting/requested all count as in-flight).

    Best-effort and FAIL-OPEN: a gh error, unparseable output, an absent run, or
    a hung gh call (SABLE-7wyl — same unbounded-subprocess class as wait_for_ci)
    all return False. An undiscoverable run cannot be protected, and the ref is
    only a sweep candidate because it is already past the age threshold — so a
    non-answer must not wedge the orphan cleanup permanently."""
    runs = _gh_runs(repo, ref_branch, "status")
    if runs is None:
        return False
    return any((r.get("status") or "completed") != "completed" for r in runs)


# --------------------------------------------------------------------------
# Throwaway-ref lifecycle
# --------------------------------------------------------------------------

def delete_ci_ref(repo: str, remote: str, ref: str) -> None:
    """Delete a throwaway ci-verify ref. Best-effort by contract: the ref may
    already be gone (a concurrent sweep, a prior attempt), and failing to clean
    up must never change a promote outcome."""
    # Resolve to an explicit path first (SABLE-ck05): a name-based `push --delete`
    # under a CWD escape would delete the ref on the wrong upstream. Best-effort
    # unchanged.
    git_lib._git(repo, "push", git_lib.resolve_remote_url(repo, remote),
                 "--delete", ref, check=False)


def sweep(repo: str, remote: str, max_age_hours: float, dry_run: bool = False) -> int:
    """Delete orphaned ci-verify/* refs older than the threshold — or, with
    dry_run, report exactly what WOULD be deleted and delete nothing
    (SABLE-o9b8u).

    THE PROBLEM THIS EXISTS FOR: the sweep's safety is a function of how
    recently it last ran, and the command has no memory of that. The
    identical --max-age-hours default deletes a handful of refs when run
    daily and hundreds when run after a gap, and an operator typing the
    command cannot tell which situation they are in before it acts on a
    SHARED remote. dry_run is the look-before-you-leap: it walks the exact
    same candidate list the real run would delete (same age filter, same
    in-flight-run exclusion) and prints it instead of deleting.

    Scale-proportionate reporting: the candidate COUNT and age span are
    always printed before any deletion is attempted (dry or real) — a
    3-ref sweep and a 232-ref sweep should not look like the same amount of
    ceremony on the way past."""
    git_lib._git(repo, "fetch", remote, "--prune")
    listing = git_lib._git(repo, "for-each-ref", "--format=%(refname:short) %(committerdate:unix)",
                           f"refs/remotes/{remote}/ci-verify/", check=False)
    # Resolve once: the fetch/for-each-ref above are name-based reads, but every
    # push --delete below must address an EXPLICIT path so a CWD escape can't reap
    # refs on the wrong upstream (SABLE-ck05).
    push_remote = git_lib.resolve_remote_url(repo, remote)
    now = time.time()
    candidates: list[tuple[str, float]] = []  # (branch, age_seconds) — what WOULD be deleted
    spared = 0
    for line in listing.stdout.splitlines():
        parts = line.rsplit(" ", 1)
        if len(parts) != 2:
            continue
        short, ts = parts
        try:
            age = now - float(ts)
        except ValueError:
            continue
        if not classify.is_orphan(age, max_age_hours):
            continue
        branch = short.split("/", 1)[1] if "/" in short else short  # strip remote/ prefix
        # SABLE-sc24: age alone does not make a ref an orphan. A ref whose Actions
        # run is still in-flight is LIVE — deleting it cancels the run and REDs the
        # gate spuriously. Never reap it, regardless of the ref's commit date. This
        # applies identically in dry_run: a dry run that lists a ref the real run
        # would spare is not a faithful preview of the real run.
        if ref_has_inflight_run(repo, branch):
            print(f"sable-merge-gate sweep: {branch} is past the {max_age_hours}h age threshold but its "
                  f"Actions run is still in-flight — NOT reaping (deleting it would cancel the run)",
                  file=sys.stderr)
            spared += 1
            continue
        candidates.append((branch, age))

    spared_note = f" (spared {spared} with an in-flight run)" if spared else ""
    if candidates:
        ages_h = sorted(age / 3600 for _, age in candidates)
        print(f"sable-merge-gate sweep: {len(candidates)} ref(s) match the >{max_age_hours}h age "
              f"threshold (oldest {ages_h[-1]:.1f}h, newest {ages_h[0]:.1f}h){spared_note}")
    else:
        print(f"sable-merge-gate sweep: 0 ref(s) match the >{max_age_hours}h age threshold{spared_note}")

    if dry_run:
        for branch, age in candidates:
            print(f"sable-merge-gate sweep --dry-run: would delete {branch} (age {age / 3600:.1f}h)")
        print(f"sable-merge-gate sweep --dry-run: {len(candidates)} ref(s) would be deleted — "
              f"nothing deleted (dry run)")
        return 0

    deleted = 0
    for branch, _age in candidates:
        git_lib._git(repo, "push", push_remote, "--delete", branch, check=False)
        deleted += 1
    print(f"sable-merge-gate sweep: deleted {deleted} orphaned ci-verify ref(s) older than {max_age_hours}h{spared_note}")
    return 0


# --------------------------------------------------------------------------
# Verdict CLI leg (chuck's read-verdict step)
# --------------------------------------------------------------------------

def report_verdict(branch: str, base: str, repo: str, remote: str, as_json: bool) -> int:
    """`sable-merge-gate verdict --branch <b>` — what does CI ALREADY say about
    merging <branch> onto <base>? Reads; never builds, pushes, waits, or
    promotes.

    This is the first step of Chuck's new flow (read-verdict -> sequence ->
    promote): with N workers' previews kicked at push time and running
    concurrently, Chuck can read N verdicts in N cheap calls, then sequence the
    GREEN ones into the serialized promote lane and leave the rest alone — rather
    than discovering each outcome only by starting a promote and blocking on it.

    States: green / red / retry (cancelled) / pending (running or not started) /
    unknown (gh could not be asked at all — missing, erroring, or timing out;
    NOT the same as pending, which means gh answered and the run is genuinely
    in flight — SABLE-fewih) / none (nothing kicked for this exact base+branch
    pair). Always exits 0 when it can answer at all — a verdict read is an
    observation, not a gate, and callers branch on the printed state. Exit 3
    only if base/branch cannot be resolved."""
    base_ref = classify.qualify_remote_ref(remote, base)
    branch_ref = classify.qualify_remote_ref(remote, branch)
    git_lib._git(repo, "fetch", remote, base, branch, check=False)
    base_sha = git_lib.resolve_commit(repo, base_ref)
    branch_sha = git_lib.resolve_commit(repo, branch_ref)

    ref = classify.preview_kick_ref(branch, base_sha, branch_sha)
    kicked = git_lib.remote_ref_commit(repo, remote, ref)
    if not kicked:
        state, verdict = "none", classify.Verdict("none", "", "", ref, complete=False)
    else:
        verdict = read_verdict(repo, ref, kicked)
        if verdict.complete:
            state = verdict.outcome
        elif not verdict.answered:
            state = "unknown"
        else:
            state = "pending"

    if as_json:
        print(json.dumps({
            "branch": branch, "base": base, "ref": ref, "state": state,
            "preview_sha": verdict.preview_sha, "conclusion": verdict.conclusion,
            "run_url": verdict.run_url,
            "promotable": state == classify.GREEN,
        }))
    else:
        print(f"verdict {state} branch={branch} base={base} ref={ref} "
              f"preview={verdict.preview_sha[:7] or 'n/a'} run={verdict.run_url or 'n/a'}")
    return 0
