#!/usr/bin/env python3
"""sable_merge_report_lib — derive-at-read merge telemetry (SABLE-jd5fj.7,
corrected by SABLE-jd5fj.11).

Produces the per-phase comparison the jd5fj S6 test-strategy story calls for:
median/p90 push-to-LANDED latency (PRIMARY, SABLE-jd5fj.11) with push-to-
CI-done retained as a labelled secondary, textual conflict / red rate, a
repeat-preview proxy, discarded-preview cost and queue depth at promote
(SABLE-kzi1a's confound), and — the flagship metric — a DIRECT measurement of
the textually-clean-but-semantically-broken disjoint-promotion rate (the
class SABLE-nueh3's baseline could only bound at 0/126, rule-of-three
<=2.4%, because the pre-S2 design structurally prevented it from occurring
at all).

Consistent with the SABLE-8b41 telemetry direction: this is a derive-at-read
reporting tool, not a new capture mechanism (relate, do not block). Three
adapters, same split as sable-merge-gate's own git/gh/bd seams:

  git   -- `git log <base>` on THIS repo. Promotion count and, critically,
            which promotions landed via OPTIMISTIC DISJOINT PROMOTION
            (SABLE-jd5fj.4): the combined-tree commit built in
            sable_gate_promote_lib._stale_base carries the message suffix
            ", disjoint re-verify)" verbatim, so the marker is a fact about
            the landed commit object itself -- no bd/gh call needed to find it.
  gh    -- `gh run list --workflow=ci-verify`. Preview-run conclusions (red
            rate) and the same cross-reference nueh3 used: did a LATER run on
            the base branch, same head SHA, come back red? That is exactly
            what "impact-tier or snapshot run reds" means for a promotion
            that already landed -- the impact tier itself gates the promote,
            so a disjoint promotion that landed was impact-green at promote
            time; the only way to observe the class directly, post-landing,
            is a subsequent run against that same landed object going red.
            The SAME base-branch run list also supplies the PRIMARY bar's
            anchor (SABLE-jd5fj.11): the EARLIEST run recorded against a
            landed sha is a real, independently timestamped observation that
            the object exists on the base branch -- see
            promotion_landed_epochs.
  log   -- ~/.claude/sable/logs/post-push-merge-notify.log. Real push
            timestamps (CONFIRMED lines). The SECONDARY push-to-CI-done join
            uses TIME PROXIMITY (nueh3's doc does not define a mechanical
            join key for its manual cross-reference, so this module names
            its own: nearest preview run created within max_gap_seconds
            after a CONFIRMED push). The PRIMARY push-to-landed join instead
            matches on the exact branch name (compute_landed_metrics) --
            both the promotion commit subject and the CONFIRMED log line
            carry the real branch name, so no proximity heuristic is needed
            there.

CORRECTNESS NOTE carried forward from SABLE-jxgm4 (found reviewing nueh3),
and the reason SABLE-jd5fj.11 exists: git commit-tree stamps a merge
commit's author AND committer dates at PREVIEW-BUILD time, not at land time
-- the whole CI wait (the dominant term) is invisible to any latency
computed from commit dates alone, and for a REBUILT preview (SABLE-kzi1a: an
earlier merge invalidated the push-time preview, so promote discards it and
builds a fresh one) even the rebuilt commit's own dates only cover the LAST
wait, not the discarded one before it. This module therefore NEVER uses
commit dates as a push-to-landed latency proxy for either metric; git is
used only to detect and count promotions (including which are disjoint).
push-to-CI-done comes from the log x gh (preview runs) join; push-to-landed
comes from the log x git (promotions) x gh (base-branch runs) join.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import sable_gate_classify_lib as classify  # noqa: E402

RED = classify.RED
GREEN = classify.GREEN
RETRY = classify.RETRY
BLOCKED = classify.BLOCKED
classify_conclusion = classify.classify_conclusion

DISJOINT_SUFFIX = ", disjoint re-verify"  # `detail` excludes the subject's closing paren
PROMOTION_SUBJECT_RE = re.compile(
    r"^ci-verify merge-preview: (?P<branch>.+) onto (?P<base>.+) \((?P<detail>.+)\)$"
)
CONFIRMED_LOG_RE = re.compile(
    r"^(?P<ts>\S+) .*? branch=(?P<branch>\S+) \| CONFIRMED local=(?P<local>[0-9a-f]+) "
    r"remote=(?P<remote>[0-9a-f]+)"
)

# --------------------------------------------------------------------------
# SABLE-nueh3 baseline reference (docs/MERGE-LATENCY-BASELINE.md).
# Source of truth is the doc; this is a citation, not a recomputation. Only
# the PRECISE, same-session column is used as the comparison basis -- the
# coarse commit-date proxy is the exact bias SABLE-jxgm4 found, and the
# full-window precise column mixes in a 15h shift-boundary gap the doc itself
# says is a queue-depth artifact, not the gate's own contribution.
# --------------------------------------------------------------------------
NUEH3_BASELINE = {
    "source": "docs/MERGE-LATENCY-BASELINE.md (SABLE-nueh3)",
    "push_to_ci_done": {
        "n": 41,
        "median": 1513.0,
        "p90": 3841.0,
        "note": "precise push->CI-done, same-session subset (excludes one 15h "
                "overnight shift-boundary gap the doc attributes to queue depth, "
                "not gate latency)",
    },
    "red_rate": {
        # Textual-conflict (exit-22) rate was UNMEASURABLE from this repo's
        # history (no durable trail before SABLE-lxvl2); the doc uses
        # published external base rates as reference only. There is no
        # in-repo red-rate baseline number to compare against directly, so
        # the comparison uses the CI preview run failure rate instead, which
        # the doc reports as part of its own gh-run cross-reference (131
        # successful preview runs observed; failure-rate baseline not stated
        # as a headline number in the doc -- treated as unknown/0 lower bound
        # here rather than fabricated).
        "n": None,
        "rate": None,
        "note": "no headline red-rate figure in the nueh3 doc; report N/A rather than invent one",
    },
    "semantic_break": {
        "n": 126,
        "breaks": 0,
        "rule_of_three_bound": 3.0 / 126,
        "note": "0/126 observed under the PRE-S2 design, which structurally "
                "prevented the class (byte-identical promotion, no re-merge "
                "after green) -- not evidence the post-S2 rate is low "
                "(sable_gate_promote_lib.py module docstring, SABLE-nueh3).",
    },
}


# --------------------------------------------------------------------------
# Pure statistics
# --------------------------------------------------------------------------

def percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolation percentile (the method the nueh3 baseline used
    for its median/p90/p95 columns). None on an empty input rather than a
    ZeroDivisionError -- an empty sample has no percentile, and callers must
    handle that explicitly instead of a stray exception surfacing as a crash
    in what is meant to be a resilient reporting tool."""
    if not values:
        return None
    if not 0 <= pct <= 100:
        raise ValueError(f"pct must be in [0, 100], got {pct!r}")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    k = (len(ordered) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(ordered) - 1)
    if f == c:
        return float(ordered[f])
    d0 = ordered[f] * (c - k)
    d1 = ordered[c] * (k - f)
    return float(d0 + d1)


def latency_stats(latencies: list[float]) -> dict:
    return {
        "n": len(latencies),
        "median": percentile(latencies, 50),
        "p90": percentile(latencies, 90),
    }


def rule_of_three_bound(n: int) -> float | None:
    """Usable upper bound on a rate observed as 0/n (the same bound nueh3
    applied to its own 0/126: with zero events observed, the rate cannot be
    bounded any tighter than ~3/n at typical confidence from history alone --
    it must be measured directly going forward, which is this tool's job."""
    return 3.0 / n if n > 0 else None


def red_rate(conclusions: list[str]) -> dict:
    """Fraction of CI conclusions that are RED under the gate's own taxonomy
    (sable_gate_classify_lib.classify_conclusion) -- the SAME classification
    promote() uses, so "red" here means exactly what it means at the gate.
    CANCELLED runs are RETRY, never RED (SABLE-sc24) and are excluded from
    the denominator entirely: counting them as either red or green would
    misrepresent a mid-flight cancellation as a content signal."""
    outcomes = [classify_conclusion(c) for c in conclusions]
    countable = [o for o in outcomes if o != RETRY]
    reds = sum(1 for o in countable if o == RED)
    n = len(countable)
    return {"n": n, "red": reds, "rate": (reds / n) if n else None}


@dataclass(frozen=True)
class PromotionCommit:
    sha: str
    committed_at: str  # ISO-8601, informational only -- see module docstring
    subject: str
    branch: str | None
    bead: str | None
    disjoint: bool


def parse_promotion_subject(sha: str, committed_at: str, subject: str) -> PromotionCommit | None:
    """A landed promotion commit, or None if `subject` is not one (this repo's
    tmux-only history also carries ordinary feature commits between merges).
    `disjoint` is True only for the SABLE-jd5fj.4 optimistic-disjoint-promotion
    path -- the ", disjoint re-verify)" suffix sable_gate_promote_lib._stale_base
    stamps on the re-verified combined-tree commit it builds and promotes."""
    m = PROMOTION_SUBJECT_RE.match(subject)
    if not m:
        return None
    detail = m.group("detail")
    disjoint = detail.endswith(DISJOINT_SUFFIX)
    if disjoint:
        bead = detail[: -len(DISJOINT_SUFFIX)] or None
    elif detail == "push-time kick":
        bead = None
    else:
        bead = detail
    return PromotionCommit(sha, committed_at, subject, m.group("branch"), bead, disjoint)


@dataclass(frozen=True)
class BaseRun:
    head_sha: str
    created_at: float  # epoch seconds
    conclusion: str


@dataclass(frozen=True)
class SemanticBreak:
    promotion: PromotionCommit
    run: BaseRun


def count_semantic_breaks(promotions: list[PromotionCommit], base_runs: list[BaseRun],
                          promotion_epoch: dict[str, float], window_seconds: float) -> dict:
    """THE counting rule for the textually-clean-but-semantically-broken rate
    (jd5fj S6 columbo case): a disjoint promotion (a commit that landed via
    SABLE-jd5fj.4's optimistic path) is a BREAK iff some run on the base
    branch, for that EXACT landed SHA, is RED and lands within
    `window_seconds` after the promotion landed.

    Exact-SHA match, not "any run after this point in history": the impact
    tier already gated THIS promotion green at promote time (decide_promotion
    refuses to promote on IMPACT_RED), so the only way this class is
    observable post-landing is a LATER run against the SAME object -- which
    is precisely nueh3's own cross-reference method (preview-green vs.
    later-same-headSha-red), extended here to disjoint promotions
    specifically and evaluated continuously instead of once.

    `promotion_epoch` maps sha -> epoch seconds (the caller resolves this,
    typically from git's committer date, which is stale on THIS SHA
    specifically only in one direction (it is stamped when the combined-tree
    commit is built, seconds before it is pushed) -- an acceptable window
    anchor since window_seconds is on the order of hours, not seconds."""
    disjoint = [p for p in promotions if p.disjoint]
    breaks: list[SemanticBreak] = []
    for p in disjoint:
        landed_at = promotion_epoch.get(p.sha)
        if landed_at is None:
            continue
        for run in base_runs:
            if run.head_sha != p.sha:
                continue
            if classify_conclusion(run.conclusion) != RED:
                continue
            if landed_at <= run.created_at <= landed_at + window_seconds:
                breaks.append(SemanticBreak(p, run))
                break
    n = len(disjoint)
    n_breaks = len(breaks)
    return {
        "disjoint_promotions": n,
        "breaks": n_breaks,
        "rate": (n_breaks / n) if n else None,
        "rule_of_three_bound": rule_of_three_bound(n) if n_breaks == 0 else None,
        "break_details": [
            {"sha": b.promotion.sha, "bead": b.promotion.bead, "run_created_at": b.run.created_at}
            for b in breaks
        ],
    }


@dataclass(frozen=True)
class PushEvent:
    branch: str
    confirmed_at: float  # epoch seconds


@dataclass(frozen=True)
class PreviewRun:
    head_branch: str      # ci-verify/<bead-or-branch>-<sha7>
    created_at: float
    completed_at: float | None
    conclusion: str | None
    head_sha: str | None = None  # the object THIS run tested -- may or may not be what lands


def _match_pushes_to_first_preview(pushes: list[PushEvent], runs: list[PreviewRun],
                                   max_gap_seconds: float) -> list[tuple[PushEvent, PreviewRun]]:
    """The push->CI join's matching rule, factored out so both the (secondary)
    push-to-CI-done latency and the (primary) discarded-preview detection use
    the exact same "nearest completed preview run within max_gap_seconds"
    rule -- one definition of "the preview a push kicked", not two that could
    drift apart. A push is matched to the EARLIEST preview run (head_branch
    starting "ci-verify/") created no more than max_gap_seconds after the
    push's CONFIRMED instant and not yet consumed by an earlier push -- the
    push-time kick fires the preview essentially immediately (SABLE-jd5fj.1),
    so a close-in-time subsequent run is the one it kicked. Runs still
    pending (no completed_at) are skipped -- they contribute no observation
    yet. Unmatched pushes are simply absent from the result."""
    candidates = sorted(
        (r for r in runs if r.head_branch.startswith("ci-verify/") and r.completed_at is not None),
        key=lambda r: r.created_at,
    )
    used = [False] * len(candidates)
    matches: list[tuple[PushEvent, PreviewRun]] = []
    for push in sorted(pushes, key=lambda p: p.confirmed_at):
        best_i, best_gap = None, None
        for i, run in enumerate(candidates):
            if used[i]:
                continue
            gap = run.created_at - push.confirmed_at
            if 0 <= gap <= max_gap_seconds and (best_gap is None or gap < best_gap):
                best_i, best_gap = i, gap
        if best_i is not None:
            used[best_i] = True
            matches.append((push, candidates[best_i]))
    return matches


def join_push_to_ci(pushes: list[PushEvent], runs: list[PreviewRun],
                    max_gap_seconds: float = 120.0) -> list[float]:
    """Push -> CI-done latency in seconds, one per matched push (SECONDARY
    metric, SABLE-jd5fj.11): the push-time preview's own completion, which is
    exactly what makes it blind to a later discarded/rebuilt preview -- see
    join_push_to_landed for the PRIMARY bar. Callers report coverage
    (n matched / n total) rather than pretending completeness."""
    return [run.completed_at - push.confirmed_at
           for push, run in _match_pushes_to_first_preview(pushes, runs, max_gap_seconds)]


def promotion_landed_epochs(promotions: list[PromotionCommit], base_runs: list[BaseRun]) -> dict[str, float]:
    """The real FAST-FORWARD instant for each promotion, in epoch seconds --
    the PRIMARY bar's anchor (SABLE-jd5fj.11). NEVER the promotion commit's
    own author/committer date (SABLE-jxgm4: git commit-tree stamps those at
    PREVIEW-BUILD time, before the CI wait -- and, for a rebuilt preview,
    before any further wait too -- the metric exists to measure). Instead:
    the EARLIEST run `gh run list` records against the base branch for that
    EXACT landed sha -- a real, independently timestamped observation that
    the object exists on the base branch, which can only happen at or after
    the fast-forward that put it there. The same join key
    count_semantic_breaks already uses for a different purpose (a LATER red
    run on a landed sha), reused here for its EARLIEST run instead."""
    epochs: dict[str, float] = {}
    for run in base_runs:
        current = epochs.get(run.head_sha)
        if current is None or run.created_at < current:
            epochs[run.head_sha] = run.created_at
    return {p.sha: epochs[p.sha] for p in promotions if p.sha in epochs}


@dataclass(frozen=True)
class LandedRecord:
    branch: str
    bead: str | None
    sha: str
    push_confirmed_at: float
    landed_at: float
    push_to_landed_seconds: float
    push_to_ci_done_seconds: float | None
    discarded_preview: bool
    queue_depth_at_promote: int


def compute_landed_metrics(pushes: list[PushEvent], promotions: list[PromotionCommit],
                           preview_runs: list[PreviewRun], base_runs: list[BaseRun],
                           max_gap_seconds: float = 120.0) -> list[LandedRecord]:
    """THE primary-bar computation (SABLE-jd5fj.11): one LandedRecord per
    promotion that can be matched to both a push (by exact branch name -- the
    promotion commit subject and the CONFIRMED log line both carry the real
    branch name, so this join needs no time-proximity heuristic) and an
    observed fast-forward instant (promotion_landed_epochs).

    `discarded_preview` is True iff the FIRST preview run a push's own
    CONFIRMED instant matched (the push-time kick) tested a DIFFERENT object
    than the one that ultimately landed -- exactly the SABLE-kzi1a shape: the
    push-time preview went green, the base moved before promote, and the
    queued branch paid for a second, later preview it then waited on.
    push_to_ci_done_seconds (when available) is that FIRST preview's own
    latency -- kept alongside push_to_landed_seconds so the report can show,
    per branch, exactly how much the discarded preview cost.

    `queue_depth_at_promote` counts OTHER promotions in this same result set
    whose landed_at falls strictly after this branch's push and at or before
    its own landing -- the number of merges that landed while this branch was
    waiting, kzi1a's confound made a first-class, per-measurement field."""
    landed_epochs = promotion_landed_epochs(promotions, base_runs)
    ci_done_matches = {
        push.branch: run for push, run in _match_pushes_to_first_preview(pushes, preview_runs, max_gap_seconds)
    }
    pushes_by_branch: dict[str, PushEvent] = {}
    for p in sorted(pushes, key=lambda x: x.confirmed_at):
        pushes_by_branch[p.branch] = p  # last CONFIRMED push wins if a branch pushed more than once

    staged = []
    for promo in promotions:
        if not promo.branch:
            continue
        landed_at = landed_epochs.get(promo.sha)
        push = pushes_by_branch.get(promo.branch)
        if landed_at is None or push is None:
            continue
        ci_run = ci_done_matches.get(promo.branch)
        push_to_ci_done = (ci_run.completed_at - push.confirmed_at
                          if ci_run is not None and ci_run.completed_at is not None else None)
        discarded = bool(ci_run is not None and ci_run.head_sha is not None and ci_run.head_sha != promo.sha)
        staged.append((promo, push, landed_at, push_to_ci_done, discarded))

    records: list[LandedRecord] = []
    for idx, (promo, push, landed_at, push_to_ci_done, discarded) in enumerate(staged):
        depth = sum(
            1 for j, (_, _, other_landed, _, _) in enumerate(staged)
            if j != idx and push.confirmed_at < other_landed <= landed_at
        )
        records.append(LandedRecord(
            branch=promo.branch, bead=promo.bead, sha=promo.sha,
            push_confirmed_at=push.confirmed_at, landed_at=landed_at,
            push_to_landed_seconds=landed_at - push.confirmed_at,
            push_to_ci_done_seconds=push_to_ci_done,
            discarded_preview=discarded,
            queue_depth_at_promote=depth,
        ))
    return records


def evaluate_success(current_median: float | None, baseline_median: float | None,
                     current_red_rate: float | None, baseline_red_rate: float | None) -> dict:
    """The jd5fj.7 acceptance bar, verbatim: >=5x median latency at
    equal-or-lower red rate. Either red rate being unknown (None) does not
    fail the check -- it makes the check UNDECIDABLE on that axis, reported
    as such rather than silently treated as a pass or a fail."""
    if not current_median or not baseline_median:
        return {"speedup": None, "meets_bar": None, "reason": "insufficient latency data"}
    speedup = baseline_median / current_median
    if current_red_rate is None or baseline_red_rate is None:
        return {"speedup": speedup, "meets_bar": None,
                "reason": "red rate unknown on one side -- speedup alone is not the full bar"}
    red_ok = current_red_rate <= baseline_red_rate
    return {"speedup": speedup, "meets_bar": (speedup >= 5.0 and red_ok), "red_rate_ok": red_ok}


# --------------------------------------------------------------------------
# Adapters (impure) -- env-overridable seams, matching sable-merge-gate's own
# SABLE_MG_* convention so both tools can be stubbed the same way in tests.
# --------------------------------------------------------------------------

def _tool(env_name: str, default: str) -> list[str]:
    return os.environ.get(env_name, default).split()


def _run(argv: list[str], *, cwd: str = ".", check: bool = True,
        timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, text=True, check=check, timeout=timeout,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _git(repo: str, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return _run(_tool("SABLE_MR_GIT", "git") + list(args), cwd=repo, check=check)


def _gh(*args: str, check: bool = False, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return _run(_tool("SABLE_MR_GH", "gh") + list(args), check=check, timeout=timeout)


def _iso_to_epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _since_epoch(since: str | None) -> float | None:
    if not since:
        return None
    return _iso_to_epoch(since) if "T" in since else _iso_to_epoch(f"{since}T00:00:00+00:00")


def collect_promotions(repo: str, base_ref: str, since: str | None = None) -> list[PromotionCommit]:
    """Every landed promotion commit reachable from base_ref, oldest first.
    %x1e/%x1f record/field separators (never a literal space or newline) so a
    commit subject can never be mis-split -- the same convention git log
    consumers elsewhere in this repo use for exactly this reason."""
    args = ["log", base_ref, "--format=%H%x1f%cI%x1f%s%x1e"]
    if since:
        args.insert(2, f"--since={since}")
    cp = _git(repo, *args)
    if cp.returncode != 0:
        return []
    out: list[PromotionCommit] = []
    for record in cp.stdout.split("\x1e"):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split("\x1f")
        if len(parts) != 3:
            continue
        sha, committed_at, subject = parts
        parsed = parse_promotion_subject(sha, committed_at, subject)
        if parsed is not None:
            out.append(parsed)
    out.reverse()  # git log is newest-first; report oldest-first
    return out


def promotion_epochs(promotions: list[PromotionCommit]) -> dict[str, float]:
    return {p.sha: _iso_to_epoch(p.committed_at) for p in promotions}


def collect_base_runs(base_branch: str, since: str | None = None, limit: int = 500) -> list[BaseRun]:
    cp = _gh("run", "list", "--workflow=ci-verify", f"--branch={base_branch}",
             f"--limit={limit}", "--json=headSha,createdAt,conclusion")
    if cp.returncode != 0:
        return []
    try:
        records = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return []
    runs = [
        BaseRun(r["headSha"], _iso_to_epoch(r["createdAt"]), r.get("conclusion") or "")
        for r in records if r.get("headSha") and r.get("createdAt")
    ]
    since_epoch = _since_epoch(since)
    if since_epoch is not None:
        runs = [r for r in runs if r.created_at >= since_epoch]
    return runs


def collect_preview_runs(limit: int = 500, since: str | None = None) -> list[PreviewRun]:
    cp = _gh("run", "list", "--workflow=ci-verify", f"--limit={limit}",
             "--json=headBranch,headSha,createdAt,updatedAt,conclusion,status")
    if cp.returncode != 0:
        return []
    try:
        records = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return []
    out = []
    for r in records:
        completed = (_iso_to_epoch(r["updatedAt"])
                    if r.get("status") == "completed" and r.get("updatedAt") else None)
        out.append(PreviewRun(r.get("headBranch", ""), _iso_to_epoch(r["createdAt"]),
                              completed, r.get("conclusion"), r.get("headSha") or None))
    since_epoch = _since_epoch(since)
    if since_epoch is not None:
        out = [r for r in out if r.created_at >= since_epoch]
    return out


def parse_notify_log(text: str, since_epoch: float | None = None) -> list[PushEvent]:
    """CONFIRMED push events from post-push-merge-notify.log's own trace
    format (hooks/multi-manager/post-push-merge-notify.sh sable_pp_trace).
    One CONFIRMED line per successful push; the leading timestamp is the
    line's own ISO-8601 UTC stamp, not parsed from the log line's content."""
    out = []
    for line in text.splitlines():
        m = CONFIRMED_LOG_RE.match(line)
        if not m:
            continue
        try:
            epoch = _iso_to_epoch(m.group("ts"))
        except ValueError:
            continue
        if since_epoch is not None and epoch < since_epoch:
            continue
        out.append(PushEvent(m.group("branch"), epoch))
    return out


def read_notify_log(path: str | None = None) -> str:
    path = path or os.environ.get(
        "SABLE_MR_NOTIFY_LOG",
        os.path.expanduser("~/.claude/sable/logs/post-push-merge-notify.log"),
    )
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def snapshot_backstop_status(bd_bead: str = "SABLE-jd5fj.5") -> str:
    """Best-effort note on whether the S3 green-snapshot backstop has landed
    (qp92x standard: a measurement must state what it did and did not
    exercise). Never raises -- an unreachable `bd` must not break the report,
    it just means this caveat is reported as unknown instead of accurate."""
    try:
        cp = _run(_tool("SABLE_MR_BD", "bd") + ["show", bd_bead, "--json"], check=False, timeout=10)
        if cp.returncode != 0:
            return "unknown (bd query failed)"
        records = json.loads(cp.stdout)
        status = records[0].get("status") if records else None
        if status == "closed":
            return "landed (closed) -- semantic-break measurement includes the snapshot classifier"
        return (f"NOT landed (status={status!r}) -- semantic-break measurement below is git+gh "
                f"cross-reference ONLY, same method as the nueh3 baseline; it does not yet include "
                f"the S3 green-snapshot classifier")
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError, KeyError, IndexError):
        return "unknown (bd query failed)"


# --------------------------------------------------------------------------
# Report assembly
# --------------------------------------------------------------------------

def build_report(repo: str, git_base_ref: str, bare_base_branch: str, since: str | None,
                 window_hours: float = 24.0, max_join_gap_seconds: float = 120.0,
                 check_snapshot_status: bool = True) -> dict:
    """`git_base_ref` is what `git log` resolves (e.g. "origin/tmux-only" --
    a remote-tracking ref); `bare_base_branch` is the bare name gh's
    `--branch` filter expects (e.g. "tmux-only"). Conflating the two is a
    real bug shape here: gh silently returns zero runs for a ref-qualified
    branch name instead of erroring, which would read as "no CI activity"
    rather than "wrong argument"."""
    promotions = collect_promotions(repo, git_base_ref, since)
    epochs = promotion_epochs(promotions)
    base_runs = collect_base_runs(bare_base_branch, since)
    preview_runs = collect_preview_runs(since=since)
    pushes = parse_notify_log(read_notify_log(), _since_epoch(since))

    # SECONDARY (SABLE-jd5fj.11): push -> the push-time preview's own
    # completion. Kept, explicitly labelled, because it isolates raw CI
    # duration from queue effects -- but it is blind to any later discarded
    # or rebuilt preview, which is exactly why it is no longer the bar the
    # epic is graded on.
    latencies = join_push_to_ci(pushes, preview_runs, max_join_gap_seconds)
    latency = latency_stats(latencies)
    latency["coverage"] = f"{len(latencies)}/{len(pushes)} pushes matched to a completed preview run"

    # PRIMARY (SABLE-jd5fj.11): push -> the FAST-FORWARD instant, anchored on
    # the sha that actually landed rather than the first preview a push
    # happened to kick -- so a discarded/rebuilt preview (SABLE-kzi1a) shows
    # up here even though push-to-CI-done cannot see it.
    landed_records = compute_landed_metrics(pushes, promotions, preview_runs, base_runs, max_join_gap_seconds)
    landed_latencies = [r.push_to_landed_seconds for r in landed_records]
    landed = latency_stats(landed_latencies)
    landed["coverage"] = f"{len(landed_records)}/{len(pushes)} pushes matched to an observed fast-forward"
    discarded_preview_count = sum(1 for r in landed_records if r.discarded_preview)

    reds = red_rate([r.conclusion for r in preview_runs if r.conclusion])
    semantic = count_semantic_breaks(promotions, base_runs, epochs, window_hours * 3600.0)

    disjoint_n = sum(1 for p in promotions if p.disjoint)
    # The success bar is now evaluated against push-to-LANDED. The baseline
    # column stays push-to-CI-done (nueh3 never measured push-to-landed; the
    # jxgm4/jd5fj.7 constraint against the COARSE commit-date column still
    # applies and is unaffected by this change) -- so the comparison is
    # honestly conservative: push-to-landed is never smaller than push-to-CI-
    # done, so the reported speedup can only shrink relative to the old bar,
    # never inflate it.
    success = evaluate_success(landed["median"], NUEH3_BASELINE["push_to_ci_done"]["median"],
                              reds["rate"], NUEH3_BASELINE["red_rate"]["rate"])

    return {
        "base_branch": bare_base_branch,
        "since": since,
        "window_hours": window_hours,
        "promotions_total": len(promotions),
        "promotions_disjoint": disjoint_n,
        "push_to_landed": landed,
        "push_to_ci_done": latency,
        "discarded_preview_count": discarded_preview_count,
        "landed_records": [
            {
                "branch": r.branch, "bead": r.bead, "sha": r.sha,
                "push_confirmed_at": r.push_confirmed_at, "landed_at": r.landed_at,
                "push_to_landed_seconds": r.push_to_landed_seconds,
                "push_to_ci_done_seconds": r.push_to_ci_done_seconds,
                "discarded_preview": r.discarded_preview,
                "queue_depth_at_promote": r.queue_depth_at_promote,
            }
            for r in landed_records
        ],
        "red_rate": reds,
        "semantic_break": semantic,
        "baseline": NUEH3_BASELINE,
        "success_metric": success,
        "snapshot_backstop": snapshot_backstop_status() if check_snapshot_status else "skipped",
    }


def format_report_text(report: dict) -> str:
    lines = []
    lines.append(f"SABLE merge-pipeline telemetry (vs SABLE-nueh3 baseline), base={report['base_branch']}")
    if report.get("since"):
        lines.append(f"  window: since {report['since']}")
    lines.append(f"  promotions observed: {report['promotions_total']} "
                 f"({report['promotions_disjoint']} via optimistic disjoint promotion)")
    landed = report["push_to_landed"]
    lines.append(f"  push->LANDED latency [PRIMARY, SABLE-jd5fj.11]: n={landed['n']} "
                 f"median={landed['median']} p90={landed['p90']} ({landed['coverage']})")
    lines.append(f"    discarded-preview cost: {report['discarded_preview_count']}/{landed['n']} landed "
                 f"branch(es) paid for a rebuilt preview after their push-time preview was discarded")
    lat = report["push_to_ci_done"]
    base_lat = report["baseline"]["push_to_ci_done"]
    lines.append(f"  push->CI-done latency [secondary -- push-time preview only, blind to discarded "
                 f"previews]: n={lat['n']} median={lat['median']} p90={lat['p90']} ({lat['coverage']})")
    lines.append(f"    baseline (nueh3): n={base_lat['n']} median={base_lat['median']} p90={base_lat['p90']}")
    rr = report["red_rate"]
    lines.append(f"  CI red rate: n={rr['n']} red={rr['red']} rate={rr['rate']}")
    sb = report["semantic_break"]
    lines.append(f"  semantic-break rate (disjoint promotion -> later red on same SHA): "
                 f"{sb['breaks']}/{sb['disjoint_promotions']}"
                 + (f" (rule-of-three bound {sb['rule_of_three_bound']:.4f})"
                    if sb["rule_of_three_bound"] is not None else ""))
    lines.append(f"    baseline (nueh3, pre-S2 structural zero): "
                 f"{report['baseline']['semantic_break']['breaks']}/{report['baseline']['semantic_break']['n']}")
    lines.append(f"  snapshot backstop (SABLE-jd5fj.5): {report['snapshot_backstop']}")
    sm = report["success_metric"]
    lines.append(f"  success metric (>=5x median PUSH-TO-LANDED latency, equal-or-lower red rate): "
                 f"speedup={sm['speedup']} meets_bar={sm['meets_bar']}"
                 + (f" ({sm.get('reason')})" if sm.get("reason") else ""))
    return "\n".join(lines)
