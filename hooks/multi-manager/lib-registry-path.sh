#!/usr/bin/env bash
# lib-registry-path.sh — resolve the SABLE agent-registry (agents.yaml) path
# (SABLE-59t6.1)
#
# Single project-first resolver shared by every consumer that reads agents.yaml
# (hooks/multi-manager/lib-identity.sh + mode-interlock.sh) so a repo can ship
# its OWN registry and every linked worktree resolves to the MAIN checkout's
# copy. Consolidates four copy-pasted `${SABLE_AGENTS_YAML:-${HOME}/…}` inline
# defaults into one function. Modeled line-for-line on lib-mode-path.sh's
# git-common-dir resolution (SABLE-5hck.1) — the only substantive difference is
# the file-existence gate on the in-repo path (see the note on step 2 below).
#
# bin/sable_registry_lib.py is the python MIRROR (used by sable-spawn-manager and
# sable-agents), kept as a copy rather than a shared source for the same reason
# bin/sable-mode mirrors lib-mode-path.sh: post-install there is no stable path
# from a ~/.local/bin symlink to the ~/.claude/hooks copy (install.sh COPIES
# hooks; sable-bin-install SYMLINKS bin tools), so a shared source would break
# under --copy installs.
#
# sable_registry_path [base_dir]
#   Prints the registry path. Resolution order:
#     1. $SABLE_AGENTS_YAML if set and non-empty (unified override env — the
#        successor to the deprecated SABLE_REGISTRY alias, SABLE-k7mx).
#     2. base_dir (default: PWD) inside a git work tree → the MAIN worktree root
#        (the parent of the shared git common-dir, so every linked worktree of a
#        project resolves to ONE shared file) + /.claude/sable/agents.yaml,
#        but ONLY when that file EXISTS (the project ships its own registry).
#     3. otherwise → $HOME/.claude/sable/agents.yaml (legacy global path).
#
# The file-existence check in step 2 is the deliberate departure from
# lib-mode-path.sh: the mode-state path is always the in-repo path when in git,
# but the registry FALLS THROUGH to the global $HOME path when the repo has no
# agents.yaml of its own. That keeps a repo without a project registry on today's
# exact global-registry behavior — registry absent everywhere is the dormant
# fail-open path, byte-identical to the `${SABLE_AGENTS_YAML:-${HOME:-}/…}`
# default these consumers replaced.
#
# Callers that run inside hooks should pass the hook-input cwd as base_dir (the
# directory git actually operates in), mirroring sable_resolve_push_repo_dir in
# lib-identity.sh (SABLE-041) rather than trusting the shell PWD.
sable_registry_path() {
  if [ -n "${SABLE_AGENTS_YAML:-}" ]; then
    printf '%s\n' "$SABLE_AGENTS_YAML"
    return 0
  fi

  local base="${1:-$PWD}"
  local common root candidate

  # --git-common-dir is shared across all worktrees of a project: for a linked
  # worktree it points at the MAIN repo's .git, so dirname gives the main
  # checkout root and every worktree maps to the same registry file. Available
  # since git 2.5 (avoid --path-format, which is 2.31+, by joining manually).
  if common="$(git -C "$base" rev-parse --git-common-dir 2>/dev/null)" && [ -n "$common" ]; then
    case "$common" in
      /*) ;;                        # absolute (typical for linked worktrees)
      *)  common="$base/$common" ;; # relative (".git") — join onto base
    esac
    if root="$(cd "$(dirname "$common")" 2>/dev/null && pwd)" && [ -n "$root" ]; then
      candidate="$root/.claude/sable/agents.yaml"
      if [ -f "$candidate" ]; then
        printf '%s\n' "$candidate"
        return 0
      fi
    fi
  fi

  printf '%s\n' "${HOME:-}/.claude/sable/agents.yaml"
}
