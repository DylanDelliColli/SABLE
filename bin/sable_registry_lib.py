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
import sys

# --- v1 fleet boundary (SABLE-59t6.4) --------------------------------------
# The fleet entry points (bin/sable-launch's session door, bin/sable-spawn-
# manager) refuse when the registry resolves to a PROJECT registry AND no global
# install exists: the managers those tools stand up rely on the global registry
# + dispatch dir, which a project-only install does not provide. The remedy is
# defined ONCE here so the python consumer (sable-spawn-manager, by import) and
# the bash consumer (sable-launch, via this module's `fleet-preflight` CLI) emit
# the SAME string — no cross-language drift.
FLEET_PROJECT_ONLY_REMEDY = (
    "fleet requires the global install in v1, or export SABLE_AGENTS_YAML and "
    "SABLE_DISPATCH_DIR in the shell that creates the tmux session"
)


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


def project_registry_candidate(base: str | None = None) -> str | None:
    """The in-repo project registry path for <base> (<main-worktree-root>/.claude/
    sable/agents.yaml), whether or not it exists — or None when <base> is not
    inside a git work tree. Shared by the resolver and the scope classifier so
    the two never disagree about where the project registry would live."""
    root = _main_worktree_root(base if base else os.getcwd())
    if not root:
        return None
    return os.path.join(root, ".claude", "sable", "agents.yaml")


def sable_registry_path(base: str | None = None) -> str:
    """Resolve the agents.yaml registry path (see module docstring)."""
    override = os.environ.get("SABLE_AGENTS_YAML")
    if override:
        return override

    candidate = project_registry_candidate(base)
    if candidate and os.path.isfile(candidate):
        return candidate

    return home_registry_path()


# Friendly alias matching the shell function name at the call sites.
registry_path = sable_registry_path


def registry_scope(base: str | None = None) -> str:
    """Which resolver branch fires for <base>, without reading the file's
    contents. One of:

      'override'  $SABLE_AGENTS_YAML is set (explicit registry — fleet allowed)
      'project'   no override, and a project registry EXISTS at the repo's
                  main-worktree root (the resolver returns it)
      'global'    no override and no project registry — the resolver falls back
                  to the legacy $HOME/.claude/sable/agents.yaml path

    Kept in lock-step with sable_registry_path via project_registry_candidate."""
    if os.environ.get("SABLE_AGENTS_YAML"):
        return "override"
    candidate = project_registry_candidate(base)
    if candidate and os.path.isfile(candidate):
        return "project"
    return "global"


def home_registry_exists() -> bool:
    """Whether the global install's registry (~/.claude/sable/agents.yaml) is
    present — the marker of a global SABLE install (install.sh writes it there)."""
    return os.path.isfile(home_registry_path())


def fleet_project_only(base: str | None = None) -> bool:
    """True when fleet operations must refuse (the v1 fleet boundary,
    SABLE-59t6.4): the registry resolves to a PROJECT registry AND no global
    install exists. Setting $SABLE_AGENTS_YAML makes the scope 'override', which
    lifts the boundary — the escape hatch named in FLEET_PROJECT_ONLY_REMEDY."""
    return registry_scope(base) == "project" and not home_registry_exists()


def _main(argv: list[str]) -> int:
    """Tiny CLI so a bash caller (bin/sable-launch) reaches the SAME resolver the
    python tools import, instead of re-implementing scope detection in shell.

      sable_registry_lib.py [path] [base]   print the resolved registry path
      sable_registry_lib.py scope [base]     print override|project|global
      sable_registry_lib.py fleet-preflight [base]
                                             exit 0 (fleet allowed) or exit 3 and
                                             print FLEET_PROJECT_ONLY_REMEDY on
                                             stdout (project-only — fleet refused)
    """
    args = argv[1:]
    cmd = args[0] if args else "path"
    base = args[1] if len(args) > 1 else None
    if cmd == "scope":
        print(registry_scope(base))
        return 0
    if cmd == "fleet-preflight":
        if fleet_project_only(base):
            print(FLEET_PROJECT_ONLY_REMEDY)
            return 3
        return 0
    print(sable_registry_path(base))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
