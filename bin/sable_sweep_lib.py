#!/usr/bin/env python3
"""sable_sweep_lib — sweep shard machinery for bounded producer panes
(SABLE-tz7h.4, architecture.json decisions 3+4).

A sweep takes ONE `bd list --json` snapshot, splits it into disjoint slices,
and hands each slice to a read-only shard subagent (repo-grep rights, ZERO bd
invocations — SABLE-eozl single-writer sidestep: shards never touch bd, only
the parent pane does, and only after merge). Shard findings merge back into
one victor-shaped verdict report; that report is turned into the ONLY bd
writes the sweep performs — a flat, append-notes-only write plan.

Used by victor's pane-mode operation (templates/multi-manager/roles/victor.md).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

# Victor's existing classification vocabulary (templates/multi-manager/roles/
# victor.md, Phase 3) — reused here so a sharded verdict report is structurally
# identical to what a single, non-sharded Victor run would produce.
# FRESH-class verdicts: "valid" and "reference" (SABLE-5s97 — a bead carrying a
# reference/runbook label short-circuits straight to "reference" and is exempt
# from staleness checks; runbooks and standing-convention beads don't rot on
# code-drift timescales). Everything else is some flavor of stale/needs-attention.
CLASSIFICATIONS = (
    "valid",
    "reference",
    "stale-fixed",
    "stale-moved",
    "description-rotted",
    "ambiguous",
    "needs-verification-spec",
)

# The harness ignores a prose cap in a dispatch/kick prompt (research pitfall,
# SABLE-mmdt) — this bound has to be enforced IN CODE to mean anything.
MAX_SHARDS = 10


def _run_bd_list(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def export_snapshot(scope_args: list[str] | None = None, run=None) -> list:
    """One `bd list --json` call for the whole sweep — a point-in-time
    snapshot. A bead created, closed, or edited in the live db after this call
    returns never retroactively appears in (or vanishes from) the list handed
    back here; shards only ever see what existed at export time."""
    run = run or _run_bd_list
    raw = run(["bd", "list", "--json", *(scope_args or [])])
    if not raw or not raw.strip():
        return []
    data = json.loads(raw)
    return data if isinstance(data, list) else [data]


def shard_count(requested: int) -> int:
    """min(10, requested), floor 1 — the mechanical cap on concurrent shard
    children. Always returns a usable shard count regardless of what garbage
    (0, negative, huge) a caller passes."""
    return max(1, min(MAX_SHARDS, requested))


def slice(beads: list, k: int) -> list:
    """`shard_count(k)` disjoint slices whose union is exactly `beads`, in
    original relative order, with no duplication. Round-robin assignment keeps
    slice sizes within one of each other regardless of len(beads) vs k.
    Always returns exactly `shard_count(k)` slices — when beads is shorter
    than k (or empty), the extra slices are simply empty lists, never omitted,
    so callers can always rely on `len(result) == shard_count(k)`."""
    k = shard_count(k)
    slices: list[list] = [[] for _ in range(k)]
    for i, bead in enumerate(beads):
        slices[i % k].append(bead)
    return slices


def completion_check(path) -> bool:
    """True only when `path` exists AND its content parses as JSON. False for
    a missing file, an empty file, or malformed JSON — a shard (or the parent's
    own deliverable write) that crashed mid-write must not be mistaken for one
    that finished cleanly."""
    p = Path(path)
    if not p.exists():
        return False
    text = p.read_text()
    if not text.strip():
        return False
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True


class ShardUnderReportError(RuntimeError):
    """Raised by merge()/reconcile_shard_counts() when a shard's findings
    don't cover every bead in its own slice (SABLE-5s97).

    Live failure this guards against: in the SABLE-tz7h.5 acceptance sweep, 3
    of 10 shards each silently omitted exactly ONE bead despite a
    correctly-sized slice file — a boundary (first/last-element) miss in shard
    enumeration, not a slicing bug (slice()'s disjoint-union property is
    covered separately and holds). Self-reported shard counts are not
    sufficient evidence the shard covered its slice; only comparing shard
    output against the slice it was actually handed catches this, which is
    why reconciliation lives here rather than trusting shard output at face
    value.
    """

    def __init__(self, shard_index, expected_ids, missing_ids):
        self.shard_index = shard_index
        self.expected_ids = expected_ids
        self.missing_ids = missing_ids
        super().__init__(
            f"shard {shard_index} under-reported: expected "
            f"{len(expected_ids)} bead(s), missing {sorted(missing_ids)}"
        )


def reconcile_shard_counts(shard_reports: list, slices: list) -> None:
    """Pre-merge validate step: for each slice, confirm every bead id in it
    appears as a `bead_id` in the corresponding shard report's findings.
    Raises ShardUnderReportError on the first shard found short — fails
    loudly rather than letting a deficient shard merge silently. A shard
    missing from `shard_reports` entirely (fewer reports than slices) is
    treated as reporting zero findings, so it fails the same way a partial
    report does. Slices with no beads are skipped — there is nothing to
    reconcile against an empty slice."""
    for idx, expected_beads in enumerate(slices):
        expected_ids = {b.get("id") for b in expected_beads}
        if not expected_ids:
            continue
        report = shard_reports[idx] if idx < len(shard_reports) else None
        report_findings = (report or {}).get("findings")
        got_ids = (
            {f.get("bead_id") for f in report_findings}
            if isinstance(report_findings, list)
            else set()
        )
        missing = expected_ids - got_ids
        if missing:
            raise ShardUnderReportError(idx, expected_ids, missing)


def merge(shard_reports: list, slices: list | None = None) -> dict:
    """Combine per-shard findings into one victor-shaped verdict report:
    ``{"findings": [...], "stats": {...}}``. Every finding from every shard
    survives — no dedup, no dropping — and the output shape is the same
    regardless of shard count: merging one shard that covered everything
    (the single-agent, k=1 case) produces the identical top-level shape as
    merging ten shards' worth of partial findings. `shard_reports` entries
    missing or with a non-list `findings` key contribute nothing (tolerant of
    a shard that failed to produce a report at all).

    Pass `slices` (the same list returned by `slice()`, in shard order) to
    reconcile each shard's reported bead_ids against what it was actually
    handed before merging (SABLE-5s97) — raises ShardUnderReportError instead
    of silently merging a deficient shard's report. `slices` is optional so
    existing non-reconciling callers keep working; the pane-mode flow
    (templates/multi-manager/roles/victor.md) always has its slices on hand
    and should always pass it."""
    if slices is not None:
        reconcile_shard_counts(shard_reports, slices)
    findings = []
    for report in shard_reports:
        report_findings = (report or {}).get("findings")
        if isinstance(report_findings, list):
            findings.extend(report_findings)
    stats = {"candidates": len(findings)}
    for cls in CLASSIFICATIONS:
        stats[cls] = sum(1 for f in findings if f.get("classification") == cls)
    stats["model_stale"] = sum(1 for f in findings if f.get("model_stale"))
    return {"findings": findings, "stats": stats}


def write_plan(merged: dict) -> list:
    """The post-merge list of `bd update --append-notes` argv commands for the
    PARENT pane to execute — the ONLY bd writes a sweep performs. Shards never
    touch bd (single-writer, SABLE-eozl sidestep); this plan is append-notes-
    ONLY by contract — no close, no label change, no description rewrite. Those
    judgment calls stay with the interactive, non-sharded Victor path (see
    Phase 4 in templates/multi-manager/roles/victor.md); the sharded pane only
    ever appends evidence for a human or manager to act on. A finding with no
    `bead_id` is skipped — there's nothing to write against."""
    plan = []
    for finding in merged.get("findings", []) or []:
        bead_id = finding.get("bead_id")
        if not bead_id:
            continue
        note = f"victor-sweep: {finding.get('classification', 'unknown')}"
        evidence = finding.get("evidence")
        if evidence:
            note += f" — {evidence}"
        if finding.get("model_stale"):
            note += " [model-stale]"
        plan.append(["bd", "update", bead_id, "--append-notes", note])
    return plan
