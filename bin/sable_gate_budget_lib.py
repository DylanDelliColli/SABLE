#!/usr/bin/env python3
"""sable_gate_budget_lib — per-tier duration budget breach detection + idempotent
bead filing (SABLE-cmar4.4, Story S2).

Wraps the ONE place bin/sable_gate_promote_lib spends the gate's own
wall-clock: the read-or-wait for a verdict (sable_gate_preview_lib.
acquire_verdict). The number this module is handed is always a per-TIER-TOTAL
duration, never a per-test one (locked, split-invariant) — this module never
decomposes or re-aggregates it, so splitting a slow suite into faster pieces
cannot evade a breach; only reducing the tier's real total does.

BUDGETS come from the cmar4.1 tier SSOT (.github/ci/test-tiers.sh --budget
<tier>), read independently here via the same read-only shell-out
sable_gate_git_lib.default_mg_timeout and sable_snapshot_lib._suite_timeout
already make for their own tiers, rather than centralized — an unreadable SSOT
degrades only this optional check, never couples it to either wait-timeout's
1800s fallback. Unlike those two callers, an unresolvable budget here means
SKIP the check entirely: there is no safe default to breach against, and
fabricating one would file beads against a number nobody declared.

IDEMPOTENT BEAD FILING follows sable_snapshot_lib's find_open_bead/
file_bead_once shape (itself descended from bin/sable-reconcile-handoffs' "the
idempotency key is a query against the open pool, not a remembered flag"
pattern): query open beads carrying the LABEL for a KEY marker in their
description, file only on a miss. The KEY is (tier, budget_sec) — the budget
VALUE doubles as its own version, so editing the SSOT's number for a tier is
what a "budget-version bump" IS; there is no separate counter to keep in sync
or forget to advance, and an unedited budget always reproduces the SAME key,
which is what makes a re-run against the same open breach a no-op. Every write
carries --sandbox (SABLE-rq9k: no Dolt auto-push — the chuck-only push lane
stays intact), same as every other bd write in this gate.

Deliberately a SIBLING seam, not a reuse of sable_snapshot_lib's bd calls:
those read SABLE_SNAPSHOT_BD, this reads SABLE_MG_BD (via sable_gate_git_lib's
_tool), the same seam sable_gate_promote_lib's own _notify/_append_evidence
already use — so any test that stubs the gate's bd (SABLE_MG_BD=true, the
pattern hooks/test/test-optimistic-promotion.sh and friends already use)
stubs this too, with no second seam to remember.

check_and_file() never raises: a budget-check fault must never fail a green
promotion, the same posture promote()'s own _notify/_append_evidence carry
(best-effort, errors printed, never propagated).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import sable_gate_git_lib as git_lib

LABEL = "suite-optimization"
KEY_PREFIX = "budget-key:"


def tier_budget_sec(repo: str, tier: str) -> float | None:
    """The tier's duration budget in seconds from the cmar4.1 SSOT
    (.github/ci/test-tiers.sh --budget <tier>), or None if it cannot be
    resolved — no SSOT file, unknown tier, a bad/empty value, or a timeout.
    Never a guessed fallback (contrast default_mg_timeout's 1800s default):
    the caller here must be able to distinguish "no budget declared" from
    "under budget", and only the caller's SKIP-the-check response is safe for
    the former."""
    tiers_sh = Path(repo) / ".github" / "ci" / "test-tiers.sh"
    if not tiers_sh.is_file():
        return None
    try:
        cp = git_lib._run(["bash", str(tiers_sh), "--budget", tier], cwd=repo,
                          check=False, timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if cp.returncode != 0:
        return None
    try:
        return float(cp.stdout.strip())
    except ValueError:
        return None


def breach(duration_sec: float, budget_sec: float) -> bool:
    """Strict '>': a run landing exactly on budget is not yet a breach —
    mirrors the strict-'>' convention age_exceeds_threshold/is_orphan already
    use elsewhere in this fleet for boundary predicates."""
    return duration_sec > budget_sec


def budget_key(tier: str, budget_sec: float) -> str:
    """The idempotency key: (tier, budget-version), where the budget VALUE is
    its own version. See the module docstring — there is no separate counter."""
    return f"{tier}:{budget_sec:g}"


def bead_title(tier: str, budget_sec: float, duration_sec: float) -> str:
    return (f"suite-optimization: {tier} tier over its duration budget "
            f"({duration_sec:.0f}s > {budget_sec:.0f}s)")


def bead_description(tier: str, budget_sec: float, duration_sec: float,
                     context: str = "") -> str:
    ctx = f"\n\n{context}" if context else ""
    return (
        f"The merge gate measured the {tier} tier's total wall-clock at "
        f"{duration_sec:.1f}s for this run, over its {budget_sec:.0f}s budget "
        f"declared in .github/ci/test-tiers.sh (SABLE-cmar4.1).{ctx}\n\n"
        f"This is a per-TIER-TOTAL measurement (SABLE-cmar4 S2, split-"
        f"invariant) — splitting a slow suite into faster pieces does not "
        f"clear this bead, only reducing the tier's real total does. Rank "
        f"the tier's suites by duration vs unique coverage contributed "
        f"(SABLE-cmar4 S4 lens) and prune or parallelize the slowest fully-"
        f"subsumed or independently-shardable ones."
    )


def _bd_cmd() -> list[str]:
    return git_lib._tool("SABLE_MG_BD", "bd")


def find_open_budget_bead(repo: str, key: str) -> str | None:
    """The bead id of an OPEN bead carrying LABEL whose description carries
    <key>, else None. Query, don't remember — the same posture
    sable_snapshot_lib.find_open_bead and bin/sable-reconcile-handoffs'
    branch_named_by_open_for_chuck already take."""
    cp = git_lib._run(_bd_cmd() + ["list", "--status=open", f"--label={LABEL}", "--json"],
                      cwd=repo, check=False)
    if cp.returncode != 0:
        return None
    try:
        items = json.loads(cp.stdout or "[]")
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(items, list):
        return None
    marker = f"{KEY_PREFIX}{key}"
    for item in items:
        if isinstance(item, dict) and marker in (item.get("description") or ""):
            return item.get("id")
    return None


def file_budget_bead_once(repo: str, *, key: str, title: str,
                          description: str) -> tuple[str | None, bool]:
    """File EXACTLY ONE bead for <key>. Returns (bead_id, created). The dedup
    is a query against the open pool, so it survives a re-run, a crashed gate,
    or a second promote for a different worker — none of which a local
    "already filed" flag would survive."""
    existing = find_open_budget_bead(repo, key)
    if existing:
        return existing, False
    body = f"{description.rstrip()}\n\n{KEY_PREFIX}{key}\n"
    cp = git_lib._run(_bd_cmd() + ["create", "--sandbox", "--title", title,
                                    "--type=task", "--priority=2",
                                    f"--labels={LABEL},coord",
                                    "--description", body],
                      cwd=repo, check=False)
    if cp.returncode != 0:
        print(f"sable-merge-gate: bd create failed for the {key!r} budget-breach "
              f"bead: {cp.stdout.strip()}", file=sys.stderr)
        return None, False
    for line in cp.stdout.splitlines():
        if "Created issue:" in line:
            parts = line.split("Created issue:", 1)[1].strip().split()
            if parts:
                return parts[0], True
    return None, True


def check_and_file(repo: str, tier: str, duration_sec: float, *,
                   context: str = "") -> dict:
    """THE entry point promote() calls around its wall-clock-spending call.
    Never raises. Returns a small report dict (mainly for tests); callers that
    only need the WARN + auto-file side effects can ignore the return value."""
    try:
        budget_sec = tier_budget_sec(repo, tier)
        if budget_sec is None:
            return {"checked": False, "reason": "budget-unresolvable"}
        if not breach(duration_sec, budget_sec):
            return {"checked": True, "breached": False, "budget_sec": budget_sec}

        print(f"sable-merge-gate: WARN {tier} tier took {duration_sec:.1f}s, over "
              f"its {budget_sec:.0f}s budget (.github/ci/test-tiers.sh, "
              f"SABLE-cmar4.1)", file=sys.stderr)
        key = budget_key(tier, budget_sec)
        bead_id, created = file_budget_bead_once(
            repo, key=key,
            title=bead_title(tier, budget_sec, duration_sec),
            description=bead_description(tier, budget_sec, duration_sec, context))
        return {"checked": True, "breached": True, "budget_sec": budget_sec,
                "key": key, "bead_id": bead_id, "filed": created}
    except Exception as exc:  # noqa: BLE001 — a budget check must never fail a promotion
        print(f"sable-merge-gate: budget check for tier {tier!r} failed "
              f"(non-fatal): {exc}", file=sys.stderr)
        return {"checked": False, "reason": f"error: {exc}"}
