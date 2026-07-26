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
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime

WORKFLOW = "ci-verify.yml"
GH_BIN = "gh"
JOB_NAME = "verify"

RUN_LIST_FIELDS = "databaseId,headBranch,createdAt,updatedAt,conclusion,status,displayTitle"

# Anchored to the FULL ci-verify/<bead>-<sha7> shape (sha7 is exactly 7 hex
# chars, sable_gate_classify_lib.preview_ref_name's preview_sha[:7]) so a
# bead id containing its own dashes/dots (e.g. "SABLE-8b41.4") still splits
# correctly -- the trailing 7-hex-char anchor is what makes the greedy
# `.+` bead group unambiguous, not the reverse.
PREVIEW_REF_RE = re.compile(r"^ci-verify/(?P<bead>.+)-(?P<sha7>[0-9a-f]{7})$")


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


def parse_preview_ref(head_branch: str) -> tuple[str, str] | None:
    """(bead, sha7) extracted from a ci-verify/<bead>-<sha7> preview-branch
    ref, or None if `head_branch` is not a preview ref at all -- e.g. the
    redundant post-merge confirmation run's headBranch is plain
    "tmux-only", which this rejects outright rather than special-casing."""
    m = PREVIEW_REF_RE.match(head_branch)
    if not m:
        return None
    return m.group("bead"), m.group("sha7")


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
    """Keep only runs whose headBranch is a ci-verify/<bead>-<sha7> preview
    ref. This IS the dedup: the redundant post-merge run's headBranch is
    just "tmux-only", which parse_preview_ref rejects, so it is dropped by
    construction -- the RESEARCH TRAP is filtering BY headBranch=="tmux-only"
    (which would grab the WRONG run); this filters FOR the preview-ref shape
    instead, so the tmux-only run is never a candidate in the first place."""
    return [run for run in raw_runs if parse_preview_ref(run.get("headBranch", "")) is not None]


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


def build_ci_run_event(run: dict, jobs: list[dict]) -> CiRunEvent | None:
    """One CiRunEvent from an already-selected preview run plus its
    `gh run view --json jobs` payload, or None if the run isn't actually a
    preview-ref run or its job timing isn't resolvable yet (still running)."""
    parsed = parse_preview_ref(run.get("headBranch", ""))
    if parsed is None:
        return None
    job = select_job(jobs)
    if job is None:
        return None
    duration = job_duration_seconds(job)
    if duration is None:
        return None
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
) -> list[CiRunEvent]:
    """Every ci-verify gate duration reachable from `gh run list`, one per
    bead's preview-ref run, with the redundant post-merge tmux-only
    confirmation run dropped by construction (select_preview_runs).

    `cwd` lets callers (and tests) point this at a specific gh-authenticated
    repo checkout instead of always querying whatever repo the current
    process happens to be sitting in.
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

    events: list[CiRunEvent] = []
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
        event = build_ci_run_event(run, jobs)
        if event is not None:
            events.append(event)
    return events
