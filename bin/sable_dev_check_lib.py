#!/usr/bin/env python3
"""Fast, conservative developer-feedback planning for this repository.

This module is advisory: it narrows local feedback, while ci-verify remains
the sealed-candidate authority. Python selection is derived from the import
graph and literal loader references. Shell selection delegates to the
existing impact manifest. An ambiguous in-scope Python change expands to the
full Python suite instead of guessing.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Sequence

import sable_gate_budget_lib as gate_budget


class DeveloperCheckError(RuntimeError):
    """A local environment or repository precondition is unavailable."""


@dataclass(frozen=True)
class PythonSelection:
    mode: str
    tests: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ShellSelection:
    mode: str
    suites: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class DeveloperPlan:
    changed_paths: tuple[str, ...]
    python: PythonSelection
    shell_suites: tuple[str, ...]
    shell_mode: str = "scoped"
    shell_reason: str = "selected shell suites"


@dataclass(frozen=True)
class BudgetDecision:
    """``seconds`` is the ABSOLUTE total this run may spend, end to end.

    ``shard_headroom_seconds`` is the sum of the per-command grants and is
    REPORTING ONLY. The two are separate fields on purpose (SABLE-8jln9): the
    sum routinely exceeds the total — a per-command floor granting N cheap
    suites 90s each sums to N*90 — and an executor handed the sum runs past the
    bound it was told to honour, which is the exit-124-with-no-verdict defect
    this whole change removes, reproduced at a larger scale.
    """

    seconds: float
    mode: str
    reason: str
    shards: tuple[gate_budget.ExecutionShard, ...] = ()
    omitted: tuple[str, ...] = ()
    shard_headroom_seconds: float = 0.0


SCOPED_TIER = "pre_push"
FULL_SNAPSHOT_TIER = "full_snapshot"

# What a blocked developer is told to DO. This text is load-bearing, not
# decoration: SABLE-b99hy records a worker who followed the old advice
# literally — rewrote `git config sable.testCommand` down to a two-file subset,
# pushed green, then restored it — so the gate certified a narrower claim than
# it was configured to enforce while printing "enforced". Advice to shrink the
# claim is therefore never offered here, and any edit that reintroduces it is
# the defect, not a wording change.
TIMEOUT_REMEDIATION = (
    "This budget is DERIVED from measured suite cost, so overrunning it means "
    "the suites really did get slower — not that the budget is too small. "
    "Re-measure (--sable-test-cost-report / --profile) so the derivation sees "
    "current cost, or reduce the suites' real runtime. Do NOT narrow "
    "sable.testCommand or the selected suite set to fit: that certifies a "
    "smaller claim than the one being enforced (SABLE-b99hy)."
)


def _normalise_path(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/")).as_posix()


def _all_python_tests(repo_root: Path) -> tuple[str, ...]:
    bin_dir = repo_root / "bin"
    if not bin_dir.is_dir():
        return ()
    return tuple(
        sorted(path.relative_to(repo_root).as_posix()
               for path in bin_dir.rglob("test_*.py"))
    )


def _module_keys(path: Path, bin_dir: Path) -> set[str]:
    relative = path.relative_to(bin_dir).with_suffix("")
    parts = list(relative.parts)
    keys = {path.stem, ".".join(parts)}
    if parts[-1] == "__init__":
        keys.add(".".join(parts[:-1]))
    return {key for key in keys if key}


def _import_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _string_literals(tree: ast.AST) -> set[str]:
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _literal_names_path(literal: str, relative: str) -> bool:
    normalised = literal.replace("\\", "/")
    basename = PurePosixPath(relative).name
    return (
        normalised == relative
        or normalised == basename
        or normalised.endswith("/" + relative)
        or normalised.endswith("/" + basename)
    )


def _is_python_owned(repo_root: Path, relative: str) -> bool:
    if not relative.startswith("bin/"):
        return False
    path = repo_root / relative
    if relative.endswith(".py"):
        return True
    if not path.exists():
        return True  # Deleted bin files are ambiguous, so fail closed.
    try:
        with path.open(errors="replace") as handle:
            first_line = handle.readline()
    except OSError:
        return True
    return "python" in first_line


def select_python_tests(
    repo_root: Path, changed_paths: Iterable[str],
) -> PythonSelection:
    """Select Python test files from imports and literal loader references."""
    repo_root = repo_root.resolve()
    changed = tuple(sorted({_normalise_path(path) for path in changed_paths}))
    tests = _all_python_tests(repo_root)
    if not changed or not tests:
        return PythonSelection("none", (), "no changed paths or Python tests")

    bin_dir = repo_root / "bin"
    sources = tuple(sorted(bin_dir.rglob("*.py"))) if bin_dir.is_dir() else ()
    trees: dict[Path, ast.AST] = {}
    for source in sources:
        try:
            trees[source] = ast.parse(source.read_text(), filename=str(source))
        except (OSError, SyntaxError) as exc:
            return PythonSelection(
                "full", tests,
                f"cannot parse Python dependency graph at "
                f"{source.relative_to(repo_root)}: {exc}",
            )

    module_paths: dict[str, set[Path]] = defaultdict(set)
    for source in sources:
        for key in _module_keys(source, bin_dir):
            module_paths[key].add(source)

    dependencies: dict[Path, set[Path]] = defaultdict(set)
    literals: dict[Path, set[str]] = {}
    for source, tree in trees.items():
        literals[source] = _string_literals(tree)
        for imported in _import_names(tree):
            candidates = module_paths.get(imported, set())
            if not candidates:
                candidates = module_paths.get(imported.rsplit(".", 1)[-1], set())
            if len(candidates) == 1:
                dependencies[source].update(candidates)

    reverse: dict[Path, set[Path]] = defaultdict(set)
    for source, imported_paths in dependencies.items():
        for imported_path in imported_paths:
            reverse[imported_path].add(source)

    test_paths = {repo_root / test: test for test in tests}
    selected: set[str] = set()
    unmapped: list[str] = []

    for relative in changed:
        absolute = repo_root / relative
        roots: set[Path] = set()
        if absolute in test_paths:
            selected.add(test_paths[absolute])
            roots.add(absolute)
        if absolute in trees:
            roots.add(absolute)
        for source, values in literals.items():
            if any(_literal_names_path(value, relative) for value in values):
                roots.add(source)

        if relative.endswith("/conftest.py") or relative == "conftest.py":
            parent = PurePosixPath(relative).parent
            selected.update(
                test for test in tests
                if PurePosixPath(test).is_relative_to(parent)
            )
            continue

        queue = deque(roots)
        seen = set(roots)
        while queue:
            current = queue.popleft()
            if current in test_paths:
                selected.add(test_paths[current])
            for dependent in reverse.get(current, ()):
                if dependent not in seen:
                    seen.add(dependent)
                    queue.append(dependent)

        reached_tests = {test_paths[path] for path in seen if path in test_paths}
        if _is_python_owned(repo_root, relative) and not reached_tests:
            unmapped.append(relative)

    if unmapped:
        return PythonSelection(
            "full", tests,
            f"unmapped Python-owned path(s): {' '.join(unmapped)}",
        )
    if not selected:
        return PythonSelection("none", (), "no Python dependency reached")
    if selected == set(tests):
        return PythonSelection("full", tests, "change reaches every Python test")
    return PythonSelection(
        "selected", tuple(sorted(selected)),
        f"{len(selected)} of {len(tests)} Python test files reached",
    )


def _git_lines(repo_root: Path, args: Sequence[str]) -> set[str]:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git failed"
        raise DeveloperCheckError(detail)
    return {_normalise_path(line) for line in result.stdout.splitlines() if line}


def collect_changed_paths(repo_root: Path, base_ref: str | None) -> tuple[str, ...]:
    """Union committed range changes, worktree changes, and untracked files."""
    changed: set[str] = set()
    if base_ref:
        changed.update(_git_lines(
            repo_root,
            ["diff", "--name-only", "--diff-filter=ACMRD", f"{base_ref}...HEAD"],
        ))
    changed.update(_git_lines(
        repo_root, ["diff", "--name-only", "--diff-filter=ACMRD", "HEAD"],
    ))
    changed.update(_git_lines(
        repo_root, ["ls-files", "--others", "--exclude-standard"],
    ))
    return tuple(sorted(changed))


def select_shell_selection(
    repo_root: Path,
    changed_paths: Iterable[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> ShellSelection:
    """Delegate shell-suite selection and its mode to the impact manifest.

    FULL versus SCOPED is load-bearing budget input, not decorative logging:
    an unmapped path intentionally selects the complete authoritative shell
    set, which cannot inherit the fast scoped budget. If an older or broken
    selector omits its mode line, fail closed to FULL for budget purposes
    while preserving the missing-mode fact in the rendered plan.
    """
    changed = tuple(sorted({_normalise_path(path) for path in changed_paths}))
    if not changed:
        return ShellSelection("none", (), "no changed paths")
    manifest = repo_root / ".github/ci/impact-manifest.sh"
    if not manifest.is_file():
        raise DeveloperCheckError(f"shell impact manifest missing: {manifest}")
    bash = shutil.which("bash")
    if not bash:
        raise DeveloperCheckError("bash is required for shell-test selection")
    result = runner(
        [bash, str(manifest), "--select", *changed],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "manifest failed"
        raise DeveloperCheckError(f"shell impact selection failed: {detail}")
    if result.stderr:
        # Preserve the manifest's FULL/SCOPED reason instead of turning this
        # Adapter into an observability sink.
        sys.stderr.write(result.stderr)
    suites = tuple(sorted({
        line.strip() for line in result.stdout.splitlines()
        if line.strip() and not line.startswith("::")
    }))
    matches = re.findall(
        r"impact-manifest:\s*(FULL|SCOPED)\s*--\s*([^\r\n]*)",
        result.stderr or "",
        flags=re.IGNORECASE,
    )
    if not matches:
        return ShellSelection(
            "full",
            suites,
            "selector omitted its FULL/SCOPED reason; using full-budget fallback",
        )
    mode, reason = matches[-1]
    return ShellSelection(mode.lower(), suites, reason.strip())


def select_shell_suites(
    repo_root: Path,
    changed_paths: Iterable[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> tuple[str, ...]:
    """Compatibility wrapper returning only the selected suite names."""
    return select_shell_selection(
        repo_root, changed_paths, runner=runner,
    ).suites


def build_plan(repo_root: Path, changed_paths: Iterable[str]) -> DeveloperPlan:
    changed = tuple(sorted({_normalise_path(path) for path in changed_paths}))
    shell = select_shell_selection(repo_root, changed)
    return DeveloperPlan(
        changed_paths=changed,
        python=select_python_tests(repo_root, changed),
        shell_suites=shell.suites,
        shell_mode=shell.mode,
        shell_reason=shell.reason,
    )


def _tier_budget_seconds(
    repo_root: Path,
    tier: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> float:
    """Read a duration from the authoritative tier SSOT.

    In particular, the full-fallback duration must never be copied into this
    module: changing full_snapshot in test-tiers.sh must change the developer
    check's implicit budget on the next invocation.
    """
    script = repo_root / ".github/ci/test-tiers.sh"
    if not script.is_file():
        raise DeveloperCheckError(f"tier budget source missing: {script}")
    bash = shutil.which("bash")
    if not bash:
        raise DeveloperCheckError("bash is required to read tier budgets")
    result = runner(
        [bash, str(script), "--budget", tier],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "tier query failed"
        raise DeveloperCheckError(f"could not read {tier} budget: {detail}")
    try:
        seconds = float(result.stdout.strip())
    except ValueError as exc:
        raise DeveloperCheckError(
            f"{tier} budget is not numeric: {result.stdout.strip()!r}"
        ) from exc
    if seconds <= 0:
        raise DeveloperCheckError(f"{tier} budget must be positive, got {seconds:g}")
    return seconds


# Measured cost baselines live under .claude/sable/state/, which .gitignore
# already treats as runtime state. That placement is deliberate on both counts:
# these numbers are wall-clock and machine-relative, so they are not shared
# configuration; and an UNTRACKED file anywhere the impact manifest does not
# map would show up as a changed path and escalate the whole plan to a FULL
# shell selection — a measurement artifact silently making every subsequent
# run maximally expensive. Regenerate with the two reporters that already own
# timing collection:
#   python -m pytest bin/ -q \
#     --sable-test-cost-report=.claude/sable/state/test-cost/python-cost.json
#   bash .github/ci/shell-run-set.sh \
#     --profile .claude/sable/state/test-cost/shell-cost.tsv
PYTHON_COST_REPORT = ".claude/sable/state/test-cost/python-cost.json"
SHELL_COST_PROFILE = ".claude/sable/state/test-cost/shell-cost.tsv"


def load_costs(
    repo_root: Path,
    *,
    python_report: str | Path | None = None,
    shell_profile: str | Path | None = None,
) -> gate_budget.SuiteCosts | None:
    """Read whatever measurements exist, or None when there are none.

    Both halves are OPTIONAL and independent. The two reporters that produce
    them already exist — conftest.py's --sable-test-cost-report and
    shell-run-set.sh's --profile — so this reads their output rather than
    timing anything itself. A missing or corrupt report degrades to "no
    measurement for those suites", which derive_plan then costs
    conservatively; it never blocks the run.
    """
    python_path = Path(python_report or repo_root / PYTHON_COST_REPORT)
    shell_path = Path(shell_profile or repo_root / SHELL_COST_PROFILE)
    python_costs: dict[str, float] = {}
    shell_costs: dict[str, float] = {}
    for path, sink, reader in (
        (python_path, "python", gate_budget.load_python_cost_report),
        (shell_path, "shell", gate_budget.load_shell_cost_profile),
    ):
        if not path.is_file():
            continue
        try:
            loaded = reader(path)
        except (OSError, ValueError) as exc:
            print(
                f"sable-dev-check: ignoring unreadable {sink} cost report "
                f"{path}: {exc}",
                file=sys.stderr,
            )
            continue
        if sink == "python":
            python_costs = loaded
        else:
            shell_costs = loaded
    costs = gate_budget.SuiteCosts(python=python_costs, shell=shell_costs)
    return costs if gate_budget.has_measurements(costs) else None


def effective_budget(
    repo_root: Path,
    plan: DeveloperPlan,
    *,
    explicit_seconds: float | None,
    costs: gate_budget.SuiteCosts | None = None,
    derived: bool = True,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> BudgetDecision:
    """Choose the execution budget after the selection shape is known.

    THE BUDGET GRADIENT USED TO RUN BACKWARDS. An unmapped path escalated to
    FULL and was handed the full_snapshot tier's duration, while a correctly
    mapped SCOPED change — a strictly smaller, cheaper suite set — was handed a
    fixed 90s and could die at exit 124 with no verdict at all (SABLE-1urtf:
    69 passing dots, then nothing). Being precise about your blast radius was
    punished; being vague was rewarded.

    So the scoped tier's duration is no longer a ceiling anywhere in this
    function. The ABSOLUTE bound is the full_snapshot tier for every mode — a
    scoped plan is a subset of a full run and can never honestly need more —
    and the OPERATIVE budget inside it is derived from the measured cost of the
    suites actually selected. The scoped tier's duration survives only as
    ``min_shard_seconds``, a per-command MINIMUM grant, so this change can
    never hand any plan less time than it gets today.

    With no measurements at all there is nothing to derive from, so the plan
    runs against the absolute bound rather than against a number nobody
    measured. That is deliberately the SAFE direction: an underived plan
    reaches a verdict slowly instead of failing fast having verified nothing.

    WHAT ``seconds`` IS, IN EVERY MODE: the absolute total. Derivation changes
    the PER-COMMAND grants inside that total, never the total itself. It is
    emphatically not the sum of those grants (SABLE-8jln9) — see BudgetDecision.
    """
    floor = 0.0
    if explicit_seconds is not None:
        ceiling = float(explicit_seconds)
        mode = "explicit"
        reason = "explicit --budget ceiling"
    else:
        full_reasons = []
        if plan.python.mode == "full":
            full_reasons.append(f"Python FULL ({plan.python.reason})")
        if plan.shell_mode == "full":
            full_reasons.append(f"Shell FULL ({plan.shell_reason})")
        ceiling = _tier_budget_seconds(repo_root, FULL_SNAPSHOT_TIER, runner=runner)
        floor = _tier_budget_seconds(repo_root, SCOPED_TIER, runner=runner)
        if full_reasons:
            mode = "full-fallback"
            reason = (
                "; ".join(full_reasons)
                + f"; bound from {FULL_SNAPSHOT_TIER} tier"
            )
        else:
            mode = "scoped"
            reason = f"bound from {FULL_SNAPSHOT_TIER} tier"

    if not derived or costs is None or not gate_budget.has_measurements(costs):
        why = "derivation disabled" if not derived else "no measured cost data"
        return BudgetDecision(ceiling, mode, f"{reason}; {why}")

    derived_plan = gate_budget.derive_plan(
        plan.python.tests,
        plan.shell_suites,
        costs,
        ceiling,
        min_shard_seconds=floor,
    )
    python_shards = sum(
        1 for shard in derived_plan.shards if shard.kind == "python"
    )
    derived_reason = (
        f"{reason}; derived from {derived_plan.measured_seconds:.1f}s measured "
        f"across {len(derived_plan.shards)} command(s) "
        f"({python_shards} Python shard(s)); per-command headroom sums to "
        f"{derived_plan.shard_headroom_seconds:.1f}s, spendable only within "
        f"the {derived_plan.total_seconds:g}s total"
    )
    if derived_plan.provisional:
        derived_reason += (
            f"; {len(derived_plan.provisional)} suite(s) UNMEASURED and "
            f"costed provisionally, so no trim was taken on guessed numbers "
            f"(re-measure to tighten)"
        )
    if derived_plan.omitted:
        derived_reason += (
            f"; TRIMMED to fit — {len(derived_plan.omitted)} suite(s) "
            f"left UNVERIFIED"
        )
    return BudgetDecision(
        # The TOTAL, not the summed per-command headroom. Reading the sum here
        # is the SABLE-8jln9 defect; the sum travels in its own field below.
        derived_plan.total_seconds,
        "derived",
        derived_reason,
        derived_plan.shards,
        derived_plan.omitted,
        derived_plan.shard_headroom_seconds,
    )


def render_plan(
    plan: DeveloperPlan,
    *,
    max_entries: int | None = 20,
    budget: BudgetDecision | None = None,
) -> str:
    lines = [
        f"sable-dev-check: {len(plan.changed_paths)} changed path(s)",
        f"  Python: {plan.python.mode} — {plan.python.reason}",
    ]
    if max_entries is None or len(plan.python.tests) <= max_entries:
        lines.extend(f"    {test}" for test in plan.python.tests)
    else:
        lines.append("    (list omitted; use --dry-run to inspect)")
    lines.append(
        f"  Shell: {plan.shell_mode} — {plan.shell_reason} "
        f"({len(plan.shell_suites)} suite(s))"
    )
    if max_entries is None or len(plan.shell_suites) <= max_entries:
        lines.extend(f"    hooks/test/{suite}" for suite in plan.shell_suites)
    else:
        lines.append("    (list omitted; use --dry-run to inspect)")
    if budget is not None:
        lines.append(
            f"  Budget: {budget.mode} — {budget.seconds:g}s — {budget.reason}"
        )
        if budget.shards:
            lines.append(
                f"  Total bound: {budget.seconds:g}s absolute; per-command "
                f"headroom sums to {budget.shard_headroom_seconds:g}s and every "
                f"command runs under min(its own grant, time left in the total)"
            )
        for index, shard in enumerate(budget.shards, start=1):
            lines.append(
                f"    Shard {index}: {shard.kind} — "
                f"{shard.budget_seconds:g}s from "
                f"{shard.measured_seconds:g}s measured — "
                f"{' '.join(shard.suites)}"
            )
        if budget.omitted:
            lines.append("  UNVERIFIED (trimmed from this run):")
            lines.extend(f"    {suite}" for suite in budget.omitted)
    lines.append(
        "  Scope: fast developer feedback only; sealed-candidate ci-verify "
        "remains authoritative."
    )
    return "\n".join(lines)


def missing_preconditions(repo_root: Path, plan: DeveloperPlan) -> tuple[str, ...]:
    """Return unavailable tools needed by the selected suites before any run."""
    required: set[str] = set()
    for suite in plan.shell_suites:
        path = repo_root / "hooks/test" / suite
        try:
            source = path.read_text(errors="replace")
        except OSError:
            continue
        for command in ("diff-cover", "tmux", "jq"):
            if command in source:
                required.add(command)
    return tuple(sorted(command for command in required if shutil.which(command) is None))


def run_plan(
    repo_root: Path,
    plan: DeveloperPlan,
    *,
    budget_seconds: float | None = None,
    budget: BudgetDecision | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Run the measured shards, or the legacy plan, under ONE total budget.

    ``budget_seconds`` is the absolute wall-clock this whole call may spend,
    and it binds in EVERY mode. Each command runs under
    ``min(its own derived grant, the time left in the total)``, and a command
    the total can no longer afford is never started — it is named as unrun and
    the call returns 124 (SABLE-8jln9).

    The two bounds do different jobs and neither substitutes for the other. The
    per-command grant keeps ONE oversized command from swallowing the run; the
    total keeps N commands, each individually within its grant, from summing
    past the outer wrapper that would then kill the whole gate with no verdict
    — the original defect, one scale up. A derived grant only ever narrows a
    command's timeout below the remaining total, never widens it.

    ``clock`` is injectable so a test can advance time deterministically:
    SABLE-af7u4 measured a real wall-clock cap that FAILED at 10.04s and PASSED
    at 10.16s on identical code, so a test that raced a real clock would be a
    load detector rather than a discriminator.
    """
    if plan.python.tests and importlib.util.find_spec("pytest") is None:
        raise DeveloperCheckError("pytest is required for selected Python tests")
    bash = shutil.which("bash")
    if plan.shell_suites and not bash:
        raise DeveloperCheckError("bash is required for selected shell tests")
    missing_suites = tuple(
        suite for suite in plan.shell_suites
        if not (repo_root / "hooks/test" / suite).is_file()
    )
    if missing_suites:
        raise DeveloperCheckError(
            f"selected shell suite(s) missing: {' '.join(missing_suites)}"
        )
    missing = missing_preconditions(repo_root, plan)
    if missing:
        raise DeveloperCheckError(
            "selected suites require unavailable command(s): "
            f"{' '.join(missing)}; install Python tools from "
            ".github/ci/test-requirements.txt and required system tools "
            "before running"
        )

    failed = False
    # (argv, per-command grant or None, the suites that command verifies).
    # The third element exists so an unrun command can be named: "what this run
    # did not verify" has to survive into the 124 report, or the report is the
    # silent narrowing by another route.
    commands: list[tuple[list[str], float | None, tuple[str, ...]]] = []
    derived = budget is not None and budget.mode == "derived"
    if derived and not budget.shards and (plan.python.tests or plan.shell_suites):
        # The bound cannot fit even one suite. Running the untrimmed plan under
        # a leftover budget is how this used to end in exit 124 with nothing
        # verified; reporting it GREEN would be worse still — SABLE-o1lnt
        # records that a skip keeps the run green, nobody reads the Skipped
        # line, and the assertion is silently retired. So: a loud verdict.
        raise DeveloperCheckError(
            "the execution bound fits NO selected suite, so this run would "
            "verify nothing. Unverified: " + " ".join(budget.omitted) + ". "
            "Raise the bound (--budget) or reduce the suites' real cost. "
            + TIMEOUT_REMEDIATION
        )
    if derived:
        for shard in budget.shards:
            if shard.kind == "python":
                command = [
                    sys.executable, "-m", "pytest", *shard.suites,
                    "-q", "-p", "no:cacheprovider",
                ]
            else:
                command = [
                    bash or "bash",
                    str(repo_root / "hooks/test" / shard.suites[0]),
                ]
            commands.append((command, shard.budget_seconds, shard.suites))
    else:
        if plan.python.tests:
            commands.append(([
                sys.executable, "-m", "pytest", *plan.python.tests,
                "-q", "-p", "no:cacheprovider",
            ], None, plan.python.tests))
        commands.extend(
            ([bash or "bash", str(repo_root / "hooks/test" / suite)], None, (suite,))
            for suite in plan.shell_suites
        )

    if budget is not None and budget.omitted:
        # Say it again at EXECUTION time, not only in the plan. A trim printed
        # once during planning and never repeated is how "what we left
        # unverified" quietly becomes "what we verified".
        print(
            "sable-dev-check: UNVERIFIED — trimmed to fit the budget: "
            + " ".join(budget.omitted),
            file=sys.stderr,
        )

    def _unrun(index: int) -> str:
        """Every suite from ``index`` onward, by name."""
        return " ".join(
            suite for _, _, suites in commands[index:] for suite in suites
        )

    started = clock()
    for index, (command, shard_timeout, _suites) in enumerate(commands):
        timeout = shard_timeout
        if budget_seconds is not None:
            # Recomputed BEFORE every invocation, not once for the run: N
            # commands each inside their own grant must still not outlive the
            # total between them.
            remaining = budget_seconds - (clock() - started)
            if remaining <= 0:
                print(
                    f"sable-dev-check: the {budget_seconds:g}s total bound is "
                    f"exhausted; NOT STARTING the remaining command(s). "
                    f"UNVERIFIED: {_unrun(index)}\n{TIMEOUT_REMEDIATION}",
                    file=sys.stderr,
                )
                return 124
            timeout = remaining if timeout is None else min(timeout, remaining)
        try:
            kwargs = {"cwd": repo_root}
            if timeout is not None:
                kwargs["timeout"] = timeout
            result = runner(command, **kwargs)
        except subprocess.TimeoutExpired:
            print(
                f"sable-dev-check: command exceeded its {timeout:.1f}s budget: "
                f"{' '.join(command)}\nUNVERIFIED: {_unrun(index)}\n"
                f"{TIMEOUT_REMEDIATION}",
                file=sys.stderr,
            )
            return 124
        if result.returncode != 0:
            failed = True
    return 1 if failed else 0
