#!/usr/bin/env python3
"""Bounded merge-train cycles for a rolling worker queue.

The lower-level batch modules already know how to admit members, fold a
combined object, read CI, land atomically, and diagnose a red batch.  This
Module is the small Interface that composes those capabilities.  It owns no
second implementation of any of them.

A cycle seals only the discovered snapshot supplied to that invocation (eight
members by default).  ``batch-drain`` re-discovers after each successful
landing, so workers that finish while combined CI is in flight enter the next
cycle rather than mutating the object already under test.  The drain is
bounded, not a daemon: an operator or timer can call it repeatedly without
creating another integration-ref writer.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import sable_batch_admission_lib as admission
import sable_batch_fold_lib as fold_lib
import sable_gate_classify_lib as classify
import sable_gate_git_lib as git_lib
import sable_gate_preview_lib as preview
import sable_gate_promote_lib as promote
import sable_snapshot_lib as snapshot_lib
from sable_gate_classify_lib import GateError


DEFAULT_MAX_MEMBERS = 8
DEFAULT_MAX_CYCLES = 4
MIN_BATCH_MEMBERS = 2
READY_STATUS = "closed"
WORKER_BRANCH_RE = re.compile(r"^wk-[A-Za-z0-9._/-]+$")
PAIR_SPLIT_RE = re.compile(r"[,\s]+")
LAND_TOGETHER = "land-together"

BATCH_DRAIN_RECORD_FILE = "batch-drain-records.jsonl"
BATCH_DRAIN_RECORD_SCHEMA_VERSION = 1

CYCLE_LANDED = "landed"
CYCLE_RED = "red"
CYCLE_NOT_FORMED = "not_formed"
CYCLE_REFORM = "reform"
CYCLE_FELL_BACK_SERIAL = "fell_back_serial"


@dataclass(frozen=True)
class BatchCycleResult:
    """Observable result of one sealed cycle."""

    outcome: str
    members: tuple[str, ...]
    excluded: tuple[admission.Exclusion, ...]
    deferred_specs: tuple[str, ...]
    combined_ref: str = ""
    fold_tip: str = ""
    reason: str = ""
    ci_rounds: int = 0

    @property
    def exit_code(self) -> int:
        if self.outcome == CYCLE_LANDED:
            return classify.EXIT_OK
        if self.outcome == CYCLE_RED:
            return classify.EXIT_RED
        if self.outcome in (CYCLE_REFORM, CYCLE_FELL_BACK_SERIAL):
            return classify.EXIT_BASE_MOVED
        return classify.EXIT_PRECONDITION


@dataclass(frozen=True)
class QueueCandidate:
    """One durable Beads-to-origin join, ordered by readiness time."""

    branch: str
    bead: str
    ready_at: str
    tip_sha: str = ""
    pair_beads: tuple[str, ...] = ()

    @property
    def spec(self) -> str:
        return f"{self.branch}:{self.bead}"


@dataclass(frozen=True)
class QueueExclusion:
    """A branch omitted before admission, with the fail-closed reason."""

    branch: str
    reason: str


@dataclass(frozen=True)
class QueueSnapshot:
    """A pinned, deterministic view of the rolling queue."""

    base: str
    base_sha: str
    observed_at: float
    candidates: tuple[QueueCandidate, ...]
    excluded: tuple[QueueExclusion, ...]

    @property
    def depth(self) -> int:
        return len(self.candidates)


@dataclass(frozen=True)
class BatchDrainRecord:
    """One bounded drain's durable throughput observation."""

    started_at: float
    finished_at: float
    initial_queue_depth: int
    final_queue_depth: int
    arrivals: int
    landed_members: int
    cycles: int
    max_members: int
    ci_rounds: int
    outcomes: tuple[str, ...]
    batch_sizes: tuple[int, ...]

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    @property
    def arrival_rate_per_minute(self) -> float:
        return (self.arrivals * 60.0 / self.duration_seconds
                if self.duration_seconds else 0.0)

    @property
    def drain_rate_per_minute(self) -> float:
        return (self.landed_members * 60.0 / self.duration_seconds
                if self.duration_seconds else 0.0)

    @property
    def batch_fill(self) -> float:
        return (sum(self.batch_sizes) / (len(self.batch_sizes) * self.max_members)
                if self.batch_sizes and self.max_members else 0.0)

    def to_dict(self) -> dict:
        return {
            "schema": BATCH_DRAIN_RECORD_SCHEMA_VERSION,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "initial_queue_depth": self.initial_queue_depth,
            "final_queue_depth": self.final_queue_depth,
            "arrivals": self.arrivals,
            "landed_members": self.landed_members,
            "cycles": self.cycles,
            "max_members": self.max_members,
            "ci_rounds": self.ci_rounds,
            "outcomes": list(self.outcomes),
            "batch_sizes": list(self.batch_sizes),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BatchDrainRecord":
        return cls(
            started_at=float(data.get("started_at") or 0.0),
            finished_at=float(data.get("finished_at") or 0.0),
            initial_queue_depth=int(data.get("initial_queue_depth") or 0),
            final_queue_depth=int(data.get("final_queue_depth") or 0),
            arrivals=int(data.get("arrivals") or 0),
            landed_members=int(data.get("landed_members") or 0),
            cycles=int(data.get("cycles") or 0),
            max_members=int(data.get("max_members") or 0),
            ci_rounds=int(data.get("ci_rounds") or 0),
            outcomes=tuple(data.get("outcomes") or ()),
            batch_sizes=tuple(int(v) for v in (data.get("batch_sizes") or ())),
        )


@dataclass(frozen=True)
class BatchDrainResult:
    """Operator-facing result of repeated sealed cycles."""

    cycles: tuple[BatchCycleResult, ...]
    snapshots: tuple[QueueSnapshot, ...]
    arrivals: int
    landed_members: int
    stopped_reason: str
    record: BatchDrainRecord | None = None

    @property
    def initial_snapshot(self) -> QueueSnapshot:
        return self.snapshots[0]

    @property
    def final_snapshot(self) -> QueueSnapshot:
        return self.snapshots[-1]

    @property
    def exit_code(self) -> int:
        if not self.cycles:
            return classify.EXIT_OK
        return self.cycles[-1].exit_code


def _bd(repo: str, *args: str):
    """Beads read seam; all queue discovery is read-only."""
    return git_lib._run(
        git_lib._tool("SABLE_MG_BD", "bd") + list(args),
        cwd=repo, check=False)


def _parse_records(text: str) -> list[dict] | None:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or any(
            not isinstance(record, dict) for record in data):
        return None
    return data


def _branch_records(repo: str, branch: str) -> tuple[list[dict], str]:
    """Every exact metadata join for a worker branch, or a loud read error."""
    try:
        cp = _bd(
            repo, "list", "--status", "all", "--metadata-field",
            f"branch={branch}", "--json", "--limit", "0")
    except OSError as exc:
        return [], f"Beads query failed: {exc}"
    if cp.returncode != 0:
        return [], (
            f"Beads query exited {cp.returncode}: "
            f"{cp.stdout.strip()[:240] or '<no output>'}")
    records = _parse_records(cp.stdout)
    if records is None:
        return [], "Beads query returned unreadable JSON"
    exact = []
    for record in records:
        metadata = record.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            return [], (
                f"Beads record {record.get('id') or '<unknown>'} has "
                "malformed metadata")
        if isinstance(metadata, dict) and metadata.get("branch") == branch:
            exact.append(record)
    return exact, ""


def _split_ids(raw) -> tuple[str, ...]:
    if not isinstance(raw, str):
        return ()
    return tuple(sorted({
        token.strip() for token in PAIR_SPLIT_RE.split(raw)
        if token.strip()
    }))


def _candidate_from_records(
        branch: str, records: list[dict], tip_sha: str = "",
        tip_ready_at: str = "",
        ) -> tuple[QueueCandidate | None, str]:
    """Resolve one branch to exactly one ready work bead.

    Holds are branch-wide: any joined bead can stop the branch.  The work-bead
    discriminator is the dispatch-written footprint metadata, which excludes
    observation/coordination beads without guessing from titles or labels.
    Multiple qualifying work beads are ambiguous and fail closed.
    """
    for record in records:
        metadata = record.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            return None, (
                f"Beads record {record.get('id') or '<unknown>'} has "
                "malformed metadata")
        hold = (metadata or {}).get("hold")
        if isinstance(hold, str) and hold.strip():
            return None, (
                f"branch is held by {record.get('id') or '<unknown bead>'}: "
                f"{hold.strip()}")

    work_records = []
    for record in records:
        metadata = record.get("metadata")
        if not isinstance(metadata, dict):
            continue
        if record.get("status") != READY_STATUS:
            continue
        if "footprint_writes" not in metadata:
            continue
        if "footprint_reads_declared" not in metadata:
            continue
        work_records.append(record)

    if not work_records:
        return None, (
            "no closed work bead with exact branch metadata and "
            "dispatch-written read/write footprints")
    if len(work_records) != 1:
        ids = ", ".join(sorted(str(r.get("id") or "<unknown>")
                               for r in work_records))
        return None, f"ambiguous branch: {len(work_records)} work beads qualify ({ids})"

    record = work_records[0]
    bead = str(record.get("id") or "").strip()
    closed_at = record.get("closed_at")
    ready_value = closed_at or tip_ready_at
    ready_at = str(ready_value or "").strip()
    if not bead or not ready_at:
        return None, (
            "work bead is missing id or no readiness timestamp can be "
            "derived from closed_at/branch tip")
    try:
        parsed_ready = datetime.fromisoformat(
            ready_at.replace("Z", "+00:00"))
        if parsed_ready.tzinfo is None:
            parsed_ready = parsed_ready.replace(tzinfo=timezone.utc)
        parsed_ready.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None, (
            f"work bead {bead} has invalid ready_at={ready_at!r}; "
            "queue order is unknowable")
    metadata = record["metadata"]
    pair_beads = set(_split_ids(metadata.get("landing_pair")))
    if metadata.get("serialize_kind") == LAND_TOGETHER:
        pair_beads.update(_split_ids(metadata.get("serialize_with")))
    pair_beads.discard(bead)
    return QueueCandidate(
        branch, bead, ready_at, tip_sha, tuple(sorted(pair_beads))), ""


def _candidate_sort_key(candidate: QueueCandidate) -> tuple:
    """Chronological readiness; branch/bead make ties total."""
    ready = datetime.fromisoformat(
        candidate.ready_at.replace("Z", "+00:00"))
    if ready.tzinfo is None:
        ready = ready.replace(tzinfo=timezone.utc)
    return ready.timestamp(), candidate.branch, candidate.bead


def _list_worker_branches(repo: str, remote: str) -> list[str]:
    cp = git_lib._git(
        repo, "for-each-ref", "--format=%(refname:short)",
        f"refs/remotes/{remote}/wk-*", check=False)
    if cp.returncode != 0:
        raise GateError(
            classify.EXIT_PRECONDITION,
            f"could not enumerate {remote}/wk-* branches: "
            f"{cp.stdout.strip()[:300]}")
    prefix = f"{remote}/"
    branches = []
    for raw in cp.stdout.splitlines():
        short = raw.strip()
        branch = short[len(prefix):] if short.startswith(prefix) else short
        if WORKER_BRANCH_RE.fullmatch(branch):
            branches.append(branch)
    return sorted(set(branches))


def _tip_timestamp(repo: str, tip_sha: str) -> str:
    cp = git_lib._git(
        repo, "show", "-s", "--format=%cI", tip_sha, check=False)
    return cp.stdout.strip() if cp.returncode == 0 else ""


def discover_queue(
        base: str | None, repo: str, remote: str) -> QueueSnapshot:
    """Discover oldest-ready unlanded workers from pinned git + Beads state."""
    base = git_lib.resolve_base(base, repo)
    fetched = git_lib._git(repo, "fetch", "--prune", remote, check=False)
    if fetched.returncode != 0:
        raise GateError(
            classify.EXIT_PRECONDITION,
            f"queue discovery could not refresh {remote}: "
            f"{fetched.stdout.strip()[:300]}")
    base_sha = git_lib.resolve_commit(
        repo, classify.qualify_remote_ref(remote, base))

    candidates: list[QueueCandidate] = []
    excluded: list[QueueExclusion] = []
    for branch in _list_worker_branches(repo, remote):
        branch_ref = classify.qualify_remote_ref(remote, branch)
        ancestry = git_lib._git(
            repo, "merge-base", "--is-ancestor", branch_ref, base_sha,
            check=False)
        if ancestry.returncode == 0:
            continue
        if ancestry.returncode != 1:
            excluded.append(QueueExclusion(
                branch,
                f"could not determine whether branch is already landed "
                f"(merge-base exit {ancestry.returncode})"))
            continue
        try:
            tip_sha = git_lib.resolve_commit(repo, branch_ref)
        except GateError as exc:
            excluded.append(QueueExclusion(
                branch, f"could not pin branch tip: {exc}"))
            continue
        records, error = _branch_records(repo, branch)
        if error:
            excluded.append(QueueExclusion(branch, error))
            continue
        candidate, reason = _candidate_from_records(
            branch, records, tip_sha, _tip_timestamp(repo, tip_sha))
        if candidate is None:
            excluded.append(QueueExclusion(branch, reason))
            continue
        candidates.append(candidate)

    candidates.sort(key=_candidate_sort_key)
    excluded.sort(key=lambda item: item.branch)
    return QueueSnapshot(
        base, base_sha, time.time(), tuple(candidates), tuple(excluded))


def _pair_groups(
        repo: str, candidates: list[admission.Candidate],
        extra_pairs: dict[str, tuple[str, ...]] | None = None,
        ) -> tuple[list[list[admission.Candidate]], list[admission.Exclusion]]:
    """Close declared landing-pair relations over admitted candidates."""
    by_bead = {candidate.bead: candidate for candidate in candidates}
    pair_map: dict[str, set[str]] = {bead: set() for bead in by_bead}
    excluded: list[admission.Exclusion] = []
    invalid: set[str] = set()
    for candidate in candidates:
        counterparts = set(promote.declared_landing_pair(repo, candidate.bead))
        counterparts.update((extra_pairs or {}).get(candidate.bead, ()))
        for counterpart in counterparts:
            if counterpart in by_bead:
                pair_map[candidate.bead].add(counterpart)
                pair_map[counterpart].add(candidate.bead)
                continue
            if promote.bead_landed(repo, counterpart):
                continue
            invalid.add(candidate.bead)
            excluded.append(admission.Exclusion(
                candidate,
                f"landing pair {counterpart} is neither already landed nor "
                "ready in this queue snapshot"))

    groups: list[list[admission.Candidate]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.bead in seen:
            continue
        pending = [candidate.bead]
        group_ids: set[str] = set()
        while pending:
            bead = pending.pop()
            if bead in group_ids:
                continue
            group_ids.add(bead)
            pending.extend(pair_map[bead] - group_ids)
        seen.update(group_ids)
        if group_ids & invalid:
            incomplete = ", ".join(sorted(group_ids & invalid))
            for bead in sorted(group_ids - invalid):
                excluded.append(admission.Exclusion(
                    by_bead[bead],
                    f"landing-pair group is incomplete because {incomplete} "
                    "cannot travel in this snapshot"))
            continue
        groups.append([item for item in candidates if item.bead in group_ids])
    return groups, excluded


def _seal_admitted_members(
        repo: str, admitted: list[admission.Candidate], max_members: int,
        extra_pairs: dict[str, tuple[str, ...]] | None = None,
        ) -> tuple[list[admission.Candidate], tuple[str, ...],
                   tuple[admission.Exclusion, ...]]:
    """Cap by atomic landing groups without splitting the oldest group."""
    groups, pair_exclusions = _pair_groups(repo, admitted, extra_pairs)
    selected: list[admission.Candidate] = []
    deferred: list[str] = []
    cap_reached = False
    for group in groups:
        if len(group) > max_members:
            pair_exclusions.extend(
                admission.Exclusion(
                    candidate,
                    f"landing-pair group has {len(group)} members, larger than "
                    f"--max-members={max_members}")
                for candidate in group)
            continue
        if cap_reached or len(selected) + len(group) > max_members:
            cap_reached = True
            deferred.extend(f"{candidate.branch}:{candidate.bead}"
                            for candidate in group)
            continue
        selected.extend(group)
    return selected, tuple(deferred), tuple(pair_exclusions)


def _validate_exact_verdict_identity(
        verdict: classify.Verdict, combined_ref: str, fold_tip: str) -> None:
    """Refuse a verdict value that describes any object but this cycle's."""
    if (not verdict.complete or verdict.ref != combined_ref
            or verdict.preview_sha != fold_tip):
        raise GateError(
            classify.EXIT_INTEGRITY,
            f"batch cycle verdict does not bind the sealed object: got "
            f"{verdict.ref or '<no-ref>'}/"
            f"{(verdict.preview_sha or '<no-sha>')[:7]} complete={verdict.complete}; "
            f"expected {combined_ref}/{fold_tip[:7]}")


def run_batch_cycle(
        base: str | None, member_specs: list[str], repo: str, remote: str,
        manager: str = "lincoln",
        max_members: int = DEFAULT_MAX_MEMBERS,
        extra_pairs: dict[str, tuple[str, ...]] | None = None,
        refresh_refs: bool = True,
        expected_tips: dict[str, str] | None = None,
        expected_base_sha: str | None = None,
        ) -> BatchCycleResult:
    """Seal, verify, and decide one micro-batch.

    ``member_specs`` is a fixed queue snapshot ordered by priority (normally
    oldest first).  Admission evaluates that snapshot, then the oldest
    admissible landing groups are sealed up to ``max_members``.  This avoids
    an unready oldest item hiding a ready item behind it while still ensuring
    that no arrival after the snapshot can enter the in-flight object.
    ``batch-drain`` supplies exact expected base/member SHAs from its one
    remote refresh; the explicit CLI refreshes refs itself.
    """
    if max_members < MIN_BATCH_MEMBERS:
        raise GateError(
            classify.EXIT_USAGE,
            f"--max-members must be at least {MIN_BATCH_MEMBERS}, got {max_members}")
    if len(member_specs) < MIN_BATCH_MEMBERS:
        raise GateError(
            classify.EXIT_USAGE,
            f"batch-cycle needs at least {MIN_BATCH_MEMBERS} --member values")

    base = git_lib.resolve_base(base, repo)
    if refresh_refs:
        git_lib._git(repo, "fetch", remote, base, check=False)
    base_sha = git_lib.resolve_commit(
        repo, expected_base_sha
        or classify.qualify_remote_ref(remote, base))
    resolved = promote.resolve_batch_members(
        repo, remote, base_sha, member_specs, "batch-cycle",
        refresh_refs=refresh_refs, expected_tips=expected_tips)

    branches = [member.branch for member in resolved]
    tips = [member.tip_sha for member in resolved]
    if len(set(branches)) != len(branches) or len(set(tips)) != len(tips):
        raise GateError(
            classify.EXIT_USAGE,
            "batch-cycle received a duplicate branch or duplicate member tip")
    for member in resolved:
        if len(member.bead_ids) != 1:
            raise GateError(
                classify.EXIT_PRECONDITION,
                f"{member.branch} must name exactly one work bead for automatic "
                f"batch admission; got {member.bead_ids or '(none)'}")

    candidates = [
        admission.Candidate(member.bead_ids[0], member.branch, member.tip_sha)
        for member in resolved
    ]
    admitted = admission.admit_batch(repo, remote, base_sha, candidates)
    sealed, deferred_specs, pair_exclusions = _seal_admitted_members(
        repo, list(admitted.admitted), max_members, extra_pairs)
    all_exclusions = admitted.excluded + pair_exclusions
    admitted_keys = {(candidate.branch, candidate.sha) for candidate in sealed}
    batch_members = [
        member for member in resolved
        if (member.branch, member.tip_sha) in admitted_keys
    ]
    # One canonical order controls both fold content and set identity.
    batch_members.sort(key=lambda member: member.tip_sha)

    if len(batch_members) < MIN_BATCH_MEMBERS:
        names = tuple(member.branch for member in batch_members)
        return BatchCycleResult(
            CYCLE_NOT_FORMED, names, all_exclusions, deferred_specs,
            reason=f"only {len(batch_members)} candidate(s) survived admission; "
                   f"need {MIN_BATCH_MEMBERS} for a batch")

    fold_members = [
        fold_lib.FoldMember(
            member.branch, member.tip_sha, member.bead_ids[0])
        for member in batch_members
    ]
    fold_tip, combined_ref = fold_lib.push_batch_ref(
        repo, remote, base_sha, fold_members)
    verdict = preview.acquire_verdict(repo, combined_ref, fold_tip)
    _validate_exact_verdict_identity(verdict, combined_ref, fold_tip)
    names = tuple(member.branch for member in batch_members)

    try:
        if verdict.outcome == classify.GREEN and verdict.conclusion != "override":
            budget = promote.combined_tree_budget(
                [list(member.footprint_paths) for member in batch_members])
            landed = promote.land_batch(
                repo, remote, base, base_sha, fold_tip, batch_members,
                budget=budget, combined_ref=combined_ref, verdict=verdict,
                manager=manager)
            outcome = {
                promote.BATCH_OUTCOME_LANDED: CYCLE_LANDED,
                promote.BATCH_OUTCOME_REFORM: CYCLE_REFORM,
                promote.BATCH_OUTCOME_FELL_BACK_SERIAL:
                    CYCLE_FELL_BACK_SERIAL,
            }[landed.outcome]
            return BatchCycleResult(
                outcome, names, all_exclusions, deferred_specs,
                combined_ref, fold_tip, landed.reason, ci_rounds=1)

        if verdict.outcome == classify.RED:
            # The full set is already measured red. Diagnose subsets; nothing
            # in that state machine can write the integration ref.
            diagnosed = promote.bisect_red_batch(
                repo, remote, base_sha, batch_members,
                promote.ci_batch_verify(repo, remote),
                combined_ref=combined_ref, manager=manager)
            return BatchCycleResult(
                CYCLE_RED, names, all_exclusions, deferred_specs,
                combined_ref, fold_tip,
                promote.render_bisection_report(diagnosed),
                ci_rounds=1 + len(diagnosed.rounds))

        raise GateError(
            verdict.exit_code,
            f"batch cycle {combined_ref} got no usable verdict "
            f"({verdict.outcome}/{verdict.conclusion}); nothing landed")
    finally:
        preview.delete_ci_ref(repo, remote, combined_ref)


def _drain_record_path(repo: str | os.PathLike) -> Path:
    override = os.environ.get("SABLE_MG_BATCH_DRAIN_LOG")
    return (Path(override) if override
            else snapshot_lib.ensure_state_dir(repo) / BATCH_DRAIN_RECORD_FILE)


def _stamp_drain_record(repo: str | os.PathLike, record: BatchDrainRecord) -> None:
    """Append under a file lock; a torn observation is never accepted."""
    if not os.environ.get("SABLE_MG_BATCH_DRAIN_LOG") and not Path(repo).is_dir():
        return
    path = _drain_record_path(repo)
    payload = (json.dumps(record.to_dict(), sort_keys=True) + "\n").encode()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        print(
            f"sable-merge-gate: WARNING could not persist batch-drain metrics: {exc}",
            file=sys.stderr)


def read_drain_records(
        repo: str | os.PathLike = ".") -> list[BatchDrainRecord]:
    if not os.environ.get("SABLE_MG_BATCH_DRAIN_LOG") and not Path(repo).is_dir():
        return []
    path = _drain_record_path(repo)
    if not path.is_file():
        return []
    records = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for raw in lines:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                records.append(BatchDrainRecord.from_dict(data))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return records


def batch_metrics(records: list[BatchDrainRecord]) -> dict:
    """Aggregate only recorded facts; no inferred CI or queue events."""
    duration = sum(record.duration_seconds for record in records)
    landed = sum(record.landed_members for record in records)
    arrivals = sum(record.arrivals for record in records)
    cycles = sum(record.cycles for record in records)
    ci_rounds = sum(record.ci_rounds for record in records)
    sizes = [size for record in records for size in record.batch_sizes]
    capacity = sum(
        len(record.batch_sizes) * record.max_members for record in records)
    outcomes: dict[str, int] = {}
    for record in records:
        for outcome in record.outcomes:
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return {
        "drains": len(records),
        "observed_seconds": duration,
        "arrivals": arrivals,
        "landed_members": landed,
        "cycles": cycles,
        "ci_rounds": ci_rounds,
        "arrival_rate_per_minute":
            (arrivals * 60.0 / duration if duration else 0.0),
        "drain_rate_per_minute":
            (landed * 60.0 / duration if duration else 0.0),
        "batch_fill": (sum(sizes) / capacity if capacity else 0.0),
        "average_batch_size": (sum(sizes) / len(sizes) if sizes else 0.0),
        "outcomes": outcomes,
    }


def run_batch_drain(
        base: str | None, repo: str, remote: str, manager: str = "lincoln",
        max_members: int = DEFAULT_MAX_MEMBERS,
        max_cycles: int = DEFAULT_MAX_CYCLES,
        dry_run: bool = False) -> BatchDrainResult:
    """Run repeated sealed cycles, refreshing the queue only between them."""
    if max_members < MIN_BATCH_MEMBERS:
        raise GateError(
            classify.EXIT_USAGE,
            f"--max-members must be at least {MIN_BATCH_MEMBERS}, got {max_members}")
    if max_cycles < 1:
        raise GateError(
            classify.EXIT_USAGE,
            f"--max-cycles must be at least 1, got {max_cycles}")

    started_at = time.time()
    initial = discover_queue(base, repo, remote)
    snapshots = [initial]
    known = {
        (candidate.spec, candidate.tip_sha)
        for candidate in initial.candidates}
    arrivals = 0
    landed_members = 0
    cycles: list[BatchCycleResult] = []
    stopped_reason = ""

    if dry_run:
        return BatchDrainResult(
            (), tuple(snapshots), 0, 0,
            "dry-run: queue discovered; no CI or integration write attempted")

    current = initial
    for _ in range(max_cycles):
        if current.depth < MIN_BATCH_MEMBERS:
            stopped_reason = (
                f"queue has {current.depth} ready candidate(s); "
                f"need {MIN_BATCH_MEMBERS} to form a batch")
            break
        cycle = run_batch_cycle(
            current.base, [candidate.spec for candidate in current.candidates],
            repo, remote, manager, max_members,
            {candidate.bead: candidate.pair_beads
             for candidate in current.candidates if candidate.pair_beads},
            refresh_refs=False,
            expected_tips={
                candidate.branch: candidate.tip_sha
                for candidate in current.candidates},
            expected_base_sha=current.base_sha)
        cycles.append(cycle)
        if cycle.outcome != CYCLE_LANDED:
            stopped_reason = (
                f"stopped after {cycle.outcome}: "
                f"{cycle.reason or 'cycle did not land'}")
            break
        landed_members += len(cycle.members)

        # This is the rolling boundary: discover only after the sealed object
        # finished and landed.  Arrivals can never alter the preceding cycle.
        current = discover_queue(current.base, repo, remote)
        snapshots.append(current)
        now_known = {
            (candidate.spec, candidate.tip_sha)
            for candidate in current.candidates}
        arrivals += len(now_known - known)
        known.update(now_known)
    else:
        stopped_reason = f"bounded drain reached --max-cycles={max_cycles}"

    finished_at = time.time()
    record = BatchDrainRecord(
        started_at=started_at,
        finished_at=finished_at,
        initial_queue_depth=initial.depth,
        final_queue_depth=snapshots[-1].depth,
        arrivals=arrivals,
        landed_members=landed_members,
        cycles=len(cycles),
        max_members=max_members,
        ci_rounds=sum(cycle.ci_rounds for cycle in cycles),
        outcomes=tuple(cycle.outcome for cycle in cycles),
        batch_sizes=tuple(len(cycle.members) for cycle in cycles),
    )
    _stamp_drain_record(repo, record)
    return BatchDrainResult(
        tuple(cycles), tuple(snapshots), arrivals, landed_members,
        stopped_reason, record)


def render_cycle_result(result: BatchCycleResult) -> str:
    """Compact operator report; every exclusion remains explicit."""
    lines = [
        f"BATCH CYCLE {result.outcome.upper()}: "
        f"{len(result.members)} member(s)"
        + (f" on {result.combined_ref}" if result.combined_ref else ""),
    ]
    if result.members:
        lines.append("  members: " + ", ".join(result.members))
    for excluded in result.excluded:
        lines.append(
            f"  excluded {excluded.candidate.branch}: {excluded.reason}")
    if result.deferred_specs:
        lines.append(
            f"  next cycle ({len(result.deferred_specs)} beyond cap): "
            + ", ".join(result.deferred_specs))
    if result.reason:
        lines.append("  " + result.reason.replace("\n", "\n  "))
    return "\n".join(lines)


def render_drain_result(result: BatchDrainResult) -> str:
    initial = result.initial_snapshot
    final = result.final_snapshot
    lines = [
        f"BATCH DRAIN: queue {initial.depth} -> {final.depth}; "
        f"landed={result.landed_members} arrivals={result.arrivals} "
        f"cycles={len(result.cycles)}",
        f"  pinned start: {initial.base}@{initial.base_sha[:7]}",
    ]
    seen_exclusions: set[tuple[str, str]] = set()
    for snapshot in result.snapshots:
        for excluded in snapshot.excluded:
            key = (excluded.branch, excluded.reason)
            if key in seen_exclusions:
                continue
            seen_exclusions.add(key)
            lines.append(f"  queue exclusion {excluded.branch}: {excluded.reason}")
    for index, cycle in enumerate(result.cycles, start=1):
        rendered = render_cycle_result(cycle).replace("\n", "\n    ")
        lines.append(f"  cycle {index}:\n    {rendered}")
    if result.record is not None:
        lines.append(
            f"  observed: {result.record.duration_seconds:.1f}s, "
            f"arrival-rate={result.record.arrival_rate_per_minute:.3f}/min, "
            f"drain-rate={result.record.drain_rate_per_minute:.3f}/min, "
            f"batch-fill={result.record.batch_fill:.1%}, "
            f"CI-rounds={result.record.ci_rounds}")
    lines.append(f"  stop: {result.stopped_reason}")
    return "\n".join(lines)


def format_batch_metrics(report: dict) -> str:
    return (
        f"BATCH METRICS: drains={report['drains']} "
        f"observed={report['observed_seconds']:.1f}s "
        f"arrivals={report['arrivals']} "
        f"landed={report['landed_members']} "
        f"arrival-rate={report['arrival_rate_per_minute']:.3f}/min "
        f"drain-rate={report['drain_rate_per_minute']:.3f}/min\n"
        f"  cycles={report['cycles']} CI-rounds={report['ci_rounds']} "
        f"average-batch={report['average_batch_size']:.2f} "
        f"fill={report['batch_fill']:.1%} outcomes={report['outcomes']}"
    )


def batch_cycle_from_args(args) -> int:
    result = run_batch_cycle(
        args.base, args.members, args.repo, args.remote, args.manager,
        args.max_members)
    print(render_cycle_result(result))
    return result.exit_code


def batch_drain_from_args(args) -> int:
    result = run_batch_drain(
        args.base, args.repo, args.remote, args.manager,
        args.max_members, args.max_cycles, args.dry_run)
    print(render_drain_result(result))
    return result.exit_code


def batch_metrics_from_args(args) -> int:
    report = batch_metrics(read_drain_records(args.repo))
    print(json.dumps(report, sort_keys=True)
          if args.json else format_batch_metrics(report))
    return classify.EXIT_OK


def register_batch_cycle(subparsers) -> None:
    parser = subparsers.add_parser(
        "batch-cycle",
        help="seal up to N ready members, run one combined CI cycle, then "
             "land green atomically or diagnose red without landing")
    parser.add_argument(
        "--base", default=None,
        help="integration branch; same resolution as promote")
    parser.add_argument(
        "--member", action="append", default=[], dest="members", required=True,
        metavar="BRANCH:BEAD",
        help="repeat in queue order; arrivals during this cycle belong to the next")
    parser.add_argument(
        "--max-members", type=int, default=DEFAULT_MAX_MEMBERS,
        help=f"seal at most this many members (default {DEFAULT_MAX_MEMBERS})")
    parser.add_argument(
        "--repo", default=os.environ.get("SABLE_MG_REPO", os.getcwd()))
    parser.add_argument(
        "--remote", default=os.environ.get("SABLE_MG_REMOTE", "origin"))
    parser.add_argument("--manager", default="lincoln")


def register_batch_drain(subparsers) -> None:
    parser = subparsers.add_parser(
        "batch-drain",
        help="discover oldest-ready workers and run bounded sealed cycles")
    parser.add_argument(
        "--base", default=None,
        help="integration branch; same resolution as promote")
    parser.add_argument(
        "--max-members", type=int, default=DEFAULT_MAX_MEMBERS,
        help=f"seal at most this many members (default {DEFAULT_MAX_MEMBERS})")
    parser.add_argument(
        "--max-cycles", type=int, default=DEFAULT_MAX_CYCLES,
        help=f"stop after this many cycles (default {DEFAULT_MAX_CYCLES})")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="discover and report the queue without starting CI or landing")
    parser.add_argument(
        "--repo", default=os.environ.get("SABLE_MG_REPO", os.getcwd()))
    parser.add_argument(
        "--remote", default=os.environ.get("SABLE_MG_REMOTE", "origin"))
    parser.add_argument("--manager", default="lincoln")


def register_batch_metrics(subparsers) -> None:
    parser = subparsers.add_parser(
        "batch-metrics",
        help="summarize durable rolling-queue drain observations")
    parser.add_argument(
        "--repo", default=os.environ.get("SABLE_MG_REPO", os.getcwd()))
    parser.add_argument("--json", action="store_true")


def register_batch_subcommands(subparsers) -> None:
    """One CLI seam for the legacy primitives and the composed cycle."""
    promote.register_batch_subcommands(subparsers)
    register_batch_cycle(subparsers)
    register_batch_drain(subparsers)
    register_batch_metrics(subparsers)


def dispatch_batch_command(args) -> int:
    if args.command == "batch-cycle":
        return batch_cycle_from_args(args)
    if args.command == "batch-drain":
        return batch_drain_from_args(args)
    if args.command == "batch-metrics":
        return batch_metrics_from_args(args)
    return promote.dispatch_batch_command(args)
