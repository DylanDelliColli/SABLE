#!/usr/bin/env python3
"""bd data adapter for sable-telemetry (SABLE-8b41.2).

Locked contract (.claude/sable/state/planning/SABLE-8b41/architecture.json,
"Three source adapters behind a thin core"): this module owns the ONE
research trap that lives in bd's query shape, so no caller re-derives it.

RESEARCH TRAP: `bd list --json` defaults to open+deferred only, silently
hiding closed beads (1000+ in the live corpus) -- a naive query yields a
flat trend. `--all` is the fix; `--limit 0` closes a second, related trap
(the default 50-row cap, which would silently truncate the very corpus
--all just stopped hiding).

Per-bead timestamp semantics (BUILD spec, SABLE-8b41.2):
  created_at  -- always present.
  closed_at   -- 100% populated on closed beads.
  started_at  -- OPTIONAL. Only set by `sable-spawn-worker --claim`
                 (bd update <id> --claim); most manager-closed beads never
                 go through that path, so callers MUST treat a missing
                 started_at as "excluded from the dispatched subset", never
                 as "started at creation" (see S2 cycle-split scoping in
                 architecture.json -- this adapter only surfaces the value,
                 it does not interpret it).

Cross-tracker ids (SABLE-jb3o): ids from other trackers (e.g.
market-brief-*) can't be resolved against this bd DB and must never leak
into telemetry as if they were native beads -- filtered even though a
full-corpus scan found zero present today (research.json), because the
guard is for corpus growth, not today's count.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

BD_BIN = "bd"

# Cross-tracker id prefixes to exclude (SABLE-jb3o). Extend this tuple, never
# hardcode a second copy of the check, if another external tracker surfaces.
CROSS_TRACKER_ID_PREFIXES = ("market-brief-",)


@dataclass(frozen=True)
class BeadRecord:
    """One bead's lifecycle timestamps, as read from bd -- the typed record
    this adapter returns instead of a loose dict (Primitive Obsession
    advisory, architecture.json smell_risks)."""

    id: str
    status: str
    created_at: str | None
    closed_at: str | None
    started_at: str | None


def build_bd_list_args(status: str | None = None) -> list[str]:
    """The bd query args this adapter runs, isolated from subprocess
    execution so the query shape is unit-testable without a real bd
    process. --all + --limit 0 is the fix for the bd_source research trap;
    --status further narrows a real query to a status subset without ever
    dropping --all's completeness contract."""
    args = ["list", "--all", "--json", "--limit", "0"]
    if status:
        args += ["--status", status]
    return args


def _is_cross_tracker_id(bead_id: str) -> bool:
    return any(bead_id.startswith(prefix) for prefix in CROSS_TRACKER_ID_PREFIXES)


def _to_bead_record(raw: dict) -> BeadRecord:
    return BeadRecord(
        id=raw["id"],
        status=raw.get("status", ""),
        created_at=raw.get("created_at"),
        closed_at=raw.get("closed_at"),
        started_at=raw.get("started_at"),
    )


def fetch_bead_records(
    status: str | None = None, cwd: str | None = None
) -> list[BeadRecord]:
    """Query bd for the full open+closed corpus (or a --status subset) and
    return typed BeadRecords, explicitly marking missing started_at as None
    and dropping cross-tracker ids before they can reach any metric
    computation.

    `cwd` lets callers (and tests) point this at a specific bd workspace via
    bd's normal auto-discovery of .beads/ from the working directory,
    instead of always querying whatever DB the current process happens to
    be sitting in.
    """
    result = subprocess.run(
        [BD_BIN, *build_bd_list_args(status)],
        capture_output=True,
        text=True,
        check=True,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
    )
    raw_records = json.loads(result.stdout)
    return [
        _to_bead_record(raw)
        for raw in raw_records
        if not _is_cross_tracker_id(raw["id"])
    ]
