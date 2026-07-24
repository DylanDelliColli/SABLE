#!/usr/bin/env python3
"""gh data adapter for sable-telemetry (SABLE-8b41.4).

Locked contract (.claude/sable/state/planning/SABLE-8b41/architecture.json,
"Three source adapters behind a thin core" / "CI-duration source matches the
preview-branch run"): this module owns the ONE research trap that lives in
gh's Actions run shape, so no caller re-derives it.

RESEARCH TRAP (research.json, SABLE-c008 example): TWO Actions runs exist per
merge -- bin/sable-merge-gate builds a preview ref named
`ci-verify/<bead>-<preview_sha[:7]>` (sable_gate_classify_lib.preview_ref_name)
and polls the run keyed to THAT ref; this is the real, authoritative gate
duration. After promotion, a SECOND run fires with headBranch=tmux-only
itself -- a redundant post-merge confirmation whose "verify" job has a step
literally named "Dedup guard -- skip tmux-only re-verify of an
already-verified preview SHA (SABLE-r3i6)" and is near-instant/skipped.
Querying `gh run list --json ...` and filtering by headBranch==tmux-only
grabs this WRONG run and mis-measures (or double-counts) the gate duration.

This adapter never filters BY headBranch==tmux-only. Instead it filters FOR
the ci-verify/<bead>-<sha7> preview-ref shape (select_preview_runs /
parse_preview_ref) -- the redundant tmux-only run's headBranch simply does
not match that shape, so it is dropped by construction, not by a
second, separately-fallible exclusion rule.

Duration is read from the JOB-level startedAt/completedAt of the preview
run's "verify" job, not the run-level createdAt/updatedAt (which includes
Actions queue time ahead of the job actually starting).

BATCH-AWARE EXTENSION (SABLE-be4lo.5): a merge-trains batch pushes its fold
chain tip as `ci-verify/batch-<setkey7>` (sable_batch_fold_lib.push_batch_ref,
sable_gate_classify_lib.preview_ref_name("batch", setkey)) -- a SECOND
ci-verify ref shape this module must recognise. Naively applying
PREVIEW_REF_RE to that ref would match anyway (its greedy `.+` bead group
just captures the literal string "batch"), which is precisely the
mis-attribution bug this extension exists to prevent: BATCH_REF_RE is tried
FIRST, and only a ref it does NOT match falls through to the legacy pattern,
so a real batch ref can never be attributed to bead="batch". A batch ref's
member set is resolved via the durable manifest
(sable_gate_promote_lib.find_batch_record) -- never guessed from the ref
itself, which carries only a hash.

LOUD-OR-COUNTED (SABLE-be4lo.5, the SABLE-x2n8a family): a headBranch that
lives under ci-verify/ but matches NEITHER shape is not a silent drop like
the non-ci-verify tmux-only run above -- it is logged by name via
_log_unparseable_ci_ref, so a truncated/unrecognised ref is distinguishable
from "there was nothing to see here" rather than rendering identically to
it.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime

import sable_gate_promote_lib as promote_lib

WORKFLOW = "ci-verify.yml"
GH_BIN = "gh"
JOB_NAME = "verify"

RUN_LIST_FIELDS = "databaseId,headBranch,createdAt,updatedAt,conclusion,status,displayTitle"

# Anchored to the FULL ci-verify/<bead>-<sha7> shape (sha7 is exactly 7 hex
# chars, sable_gate_classify_lib.preview_ref_name's preview_sha[:7]) so a
# bead id containing its own dashes/dots (e.g. "SABLE-8b41.4") still splits
# correctly -- the trailing 7-hex-char anchor is what makes the greedy
# `.+` bead group unambiguous, not the reverse.
#
# UNCHANGED byte-for-byte by the batch-aware extension below (SABLE-be4lo.5
# P1 regression requirement) -- a batch ref is intercepted by BATCH_REF_RE
# before this pattern ever sees it (see classify_head_branch), so this
# pattern itself never had to change to stop mis-attributing batch refs.
PREVIEW_REF_RE = re.compile(r"^ci-verify/(?P<bead>.+)-(?P<sha7>[0-9a-f]{7})$")

# Anchored to the FULL ci-verify/batch-<setkey7> shape
# (sable_batch_key_lib.setkey's sha1 hexdigest, sliced to 7 chars by
# sable_gate_classify_lib.preview_ref_name exactly like a bead's sha7). The
# literal "batch-" segment is what makes this shape unambiguous against
# PREVIEW_REF_RE -- a bead id can never legitimately be the literal string
# "batch".
BATCH_REF_RE = re.compile(r"^ci-verify/batch-(?P<setkey7>[0-9a-f]{7})$")


@dataclass(frozen=True)
class CiRunEvent:
    """One ci-verify gate run's duration, attributed to the bead whose
    preview ref it ran on -- the typed record this adapter returns instead
    of a loose dict (Primitive Obsession advisory, architecture.json
    smell_risks)."""

    bead_id: str
    sha7: str
    run_id: int
    duration_seconds: float
    completed_at: str


@dataclass(frozen=True)
class BatchRunEvent:
    """One ci-verify gate run's duration for a BATCHED landing
    (SABLE-be4lo.5) -- attributed to every member bead the batch's durable
    manifest names, never to the literal string "batch" (the mis-attribution
    a naive PREVIEW_REF_RE match on a batch ref would produce).

    member_bead_ids/member_branches are the union of every batch member's own
    bead ids/branch, flattened in the manifest's canonical (tip_sha-sorted)
    member order. Both are empty tuples -- not a guess, not an error -- when
    no durable manifest record exists yet for this ref: the member set is
    RESOLVED VIA THE MANIFEST ONLY, never inferred from the ref's setkey7
    hash, which carries no member information to guess from."""

    setkey7: str
    run_id: int
    duration_seconds: float
    completed_at: str
    member_bead_ids: tuple[str, ...]
    member_branches: tuple[str, ...]


def parse_preview_ref(head_branch: str) -> tuple[str, str] | None:
    """(bead, sha7) extracted from a ci-verify/<bead>-<sha7> preview-branch
    ref, or None if `head_branch` is not a preview ref at all -- e.g. the
    redundant post-merge confirmation run's headBranch is plain
    "tmux-only", which this rejects outright rather than special-casing.

    UNCHANGED behaviour (SABLE-be4lo.5 P1 regression requirement): this
    function does not know about the batch shape at all, and a batch ref
    happens to still satisfy this pattern syntactically (bead="batch"). It is
    classify_head_branch's job -- not this function's -- to try BATCH_REF_RE
    first so that mis-attribution never actually surfaces to a caller."""
    m = PREVIEW_REF_RE.match(head_branch)
    if not m:
        return None
    return m.group("bead"), m.group("sha7")


def parse_batch_ref(head_branch: str) -> str | None:
    """setkey7 extracted from a ci-verify/batch-<setkey7> ref, or None if
    `head_branch` is not a batch ref."""
    m = BATCH_REF_RE.match(head_branch)
    if not m:
        return None
    return m.group("setkey7")


def _is_ci_verify_ref(head_branch: str) -> bool:
    """True for any run whose headBranch lives under the ci-verify/
    namespace at all. Used only to decide whether an unrecognised shape
    deserves the loud-or-counted treatment: a non-ci-verify branch (e.g.
    "tmux-only", the redundant post-merge confirmation run) was never a
    preview-ref candidate in the first place, so it stays a silent,
    by-construction exclusion -- never counted as garbage."""
    return head_branch.startswith("ci-verify/")


def classify_head_branch(head_branch: str) -> tuple[str, str | tuple[str, str] | None]:
    """The one place head_branch shape-dispatch happens, so select_preview_runs
    and build_ci_run_event can never independently drift on which shape wins.

    Returns exactly one of:
      ("batch", setkey7)      -- ci-verify/batch-<setkey7>
      ("legacy", (bead, sha7)) -- ci-verify/<bead>-<sha7>
      ("unmatched", None)     -- under ci-verify/ but neither shape (LOUD-OR-COUNTED)
      ("not_ci_verify", None) -- not a ci-verify ref at all (e.g. "tmux-only")

    BATCH_REF_RE is tried before PREVIEW_REF_RE -- the precedence that makes
    the mis-attribution bug (bead="batch") structurally unreachable rather
    than merely untested."""
    setkey7 = parse_batch_ref(head_branch)
    if setkey7 is not None:
        return ("batch", setkey7)
    legacy = parse_preview_ref(head_branch)
    if legacy is not None:
        return ("legacy", legacy)
    if _is_ci_verify_ref(head_branch):
        return ("unmatched", None)
    return ("not_ci_verify", None)


def _log_unparseable_ci_ref(head_branch: str) -> None:
    """LOUD-OR-COUNTED (SABLE-be4lo.5, the SABLE-x2n8a family): a ci-verify
    ref matching neither the legacy nor the batch shape must never be a
    silent drop -- an unrecognised/truncated ref would otherwise render
    identically to "nothing happened here". Named line to stderr so it is
    visible in CI logs without requiring a caller to thread a counter
    through; the exact substring below is the fingerprint this module's own
    tests (and any future consumer) anchor on."""
    print(
        f"sable-telemetry: gh-source: unparseable ci-verify ref "
        f"(neither legacy ci-verify/<bead>-<sha7> nor batch "
        f"ci-verify/batch-<setkey7> shape): {head_branch}",
        file=sys.stderr,
    )


def build_run_list_args(workflow: str = WORKFLOW, limit: int = 500) -> list[str]:
    """The `gh run list` args this adapter runs, isolated from subprocess
    execution so the query shape is unit-testable without a real gh
    process."""
    return [
        "run", "list",
        f"--workflow={workflow}",
        "--json", RUN_LIST_FIELDS,
        "--limit", str(limit),
    ]


def select_preview_runs(raw_runs: list[dict]) -> list[dict]:
    """Keep only runs whose headBranch is a ci-verify/<bead>-<sha7> legacy
    preview ref OR a ci-verify/batch-<setkey7> batch ref. This IS the dedup:
    the redundant post-merge run's headBranch is just "tmux-only", which
    matches neither shape, so it is dropped by construction -- the RESEARCH
    TRAP is filtering BY headBranch=="tmux-only" (which would grab the WRONG
    run); this filters FOR the preview-ref shapes instead, so the tmux-only
    run is never a candidate in the first place, exactly as before the batch
    shape existed (byte-identical for every legacy fixture: SABLE-be4lo.5 P1
    regression requirement).

    A headBranch under ci-verify/ that matches NEITHER shape is excluded from
    the result exactly like tmux-only always was, but -- unlike tmux-only --
    it is not a silent drop: classify_head_branch's "unmatched" disposition
    is logged loudly (_log_unparseable_ci_ref) before being excluded."""
    selected = []
    for run in raw_runs:
        disposition, _ = classify_head_branch(run.get("headBranch", ""))
        if disposition in ("batch", "legacy"):
            selected.append(run)
        elif disposition == "unmatched":
            _log_unparseable_ci_ref(run.get("headBranch", ""))
    return selected


def build_run_view_args(run_id: int) -> list[str]:
    """The `gh run view` args this adapter runs to pull job-level timing for
    one already-selected preview run."""
    return ["run", "view", str(run_id), "--json", "jobs"]


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def select_job(jobs: list[dict]) -> dict | None:
    """The one job whose duration IS the gate duration. Matches by name
    (JOB_NAME == "verify", ci-verify.yml's only job) rather than assuming
    position, falling back to the first job if the workflow's job name ever
    drifts -- a run with no jobs at all yields None."""
    if not jobs:
        return None
    for job in jobs:
        if job.get("name") == JOB_NAME:
            return job
    return jobs[0]


def job_duration_seconds(job: dict) -> float | None:
    """The real CI-verify gate duration: job-level startedAt->completedAt,
    not the run-level createdAt->updatedAt (which includes Actions queue
    time), and not derivable at all from a job still in progress."""
    started = job.get("startedAt")
    completed = job.get("completedAt")
    if not started or not completed:
        return None
    return (_parse_iso(completed) - _parse_iso(started)).total_seconds()


def _resolve_batch_members(cwd: str | None, combined_ref: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(member_bead_ids, member_branches) for a batch ref, RESOLVED VIA THE
    DURABLE MANIFEST (sable_gate_promote_lib.find_batch_record) -- never
    guessed from the ref itself, which carries only setkey7, a hash with no
    member information to recover. Both tuples are empty when no manifest
    record exists yet for this ref; that is a legitimate "not landed via the
    manifest yet" outcome, not an error and not a garbage ref (the ref shape
    itself parsed fine)."""
    record = promote_lib.find_batch_record(cwd if cwd is not None else ".", combined_ref)
    if record is None:
        return (), ()
    member_bead_ids = tuple(
        bead_id for beads in record.member_bead_ids() for bead_id in beads
    )
    return member_bead_ids, record.member_branches()


def build_ci_run_event(
    run: dict, jobs: list[dict], cwd: str | None = None
) -> CiRunEvent | BatchRunEvent | None:
    """One CiRunEvent (legacy single-branch preview) or BatchRunEvent (batch
    landing, SABLE-be4lo.5) from an already-selected preview run plus its
    `gh run view --json jobs` payload, or None if the run isn't actually a
    recognised preview-ref/batch-ref run or its job timing isn't resolvable
    yet (still running).

    `cwd` is only consulted for a batch ref's manifest lookup
    (_resolve_batch_members) -- a legacy ref never touches it, so passing no
    `cwd` at all (the existing call convention) leaves legacy behaviour
    byte-identical."""
    head_branch = run.get("headBranch", "")
    disposition, parsed = classify_head_branch(head_branch)
    if disposition not in ("batch", "legacy"):
        return None
    job = select_job(jobs)
    if job is None:
        return None
    duration = job_duration_seconds(job)
    if duration is None:
        return None
    if disposition == "batch":
        setkey7 = parsed
        member_bead_ids, member_branches = _resolve_batch_members(cwd, head_branch)
        return BatchRunEvent(
            setkey7=setkey7,
            run_id=run["databaseId"],
            duration_seconds=duration,
            completed_at=job["completedAt"],
            member_bead_ids=member_bead_ids,
            member_branches=member_branches,
        )
    bead_id, sha7 = parsed
    return CiRunEvent(
        bead_id=bead_id,
        sha7=sha7,
        run_id=run["databaseId"],
        duration_seconds=duration,
        completed_at=job["completedAt"],
    )


def fetch_ci_run_events(
    workflow: str = WORKFLOW, cwd: str | None = None, limit: int = 500
) -> list[CiRunEvent | BatchRunEvent]:
    """Every ci-verify gate duration reachable from `gh run list`, one per
    bead's preview-ref run or per batch's combined-ref run, with the
    redundant post-merge tmux-only confirmation run dropped by construction
    (select_preview_runs) and any unparseable ci-verify ref logged loudly
    rather than silently dropped.

    `cwd` lets callers (and tests) point this at a specific gh-authenticated
    repo checkout instead of always querying whatever repo the current
    process happens to be sitting in -- and, for a batch ref, is also the
    repo whose durable batch manifest resolves its member set.
    """
    result = subprocess.run(
        [GH_BIN, *build_run_list_args(workflow=workflow, limit=limit)],
        capture_output=True,
        text=True,
        check=True,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
    )
    raw_runs = json.loads(result.stdout)

    events: list[CiRunEvent | BatchRunEvent] = []
    for run in select_preview_runs(raw_runs):
        view = subprocess.run(
            [GH_BIN, *build_run_view_args(run["databaseId"])],
            capture_output=True,
            text=True,
            check=True,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
        )
        jobs = json.loads(view.stdout).get("jobs", [])
        event = build_ci_run_event(run, jobs, cwd=cwd)
        if event is not None:
            events.append(event)
    return events
