#!/usr/bin/env python3
"""git data adapter for sable-telemetry (SABLE-8b41.3).

Locked contract (.claude/sable/state/planning/SABLE-8b41/architecture.json,
"Three source adapters behind a thin core"): this module owns the ONE
research trap that lives in the git history's merge-preview commit shape, so
no caller re-derives it.

Merge-preview commits are built by sable-merge-gate's promote path via
`git commit-tree` (bin/sable-merge-gate), stamping the exact subject
"ci-verify merge-preview: <branch> onto <base> (<bead>)" -- a MECHANICAL
commit, not a human `git commit` typed over time. Its author date and
committer date are therefore identical and instantaneous (both stamped at
preview-build time in one process), so the commit's own committer date IS
the merge event's timestamp -- this adapter only counts merge events, it
never computes a push-to-land latency from these dates (that caveat, and why
it would be wrong to, is documented at length in
bin/sable_merge_report_lib.py's module docstring for a tool that DOES
attempt a latency measurement; this adapter has no such concern).

RESEARCH TRAP this adapter must not get wrong: the subject regex is anchored
to the FULL "ci-verify merge-preview: ... onto ... (...)" shape, not just
"look for a trailing (SABLE-xxxx)" -- a human commit like
"fix(auth): rotate token (SABLE-9182)" carries a trailing bead reference but
is NOT a merge-preview event and must never be counted as one.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

# %x1e/%x1f record/field separators -- never a literal space or newline --
# so a commit subject can never be mis-split. Same convention
# bin/sable_merge_report_lib.py's collect_promotions uses.
GIT_LOG_FORMAT = "%H%x1f%cI%x1f%s%x1e"

MERGE_PREVIEW_SUBJECT_RE = re.compile(
    r"^ci-verify merge-preview: (?P<branch>.+) onto (?P<base>.+) \((?P<detail>.+)\)$"
)
DISJOINT_SUFFIX = ", disjoint re-verify"
PUSH_TIME_KICK = "push-time kick"


@dataclass(frozen=True)
class MergeEvent:
    """One merge-preview event: a bead landing on the base branch, as read
    from git -- the typed record this adapter returns instead of a loose
    tuple (Primitive Obsession advisory, architecture.json smell_risks)."""

    bead_id: str
    sha: str
    committed_at: str  # ISO-8601 committer date; author == committer, instantaneous


def extract_bead_id_from_subject(subject: str) -> str | None:
    """The bead id a merge-preview commit subject carries, or None if
    `subject` is not a merge-preview commit at all (e.g. an ordinary human
    commit, even one with its own trailing (SABLE-xxxx) reference) or is a
    push-time-kick merge-preview commit that carries no bead."""
    m = MERGE_PREVIEW_SUBJECT_RE.match(subject)
    if not m:
        return None
    detail = m.group("detail")
    if detail.endswith(DISJOINT_SUFFIX):
        detail = detail[: -len(DISJOINT_SUFFIX)]
    if detail == PUSH_TIME_KICK:
        return None
    return detail or None


def build_git_log_args(base_ref: str) -> list[str]:
    """The `git log` args this adapter runs, isolated from subprocess
    execution so the query shape is unit-testable without a real git
    process."""
    return ["log", base_ref, f"--format={GIT_LOG_FORMAT}"]


def parse_git_log_output(output: str) -> list[MergeEvent]:
    """Parse raw `git log --format=...` stdout (GIT_LOG_FORMAT) into merge
    events, oldest first (git log itself is newest-first). Records with no
    bead id (decoy human commits, push-time kicks) are silently dropped --
    they are not merge events this adapter tracks."""
    events: list[MergeEvent] = []
    for record in output.split("\x1e"):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split("\x1f")
        if len(parts) != 3:
            continue
        sha, committed_at, subject = parts
        bead_id = extract_bead_id_from_subject(subject)
        if bead_id is not None:
            events.append(MergeEvent(bead_id=bead_id, sha=sha, committed_at=committed_at))
    events.reverse()
    return events


def fetch_merge_events(
    base_ref: str = "origin/tmux-only", cwd: str | None = None
) -> list[MergeEvent]:
    """Every merge-preview event reachable from `base_ref`, oldest first.

    `cwd` lets callers (and tests) point this at a specific git repo instead
    of always querying whatever repo the current process happens to be
    sitting in.
    """
    result = subprocess.run(
        ["git", *build_git_log_args(base_ref)],
        capture_output=True,
        text=True,
        check=True,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
    )
    return parse_git_log_output(result.stdout)
