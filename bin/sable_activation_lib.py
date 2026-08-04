#!/usr/bin/env python3
"""Classify how each landed file reaches its installed consumer (SABLE-tvhzw).

This module is evidence, not promotion policy.  It measures installed paths and
reports per-file obligations; callers may print or persist the report, but a
probe failure must not turn a verified green promotion red.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections import defaultdict
from enum import Enum
from pathlib import Path
from typing import Iterable, Mapping

import sable_activation_debt_lib as debt


class Shape(str, Enum):
    MERGE_IS_ACTIVATION = "merge-is-activation"
    PULL_HOT_SWAP = "pull-hot-swap"
    REPIN = "repin"
    INSTALL_REFRESH = "install-refresh"
    NOT_INSTALLED = "not-installed"


_BAR = {
    Shape.MERGE_IS_ACTIVATION: 0,
    Shape.INSTALL_REFRESH: 1,
    Shape.NOT_INSTALLED: 1,
    Shape.REPIN: 2,
    Shape.PULL_HOT_SWAP: 3,
}


class ActivationBarRefused(RuntimeError):
    """The pending tree requires a stricter activation bar than declared."""


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _installed_paths(repo_path: str, repo_root: Path,
                     roots: debt.Roots | None,
                     installed_path: os.PathLike[str] | str | None) -> list[Path]:
    if installed_path is not None:
        return [Path(installed_path)]
    return [Path(site.consumed_path)
            for site in debt.consumed_sites(str(repo_root), repo_path, roots)]


def classify_activation(repo_path: str, *, repo_root: os.PathLike[str] | str,
                        roots: debt.Roots | None = None,
                        installed_path: os.PathLike[str] | str | None = None) -> Shape:
    """Measure the installed state for one repo-relative path.

    An executable ``bin/sable-*`` with no consumer is NOT_INSTALLED.  An
    unowned/ordinary path has no separate consumer, so merge is its activation.
    Copy-owned hooks with a missing destination still owe an install refresh.
    """
    repo_root = Path(repo_root)
    rel = repo_path.strip().lstrip("./")
    installed = _installed_paths(rel, repo_root, roots, installed_path)
    if not installed:
        return Shape.MERGE_IS_ACTIVATION

    # Multiple consumed sites are possible for a hook support library. Return
    # the strictest measured shape so no consumer silently disappears.
    shapes: list[Shape] = []
    for consumed in installed:
        if not os.path.lexists(consumed):
            # The merge seat may have pushed ``tree_ish`` without checking it
            # out, so the source need not exist in its worktree yet.  The bin
            # installer's executable namespace is the stable discriminator;
            # support modules (sable_*.py) deliberately do not match it.
            if rel.startswith("bin/sable-"):
                shapes.append(Shape.NOT_INSTALLED)
            elif installed_path is not None:
                shapes.append(Shape.MERGE_IS_ACTIVATION)
            else:
                shapes.append(Shape.INSTALL_REFRESH)
            continue
        if consumed.is_symlink():
            target = consumed.resolve(strict=False)
            live = debt.live_roots(str(repo_root))
            shapes.append(Shape.PULL_HOT_SWAP if any(
                _inside(target, Path(root)) for root in live)
                          else Shape.REPIN)
        else:
            shapes.append(Shape.INSTALL_REFRESH)
    return max(shapes, key=_BAR.__getitem__)


def activation_obligations(changed_paths: Iterable[str], *,
                           repo_root: os.PathLike[str] | str,
                           roots: debt.Roots | None = None) -> dict[Shape, list[str]]:
    """Group activation obligations by shape while retaining every file."""
    grouped: dict[Shape, list[str]] = defaultdict(list)
    for rel in changed_paths:
        clean = rel.strip().lstrip("./")
        shape = classify_activation(clean, repo_root=repo_root, roots=roots)
        grouped[shape].append(clean)
    return {shape: paths for shape, paths in grouped.items()}


def _blob_bytes(repo_root: Path, tree_ish: str, rel: str) -> bytes | None:
    if tree_ish == "WORKTREE":
        try:
            return (repo_root / rel).read_bytes()
        except OSError:
            return None
    try:
        cp = subprocess.run(
            ["git", "-C", str(repo_root), "cat-file", "blob", f"{tree_ish}:{rel}"],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return cp.stdout if cp.returncode == 0 else None


def stale_installed(changed_paths: Iterable[str], tree_ish: str, *,
                    repo_root: os.PathLike[str] | str,
                    roots: debt.Roots | None = None) -> list[str]:
    """Return repo paths whose installed consumer differs or is absent.

    Blob reads use real ``git show`` and comparisons use real filesystem bytes.
    A missing installed executable is a positive finding, never a clean diff.
    Paths with no separate installed consumer are intentionally omitted.
    """
    repo_root = Path(repo_root)
    stale: list[str] = []
    for raw in changed_paths:
        rel = raw.strip().lstrip("./")
        installed = _installed_paths(rel, repo_root, roots, None)
        if not installed:
            continue
        expected = _blob_bytes(repo_root, tree_ish, rel)
        mismatch = expected is None
        if expected is not None:
            with tempfile.NamedTemporaryFile() as blob:
                blob.write(expected)
                blob.flush()
                for consumed in installed:
                    if not os.path.lexists(consumed):
                        mismatch = True
                        continue
                    try:
                        cp = subprocess.run(
                            ["cmp", "-s", str(consumed), blob.name], timeout=30)
                        mismatch = mismatch or cp.returncode != 0
                    except (OSError, subprocess.SubprocessError):
                        mismatch = True
        if mismatch:
            stale.append(rel)
    return stale


def _expects_settings_wiring(rel: str) -> bool:
    parts = rel.split("/")
    return (len(parts) == 3 and parts[:2] == ["hooks", "multi-manager"]
            and rel.endswith(".sh")) or (
                len(parts) == 2 and parts[0] == "hooks" and rel.endswith(".sh"))


def unwired_hooks(changed_paths: Iterable[str], *,
                  settings_paths: Iterable[os.PathLike[str] | str]) -> list[str]:
    """Report settings-invoked hooks absent from all settings files.

    ``hooks/test/**`` is deliberately outside this predicate: those files are
    shell suites, not hooks settings.json is expected to name.
    """
    bodies: list[str] = []
    for path in settings_paths:
        try:
            bodies.append(Path(path).read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    joined = "\n".join(bodies)
    return [rel for rel in changed_paths
            if _expects_settings_wiring(rel) and Path(rel).name not in joined]


def highest_bar(obligations: Mapping[Shape, list[str]]) -> Shape:
    """Strictest activation shape in a pending tree."""
    if not obligations:
        return Shape.MERGE_IS_ACTIVATION
    return max(obligations, key=_BAR.__getitem__)


def assert_declared_bar(obligations: Mapping[Shape, list[str]],
                        declared: Shape) -> None:
    required = highest_bar(obligations)
    if _BAR[required] > _BAR[declared]:
        raise ActivationBarRefused(
            f"pending tree requires {required.value}, stricter than declared {declared.value}")


def changed_paths(repo_root: os.PathLike[str] | str, base: str, tip: str) -> list[str]:
    """Every path a pull/landing from base to tip changes."""
    cp = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--name-only", "--diff-filter=ACDMRTUXB",
         base, tip, "--"],
        capture_output=True, text=True, timeout=30,
    )
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or "git diff failed")
    return [line for line in cp.stdout.splitlines() if line]


def activation_report(changed: Iterable[str], tree_ish: str, *,
                      repo_root: os.PathLike[str] | str,
                      roots: debt.Roots | None = None,
                      settings_paths: Iterable[os.PathLike[str] | str] = ()) -> str:
    """Stable JSON evidence suitable for stdout and a bead note."""
    paths = list(changed)
    obligations = activation_obligations(paths, repo_root=repo_root, roots=roots)
    payload = {
        "obligations": {shape.value: members for shape, members in obligations.items()},
        "highest_bar": highest_bar(obligations).value,
        "stale_installed": stale_installed(
            paths, tree_ish, repo_root=repo_root, roots=roots),
        "unwired_hooks": unwired_hooks(paths, settings_paths=settings_paths),
    }
    return json.dumps(payload, sort_keys=True)
