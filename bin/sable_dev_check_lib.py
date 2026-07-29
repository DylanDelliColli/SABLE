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
    seconds: float
    mode: str
    reason: str


SCOPED_BUDGET_SECONDS = 90.0
FULL_SNAPSHOT_TIER = "full_snapshot"


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


def effective_budget(
    repo_root: Path,
    plan: DeveloperPlan,
    *,
    explicit_seconds: float | None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> BudgetDecision:
    """Choose the execution budget after the selection shape is known.

    An explicit CLI value always wins. Otherwise, any FULL selector expands
    the budget to the authoritative full_snapshot tier. Truly proportional
    plans retain the short 90-second developer-feedback ceiling.
    """
    if explicit_seconds is not None:
        return BudgetDecision(
            float(explicit_seconds), "explicit", "explicit --budget override",
        )

    full_reasons = []
    if plan.python.mode == "full":
        full_reasons.append(f"Python FULL ({plan.python.reason})")
    if plan.shell_mode == "full":
        full_reasons.append(f"Shell FULL ({plan.shell_reason})")
    if full_reasons:
        seconds = _tier_budget_seconds(
            repo_root, FULL_SNAPSHOT_TIER, runner=runner,
        )
        return BudgetDecision(
            seconds,
            "full-fallback",
            "; ".join(full_reasons)
            + f"; budget from {FULL_SNAPSHOT_TIER} tier",
        )
    return BudgetDecision(
        SCOPED_BUDGET_SECONDS,
        "scoped",
        "Python and shell selections remain proportional",
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
    budget_seconds: float = SCOPED_BUDGET_SECONDS,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int:
    """Run one pytest process plus selected shell suites within one budget."""
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

    started = time.monotonic()
    failed = False

    def remaining() -> float:
        return max(0.0, budget_seconds - (time.monotonic() - started))

    commands: list[list[str]] = []
    if plan.python.tests:
        commands.append([
            sys.executable, "-m", "pytest", *plan.python.tests,
            "-q", "-p", "no:cacheprovider",
        ])
    commands.extend(
        [bash or "bash", str(repo_root / "hooks/test" / suite)]
        for suite in plan.shell_suites
    )

    for command in commands:
        timeout = remaining()
        if timeout <= 0:
            print(
                f"sable-dev-check: budget exhausted after {budget_seconds:g}s",
                file=sys.stderr,
            )
            return 124
        try:
            result = runner(command, cwd=repo_root, timeout=timeout)
        except subprocess.TimeoutExpired:
            print(
                f"sable-dev-check: command exceeded remaining {timeout:.1f}s budget: "
                f"{' '.join(command)}",
                file=sys.stderr,
            )
            return 124
        if result.returncode != 0:
            failed = True
    return 1 if failed else 0
