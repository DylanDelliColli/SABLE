#!/usr/bin/env python3
"""sable_batch_key_lib — the single owned module for (base, member) preview
identity keys (SABLE-be4lo.1).

The Shotgun Surgery fix for the merge-trains epic (SABLE-be4lo): what
identifies a preview — "this object is the tested one for exactly this set of
parents" — used to live independently in four sites (sable_gate_preview_lib's
two-parent build and adoption-identity check, sable_gate_classify_lib's
idempotency key, sable_gate_promote_lib's tip-equals-tested integrity check).
It lives here now, once, and those four sites call it instead of each
re-deriving it. No consumer re-derives a key.

PAIRWISE (existing, unchanged semantics): preview_kick_key(base, branch) is
the SHA1 idempotency key for a push-time preview kick — a pure function of the
two commits being merged, used to key the ci-verify ref so a kick, the poll-leg
reconciler, and promote's adoption check all name the SAME ref for the same
merge.

N-ARY (new, SABLE-be4lo): setkey(base, members) generalizes the same idea to a
batch — sha1 over the base SHA plus the newline-joined SORTED member tip SHAs.
Sorting is the identity contract: the same member set produces the same key
regardless of admission order, so two callers that admitted the same batch in
different orders still agree on its ref. The degenerate N=1 case IS the serial
path: setkey(base, [branch]) MUST equal preview_kick_key(base, branch), and
this module's own tests assert that equivalence rather than assume it."""
from __future__ import annotations

import hashlib


def preview_kick_key(base_sha: str, branch_sha: str) -> str:
    """The SHARED IDEMPOTENCY KEY for a push-time preview kick (SABLE-jd5fj.1):
    a pure function of the two commits being merged. A preview is fully
    determined by (base, branch), but the preview COMMIT's own SHA is not —
    git commit-tree stamps a committer date, so building the same merge twice
    yields two distinct SHAs. Keying the ref on the parents instead means the
    push-time kick, the poll-leg reconciler (jd5fj.2) and promote's adoption
    check all name the SAME ref for the same merge, so the work happens once."""
    if not base_sha or not branch_sha:
        raise ValueError(f"both parent SHAs are required, got {base_sha!r}, {branch_sha!r}")
    return hashlib.sha1(f"{base_sha}\n{branch_sha}\n".encode()).hexdigest()


def pair_parents(base_sha: str, branch_sha: str) -> list[str]:
    """The canonical two-parent order for a pairwise preview commit: base
    first, branch second. Both the commit that BUILDS a preview
    (commit-tree -p base -p branch) and the check that VERIFIES an adopted
    one's identity (are its actual parents exactly this pair?) need the same
    ordered pair — owned here so the two can never independently drift."""
    return [base_sha, branch_sha]


def setkey(base_sha: str, member_shas: list[str]) -> str:
    """N-ary generalization of preview_kick_key for a batch of member tips
    onto one base: sha1 over the base SHA plus the newline-joined SORTED
    member tips. Sorted input is the identity contract — the same member set
    keys identically regardless of admission order. Raises on an empty member
    list: a batch key with no members would be a vacuous identity, never a
    valid one (SABLE-p9n7k).

    N=1 is the serial path in disguise: setkey(base, [branch]) ==
    preview_kick_key(base, branch), asserted by this module's own tests."""
    if not member_shas:
        raise ValueError("setkey requires at least one member SHA, got an empty list")
    payload = base_sha + "\n" + "\n".join(sorted(member_shas)) + "\n"
    return hashlib.sha1(payload.encode()).hexdigest()


def tip_matches(landed: str, expected: str) -> bool:
    """True iff a ref's landed tip is exactly the object that was tested —
    the tip-equals-tested integrity invariant every promote enforces
    (pairwise today; the fold-chain tip for an N-ary batch, a later child of
    SABLE-be4lo). Owned here so that later path reuses this exact predicate
    instead of re-deriving its own equality check."""
    return landed == expected
