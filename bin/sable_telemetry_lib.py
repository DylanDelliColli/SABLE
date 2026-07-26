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

import json
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from sable_telemetry_bd_source import BeadRecord
    from sable_telemetry_git_source import MergeEvent
    from sable_telemetry_gh_source import CiRunEvent

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


def build_shift_report(
    bead_records: Iterable["BeadRecord"] = (),
    merge_events: Iterable["MergeEvent"] = (),
    ci_events: Iterable["CiRunEvent"] = (),
    since: str | None = None,
    tz: tzinfo | None = None,
) -> ShiftReport:
    """Compose one shift's metrics (SABLE-8b41.8 surfacing) from already
    -fetched adapter records: closes/intake/net-burn for the window plus the
    S2 cycle-split denominator (always printed — see
    denominator_invariant_string — even when the window has no closed beads
    at all, per the same "never silently omit" philosophy as the S4 gap-day
    backfill).

    The window defaults to today's host-local calendar day (`since=None`);
    `since` narrows it to activity at/after that instant — either a full
    ISO-8601 timestamp or a bare YYYY-MM-DD date (host-local midnight of
    that date). Adapters are the caller's job to fetch (main() in
    bin/sable-telemetry); this function only composes what it is given, so
    it stays unit-testable against synthetic records with no bd/git/gh
    subprocess in the loop."""
    records = list(bead_records)
    window_start = _resolve_shift_window_start(since, tz)

    intake_in_window = [b for b in records if _in_window(b.created_at, window_start)]
    closed_in_window = [
        b for b in records
        if b.status == "closed" and _in_window(b.closed_at, window_start)
    ]

    closes_count = len(closed_in_window)
    intake_count = len(intake_in_window)

    cycle = build_cycle_split_report(closed_in_window, merge_events, ci_events)

    metrics = [
        MetricRecord(name="closes", value=float(closes_count), unit="count"),
        MetricRecord(name="intake", value=float(intake_count), unit="count"),
        MetricRecord(
            name="net_burn", value=float(net_burn(closes_count, intake_count)),
            unit="count",
        ),
        MetricRecord(
            name="cycle_split_dispatched", value=float(cycle.dispatched_count),
            unit="count",
            meta={"closed_count": cycle.closed_count, "note": cycle.denominator_note},
        ),
    ]
    share = cycle.merge_queue_wait_share
    if share is not None:
        metrics.append(MetricRecord(
            name="merge_queue_wait_share", value=share, unit="fraction",
            meta={"note": cycle.denominator_note},
        ))

    return ShiftReport(since=since, metrics=tuple(metrics))


_TREND_WINDOW_RE = re.compile(r"^(\d+)d$")


def parse_trend_window(window: str) -> int:
    """Parse a trend window like '7d' into its day count. Only day-suffixed
    windows are supported — the architecture's trend spine is calendar
    days (host-local), not hours/weeks."""
    m = _TREND_WINDOW_RE.match(window)
    if not m:
        raise ValueError(f"unsupported trend window {window!r}; expected e.g. '7d'")
    return int(m.group(1))


def build_trend_report(
    window: str = "7d",
    bead_records: Iterable["BeadRecord"] = (),
    tz: tzinfo | None = None,
) -> TrendReport:
    """Compose the S4 trend report: the full backfilled daily-burn spine
    (build_daily_burn_series — zero-activity gap days included, never
    skipped) truncated to the trailing `window` days, plus a total net-burn
    summary across whatever days remain after truncation."""
    n_days = parse_trend_window(window)
    series = build_daily_burn_series(bead_records, tz=tz)
    if n_days > 0 and len(series) > n_days:
        series = series[-n_days:]

    metrics = [
        MetricRecord(
            name="daily_net_burn", value=float(day.net_burn), unit="count/day",
            meta={
                "date": day.date, "closes": day.closes, "intake": day.intake,
                "shift_report_ids": list(day.shift_report_ids),
            },
        )
        for day in series
    ]
    total_closes = sum(d.closes for d in series)
    total_intake = sum(d.intake for d in series)
    metrics.append(MetricRecord(
        name="total_net_burn", value=float(net_burn(total_closes, total_intake)),
        unit="count",
        meta={"days": len(series), "total_closes": total_closes, "total_intake": total_intake},
    ))

    return TrendReport(window=window, metrics=tuple(metrics))


# ---------------------------------------------------------------------------
# S2: dispatch->closed cycle-split + denominator invariant (SABLE-8b41.6)
#
# ARCHITECTURE INVARIANT (architecture.json, "S2 cycle-split scoping + explicit
# denominator invariant"): cycle-time is computed ONLY over the
# worker-dispatched subset -- closed beads whose bd started_at is present
# (sable_telemetry_bd_source.BeadRecord.started_at). ~61% of closed beads are
# manager-closed and never went through `sable-spawn-worker --claim`, so they
# never traversed push->CI->merge; silently folding them into a merge-queue
# bottleneck number would bias it. Every report MUST surface the exact
# denominator string below so that bias is visible, never silent.
# ---------------------------------------------------------------------------


def denominator_invariant_string(dispatched_count: int, closed_count: int) -> str:
    """The exact, mandatory sentence every cycle-split output must print (S2
    architecture invariant). `dispatched_count` (N) is the closed beads with a
    bd started_at; `closed_count` (M) is all closed beads in the corpus."""
    return f"{dispatched_count} of {closed_count} closed beads had dispatch timestamps"


@dataclass(frozen=True)
class CycleSplitEntry:
    """One worker-dispatched, merged bead's dispatch->merge cycle, split into
    in-worker development (started_at -> closed_at, the worker's own active
    span: develop, test, push, self-close) and post-push merge-queue-wait
    (closed_at -> the merge-preview commit's committer timestamp from
    git_source, i.e. ci-verify running plus Chuck's serial merge queue)."""

    bead_id: str
    started_at: str
    closed_at: str
    merged_at: str
    in_worker_seconds: float
    merge_queue_wait_seconds: float
    ci_duration_seconds: float | None = None

    @property
    def total_seconds(self) -> float:
        return self.in_worker_seconds + self.merge_queue_wait_seconds


@dataclass(frozen=True)
class CycleSplitReport:
    """S2 report: the denominator invariant plus the per-bead split for
    whichever dispatched beads also have a resolved merge-preview commit
    (a dispatched bead not yet merged contributes to the denominator but has
    no entry -- there is no merge timestamp to split against yet)."""

    dispatched_count: int
    closed_count: int
    entries: tuple[CycleSplitEntry, ...]

    @property
    def denominator_note(self) -> str:
        return denominator_invariant_string(self.dispatched_count, self.closed_count)

    @property
    def merge_queue_wait_share(self) -> float | None:
        """Fraction of total dispatch->merge time spent waiting post-push
        (ci-verify + merge queue) rather than in worker development. None
        when no entry has a resolved, positive-duration cycle to divide by."""
        total = sum(e.total_seconds for e in self.entries)
        if total <= 0:
            return None
        wait = sum(e.merge_queue_wait_seconds for e in self.entries)
        return wait / total


def _parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def build_cycle_split_report(
    bead_records: Iterable["BeadRecord"],
    merge_events: Iterable["MergeEvent"],
    ci_events: Iterable["CiRunEvent"] = (),
) -> CycleSplitReport:
    """Join bd_source (started_at/closed_at), git_source (merge-commit ts),
    and gh_source (CI duration) by bead id into the S2 cycle-split report.

    Scoping (architecture invariant): `closed_count` (M) is every closed bead
    in `bead_records`; `dispatched_count` (N) is the subset with a non-None
    started_at. Only that dispatched subset can ever produce a
    CycleSplitEntry, and only once a matching merge_events record resolves
    the merge timestamp -- a dispatched-but-not-yet-merged bead still counts
    toward N/M but has no entry.
    """
    closed = [b for b in bead_records if b.status == "closed" and b.closed_at is not None]
    closed_count = len(closed)
    dispatched = [b for b in closed if b.started_at is not None]
    dispatched_count = len(dispatched)

    merge_by_bead = {m.bead_id: m for m in merge_events}
    ci_by_bead = {c.bead_id: c for c in ci_events}

    entries = []
    for bead in dispatched:
        merge = merge_by_bead.get(bead.id)
        if merge is None:
            continue
        started = _parse_iso(bead.started_at)
        closed_ts = _parse_iso(bead.closed_at)
        merged_ts = _parse_iso(merge.committed_at)
        ci = ci_by_bead.get(bead.id)
        entries.append(
            CycleSplitEntry(
                bead_id=bead.id,
                started_at=bead.started_at,
                closed_at=bead.closed_at,
                merged_at=merge.committed_at,
                in_worker_seconds=(closed_ts - started).total_seconds(),
                merge_queue_wait_seconds=(merged_ts - closed_ts).total_seconds(),
                ci_duration_seconds=ci.duration_seconds if ci is not None else None,
            )
        )

    return CycleSplitReport(
        dispatched_count=dispatched_count,
        closed_count=closed_count,
        entries=tuple(entries),
    )


# ---------------------------------------------------------------------------
# S1/S4: net burn + host-local calendar-day bucketing + backfilled trend +
# shift-report overlays (SABLE-8b41.5)
#
# ARCHITECTURE DECISION (architecture.json, "Shift boundary: calendar-day
# spine (host-local TZ) + shift-report overlays"): trend buckets are
# calendar days boundaried at HOST-LOCAL midnight -- NOT UTC -- so an
# evening EDT session lands in one bucket instead of splitting across two
# UTC days. shift-report beads overlay the resulting spine as timeline
# EVENT MARKERS; they are never the boundary itself (that alternative was
# explicitly rejected). This section is pure computation -- CLI formatting
# and the auto-filed shift-ledger bead are SABLE-8b41.8's job.
# ---------------------------------------------------------------------------

# shift-report bead titles observed in the live corpus (case- and
# punctuation-varied by whichever agent filed them): "[SHIFT REPORT] ...",
# "SHIFT REPORT lincoln ...", "shift-report: tarzan ...". Matched at the
# start of the (stripped) title only, so a bead that merely mentions a
# shift report in passing is not swept in.
SHIFT_REPORT_TITLE_RE = re.compile(r"^\[?shift[\s-]report\]?:?", re.IGNORECASE)


def is_shift_report_bead(title: str | None) -> bool:
    """Whether `title` matches the shift-report bead naming convention --
    the overlay-marker detector for build_daily_burn_series. None/empty
    titles (a BeadRecord with no title populated) are never a match."""
    if not title:
        return False
    return bool(SHIFT_REPORT_TITLE_RE.match(title.strip()))


def day_bucket(iso_ts: str, tz: tzinfo | None = None) -> str:
    """The host-local calendar-day key (YYYY-MM-DD) `iso_ts` falls into.

    `tz` overrides the resolved zone (tests only, for determinism
    independent of the host running them); the production path (tz=None)
    resolves the true system-local zone via datetime.astimezone(), matching
    the architecture's HOST-LOCAL (not UTC) contract."""
    return _parse_iso(iso_ts).astimezone(tz).date().isoformat()


def net_burn(closes: int, intake: int) -> int:
    """BUILD spec: net burn for one calendar-day bucket = closes - intake."""
    return closes - intake


@dataclass(frozen=True)
class DayBucket:
    """One host-local calendar day's burn numbers, plus any shift-report
    beads created that day as overlay event markers (never the boundary)."""

    date: str
    closes: int
    intake: int
    shift_report_ids: tuple[str, ...] = ()

    @property
    def net_burn(self) -> int:
        return net_burn(self.closes, self.intake)


def build_daily_burn_series(
    bead_records: Iterable["BeadRecord"], tz: tzinfo | None = None
) -> tuple[DayBucket, ...]:
    """Backfilled, host-local calendar-day burn series: closes/day (from
    closed_at) and intake/day (from created_at, every bead regardless of
    current status) with net burn = closes - intake, oldest day first.

    Backfillable from bd history alone (S4): every day strictly between the
    earliest and latest day with recorded activity is present in the
    result, zero-filled if bd shows no created/closed activity that day --
    a missing key would otherwise look identical to "no activity" instead
    of "not computed". Returns () when `bead_records` carries no
    created_at/closed_at at all (nothing to backfill between).

    Shift-report beads (is_shift_report_bead on BeadRecord.title) are
    overlaid onto the day they were created as event markers in
    `shift_report_ids` -- they still count toward that day's intake like
    any other bead; only the marker list is special-cased, never the day
    boundary itself.
    """
    closes: dict[str, int] = defaultdict(int)
    intake: dict[str, int] = defaultdict(int)
    markers: dict[str, list[str]] = defaultdict(list)

    for bead in bead_records:
        if bead.created_at:
            day = day_bucket(bead.created_at, tz)
            intake[day] += 1
            if is_shift_report_bead(getattr(bead, "title", None)):
                markers[day].append(bead.id)
        if bead.status == "closed" and bead.closed_at:
            closes[day_bucket(bead.closed_at, tz)] += 1

    known_days = set(closes) | set(intake)
    if not known_days:
        return ()

    start = date.fromisoformat(min(known_days))
    end = date.fromisoformat(max(known_days))

    series = []
    day = start
    while day <= end:
        key = day.isoformat()
        series.append(
            DayBucket(
                date=key,
                closes=closes.get(key, 0),
                intake=intake.get(key, 0),
                shift_report_ids=tuple(sorted(markers.get(key, ()))),
            )
        )
        day += timedelta(days=1)
    return tuple(series)


# ---------------------------------------------------------------------------
# Surfacing: --shift/--trend output formatting + auto-filed shift-telemetry
# ledger bead (SABLE-8b41.8)
#
# ARCHITECTURE (architecture.json, "Surfacing: CLI engine + auto-filed
# shift-ledger bead"): one compute path (build_shift_report/build_trend_report
# above), two surfaces -- pullable stdout/--json (format_* below) and a
# durable ledger bead (file_shift_ledger_bead). SABLE-sn20 (session-end
# protocol) is not live yet, so the ledger is filed via an explicit `--file`
# flag rather than an automatic wind-down trigger; the sn20-integrated
# auto-trigger is a follow-up once sn20 lands.
# ---------------------------------------------------------------------------


def _resolve_shift_window_start(since: str | None, tz: tzinfo | None) -> datetime:
    """The shift window's start instant. `since=None` means today's
    host-local midnight (the same HOST-LOCAL convention as day_bucket,
    architecture.json's Shift-boundary decision); `since` given accepts
    either a full ISO-8601 timestamp or a bare YYYY-MM-DD date (that date's
    host-local midnight)."""
    now = datetime.now(tz) if tz is not None else datetime.now().astimezone()
    if since is None:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        return _parse_iso(since)
    except ValueError:
        d = date.fromisoformat(since)
        return datetime(d.year, d.month, d.day, tzinfo=now.tzinfo)


def _in_window(ts: str | None, window_start: datetime) -> bool:
    """Whether ISO timestamp `ts` falls at/after `window_start`. A missing
    timestamp (None) is never in-window -- it has no activity to attribute."""
    if ts is None:
        return False
    return _parse_iso(ts) >= window_start


def format_metrics_table(metrics: tuple[MetricRecord, ...]) -> str:
    """The --shift/--trend human-readable table: one row per metric, with
    any denominator/invariant note appended as a trailing comment so it is
    never silently dropped from the human path (architecture.json S2
    invariant -- the note must render wherever a cycle-time-derived figure
    does). Empty input renders an explicit placeholder rather than a blank
    string, matching the "never silently omit" convention the S4 gap-day
    backfill established."""
    if not metrics:
        return "(no metrics)"
    rows = []
    for m in metrics:
        row = f"{m.name:<28} {m.value!s:>14} {m.unit}"
        note = m.meta.get("note") if m.meta else None
        if note:
            row += f"   # {note}"
        rows.append(row)
    return "\n".join(rows)


def format_shift_report_human(report: ShiftReport) -> str:
    scope = report.since or "today (host-local)"
    return f"sable-telemetry --shift (since={scope})\n" + format_metrics_table(report.metrics)


def format_trend_report_human(report: TrendReport) -> str:
    return f"sable-telemetry --trend ({report.window})\n" + format_metrics_table(report.metrics)


# shift-telemetry ledger beads are a distinct artifact from the human
# "[SHIFT REPORT] ..." overlay-marker beads SHIFT_REPORT_TITLE_RE matches
# (build_daily_burn_series's event markers) -- this prefix must never match
# that regex, or the ledger beads this bead auto-files would start feeding
# back into the trend spine's overlay markers as if a human had filed a
# shift report.
SHIFT_TELEMETRY_LABEL = "shift-telemetry"
SHIFT_TELEMETRY_TITLE_PREFIX = "shift-telemetry:"


def build_shift_ledger_title(report: ShiftReport, tz: tzinfo | None = None) -> str:
    scope = report.since or (
        datetime.now(tz) if tz is not None else datetime.now().astimezone()
    ).date().isoformat()
    return f"{SHIFT_TELEMETRY_TITLE_PREFIX} shift ledger ({scope})"


def build_shift_ledger_description(report: ShiftReport) -> str:
    return (
        "Auto-filed shift-telemetry ledger -- the lightweight wind-down "
        "convention SABLE-8b41.8 ships ahead of SABLE-sn20's session-end "
        "auto-trigger (architecture.json 'Surfacing' decision).\n\n"
        f"{format_shift_report_human(report)}\n\n"
        f"JSON:\n{json.dumps(shift_report_to_dict(report), indent=2)}"
    )


def file_shift_ledger_bead(
    report: ShiftReport,
    cwd: str | None = None,
    env: dict | None = None,
    origin: str = "operator",
    bd_bin: str = "bd",
) -> str:
    """File `report` as a durable shift-telemetry ledger bead via a real
    `bd create` -- SABLE-8b41.8's wind-down-convention fallback while
    SABLE-sn20's session-end protocol isn't live yet. Returns the new bead's
    id.

    `origin` defaults to "operator" (a human ran `--file`); pass
    "planned" once a future sn20-integrated auto-trigger calls this
    programmatically instead. `cwd`/`env` let callers (and tests) target an
    isolated sandbox DB instead of always writing into whatever DB the
    current process happens to be sitting in -- SABLE-j0vr: a prior test
    suite's real bd-invoking test filed durable beads into the production
    pool by skipping this isolation."""
    if origin not in ORIGIN_LABELS:
        raise ValueError(
            f"origin {origin!r} is not in the single-source origin: taxonomy "
            f"{ORIGIN_LABELS!r}"
        )
    title = build_shift_ledger_title(report)
    description = build_shift_ledger_description(report)
    result = subprocess.run(
        [bd_bin, "create", "--title", title, "--type=task", "--priority=3",
         "--labels", f"{SHIFT_TELEMETRY_LABEL},origin:{origin}",
         "--description", description, "--json"],
        capture_output=True, text=True, check=True, cwd=cwd, env=env,
        stdin=subprocess.DEVNULL,
    )
    return json.loads(result.stdout)["id"]
