#!/usr/bin/env python3
"""sable_batch_fold_lib — the fold builder for merge trains (SABLE-be4lo.4).

Builds the BATCH OBJECT the epic's architecture locked: a chain of ordinary
TWO-PARENT commits, never an octopus. Each member is folded into an
accumulator one at a time via `git merge-tree --write-tree` (the pairwise
combine) + `git commit-tree` (the pairwise two-parent commit,
sable_batch_key_lib.pair_parents's own contract) — so the chain tip has every
member tip as a first-or-second parent somewhere in the chain, and the chain's
FIRST-PARENT lineage walks straight back to base, exactly like a single-branch
preview does.

Why iterative-pairwise and not one octopus commit: `git commit-tree` accepts
N parents, but `git merge-tree` fundamentally merges TWO trees — there is no
single mechanical operation that folds four trees at once, and the
two-parent-commit invariant every existing preview/promote check relies on
(sable_gate_preview_lib.py:57, batch_key.pair_parents) would have to be
redefined for an octopus tip. Folding iteratively keeps every existing
two-parent assumption true of every commit in the chain; only the CALLER now
walks a chain instead of reading one commit.

Experiment 3 (2026-07-23) validated the mechanics live: 4 disjoint-footprint
branches fold clean, tree content equals base plus every member's edit; a
deliberately overlapping 5th branch fails the fold LOUDLY (merge-tree exits 1,
naming the conflicting path) before anything is pushed. This module hardens
that experiment into fold_chain (the builder) and fold_check (the read-only
admission probe — literally the same code path, so a permissive fold_check
that diverges from what fold_chain would actually do is structurally
impossible).

MODULE BOUNDARY: like sable_gate_preview_lib, this module only builds and
(via push_batch_ref) pushes the ci-verify trigger ref. It never promotes,
never writes bead evidence, never notifies a manager. The BatchRecord /
manifest type is SABLE-be4lo.2's deliverable — this module hands back plain
FoldMember/FoldResult values, not a typed batch record, so as not to define a
second competing type.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import sable_batch_key_lib as batch_key
import sable_gate_classify_lib as classify
import sable_gate_git_lib as git_lib
from sable_gate_classify_lib import GateError


@dataclass(frozen=True)
class FoldMember:
    """One member of a batch: its branch/label (named in the fold commit
    message — the manifest contract), its tip SHA (the object actually
    folded), and an optional bead id (also named in the message, so the
    batch record's later reconstruction from fold commit messages alone has
    both the branch and the bead to work with)."""
    label: str
    sha: str
    bead: str = ""


@dataclass(frozen=True)
class FoldResult:
    """The chain a fold produced: its tip SHA and every intermediate fold
    commit, in fold order (commits[-1] == tip, commits[0]'s first parent is
    the base SHA the fold started from)."""
    tip: str
    commits: list[str] = field(default_factory=list)


def _conflicting_paths(merge_tree_stdout: str) -> list[str]:
    """Extract the conflicting paths from a conflicted `git merge-tree
    --write-tree` run. On conflict, stdout is: the (conflict) tree OID, then
    one `<mode> <oid> <stage>\\t<path>` line per higher-stage entry, then a
    blank line, then human-readable Auto-merging/CONFLICT messages — this
    reads the tab-separated stage lines rather than screen-scraping the
    prose, so it survives a wording change to the messages."""
    paths: list[str] = []
    for line in merge_tree_stdout.splitlines():
        if "\t" not in line:
            continue
        _info, path = line.split("\t", 1)
        path = path.strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def _fold_message(index: int, total: int, member: FoldMember) -> str:
    """The manifest half of the fold contract: a later reader (S2's batch
    record reconstruction) must be able to name every member from the fold
    commit messages ALONE. Every message therefore names this member's label,
    its position in the chain, and its bead when one was supplied."""
    bead_part = f" ({member.bead})" if member.bead else ""
    return f"batch fold {index}/{total}: {member.label}{bead_part}"


def fold_chain(repo: str, base_sha: str, members: list[FoldMember]) -> FoldResult:
    """Build the batch object: iteratively merge each member into an
    accumulator that starts at base_sha, producing a chain of ordinary
    two-parent commits (never an octopus). Raises GateError(22) the moment
    any member fails to fold cleanly — LOUDLY, naming the conflicting paths —
    and pushes/commits nothing beyond the git objects already written for the
    commits built before the failing one (those are unreferenced by any ref
    and never pushed by this function; only push_batch_ref pushes, and only
    the final tip, and only on a fully clean fold).

    Non-emptiness guard (SABLE-p9n7k applied here): an empty member list
    raises ValueError rather than silently returning base_sha as a vacuous
    one-commit-shorter-than-expected 'chain' — a fold with no members folded
    is not a batch, it is a bug in the caller."""
    if not members:
        raise ValueError("fold_chain requires at least one member, got an empty list")
    total = len(members)
    acc = base_sha
    commits: list[str] = []
    for index, member in enumerate(members, start=1):
        if not member.sha:
            raise ValueError(f"member {member.label!r} has no tip SHA")
        mt = git_lib._git(repo, "merge-tree", "--write-tree", acc, member.sha, check=False)
        if mt.returncode == 1:
            paths = _conflicting_paths(mt.stdout)
            named = ", ".join(paths) if paths else "(no path extracted)"
            raise GateError(
                22,
                f"batch fold conflict: {member.label!r} does not fold cleanly onto the "
                f"accumulator — conflicting path(s): {named}\n{mt.stdout.strip()}",
            )
        if mt.returncode != 0:
            raise GateError(3, f"merge-tree failed folding {member.label!r}: {mt.stdout.strip()}")
        tree = mt.stdout.splitlines()[0].strip()
        parent1, parent2 = batch_key.pair_parents(acc, member.sha)
        message = _fold_message(index, total, member)
        ct = git_lib._git(repo, "commit-tree", tree, "-p", parent1, "-p", parent2, "-m", message)
        acc = ct.stdout.strip()
        commits.append(acc)
    return FoldResult(tip=acc, commits=commits)


def fold_check(repo: str, base_sha: str, members: list[FoldMember]) -> tuple[bool, str]:
    """Read-only clean-fold probe for the admission child: would fold_chain
    succeed for this member set onto this base? Reuses fold_chain's own
    conflict detection rather than a second, independently-written check —
    a fold_check that could say clean while fold_chain would actually raise
    (or vice versa) is exactly the false-green shape SABLE-5lli exists to
    catch, and calling the same function is what makes that divergence
    structurally impossible instead of merely untested.

    'Read-only' here means it never pushes a ref and never advances any
    branch — build_preview has the identical property (a commit-tree write is
    a git-object write, not a ref mutation, and nothing outside this process
    can observe it until something pushes it). Only a conflict (GateError 22)
    is folded into the boolean result; a tool-level failure (GateError 3) or a
    malformed member (ValueError) is a precondition problem, not a fold
    verdict, and propagates so the caller does not mistake 'git is broken'
    for 'this batch does not fold'."""
    try:
        fold_chain(repo, base_sha, members)
        return (True, "")
    except GateError as exc:
        if exc.code == 22:
            return (False, str(exc))
        raise


def push_batch_ref(repo: str, remote: str, base_sha: str, members: list[FoldMember]) -> tuple[str, str]:
    """Fold `members` onto `base_sha` and push the chain tip as
    ci-verify/batch-<setkey7> (experiment 1, hardened): setkey comes from
    sable_batch_key_lib.setkey — the ONE owned N-ary keying function, not a
    second derivation here — over base_sha and every member's tip SHA.
    classify.preview_ref_name shares the exact sanitize-and-slice logic every
    other ci-verify ref uses, so this ref is indistinguishable in shape from a
    single-branch preview ref to the sweep, the ci-verify/** trigger glob, and
    the already-verified lookup (which key on SHA + the ci-verify/ prefix
    alone, never on ref shape).

    Returns (tip_sha, ref). Raises GateError(22) via fold_chain on any
    conflicting member — nothing is pushed when the fold itself fails."""
    member_shas = [m.sha for m in members]
    key = batch_key.setkey(base_sha, member_shas)
    ref = classify.preview_ref_name("batch", key)
    result = fold_chain(repo, base_sha, members)
    git_lib._git(repo, "push", git_lib.resolve_remote_url(repo, remote),
                 f"{result.tip}:refs/heads/{ref}")
    return (result.tip, ref)
