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
CLASSIFICATIONS = (
    "valid",
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


def merge(shard_reports: list) -> dict:
    """Combine per-shard findings into one victor-shaped verdict report:
    ``{"findings": [...], "stats": {...}}``. Every finding from every shard
    survives — no dedup, no dropping — and the output shape is the same
    regardless of shard count: merging one shard that covered everything
    (the single-agent, k=1 case) produces the identical top-level shape as
    merging ten shards' worth of partial findings. `shard_reports` entries
    missing or with a non-list `findings` key contribute nothing (tolerant of
    a shard that failed to produce a report at all)."""
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
