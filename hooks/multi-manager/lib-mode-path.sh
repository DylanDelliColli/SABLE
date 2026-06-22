#!/usr/bin/env bash
# lib-mode-path.sh — resolve the SABLE mode-state file path (SABLE-5hck.1)
#
# Single resolver shared by the mode machinery so a session's orchestration mode
# lives in the REPO it operates on, not one global file. That lets independent
# SABLE sessions run in different repos at the same time (one planning, one
# executing) without clobbering each other's mode.
#
# Sourced by hooks/multi-manager/lib-identity.sh and mode-interlock.sh (siblings,
# copied together to ~/.claude/hooks/multi-manager/). bin/sable-mode keeps a
# MIRRORED copy of this logic rather than sourcing it: post-install there is no
# stable relative path from the binary to this file (install.sh COPIES hooks to
# ~/.claude/hooks while sable-bin-install SYMLINKS bin tools to ~/.local/bin), so
# a shared source would break under --copy installs. The mirror is kept honest by
# a drift-guard test (test-sable-mode.sh, SABLE-5hck.2) that asserts the binary's
# `sable-mode path` agrees with this function for the same inputs.
#
# sable_mode_state_path [base_dir]
#   Prints the mode-state file path. Resolution order:
#     1. $SABLE_MODE_STATE if set and non-empty (test override + manual escape
#        hatch — unified override env var, see SABLE-d50.4).
#     2. base_dir (default: PWD) inside a git work tree → the MAIN worktree root
#        (the parent of the shared git common-dir, so EVERY linked worktree of a
#        project resolves to one shared file) + /.claude/sable/state/mode-state.json
#     3. otherwise → $HOME/.claude/sable/state/mode-state.json (legacy global path,
#        preserves single-repo and non-git behavior unchanged).
#
# Callers that run inside hooks should pass the hook-input cwd as base_dir (the
# directory git actually operates in), mirroring sable_resolve_push_repo_dir in
# lib-identity.sh (SABLE-041) rather than trusting the shell PWD.
sable_mode_state_path() {
  if [ -n "${SABLE_MODE_STATE:-}" ]; then
    printf '%s\n' "$SABLE_MODE_STATE"
    return 0
  fi

  local base="${1:-$PWD}"
  local common root

  # --git-common-dir is shared across all worktrees of a project: for a linked
  # worktree it points at the MAIN repo's .git, so dirname gives the main
  # checkout root and every worktree maps to the same state file. Available
  # since git 2.5 (avoid --path-format, which is 2.31+, by joining manually).
  if common="$(git -C "$base" rev-parse --git-common-dir 2>/dev/null)" && [ -n "$common" ]; then
    case "$common" in
      /*) ;;                        # absolute (typical for linked worktrees)
      *)  common="$base/$common" ;; # relative (".git") — join onto base
    esac
    if root="$(cd "$(dirname "$common")" 2>/dev/null && pwd)" && [ -n "$root" ]; then
      printf '%s\n' "$root/.claude/sable/state/mode-state.json"
      return 0
    fi
  fi

  printf '%s\n' "${HOME:-}/.claude/sable/state/mode-state.json"
}
