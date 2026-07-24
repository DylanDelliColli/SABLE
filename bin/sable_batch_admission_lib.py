#!/usr/bin/env python3
"""sable_batch_admission_lib — merge-trains admission (SABLE-be4lo.3).

Member selection for a batch. A candidate is admissible iff ALL of:

  (a) NON-GATE-CLASS       no merge-tooling / gate-lib / dispatch file in its
                            declared-or-mechanical footprint.
  (b) INDIVIDUALLY GREEN    a kicked preview exists for the CURRENT
                            (base, branch) pair and its stored verdict is
                            GREEN — never a stale or recomputed one.
  (c) PAIRWISE RW-DISJOINT  its declared write/read footprint does not
                            couple with any OTHER member already admitted
                            into this batch (sable_footprint_lib.is_rw_disjoint,
                            checked C(n,2) as the batch fills).
  (d) CLEAN MECHANICAL FOLD folding this member onto the base (alongside every
                            member already admitted) is conflict-free —
                            sable_batch_fold_lib.fold_check, never a second,
                            independently written merge-tree path.

Any failure EXCLUDES the member and records WHICH condition failed, loudly —
an exclusion is a report, never a silent drop (a shortened batch must never
read the same as a small one, SABLE-x2n8a).

CROSS-EPIC CONTRACT (SABLE-21rug architecture decision 2, amendment
2026-07-23): clauses (a)+(b) above, plus two more that this predicate is the
SINGLE HOME of, are factored into one exported per-member function,
`is_this_branch_mechanical` — "mechanical class" for a single branch:

  * NON-GATE-CLASS (roster-derived — see gate_class_roster() — INCLUDING the
    SABLE-78qck reaches-the-tier-mechanism exclusion)
  * INDIVIDUALLY GREEN, verdict bound to the CURRENT (base, branch) pair
  * ZERO HOLDS on the branch's work bead
  * CLEAN FAST-FORWARD ADOPTION (a kicked preview exists whose parents are
    EXACTLY this (base, branch) pair — pushing it to the base ref is a
    fast-forward by construction)
  * ZERO CONFLICTS folding this branch alone onto the current base

`SABLE-21rug.4` (a DIFFERENT epic's auto-promote assert, owned by tarzan) is
dep-BLOCKED on this function and calls it directly. NO consumer re-derives
any clause — a clause appearing in two places is the drift this factoring
exists to prevent. The result is per-clause (a `MechanicalVerdict` naming
which of the five failed), not a bare boolean, because both this module's own
loud exclusions and 21rug's disqualifier table need to name the clause.

WHAT THIS MODULE DOES NOT DO. It never builds or pushes the batch's fold
chain (sable_batch_fold_lib.push_batch_ref, a later child's job); it only
asks fold_check whether folding WOULD be clean. It never promotes and never
writes bead evidence — admission is a pure member-selection question, exactly
like sable_footprint_lib never decides a promotion (see that module's own
docstring for the same boundary, one layer down).
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass

import sable_batch_fold_lib as fold_lib
import sable_footprint_lib as fp
import sable_gate_classify_lib as classify
import sable_gate_preview_lib as preview_lib
from sable_batch_fold_lib import FoldMember
from sable_gate_classify_lib import GateError

# --------------------------------------------------------------------------
# The gate-class roster
# --------------------------------------------------------------------------

# SABLE-78qck "reaches-the-tier-mechanism" exclusion, quoted verbatim from
# this bead's amendment: files that control WHICH suites/tier runs are
# gate-class even though nothing yet imports them from the gate's own module
# graph (78qck's own fix — a mini-tier trigger — is not built yet; until it
# is, this predicate independently carries the same file list).
GATE_TIER_FILES = frozenset({
    ".github/ci/impact-manifest.sh",
    ".github/ci/shell-run-set.sh",
    ".github/ci/test-tiers.sh",
    "bin/tier_selection.py",
})

# The dispatch file (bead text: "no merge-tooling / gate-lib / dispatch file
# in its declared footprint"). Named explicitly because dispatch-time
# metadata stamping (tag_footprint_metadata) is what every other clause in
# this module ultimately reads evidence through — a branch that edits its own
# evidence-writer cannot be mechanically batched.
DISPATCH_FILE = "bin/sable-spawn-worker"

# The gate CLI itself is the root of the derivation below; it is not reached
# by walking its OWN imports, so it is added once, by name, here.
_GATE_ENTRY = "bin/sable-merge-gate"


def gate_class_roster(repo: str) -> frozenset[str]:
    """The gate-tooling file roster, DERIVED from bin/sable-merge-gate's real
    `import sable_*` graph (AST, transitive) — never hand-restated. A module
    only needs to be wired into the gate's actual import graph to become
    gate-class automatically; nothing here has to be told about it a second
    time.

    Deliberately conservative in what it walks: only names starting with
    `sable_` are followed (so `os`, `json`, etc. are never mistaken for gate
    modules), and only files that actually exist under <repo>/bin are added —
    a name that resolves to nothing (a third-party import, a typo) is simply
    not part of the roster rather than raising.
    """
    bin_dir = os.path.join(repo, "bin")
    entry_path = os.path.join(repo, _GATE_ENTRY)
    roster: set[str] = set()
    seen_modules: set[str] = set()
    queue: list[str] = []
    if os.path.isfile(entry_path):
        roster.add(_GATE_ENTRY)
        queue.append(entry_path)
    while queue:
        path = queue.pop()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                if not name.startswith("sable_") or name in seen_modules:
                    continue
                seen_modules.add(name)
                mod_path = os.path.join(bin_dir, name + ".py")
                if os.path.isfile(mod_path):
                    roster.add(f"bin/{name}.py")
                    queue.append(mod_path)
    return frozenset(roster | GATE_TIER_FILES | {DISPATCH_FILE})


# --------------------------------------------------------------------------
# Per-member mechanical-class predicate
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ClauseResult:
    """One named clause of the mechanical-class predicate: whether it passed
    and why — the per-clause shape SABLE-21rug.4's disqualifier table and
    this module's own loud exclusions both need."""
    name: str
    passed: bool
    reason: str


@dataclass(frozen=True)
class MechanicalVerdict:
    """The whole mechanical-class story for one branch: every clause, in a
    fixed order, so a consumer can always find (say) `non_gate_class` by name
    without depending on evaluation order."""
    branch: str
    bead: str
    mechanical: bool
    clauses: tuple[ClauseResult, ...]

    @property
    def reason(self) -> str:
        failing = [f"{c.name}: {c.reason}" for c in self.clauses if not c.passed]
        return "; ".join(failing) if failing else "all clauses passed"

    def clause(self, name: str) -> ClauseResult:
        for c in self.clauses:
            if c.name == name:
                return c
        raise KeyError(name)


def _non_gate_class(repo: str, base_sha: str, branch_sha: str, bead: str) -> ClauseResult:
    try:
        declared = fp.declared_footprint(repo, bead)
        mechanical_fp = fp.mechanical_footprint(repo, base_sha, branch_sha)
    except fp.FootprintUndetermined as exc:
        return ClauseResult("non_gate_class", False,
                            f"footprint undetermined, fails closed: {exc}")
    combined = fp.widen(mechanical_fp, declared)
    roster_fp = fp.footprint(gate_class_roster(repo))
    verdict = fp.is_disjoint(combined, roster_fp)
    if verdict.disjoint:
        return ClauseResult("non_gate_class", True,
                            "no gate-tooling / dispatch file in the declared+mechanical footprint")
    return ClauseResult("non_gate_class", False, verdict.reason)


def _individually_green_and_ff(repo: str, remote: str, branch: str,
                               base_sha: str, branch_sha: str
                               ) -> tuple[ClauseResult, ClauseResult]:
    adopted = preview_lib.adopt_kicked_preview(repo, remote, branch, base_sha, branch_sha)
    if adopted is None:
        reason = (f"no kicked preview found for the current (base={base_sha[:7]}, "
                  f"branch={branch_sha[:7]}) pair — cannot verify a verdict bound to this pair")
        return (ClauseResult("clean_ff_adoption", False, reason),
               ClauseResult("individually_green", False, reason))
    preview_sha, ref = adopted
    ff = ClauseResult(
        "clean_ff_adoption", True,
        f"kicked preview {preview_sha[:7]} on {ref!r} has parents exactly (base, branch) — "
        f"fast-forward onto the current base is structurally guaranteed")
    verdict = preview_lib.read_verdict(repo, ref, preview_sha)
    if verdict.complete and verdict.outcome == classify.GREEN:
        green = ClauseResult("individually_green", True,
                             f"verdict for {preview_sha[:7]} is GREEN (source={verdict.source})")
    else:
        state = verdict.outcome if verdict.complete else "pending/unanswered"
        green = ClauseResult("individually_green", False,
                             f"verdict for {preview_sha[:7]} is {state}, not green")
    return ff, green


def _zero_holds(repo: str, bead: str) -> ClauseResult:
    if not bead:
        return ClauseResult("zero_holds", False, "no bead id given — hold state unknown, fails closed")
    try:
        record = fp._read_bead(repo, bead)  # noqa: SLF001 — the one bd-read seam, shared not re-derived
    except fp.FootprintUndetermined as exc:
        return ClauseResult("zero_holds", False, f"hold state undetermined, fails closed: {exc}")
    hold = (record.get("metadata") or {}).get("hold")
    if isinstance(hold, str) and hold.strip():
        return ClauseResult("zero_holds", False, f"{bead} is under a hold: {hold.strip()}")
    return ClauseResult("zero_holds", True, f"{bead} carries no hold")


def _zero_conflicts(repo: str, base_sha: str, branch: str, branch_sha: str, bead: str) -> ClauseResult:
    member = FoldMember(label=branch, sha=branch_sha, bead=bead or "")
    try:
        clean, reason = fold_lib.fold_check(repo, base_sha, [member])
    except GateError as exc:
        return ClauseResult("zero_conflicts", False, f"fold-check tool failure: {exc}")
    if clean:
        return ClauseResult("zero_conflicts", True, "single-member fold onto the current base is clean")
    return ClauseResult("zero_conflicts", False, f"fold-check conflict: {reason}")


def is_this_branch_mechanical(repo: str, remote: str, bead: str, branch: str,
                              base_sha: str, branch_sha: str) -> MechanicalVerdict:
    """The exported per-member mechanical-class predicate (SABLE-21rug
    amendment). Every clause is always evaluated — never short-circuited —
    so a caller gets the FULL per-clause picture even when the first clause
    already fails; 21rug.4's disqualifier table needs every clause name that
    failed, not just the first."""
    gate_clause = _non_gate_class(repo, base_sha, branch_sha, bead)
    ff_clause, green_clause = _individually_green_and_ff(repo, remote, branch, base_sha, branch_sha)
    hold_clause = _zero_holds(repo, bead)
    conflict_clause = _zero_conflicts(repo, base_sha, branch, branch_sha, bead)
    clauses = (gate_clause, green_clause, hold_clause, ff_clause, conflict_clause)
    return MechanicalVerdict(branch=branch, bead=bead,
                             mechanical=all(c.passed for c in clauses), clauses=clauses)


# --------------------------------------------------------------------------
# Batch admission
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """One candidate for batch membership: its work bead, branch name, and
    tip SHA — everything the admission checks need and nothing they have to
    re-derive."""
    bead: str
    branch: str
    sha: str


@dataclass(frozen=True)
class Exclusion:
    """A REPORT, never a silent drop (SABLE-x2n8a family): which candidate
    was excluded and, in one string, exactly which condition it failed."""
    candidate: Candidate
    reason: str


@dataclass(frozen=True)
class AdmissionResult:
    admitted: tuple[Candidate, ...]
    excluded: tuple[Exclusion, ...]


def admit_batch(repo: str, remote: str, base_sha: str,
                candidates: list[Candidate]) -> AdmissionResult:
    """Select the admissible subset of `candidates` for one batch onto
    `base_sha`, in the order given.

    Greedy, not exhaustive: a candidate is admitted iff it is mechanical
    (is_this_branch_mechanical), its declared write/read footprint is
    rw-disjoint from EVERY already-admitted member's, AND folding it onto the
    base alongside every already-admitted member is clean (fold_check on the
    accumulated set). A candidate that fails any check is EXCLUDED with a
    loud, named reason and the loop continues with the remaining candidates —
    one exclusion never drops the rest of the batch (bd close evidence for
    SABLE-be4lo.3's 'excludes a gate-class branch loudly; remaining members
    still form a batch' case).

    The footprint check runs BEFORE fold_check on purpose: an overlapping
    declared footprint is cheap to detect and excludes the candidate with NO
    combined run spent, while fold_check does real git work (merge-tree +
    commit-tree) — expensive work is only paid for candidates that already
    cleared the cheap check.

    VACUOUS-PASS GUARD (SABLE-p9n7k): an empty candidate list is REJECTED
    loudly (ValueError) rather than silently returning an empty-but-successful
    AdmissionResult — a batch admission with nothing to admit is a bug in the
    caller, never a vacuously green zero-member batch.
    """
    if not candidates:
        raise ValueError(
            "admit_batch requires at least one candidate, got an empty list — an "
            "empty candidate set must be REJECTED loudly, never silently "
            "admitted-and-green (SABLE-p9n7k)")

    admitted: list[Candidate] = []
    admitted_members: list[FoldMember] = []
    footprints: dict[str, tuple[fp.Footprint, fp.Footprint]] = {}
    excluded: list[Exclusion] = []

    for cand in candidates:
        verdict = is_this_branch_mechanical(repo, remote, cand.bead, cand.branch,
                                            base_sha, cand.sha)
        if not verdict.mechanical:
            excluded.append(Exclusion(cand, verdict.reason))
            continue

        try:
            writes_c = fp.declared_footprint(repo, cand.bead)
            reads_c = fp.declared_reads(repo, cand.bead)
        except fp.FootprintUndetermined as exc:
            excluded.append(Exclusion(cand, f"declared footprint undetermined: {exc}"))
            continue
        footprints[cand.bead] = (writes_c, reads_c)

        conflict_reason = None
        for other in admitted:
            writes_o, reads_o = footprints[other.bead]
            rw = fp.is_rw_disjoint(writes_c, reads_c, writes_o, reads_o)
            if not rw.disjoint:
                conflict_reason = (f"declared footprint overlaps admitted member "
                                   f"{other.branch!r} ({other.bead}): {rw.reason}")
                break
        if conflict_reason:
            excluded.append(Exclusion(cand, conflict_reason))
            continue

        member = FoldMember(label=cand.branch, sha=cand.sha, bead=cand.bead)
        trial_members = admitted_members + [member]
        if not admitted_members:
            # is_this_branch_mechanical's zero_conflicts clause already ran
            # exactly this single-member fold — do not spend it twice. Only
            # once a SECOND member joins does the fold become a genuinely
            # NEW (combined) run this candidate has not already paid for.
            clean = verdict.clause("zero_conflicts").passed
            fold_reason = verdict.clause("zero_conflicts").reason
        else:
            try:
                clean, fold_reason = fold_lib.fold_check(repo, base_sha, trial_members)
            except GateError as exc:
                excluded.append(Exclusion(cand, f"fold-check tool failure: {exc}"))
                continue
        if not clean:
            excluded.append(Exclusion(
                cand,
                f"fold-check FAILURE against the admitted set (declared footprints were "
                f"disjoint, but the real fold conflicts) — falls back to serial: {fold_reason}"))
            continue

        admitted.append(cand)
        admitted_members = trial_members

    return AdmissionResult(admitted=tuple(admitted), excluded=tuple(excluded))
