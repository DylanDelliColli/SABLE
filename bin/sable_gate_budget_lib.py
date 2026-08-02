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
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

import sable_gate_git_lib as git_lib

LABEL = "suite-optimization"
KEY_PREFIX = "budget-key:"

# A derived budget exists to BOUND a runaway command, not to ASSERT a duration.
# Performance enforcement is a separate, already-built lane: check_and_file()
# below files a suite-optimization bead when a tier's real total drifts past
# its declared budget. Keeping the two apart is what lets this factor be
# generous — SABLE-af7u4 measured a wall-clock cap that FAILED at 10.04s and
# PASSED at 10.16s on the same code, so a tight multiple of a measured cost is
# a load detector, not a correctness signal, and every false trip it produces
# costs a full verdict (exit 124, nothing verified).
DERIVED_HEADROOM_FACTOR = 2.0


@dataclass(frozen=True)
class SuiteCosts:
    """Measured wall-clock seconds keyed by executable suite identity."""

    python: Mapping[str, float]
    shell: Mapping[str, float]


@dataclass(frozen=True)
class ExecutionShard:
    """One command-sized shard whose budget is derived from its members."""

    kind: str
    suites: tuple[str, ...]
    measured_seconds: float
    budget_seconds: float


@dataclass(frozen=True)
class DerivedPlan:
    """A bounded execution plan plus an explicit incomplete-verification set.

    ``omitted`` is the whole point of the type: a plan that cannot fit its
    ceiling must say what it left unverified BY NAME. An implementation that
    quietly returned a shorter shard list would be the silent narrowing this
    module exists to prevent (SABLE-b99hy).

    TWO DIFFERENT NUMBERS, DELIBERATELY NAMED APART (SABLE-8jln9).
    ``total_seconds`` is the ABSOLUTE executable wall-clock bound for the whole
    plan. ``shard_headroom_seconds`` is merely the SUM of the per-command
    grants, and it may legitimately exceed the total — per-command headroom
    exists so no single command trips on load, not so the run may spend the sum
    of them. The field this type used to expose was called ``budget_seconds``
    and held the SUM; every consumer read it as "the budget" and no total was
    ever enforced, so two 1s shell suites under a 100s ceiling with a 90s
    per-command floor became a 180s executable run. Handing the sum to an
    executor reinstates the exit-124-with-no-verdict defect this module exists
    to remove, one scale up.
    """

    shards: tuple[ExecutionShard, ...]
    omitted: tuple[str, ...]
    measured_seconds: float
    ceiling_seconds: float
    shard_headroom_seconds: float = 0.0
    # Suites running on a provisional (unmeasured) cost. Reported so a large
    # derived total is legible as "mostly guesses" rather than mistaken for a
    # measurement, and so the suppressed trim is explainable.
    provisional: tuple[str, ...] = ()

    @property
    def total_seconds(self) -> float:
        """THE number an executor may spend across the entire plan.

        A property, not a stored field, so it cannot drift from the ceiling it
        is and cannot be assigned the summed headroom by a later edit.
        """
        return self.ceiling_seconds


def _positive_seconds(value: object, *, identity: str) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"non-numeric measured cost for {identity}: {value!r}") from exc
    if seconds <= 0:
        raise ValueError(f"measured cost for {identity} must be positive")
    return seconds


def load_python_cost_report(path: str | Path) -> dict[str, float]:
    """Read conftest.py's existing module-level cost report."""
    report = json.loads(Path(path).read_text())
    modules = report.get("modules")
    if not isinstance(modules, list):
        raise ValueError(f"Python cost report has no modules list: {path}")
    costs: dict[str, float] = {}
    for record in modules:
        if not isinstance(record, dict) or not record.get("module"):
            raise ValueError(f"invalid Python module cost record in {path}: {record!r}")
        identity = str(record["module"])
        costs[identity] = _positive_seconds(
            record.get("seconds"), identity=identity,
        )
    return costs


def load_shell_cost_profile(path: str | Path) -> dict[str, float]:
    """Read shell-run-set.sh's existing tab-separated suite profile."""
    lines = Path(path).read_text().splitlines()
    if not lines or lines[0] != "suite\tstatus\tseconds":
        raise ValueError(f"invalid shell cost profile header: {path}")
    costs: dict[str, float] = {}
    for line in lines[1:]:
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 3:
            raise ValueError(f"invalid shell cost profile row in {path}: {line!r}")
        identity, status, raw_seconds = fields
        if status != "pass":
            continue
        costs[identity] = _positive_seconds(raw_seconds, identity=identity)
    return costs


def load_suite_costs(
    python_report: str | Path,
    shell_profile: str | Path,
) -> SuiteCosts:
    """Load the two reporters that already own timing collection."""
    return SuiteCosts(
        python=load_python_cost_report(python_report),
        shell=load_shell_cost_profile(shell_profile),
    )


def derived_seconds(
    measured_seconds: float,
    *,
    headroom_factor: float = DERIVED_HEADROOM_FACTOR,
) -> float:
    """Turn an observed cost into a budget without introducing a time literal."""
    measured = _positive_seconds(measured_seconds, identity="suite aggregate")
    if headroom_factor < 1:
        raise ValueError("headroom factor must be at least 1")
    return measured * headroom_factor


def has_measurements(costs: SuiteCosts) -> bool:
    """Whether ANY measurement exists to derive from.

    With an empty cost set there is no measured quantity in play, so nothing
    can be derived and the caller must say so rather than invent a number.
    """
    return bool(costs.python) or bool(costs.shell)


def provisional_seconds(costs: SuiteCosts, kind: str | None = None) -> float:
    """The stand-in cost for a suite the reports have never measured.

    A brand-new test file has no measurement, and dropping it as "unmeasured"
    would mean the gate never runs the very test the change added — the exact
    silent narrowing this module exists to prevent. So an unmeasured suite is
    assumed to cost as much as the MOST EXPENSIVE thing of its OWN KIND we
    have ever measured: conservative enough to reach a verdict, still derived
    from real data rather than a literal.

    KIND-AWARE ON PURPOSE. Measured on this repo: the priciest Python module
    is a 200s integration suite, while shell suites are far cheaper. Charging
    an unmeasured shell suite 200s would let a handful of them exhaust a real
    ceiling and trigger a trim that the actual costs never justified — a
    conservative estimate turning into the narrowing it was meant to avoid.
    Only when its own kind has no measurements at all does it fall back to the
    overall maximum.
    """
    same_kind = {"python": costs.python, "shell": costs.shell}.get(kind or "", {})
    observed = list(same_kind.values()) or [*costs.python.values(), *costs.shell.values()]
    if not observed:
        raise ValueError("no measurements: nothing to derive a provisional cost from")
    return max(_positive_seconds(value, identity="provisional") for value in observed)


def _cost_entries(
    python_suites: Iterable[str],
    shell_suites: Iterable[str],
    costs: SuiteCosts,
) -> list[tuple[str, str, float]]:
    """Cost every selected suite as (kind, suite, seconds), measured or not.

    The fourth field records whether the cost is a real MEASUREMENT or a
    provisional stand-in — the trim is not allowed to act on the latter.
    """
    cached: dict[str, float] = {}

    def cost_of(suite: str, table: Mapping[str, float], kind: str) -> tuple[float, bool]:
        if suite in table:
            return _positive_seconds(table[suite], identity=suite), True
        if kind not in cached:
            cached[kind] = provisional_seconds(costs, kind)
        return cached[kind], False

    entries = [
        ("python", suite, *cost_of(suite, costs.python, "python"))
        for suite in python_suites
    ]
    entries.extend(
        ("shell", suite, *cost_of(suite, costs.shell, "shell"))
        for suite in shell_suites
    )
    return entries


def _trim_to_ceiling(
    entries: list[tuple[str, str, float, bool]],
    ceiling_seconds: float,
) -> tuple[list[tuple[str, str, float, bool]], list[str]]:
    """Drop the most expensive suites until the MEASURED total fits.

    The fit question is "will this actually finish inside the ceiling?", so it
    is asked of the measured cost, NOT of the padded per-command budget.
    Padding both would double-count the headroom: this repo's own full Python
    suite measures ~1569s against an 1800s bound and fits comfortably, yet
    against a 2x-padded 3138s it would "not fit" and half of it would be
    trimmed away — turning the headroom that exists to PREVENT false timeouts
    into a cause of silent narrowing.

    A TRIM MAY NEVER ACT ON A PROVISIONAL COST. If any selected suite is
    unmeasured this returns everything untouched: dropping a real suite to
    satisfy a ceiling computed partly from invented numbers is the same silent
    narrowing by another route. Measured on this repo — a Python cost report
    with no shell profile made all 95 shell suites inherit the 200s Python
    maximum, and 88 of them were "trimmed" on the strength of a number nobody
    measured. Provisional costs may SIZE a per-command budget (generously);
    they may not decide what goes unverified.

    Most-expensive-first is chosen deliberately: for a given ceiling it leaves
    the LARGEST NUMBER of suites still verified. The dropped names are returned
    so the caller can print them — a trim nobody can see is a narrowing.
    """
    kept = list(entries)
    if any(not measured for *_, measured in kept):
        return kept, []
    dropped: list[str] = []
    while kept and sum(cost for _, _, cost, _ in kept) > ceiling_seconds:
        index = max(range(len(kept)), key=lambda i: (kept[i][2], kept[i][1]))
        dropped.append(kept.pop(index)[1])
    return kept, sorted(dropped)


def _pack_python_shards(
    entries: Iterable[tuple[str, str, float, bool]],
    shard_ceiling_seconds: float,
    *,
    headroom_factor: float,
) -> list[ExecutionShard]:
    """Split the Python selection into commands that each fit the shard bound.

    Splitting does not reduce total cost; it keeps any SINGLE command's derived
    timeout under the bound so one oversized pytest process cannot swallow the
    whole run and return exit 124 with no verdict.
    """
    shards: list[ExecutionShard] = []
    current: list[str] = []
    current_cost = 0.0

    def flush() -> None:
        nonlocal current, current_cost
        if current:
            shards.append(ExecutionShard(
                "python",
                tuple(current),
                current_cost,
                derived_seconds(current_cost, headroom_factor=headroom_factor),
            ))
            current = []
            current_cost = 0.0

    for _, suite, cost, _measured in entries:
        if current and derived_seconds(
            current_cost + cost, headroom_factor=headroom_factor,
        ) > shard_ceiling_seconds:
            flush()
        current.append(suite)
        current_cost += cost
    flush()
    return shards


def derive_plan(
    python_suites: Iterable[str],
    shell_suites: Iterable[str],
    costs: SuiteCosts,
    ceiling_seconds: float,
    *,
    headroom_factor: float = DERIVED_HEADROOM_FACTOR,
    shard_max_seconds: float | None = None,
    min_shard_seconds: float = 0.0,
) -> DerivedPlan:
    """Turn a selected suite set plus measurements into a plan that can finish.

    Every command's budget is DERIVED from the measured cost of its own
    members, so a bigger honest selection is granted a bigger budget instead of
    being cut off by a fixed number it never had a chance against.

    ``ceiling_seconds`` bounds the TOTAL, and is returned unchanged as
    ``total_seconds``. When the measured total exceeds it the plan TRIMS —
    dropping suites and naming them in ``omitted`` — and when a single Python
    command would exceed ``shard_max_seconds`` the plan SPLITS. What it never
    does is return a plan that cannot complete.

    ``min_shard_seconds`` is a per-command MINIMUM grant, not a ceiling: a
    suite measured at 0.3s would otherwise derive a sub-second timeout and trip
    on ordinary machine load. IT CAN AND DOES PUSH THE SUMMED HEADROOM OVER
    ``ceiling_seconds`` — two 1s shell suites under a 100s ceiling with a 90s
    floor sum to 180s (SABLE-8jln9 measured exactly this), and N of them sum to
    N*90. That is why the sum is not the executable bound: the caller runs
    every command under min(its own grant, the time left in ``total_seconds``),
    so the floor buys a small suite room to breathe without ever buying the
    plan more wall-clock than the ceiling allows.
    """
    if ceiling_seconds <= 0:
        raise ValueError("ceiling must be positive")
    shard_ceiling = (
        ceiling_seconds if shard_max_seconds is None else float(shard_max_seconds)
    )
    if shard_ceiling <= 0:
        raise ValueError("shard bound must be positive")

    entries = _cost_entries(python_suites, shell_suites, costs)
    kept, omitted = _trim_to_ceiling(entries, ceiling_seconds)
    packed = _pack_python_shards(
        [entry for entry in kept if entry[0] == "python"],
        shard_ceiling,
        headroom_factor=headroom_factor,
    )
    packed.extend(
        ExecutionShard(
            "shell", (suite,), cost,
            derived_seconds(cost, headroom_factor=headroom_factor),
        )
        for kind, suite, cost, _measured in kept if kind == "shell"
    )
    floor = max(0.0, float(min_shard_seconds))
    shards = tuple(
        ExecutionShard(
            shard.kind,
            shard.suites,
            shard.measured_seconds,
            max(shard.budget_seconds, floor),
        )
        for shard in packed
    )
    return DerivedPlan(
        shards,
        tuple(omitted),
        sum(shard.measured_seconds for shard in shards),
        float(ceiling_seconds),
        # The SUM of the per-command grants — reported, never executed against.
        sum(shard.budget_seconds for shard in shards),
        tuple(sorted(suite for _, suite, _, measured in kept if not measured)),
    )


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
