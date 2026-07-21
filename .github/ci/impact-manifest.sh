#!/usr/bin/env bash
# impact-manifest.sh — shell impact manifest + mechanically enforced lib
# fan-out completeness check (SABLE-cmar4.2, story S1 of SABLE-cmar4, locked
# contract SABLE-z5sx3).
#
# Two declarative arrays this sources from shell-run-set.sh's sibling
# declarations (added there, not duplicated — one classification surface):
#
#   COVERS      suite name -> the production file(s) it most directly
#               exercises (space-separated repo-relative paths). A suite with
#               no COVERS entry defaults to covering itself
#               (hooks/test/<suite>) — the common self-test pattern, e.g.
#               test-lib-identity.sh directly sources lib-identity.sh to unit
#               test it.
#   LIB_FANOUT  each shared lib path (hooks/multi-manager/lib-*.sh) -> every
#               ALLOW suite whose covered file(s) source it, DIRECTLY OR
#               TRANSITIVELY (lib-A sources lib-B is included — the fan-out
#               exemplar: hooks/multi-manager/post-push-merge-notify.sh
#               sources lib-identity.sh + lib-hook-trace.sh, and
#               lib-identity.sh itself sources lib-mode-path.sh +
#               lib-registry-path.sh, so all four libs must list every suite
#               covering post-push-merge-notify.sh).
#
# fanout_check(): the completeness ENFORCEMENT. For every suite in ALLOW,
# resolves its covered file(s), greps their source/. includes for a
# hooks/multi-manager/lib-*.sh reference (recursing into whatever THOSE libs
# source, so transitive closure is caught), and asserts every lib found is
# declared in LIB_FANOUT with this suite listed. A sourced lib missing
# entirely from LIB_FANOUT, or a suite missing from an existing lib's list:
# ::error:: + non-zero exit. This is what makes "a suite starts sourcing a
# lib and nobody updates the fan-out table" a gate failure instead of a
# silent, unnoticed coverage gap (the same silent-green failure class as
# SABLE-7v3z / SABLE-lcevs, one level deeper: narrower-than-the-phenomenon).
#
# select_impacted(): the selection rule for a set of changed paths (git diff
# --name-only style, one per arg or one per line on stdin). A changed path
# that IS a declared lib (a LIB_FANOUT key) selects that lib's fan-out
# suites. A changed path matching a suite's covered file(s) selects that
# suite. Any changed path matching NEITHER a lib nor any suite's covered
# file(s) is UNMAPPED and selects the FULL ALLOW set (conservative default) —
# this is the impact tier's under-selection backstop; it is intentional that
# an unrecognized path errs toward running everything rather than guessing.
#
# Usage (CLI):
#   impact-manifest.sh --check             fan-out completeness check; exit
#                                           non-zero on any gap (see above)
#   impact-manifest.sh --select <path>...  print the suites selected for the
#                                           given changed paths, one per line
#                                           (reads stdin instead if no args)
#
# Usage (sourced, bash lib):
#   . .github/ci/impact-manifest.sh
#   sable_fanout_check                     # -> same as --check
#   sable_select_impacted path...          # -> same as --select
set -uo pipefail

CI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$CI_DIR/../.." && pwd)"

# shellcheck source=shell-run-set.sh
# Sourcing (not executing) pulls in ALLOW, COVERS, LIB_FANOUT and the
# sourced-vs-executed guard shell-run-set.sh already defines for exactly this
# purpose (SABLE-cmar4.1 set the precedent with test-tiers.sh).
. "$CI_DIR/shell-run-set.sh"

# --- direct lib-sourcing detector -------------------------------------------
# Prints, one per line, the hooks/multi-manager/lib-*.sh basenames that the
# file at absolute path $1 sources directly. Matches both dot-include forms
# ("source ...lib-foo.sh" and ". ...lib-foo.sh") regardless of how the
# directory prefix before the filename is spelled (a literal path, or a
# $(dirname ...) expression) — the filename itself is the identifying token.
#
# Anchored to the START of the (optionally indented) line: a real source
# statement in this codebase is always its own statement, never embedded
# mid-line. This deliberately does NOT match "source"/"." appearing inside a
# comment (e.g. lib-identity.sh's own usage-example header line, or
# control-trace.sh's "does NOT source lib-hook-trace.sh" note) — a
# non-anchored match on those false-positived in early testing.
#
# Scoped to libs that actually live in hooks/multi-manager/: a suite may
# source an unrelated test-helper also named lib-*.sh (e.g.
# hooks/test/lib-git-sandbox.sh) that is out of this checker's scope per
# SABLE-cmar4.2 ("each shared lib (hooks/multi-manager/lib-*.sh)") — filtered
# out here rather than left for the caller to mis-flag as a shared lib.
_libs_sourced_by() {
  local file="$1" lib
  [ -f "$file" ] || return 0
  grep -oE '^[[:space:]]*(source|\.)[[:space:]]+.*lib-[A-Za-z0-9_-]+\.sh' "$file" \
    | grep -oE 'lib-[A-Za-z0-9_-]+\.sh' \
    | sort -u \
    | while IFS= read -r lib; do
        [ -f "$REPO/hooks/multi-manager/$lib" ] && printf '%s\n' "$lib"
      done
}

# Transitive closure of libs reachable from absolute file path $1: every lib
# it sources directly, plus every lib THOSE libs source, fixed-point
# (repeats until nothing new appears — small N, no need for anything
# fancier than a worklist).
_libs_closure() {
  local file="$1" lib next found
  local -a queue=() seen=()
  while IFS= read -r lib; do
    [ -z "$lib" ] && continue
    queue+=("$lib")
  done < <(_libs_sourced_by "$file")
  while [ "${#queue[@]}" -gt 0 ]; do
    lib="${queue[0]}"
    queue=("${queue[@]:1}")
    found=0
    for next in "${seen[@]:-}"; do [ "$next" = "$lib" ] && { found=1; break; }; done
    [ "$found" -eq 1 ] && continue
    seen+=("$lib")
    while IFS= read -r next; do
      [ -z "$next" ] && continue
      queue+=("$next")
    done < <(_libs_sourced_by "$REPO/hooks/multi-manager/$lib")
  done
  printf '%s\n' "${seen[@]:-}"
}

# _covered_files <suite>: prints the repo-relative production file path(s)
# the suite covers, one per line — its COVERS entry if declared, else its
# own hooks/test/<suite> path.
_covered_files() {
  local suite="$1"
  if [ -n "${COVERS[$suite]:-}" ]; then
    printf '%s\n' ${COVERS[$suite]}
  else
    printf 'hooks/test/%s\n' "$suite"
  fi
}

sable_fanout_check() {
  local suite path abspath lib libpath errors=0
  for suite in "${ALLOW[@]}"; do
    while IFS= read -r path; do
      [ -z "$path" ] && continue
      abspath="$REPO/$path"
      while IFS= read -r lib; do
        [ -z "$lib" ] && continue
        libpath="hooks/multi-manager/$lib"
        if [ -z "${LIB_FANOUT[$libpath]:-}" ]; then
          echo "::error::impact-manifest: $suite (via $path) sources $lib, but $libpath has no LIB_FANOUT entry in .github/ci/shell-run-set.sh (SABLE-cmar4.2)"
          errors=$((errors+1))
        elif ! printf ' %s ' "${LIB_FANOUT[$libpath]}" | grep -q " $suite "; then
          echo "::error::impact-manifest: $suite sources $lib (via $path) but is missing from $libpath's LIB_FANOUT entry in .github/ci/shell-run-set.sh (SABLE-cmar4.2)"
          errors=$((errors+1))
        fi
      done < <(_libs_closure "$abspath")
    done < <(_covered_files "$suite")
  done
  if [ "$errors" -gt 0 ]; then
    echo "impact-manifest --check: $errors completeness error(s)"
    return 1
  fi
  echo "impact-manifest --check: complete — every lib a covered suite sources (directly or transitively) is fanned out"
  return 0
}

sable_select_impacted() {
  local -a paths=("$@")
  local p lib suite cf matched
  declare -A selected=()
  if [ "${#paths[@]}" -eq 0 ]; then
    while IFS= read -r p; do
      [ -n "$p" ] && paths+=("$p")
    done
  fi
  for p in "${paths[@]}"; do
    matched=0
    if [ -n "${LIB_FANOUT[$p]:-}" ]; then
      matched=1
      for suite in ${LIB_FANOUT[$p]}; do selected[$suite]=1; done
    fi
    for suite in "${ALLOW[@]}"; do
      while IFS= read -r cf; do
        [ "$cf" = "$p" ] && { matched=1; selected[$suite]=1; }
      done < <(_covered_files "$suite")
    done
    if [ "$matched" -eq 0 ]; then
      # UNMAPPED path: conservative default is the full ALLOW set, and it
      # dominates any partial selection so far — print it and stop.
      printf '%s\n' "${ALLOW[@]}"
      return 0
    fi
  done
  if [ "${#selected[@]}" -gt 0 ]; then
    printf '%s\n' "${!selected[@]}"
  fi
}

# --- CLI dispatch (only when executed directly, not sourced) ---------------
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  case "${1:-}" in
    --check)  sable_fanout_check ;;
    --select) shift; sable_select_impacted "$@" ;;
    *) echo "usage: $0 --check | --select <path>..." >&2; exit 2 ;;
  esac
fi
