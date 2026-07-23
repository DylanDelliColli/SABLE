#!/usr/bin/env python3
"""sable_gate_classify_lib — the merge gate's PURE layer (SABLE-jd5fj.3).

One of the three modules bin/sable-merge-gate was split into (classify /
preview / promote). This one holds everything that is a pure function of its
arguments: ref naming, the shared idempotency key, the sweep age predicate,
remote-ref qualification, and — the piece that gives the split its name — the
VERDICT VOCABULARY: what an Actions conclusion MEANS, and which exit code that
meaning carries.

Why the taxonomy lives here rather than inline in promote(): the exit codes are
this tool's public contract (hooks/test/test-preview-kick.sh asserts a named
case per code, and Chuck's role text branches on them). Keeping the
conclusion -> outcome -> exit-code mapping in one pure, importable table means a
reader can see the WHOLE contract at once, and a future caller that wants to
know what a stored verdict means does not have to re-run promote to find out.

IRON RULE (SABLE-jd5fj.3): the taxonomy 0/20/21/22/23/24/4 is UNCHANGED by the
split. This module is where that claim is checkable — the table below is the
whole of it.

SABLE-jd5fj.5 EXTENDS that table with 25 (green-snapshot freeze in force) and
changes none of it. 25 is deliberately NOT an outcome of a CI conclusion — no
entry joins classify_conclusion's mapping — because a freeze is not something a
run concluded about this merge; it is a standing refusal to promote ANY merge
until the integration branch is healthy again. Keeping it out of OUTCOME_EXIT is
what stops it from ever being reachable by mis-reading a verdict.

SABLE-cmar4.5 EXTENDS the table again with 27 (a pruning diff without a
carried coverage-delta check) for the same reason and the same way: not an
outcome of a CI conclusion, not in OUTCOME_EXIT, a standing refusal that is a
property of the DIFF being promoted rather than of what Actions concluded.

SABLE-rzkw7 EXTENDS the table again with 28 (a bead declares a
MUST-LAND-TOGETHER `landing_pair` whose counterpart is neither landed nor
named on this same promote call) for the same reason again: not an outcome of
a CI conclusion, not in OUTCOME_EXIT, a standing refusal that is a property of
a DECLARED CROSS-BEAD CONSTRAINT rather than of what Actions concluded.

Nothing here shells out, touches git, or reads the environment, so every
function is unit-testable with no fixtures at all.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


# --------------------------------------------------------------------------
# Exit-code taxonomy (the gate's public contract — see the module docstring)
# --------------------------------------------------------------------------

EXIT_OK = 0                # promoted / preview kicked / sweep ok / verdict read
EXIT_USAGE = 2
EXIT_PRECONDITION = 3
EXIT_INTEGRITY = 4         # base tip != preview SHA after promote
EXIT_RED = 20              # ci red, not promoted
EXIT_BLOCKED = 21          # actions-down / blocked (needs --override)
EXIT_CONFLICT = 22         # merge-preview conflict (delegate to author)
EXIT_BASE_MOVED = 23       # tip moved during gate (non-ff promote) — retry-safe
EXIT_CANCELLED = 24        # ci-verify run cancelled mid-flight — retry-safe
EXIT_FROZEN = 25           # green-snapshot freeze in force — promotion denied (SABLE-jd5fj.5)
EXIT_COVERAGE_FLOOR = 27   # pruning diff without a carried coverage-delta check (SABLE-cmar4.5)
EXIT_PAIR_REFUSED = 28     # MUST-LAND-TOGETHER counterpart not landed / not named (SABLE-rzkw7)

# Outcome names: the MEANING of a completed Actions conclusion, independent of
# how the verdict was obtained (waited for, or read back precomputed).
GREEN = "green"            # promotable
RED = "red"                # content defect — author fixes and re-pushes
BLOCKED = "blocked"        # no verdict obtainable — needs --override or a fix to Actions
RETRY = "retry"            # not a content defect — rebuild preview + re-gate

OUTCOME_EXIT = {
    GREEN: EXIT_OK,
    RED: EXIT_RED,
    BLOCKED: EXIT_BLOCKED,
    RETRY: EXIT_CANCELLED,
}


class GateError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def classify_conclusion(conclusion: str) -> str:
    """An Actions run conclusion -> one of GREEN / RED / BLOCKED / RETRY.

    'success' promotes. 'cancelled' is RETRY, never RED (SABLE-sc24): a
    mid-flight cancellation — a concurrent sweep deleting the in-flight ref, a
    manual cancel, or a concurrency group pre-empting the run — is not a content
    defect, so the author is told NOTHING to fix. 'actions_down'/'timeout' are
    BLOCKED: no verdict was obtainable, which is not the same as a bad one.
    EVERYTHING ELSE IS RED — the default is deliberately the conservative one, so
    an Actions conclusion nobody anticipated blocks promotion instead of
    sliding through as green."""
    if conclusion == "success" or conclusion == "override":
        return GREEN
    if conclusion == "cancelled":
        return RETRY
    if conclusion in ("actions_down", "timeout"):
        return BLOCKED
    return RED


@dataclass(frozen=True)
class Verdict:
    """A CI verdict for one merge-preview commit, as promote consumes it.

    `source` records HOW the verdict was obtained and is the observable that
    SABLE-jd5fj.3 exists to create:

      'precomputed' — the run keyed to this preview SHA had ALREADY completed
                      when promote looked; promote consumed the stored result in
                      one API call and never waited. This is the push-time-kick
                      payoff: the kick fired at push, CI ran while the worker was
                      still writing its report, and Chuck's merge pays only the
                      read.
      'waited'      — no completed run existed yet, so promote polled for one
                      (the pre-split behaviour, unchanged and still correct).
      'override'    — a human actions-down bypass; no run was consulted.

    `complete` is False only for the sentinel returned when nothing is known yet
    (a pending run), which read_verdict uses to say "come back later" without
    inventing a conclusion.

    `answered` is False only when `complete` is also False AND the reason is
    that no answer was obtainable at all (gh missing, erroring, or hanging —
    read_verdict's _gh_runs returning None) rather than gh having genuinely
    answered "no completed run yet". Both collapse to the same `complete=False`
    sentinel because promote's fallthrough to wait_for_ci treats them alike
    (SABLE-jd5fj.3) — wait_for_ci owns the grace/actions_down distinction for
    that path. But a NON-BLOCKING caller like report_verdict has no wait loop to
    converge through, so `answered` is what lets it tell "gh could not be asked"
    apart from "asked, still running" instead of printing 'pending' for both
    (SABLE-fewih).
    """
    conclusion: str
    run_url: str = ""
    preview_sha: str = ""
    ref: str = ""
    source: str = "waited"
    complete: bool = True
    answered: bool = True

    @property
    def outcome(self) -> str:
        return classify_conclusion(self.conclusion)

    @property
    def exit_code(self) -> int:
        return OUTCOME_EXIT[self.outcome]


def preview_ref_name(bead: str, preview_sha: str) -> str:
    """Throwaway ci-verify ref for a merge-preview commit. The 7-char preview
    SHA makes it unique per attempt (a re-push after red never collides). The
    bead id is sanitized to the ref-safe charset so a stray slash cannot create
    a nested ref the flat sweep listing would miss.

    Uniqueness per attempt is ALSO what makes concurrent previews concurrent:
    ci-verify.yml's concurrency group is keyed on github.ref, so N previews on N
    distinct ci-verify/<bead>-<sha7> refs land in N distinct groups and none
    cancels another (SABLE-jd5fj.3). Collapsing this to a per-bead or per-branch
    ref would put every preview in ONE group under cancel-in-progress, and each
    new kick would cancel the run before it — the SABLE-sc24 spurious-cancel
    shape, fleet-wide."""
    if not preview_sha or len(preview_sha) < 7:
        raise ValueError(f"preview_sha must be >= 7 chars, got {preview_sha!r}")
    return f"{preview_ref_prefix(bead)}{preview_sha[:7]}"


def preview_ref_prefix(name: str) -> str:
    """Everything preview_ref_name can produce for <name>, up to the per-attempt
    suffix — i.e. the ci-verify ref NAMESPACE of one bead (promote-built refs) or
    one branch (kicked refs).

    Exists so a caller can LIST a branch's kicked previews without knowing their
    parent-pair keys (SABLE-kzi1a): the kick ref is keyed on a HASH of the two
    parent SHAs, so the only way to find the preview a branch was kicked with
    against a base that has since moved is to enumerate the namespace and read
    each candidate's actual parents. Sanitization is shared with preview_ref_name
    rather than re-derived, because a prefix that sanitized differently from the
    names it is meant to match would silently list nothing."""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in name)
    if not safe.strip("-"):
        raise ValueError(f"ref name sanitizes to empty: {name!r}")
    return f"ci-verify/{safe}-"


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


def preview_kick_ref(branch: str, base_sha: str, branch_sha: str) -> str:
    """ci-verify ref for a kicked preview: keyed on the BRANCH (the hook knows
    the branch, not the bead) plus the parent-pair key above. Shares the
    ci-verify/ prefix — and therefore the sweep's orphan cleanup — with the
    promote-built refs."""
    return preview_ref_name(branch, preview_kick_key(base_sha, branch_sha))


def is_orphan(ref_age_seconds: float, max_age_hours: float) -> bool:
    """True iff a ci-verify ref is old enough to sweep. Pure so the threshold
    logic is testable without real refs or wall-clock."""
    return ref_age_seconds > max_age_hours * 3600.0


def qualify_remote_ref(remote: str, branch: str) -> str:
    """Fully-qualified remote-tracking ref. Refuse anything that would resolve
    ambiguously — callers pass a bare branch name, never 'origin/foo'."""
    if "/" in remote:
        raise ValueError(f"remote must be a bare name, got {remote!r}")
    if branch.startswith("refs/") or branch.startswith(f"{remote}/"):
        raise ValueError(f"branch must be a bare name (no refs/ or {remote}/ prefix): {branch!r}")
    return f"refs/remotes/{remote}/{branch}"
