#!/usr/bin/env python3
"""Core library for sable-telemetry (SABLE-8b41.1 foundation).

Holds the two things every later SABLE-8b41 child depends on so they don't
each reinvent (or drift on) them:

  * ORIGIN_LABELS — the single-source origin: taxonomy. Both this tool and
    hooks/bead-description-gate.sh read it from here (via the CLI's
    --print-origin-labels accessor) rather than hardcoding a second copy —
    the Shotgun Surgery risk the SABLE-8b41 architecture review flagged.
  * MetricRecord — the typed record adapters (SABLE-8b41.2/.3/.4: bd/git/gh
    sources) populate and formatters (SABLE-8b41.8) consume, instead of
    passing loose dicts/tuples between them (the Primitive Obsession advisory
    from the same review).

build_shift_report/build_trend_report are foundation stubs: they return
empty, well-typed reports so the CLI is exercisable end-to-end (parsing,
--json rendering) before any source adapter exists. Real metrics land when
SABLE-8b41.2 (bd), .3 (git), .4 (gh), .5 (burn/trend), and .6 (cycle-split)
are wired in.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# origin: taxonomy — locked in
# .claude/sable/state/planning/SABLE-8b41/architecture.json ("origin: taxonomy
# with single source of truth + soft-nudge integration"). Order is the
# locked contract order, not alphabetical.
ORIGIN_LABELS = (
    "planned",
    "dogfood",
    "recurrence",
    "cross-fleet",
    "operator",
    "followup",
)


def origin_labels() -> tuple[str, ...]:
    """The taxonomy accessor both the CLI (--print-origin-labels) and any
    adapter code call instead of touching ORIGIN_LABELS directly — one named
    entry point to grep for every consumer."""
    return ORIGIN_LABELS


@dataclass(frozen=True)
class MetricRecord:
    """One named metric value flowing from an adapter to a formatter.

    `origin` is validated against ORIGIN_LABELS at construction (see
    __post_init__) so a typo or a hardcoded copy of the taxonomy fails loudly
    at the point of creation instead of silently drifting downstream —
    "taxonomy is read, never hardcoded downstream" (architecture.json
    interface contract)."""

    name: str
    value: float
    unit: str
    origin: str | None = None
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.origin is not None and self.origin not in ORIGIN_LABELS:
            raise ValueError(
                f"MetricRecord.origin {self.origin!r} is not in the "
                f"single-source origin: taxonomy {ORIGIN_LABELS!r}"
            )


def metric_to_dict(m: MetricRecord) -> dict:
    d: dict = {"name": m.name, "value": m.value, "unit": m.unit}
    if m.origin is not None:
        d["origin"] = m.origin
    if m.meta:
        d["meta"] = m.meta
    return d


@dataclass(frozen=True)
class ShiftReport:
    since: str | None
    metrics: tuple[MetricRecord, ...] = ()


@dataclass(frozen=True)
class TrendReport:
    window: str
    metrics: tuple[MetricRecord, ...] = ()


def shift_report_to_dict(r: ShiftReport) -> dict:
    return {"since": r.since, "metrics": [metric_to_dict(m) for m in r.metrics]}


def trend_report_to_dict(r: TrendReport) -> dict:
    return {"window": r.window, "metrics": [metric_to_dict(m) for m in r.metrics]}


def build_shift_report(since: str | None = None) -> ShiftReport:
    """Foundation stub — no source adapter is wired yet (SABLE-8b41.2/.3/.4),
    so this always returns an empty, well-typed report."""
    return ShiftReport(since=since, metrics=())


def build_trend_report(window: str = "7d") -> TrendReport:
    """Foundation stub — see build_shift_report."""
    return TrendReport(window=window, metrics=())
