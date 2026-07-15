#!/usr/bin/env python3
"""sable_registry_lib — project-first agents.yaml registry-path resolver.

Python MIRROR of hooks/multi-manager/lib-registry-path.sh (SABLE-59t6.1), used
by the bin tools (sable-spawn-manager, sable-agents). Kept as a copy rather than
a shell-out for the same reason bin/sable-mode mirrors lib-mode-path.sh:
post-install there is no stable path from a ~/.local/bin symlink to the
~/.claude/hooks copy (install.sh COPIES hooks; sable-bin-install SYMLINKS bin
tools), so shelling out to the .sh resolver would break under --copy installs.
The two stay honest by the shared test contract (test-lib-registry-path.sh +
test-registry.sh assert both resolve the same file for the same env).

sable_registry_path([base]) resolves the registry path in order:
  1. $SABLE_AGENTS_YAML if set and non-empty (unified override — the successor to
     the deprecated SABLE_REGISTRY alias, SABLE-k7mx).
  2. <main-worktree-root>/.claude/sable/agents.yaml IF that file exists
     (project-first: a repo ships its own registry; every linked worktree
     resolves to the MAIN checkout via the shared git common-dir).
  3. ~/.claude/sable/agents.yaml (legacy global path; also the byte-identical
     dormant fallback when no project registry exists anywhere).
"""
from __future__ import annotations

import os
import subprocess


def home_registry_path() -> str:
    """The legacy global registry path (~/.claude/sable/agents.yaml).

    Uses ${HOME} verbatim (empty string when unset) to stay byte-identical with
    the shell default ``${HOME:-}/.claude/sable/agents.yaml`` the converted
    consumers previously inlined."""
    return os.path.join(os.environ.get("HOME", ""), ".claude", "sable", "agents.yaml")


def _main_worktree_root(base: str) -> str | None:
    """The main checkout root for <base> — the parent of the shared git
    common-dir, so every linked worktree of a project maps to one shared path.
    None when <base> is not inside a git work tree (mirrors the shell resolver's
    silent git failure). No git.exe on PATH is treated the same as not-in-git."""
    try:
        proc = subprocess.run(
            ["git", "-C", base, "rev-parse", "--git-common-dir"],
            capture_output=True, text=True,
        )
    except (OSError, ValueError):
        return None
    common = (proc.stdout or "").strip()
    if proc.returncode != 0 or not common:
        return None
    if not os.path.isabs(common):
        common = os.path.join(base, common)
    try:
        # os.path.realpath mirrors the shell's `cd "$(dirname "$common")" && pwd`
        # (absolute, symlinks resolved).
        return os.path.realpath(os.path.dirname(common))
    except OSError:
        return None


def sable_registry_path(base: str | None = None) -> str:
    """Resolve the agents.yaml registry path (see module docstring)."""
    override = os.environ.get("SABLE_AGENTS_YAML")
    if override:
        return override

    root = _main_worktree_root(base if base else os.getcwd())
    if root:
        candidate = os.path.join(root, ".claude", "sable", "agents.yaml")
        if os.path.isfile(candidate):
            return candidate

    return home_registry_path()


# Friendly alias matching the shell function name at the call sites.
registry_path = sable_registry_path
