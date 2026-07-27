#!/usr/bin/env python3
"""One bounded merge-train cycle for a rolling worker queue.

The lower-level batch modules already know how to admit members, fold a
combined object, read CI, land atomically, and diagnose a red batch.  This
Module is the small Interface that composes those capabilities.  It owns no
second implementation of any of them.

A cycle seals only the member specs supplied to this invocation (eight by
default).  Workers that finish while its combined CI run is in flight simply
remain in the queue for the next invocation.  That is the rolling behavior:
there is no global swarm boundary and no quiescence requirement.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import sable_batch_admission_lib as admission
import sable_batch_fold_lib as fold_lib
import sable_gate_classify_lib as classify
import sable_gate_git_lib as git_lib
import sable_gate_preview_lib as preview
import sable_gate_promote_lib as promote
from sable_gate_classify_lib import GateError


DEFAULT_MAX_MEMBERS = 8
MIN_BATCH_MEMBERS = 2

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

    @property
    def exit_code(self) -> int:
        if self.outcome == CYCLE_LANDED:
            return classify.EXIT_OK
        if self.outcome == CYCLE_RED:
            return classify.EXIT_RED
        if self.outcome in (CYCLE_REFORM, CYCLE_FELL_BACK_SERIAL):
            return classify.EXIT_BASE_MOVED
        return classify.EXIT_PRECONDITION


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
        max_members: int = DEFAULT_MAX_MEMBERS) -> BatchCycleResult:
    """Seal, verify, and decide one micro-batch.

    ``member_specs`` is ordered by queue priority (normally oldest first).
    Only the first ``max_members`` are sealed.  Excluded candidates and specs
    beyond the cap stay visible in the result for the next cycle or serial
    fallback.
    """
    if max_members < MIN_BATCH_MEMBERS:
        raise GateError(
            classify.EXIT_USAGE,
            f"--max-members must be at least {MIN_BATCH_MEMBERS}, got {max_members}")
    if len(member_specs) < MIN_BATCH_MEMBERS:
        raise GateError(
            classify.EXIT_USAGE,
            f"batch-cycle needs at least {MIN_BATCH_MEMBERS} --member values")

    selected_specs = list(member_specs[:max_members])
    deferred_specs = tuple(member_specs[max_members:])
    base = git_lib.resolve_base(base, repo)
    git_lib._git(repo, "fetch", remote, base, check=False)
    base_sha = git_lib.resolve_commit(
        repo, classify.qualify_remote_ref(remote, base))
    resolved = promote.resolve_batch_members(
        repo, remote, base_sha, selected_specs, "batch-cycle")

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
    admitted_keys = {(candidate.branch, candidate.sha)
                     for candidate in admitted.admitted}
    batch_members = [
        member for member in resolved
        if (member.branch, member.tip_sha) in admitted_keys
    ]
    # One canonical order controls both fold content and set identity.
    batch_members.sort(key=lambda member: member.tip_sha)

    if len(batch_members) < MIN_BATCH_MEMBERS:
        names = tuple(member.branch for member in batch_members)
        return BatchCycleResult(
            CYCLE_NOT_FORMED, names, admitted.excluded, deferred_specs,
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
                outcome, names, admitted.excluded, deferred_specs,
                combined_ref, fold_tip, landed.reason)

        if verdict.outcome == classify.RED:
            # The full set is already measured red. Diagnose subsets; nothing
            # in that state machine can write the integration ref.
            diagnosed = promote.bisect_red_batch(
                repo, remote, base_sha, batch_members,
                promote.ci_batch_verify(repo, remote),
                combined_ref=combined_ref, manager=manager)
            return BatchCycleResult(
                CYCLE_RED, names, admitted.excluded, deferred_specs,
                combined_ref, fold_tip,
                promote.render_bisection_report(diagnosed))

        raise GateError(
            verdict.exit_code,
            f"batch cycle {combined_ref} got no usable verdict "
            f"({verdict.outcome}/{verdict.conclusion}); nothing landed")
    finally:
        preview.delete_ci_ref(repo, remote, combined_ref)


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


def batch_cycle_from_args(args) -> int:
    result = run_batch_cycle(
        args.base, args.members, args.repo, args.remote, args.manager,
        args.max_members)
    print(render_cycle_result(result))
    return result.exit_code


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


def register_batch_subcommands(subparsers) -> None:
    """One CLI seam for the legacy primitives and the composed cycle."""
    promote.register_batch_subcommands(subparsers)
    register_batch_cycle(subparsers)


def dispatch_batch_command(args) -> int:
    if args.command == "batch-cycle":
        return batch_cycle_from_args(args)
    return promote.dispatch_batch_command(args)
