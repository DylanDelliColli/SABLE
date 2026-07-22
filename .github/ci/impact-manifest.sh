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
# suites. A changed path matching a suite's covered file(s) — which now
# ALWAYS includes the suite's own hooks/test/<suite> path, whether or not it
# also has a COVERS entry (SABLE-m4exv) — selects that suite. A bin/test_*.py
# pytest file maps to its production companion's coverage, if any
# (SABLE-m4exv — see _py_test_companion). Any changed path matching none of
# the above is UNMAPPED and selects the FULL ALLOW set (conservative
# default) — this is the impact tier's under-selection backstop; it is
# intentional that an unrecognized path errs toward running everything
# rather than guessing, and this fix does not touch that direction.
#
# test_coverage_check(): a SEPARATE completeness enforcement (SABLE-m4exv),
# scoped to test files specifically — every git-tracked bin/test_*.py and
# hooks/test/test-*.sh must resolve to a matched (non-full-set) selection
# rule, or the gate errors naming the path. This is what caught "test files
# are unmapped as a class" in the first place and is what stops a NEW
# unmapped test-file class from recurring silently.
#
# Usage (CLI):
#   impact-manifest.sh --check             both completeness checks above;
#                                           exit non-zero on any gap
#   impact-manifest.sh --check-test-coverage
#                                           just the test-file completeness
#                                           check
#   impact-manifest.sh --select <path>...  print the suites selected for the
#                                           given changed paths, one per line
#                                           (reads stdin instead if no args)
#
# Usage (sourced, bash lib):
#   . .github/ci/impact-manifest.sh
#   sable_check_all                        # -> same as --check
#   sable_fanout_check                     # just the lib fan-out check
#   sable_test_coverage_check              # just the test-file check
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
# the suite covers, one per line — ALWAYS its own hooks/test/<suite> path
# (SABLE-m4exv: a suite is itself a valid change target — editing a suite's
# own test file, e.g. adding a case, must select that suite rather than
# falling through to UNMAPPED), plus its COVERS entry if declared. Before
# this fix, a suite WITH a COVERS entry lost its self-mapping entirely (only
# a suite with NO entry defaulted to covering itself), which is why editing
# hooks/test/test-optimistic-promotion.sh — a suite with 5 COVERS files —
# escalated to the full ALLOW set instead of selecting itself.
_covered_files() {
  local suite="$1"
  printf 'hooks/test/%s\n' "$suite"
  if [ -n "${COVERS[$suite]:-}" ]; then
    printf '%s\n' ${COVERS[$suite]}
  fi
}

# _py_test_companion <path>: for a pytest test file bin/test_X.py or
# bin/test_X_integration.py, prints the repo-relative production file it
# tests, bin/X.py — empty otherwise. SABLE-m4exv: bin/test_*.py has no shell
# suite of its own (it belongs to the SEPARATE pytest/testmon-scoped half of
# the tier, bin/tier_selection.py), so treating it as literally unmapped
# escalated the shell half to the full ALLOW set on every pytest-only change
# — the single largest instance of "test files are unmapped as a class".
# Mapping it to its production companion's shell coverage (if any) keeps the
# shell selection honest without duplicating the pytest tier's own scoping.
_py_test_companion() {
  local path="$1" base
  case "$path" in
    bin/test_*.py)
      base="${path#bin/test_}"
      base="${base%_integration.py}"
      base="${base%.py}"
      printf 'bin/%s.py\n' "$base"
      ;;
  esac
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

# _match_path <path>: classifies ONE changed path against the manifest.
# Prints "1" (matched) or "0" (unmapped) on its first line, followed by every
# suite the path selects (may be zero lines even when matched=1 — see the
# pytest-companion case below). Factored out of sable_select_impacted so
# sable_test_coverage_check (below) can classify a path the same way without
# duplicating the matching rules — two copies of "what counts as mapped" is
# exactly the drift class this whole manifest exists to prevent.
#
# A path matches when it is a declared lib (LIB_FANOUT key), a declared or
# self covered file for some ALLOW suite (_covered_files), OR — SABLE-m4exv —
# a bin/test_*.py pytest file whose production companion (_py_test_companion)
# matches either of those. The companion case can match with ZERO selected
# suites: a pytest file whose companion has no shell coverage is still a
# KNOWN, CLASSIFIED path (the pytest/testmon half of the tier owns its
# scoping), not an unmapped one that should escalate the shell half to full.
_match_path() {
  local p="$1" lib suite cf matched=0 companion
  declare -A sel=()
  if [ -n "${LIB_FANOUT[$p]:-}" ]; then
    matched=1
    for suite in ${LIB_FANOUT[$p]}; do sel[$suite]=1; done
  fi
  for suite in "${ALLOW[@]}"; do
    while IFS= read -r cf; do
      [ "$cf" = "$p" ] && { matched=1; sel[$suite]=1; }
    done < <(_covered_files "$suite")
  done
  if [ "$matched" -eq 0 ]; then
    companion="$(_py_test_companion "$p")"
    if [ -n "$companion" ]; then
      matched=1
      if [ -n "${LIB_FANOUT[$companion]:-}" ]; then
        for suite in ${LIB_FANOUT[$companion]}; do sel[$suite]=1; done
      fi
      for suite in "${ALLOW[@]}"; do
        while IFS= read -r cf; do
          [ "$cf" = "$companion" ] && sel[$suite]=1
        done < <(_covered_files "$suite")
      done
    fi
  fi
  if [ "$matched" -eq 0 ] && [ -n "${EXCLUDE[${p#hooks/test/}]:-}" ] && [ "$p" != "${p#hooks/test/}" ]; then
    # p is itself an EXCLUDE-listed suite's own file (e.g.
    # hooks/test/test-install.sh) — it is deliberately absent from ALLOW, so
    # it has no suites to select, but it is a KNOWN path, not an unmapped
    # one: escalating the whole ALLOW set because a suite the tier never
    # runs got edited would be exactly the bug this file fixes, one class
    # over (SABLE-m4exv).
    matched=1
  fi
  echo "$matched"
  printf '%s\n' "${!sel[@]}"
}

# sable_select_impacted also EMITS an observability line to stderr
# (SABLE-m4exv, cockpit-raised gap): before this, --select had NO mode/reason
# output at all — a FULL answer from the correct, intentional conservative
# fallback was byte-for-byte identical to a FULL answer from a genuinely
# unmapped diff, so nothing about a run ever revealed WHICH one occurred.
# That silence is exactly how "test files are unmapped as a class" survived
# undetected: the escalation fired on nearly every promote and the tool
# never once said why. The line is prefixed "::notice::"/"::warning::" so it
# rides the SAME GitHub-Actions-annotation convention this file already uses
# for ::error:: — _selected_suites in bin/sable_gate_promote_lib.py already
# strips any line starting with "::" from the suites it parses (that
# filtering predates this change), so this needed no changes on the Python
# side and stdout stays a clean, unpolluted suite list either way.
sable_select_impacted() {
  local -a paths=("$@")
  local p suite matched line
  declare -A selected=()
  local -a unmapped=()
  if [ "${#paths[@]}" -eq 0 ]; then
    while IFS= read -r p; do
      [ -n "$p" ] && paths+=("$p")
    done
  fi
  for p in "${paths[@]}"; do
    local -a result=()
    while IFS= read -r line; do result+=("$line"); done < <(_match_path "$p")
    matched="${result[0]}"
    if [ "$matched" -eq 0 ]; then
      unmapped+=("$p")
      continue
    fi
    for suite in "${result[@]:1}"; do
      [ -n "$suite" ] && selected[$suite]=1
    done
  done
  if [ "${#unmapped[@]}" -gt 0 ]; then
    # One or more UNMAPPED paths: conservative default is the full ALLOW
    # set, and it dominates any partial selection collected above — the
    # unmapped paths are named so the fallback is diagnosable, not just
    # observable as a bare count.
    echo "::notice::impact-manifest: FULL -- unmapped path(s): ${unmapped[*]}" >&2
    printf '%s\n' "${ALLOW[@]}"
    return 0
  fi
  echo "::notice::impact-manifest: SCOPED -- ${#selected[@]} suite(s) from ${#paths[@]} mapped path(s)" >&2
  if [ "${#selected[@]}" -gt 0 ]; then
    printf '%s\n' "${!selected[@]}"
  fi
}

# --- Mechanical completeness check for TEST FILES AS A CLASS (SABLE-m4exv) -
# The defect this bead fixes: bin/test_*.py and hooks/test/test-*.sh matched
# NO manifest rule at all, so a change to one silently fell through to the
# UNMAPPED full-ALLOW-set fallback on EVERY compliant branch (this fleet's
# prime directive requires a test file with every change) — the conservative
# backstop had silently become the primary path instead of the rare case it
# was designed for (see this file's module comment; the fallback direction
# itself is correct and must not be inverted).
#
# sable_test_coverage_check walks every git-tracked path in those two
# classes and asserts _match_path resolves it (matched=1), erroring — naming
# the path — for any that still fall through. This is what makes a NEW
# unmapped test-file class fail the gate at introduction instead of showing
# up later as an unexplained full-set run. A path can be excused via
# TEST_COVERAGE_EXEMPT below (with a reason), the same "state the exemption,
# don't silently allow it" shape as shell-run-set.sh's own EXCLUDE table.
TEST_COVERAGE_EXEMPT=(
)

sable_test_coverage_check() {
  local path matched errors=0 f
  local -a tracked=()
  while IFS= read -r f; do
    [ -n "$f" ] && tracked+=("$f")
  done < <(cd "$REPO" && git ls-files 'bin/test_*.py' 'hooks/test/test-*.sh' 2>/dev/null)
  for path in "${tracked[@]}"; do
    if in_array "$path" "${TEST_COVERAGE_EXEMPT[@]:-}"; then
      continue
    fi
    matched="$(_match_path "$path" | head -1)"
    if [ "$matched" -ne 1 ]; then
      echo "::error::impact-manifest: $path (a test file) matches no selection rule — it would escalate to the full ALLOW set on every change touching it. Add a COVERS/self-mapping rule or an explicit TEST_COVERAGE_EXEMPT entry with a reason (SABLE-m4exv)."
      errors=$((errors+1))
    fi
  done
  if [ "$errors" -gt 0 ]; then
    echo "impact-manifest --check-test-coverage: $errors unmapped test file(s)"
    return 1
  fi
  echo "impact-manifest --check-test-coverage: complete — every tracked test file resolves to a scoped selection rule (${#tracked[@]} checked)"
  return 0
}

# sable_check_all: both mechanical completeness checks, run to completion
# (not short-circuited) so a single --check invocation reports every gap in
# one pass rather than making the author fix-and-rerun to discover the next
# one. Wired as the --check CLI mode, which is ci-verify.yml's required gate
# step — this is what makes sable_test_coverage_check load-bearing on merge
# with no workflow changes (SABLE-m4exv).
sable_check_all() {
  local rc=0
  sable_fanout_check || rc=1
  sable_test_coverage_check || rc=1
  return "$rc"
}

# --- CLI dispatch (only when executed directly, not sourced) ---------------
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  case "${1:-}" in
    --check)  sable_check_all ;;
    --check-test-coverage) sable_test_coverage_check ;;
    --select) shift; sable_select_impacted "$@" ;;
    *) echo "usage: $0 --check | --check-test-coverage | --select <path>..." >&2; exit 2 ;;
  esac
fi
