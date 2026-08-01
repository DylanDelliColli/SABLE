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
# pytest file maps to its production companion's coverage, if any. A changed
# bin/X.py with a matching bin/test_X.py is likewise classified as Python-
# owned after any explicit shell COVERS entries are selected
# (SABLE-m4exv/SABLE-z3j28.5 — see the companion helpers). A changed path that
# the golden install manifest pins a hash for additionally selects
# test-install-golden-manifest.sh (SABLE-slip0.7 — see the pin rule below).
# Install-surface families (immediate skills/<dir>/ files with SKILL.md,
# immediate hooks/multi-manager/*.sh, BASE_HOOK_FILES members) additionally
# select test-orchestration-install.sh by rule, and the declared classes
# ZERO_IMPACT / DECLARED_BROAD / PY_OWNED classify with zero, full, and
# zero-shell selections respectively (SABLE-y4nom.7.1).
#
# THE TWO ABSENCE OUTCOMES AND THE ERROR DIRECTION (SABLE-y4nom.7.1 —
# replaces the old UNMAPPED->FULL conservative fallback, which had silently
# become the primary path: 111 of 434 tracked paths escalated to the full
# 96-suite set on every touch):
#   * A changed path ABSENT from the tree is the DELETED class — checked
#     BEFORE any table row or rule, so a stale row or generic family match
#     can never narrow a deletion — and deliberately selects the FULL set
#     with a deletion-naming notice (its classification row leaves with it).
#   * A changed path PRESENT but matching no classification is an ERROR
#     naming the path and the remediation menu, with EMPTY stdout — never
#     FULL and never zero. The author picks the class deliberately.
#
# test_coverage_check(): the completeness enforcement, generalized by
# SABLE-y4nom.7.1 from the original test-files-only walk (SABLE-m4exv) to
# EVERY git-tracked path: each must resolve to exactly one classification
# (mapped-family / ZERO_IMPACT / DECLARED_BROAD / PY_OWNED), table rows must
# point at present Python-eligible paths where claimed, and contradictions
# across static tables AND dynamic rules (golden pins, install surface,
# python convention, suite self-mapping, pytest companions) are gate errors.
#
# Usage (CLI):
#   impact-manifest.sh --check             both completeness checks above;
#                                           exit non-zero on any gap
#   impact-manifest.sh --check-test-coverage
#                                           just the every-tracked-path
#                                           classification walk
#   impact-manifest.sh --select <path>...  print the suites selected for the
#                                           given changed paths, one per line
#                                           (reads stdin instead if no args)
#
# Usage (sourced, bash lib):
#   . .github/ci/impact-manifest.sh
#   sable_check_all                        # -> same as --check
#   sable_fanout_check                     # just the lib fan-out check
#   sable_test_coverage_check              # just the classification walk
#   sable_select_impacted path...          # -> same as --select
set -uo pipefail

CI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$CI_DIR/../.." && pwd)"

# shellcheck source=shell-run-set.sh
# Sourcing (not executing) pulls in ALLOW, COVERS, LIB_FANOUT and the
# sourced-vs-executed guard shell-run-set.sh already defines for exactly this
# purpose (SABLE-cmar4.1 set the precedent with test-tiers.sh).
. "$CI_DIR/shell-run-set.sh"

# SABLE-y4nom.7.1 classification tables. Declared in shell-run-set.sh beside
# COVERS (one classification surface); defaulted empty here so a consumer
# repo carrying an older shell-run-set copy degrades to the pre-migration
# behavior instead of aborting under `set -u`.
declare -p ZERO_IMPACT   >/dev/null 2>&1 || declare -A ZERO_IMPACT=()
declare -p DECLARED_BROAD >/dev/null 2>&1 || declare -A DECLARED_BROAD=()
declare -p PY_OWNED      >/dev/null 2>&1 || PY_OWNED=()

# The remediation menu every unknown-path error names (SABLE-y4nom.7.1):
# an unknown is an ERROR, never FULL and never zero — the author picks the
# class deliberately.
SABLE_CLASS_REMEDIATION="add a COVERS entry mapping it to its consuming ALLOW suite(s), a ZERO_IMPACT entry with a reason, a DECLARED_BROAD entry with a reason, or a PY_OWNED entry (python-lane-owned), in .github/ci/shell-run-set.sh (SABLE-y4nom.7.1)"

# Install-surface rule (SABLE-y4nom.7.1, transitive tested-entrypoint
# closure): test-orchestration-install.sh executes the REAL installer, which
# globs every skills/*/ file and every hooks/multi-manager/*.sh, and copies
# each BASE_HOOK_FILES member — so those families select that suite BY RULE
# (new files inherit automatically; per-path rows would rot). The member
# list is read from the installer itself so the rule tracks its source.
# Guarded on the suite being in ALLOW so fixture universes without it (and
# repos without the installer) leave the rule inert.
_INSTALL_SURFACE_SUITE="test-orchestration-install.sh"
_installer_base_hooks() {
  sed -n 's/^BASE_HOOK_FILES="\(.*\)"$/\1/p' "$REPO/bin/sable-orchestration-install" 2>/dev/null | head -1
}
# The rule mirrors the installer's ACTUAL read surface (B2 correction —
# verified against bin/sable-orchestration-install :811 and :900-908):
# IMMEDIATE hooks/multi-manager/*.sh only (bash case '*' matches '/', so the
# nested-descendant exclusion is explicit), and IMMEDIATE files of a skill
# dir ONLY when that dir carries SKILL.md (the installer's own gate).
_install_surface_rule_matches() {
  local p="$1" member rest dir file
  in_array "$_INSTALL_SURFACE_SUITE" "${ALLOW[@]}" || return 1
  case "$p" in
    skills/*/*/*) return 1 ;;
    skills/*/*)
      # Dot-glob boundary (codex round-1 addendum): the installer globs with
      # dotglob OFF, so a hidden skill dir or hidden skill file is never
      # read — and bash `case *` WOULD match leading dots, so the exclusion
      # is explicit. Actual-read rule, not a prefix approximation.
      rest="${p#skills/}"
      dir="${rest%%/*}"
      file="${rest#*/}"
      case "$dir" in .*) return 1 ;; esac
      case "$file" in .*) return 1 ;; esac
      [ -f "$REPO/skills/$dir/SKILL.md" ] && return 0
      return 1
      ;;
    hooks/multi-manager/*.sh)
      rest="${p#hooks/multi-manager/}"
      case "$rest" in */*) return 1 ;; esac
      case "$rest" in .*) return 1 ;; esac
      return 0
      ;;
    hooks/*)
      for member in $(_installer_base_hooks); do
        [ "$p" = "hooks/$member" ] && return 0
      done
      ;;
  esac
  return 1
}

# The golden install-manifest relation (SABLE-slip0.7). Sourced — not
# duplicated — so the selection rule below and the suite that asserts the
# golden read the SAME mapping. Guarded because the two test suites for this
# file build fixture repos that carry only these .github/ci scripts: there the
# rule is simply inert, which is what their negative controls exercise.
GOLDEN_LIB="$REPO/hooks/test/lib-golden-manifest.sh"
# shellcheck source=../../hooks/test/lib-golden-manifest.sh
[ -f "$GOLDEN_LIB" ] && . "$GOLDEN_LIB"

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
# --- golden install-manifest pin rule (SABLE-slip0.7) -----------------------
# hooks/test/fixtures/install-golden-manifest.txt pins a sha256 for every
# artifact install.sh lands. Its historical consumer, test-install.sh, is
# PERMANENTLY excluded from the run-set (it needs a real ~/.claude install;
# SABLE-59zu), so the golden had no executor in the gate at all — an installed
# artifact could change while its pinned hash stayed behind and nothing went
# red. Three entries had already rotted that way when this rule was written.
#
# The rule: a changed path that the golden pins a hash FOR selects
# test-install-golden-manifest.sh, the clean-room-runnable suite that compares
# each pin against sha256 of its repo source. This is what makes "edit a hook,
# forget the manifest" a proportional-run failure instead of a silent landing.
#
# DERIVED, NOT DECLARED. The pinned set comes from reading the golden itself
# (golden_pinned_sources), not from a ~50-path COVERS string. A hand-copied list
# would need its own completeness gate and would rot the first time someone
# regenerated the golden without updating it — the exact drift class this file
# exists to prevent. Loaded once and memoized: _match_path is called per tracked
# test file by sable_test_coverage_check, and re-reading the fixture ~110 times
# is pure waste.
declare -A GOLDEN_PINS=()
GOLDEN_PINS_LOADED=0
_load_golden_pins() {
  [ "$GOLDEN_PINS_LOADED" -eq 1 ] && return 0
  GOLDEN_PINS_LOADED=1
  # Optional load-count trace (SABLE-y4nom.7.1 perf regression control): the
  # memo guard above is per-SHELL, so a caller that first touches the pins
  # inside a command/process substitution loads them in a throwaway subshell
  # and the parent pays again — measured reparsing the golden ~436 times
  # (~52s walk) before the parents preloaded. Real loads append here so a
  # test can assert the count stays small; time is never asserted (the
  # SABLE-af7u4 wall-clock rule).
  [ -n "${SABLE_GOLDEN_PIN_TRACE:-}" ] && echo load >> "$SABLE_GOLDEN_PIN_TRACE"
  command -v golden_pinned_sources >/dev/null 2>&1 || return 0
  local src
  while IFS= read -r src; do
    [ -n "$src" ] && GOLDEN_PINS["$src"]=1
  done < <(golden_pinned_sources)
}

_covered_files() {
  # Optional call trace (SABLE-y4nom.7.1 no-fork structural control): the
  # per-path selection MUST NOT come through here — see COVERED_BY below.
  [ -n "${SABLE_COVERED_TRACE:-}" ] && echo call >> "$SABLE_COVERED_TRACE"
  local suite="$1"
  printf 'hooks/test/%s\n' "$suite"
  if [ -n "${COVERS[$suite]:-}" ]; then
    printf '%s\n' ${COVERS[$suite]}
  fi
}

# COVERED_BY: reverse index path -> "suite ..." over ALLOW self-mappings and
# COVERS values, built ONCE per shell (SABLE-y4nom.7.1 perf blocker: the
# per-path ALLOW loop invoked _covered_files through a process substitution
# per suite — ~97 forks per classified path, ~42k child shells across the
# 436-path walk). Same semantics as _covered_files, indexed: every ALLOW
# suite covers its own hooks/test/<suite> file plus its COVERS entries.
declare -A COVERED_BY=()
COVERED_INDEX_BUILT=0
_build_covered_index() {
  [ "$COVERED_INDEX_BUILT" -eq 1 ] && return 0
  COVERED_INDEX_BUILT=1
  local suite cf
  for suite in "${ALLOW[@]}"; do
    COVERED_BY["hooks/test/$suite"]="${COVERED_BY[hooks/test/$suite]:-} $suite"
    for cf in ${COVERS[$suite]:-}; do
      COVERED_BY["$cf"]="${COVERED_BY[$cf]:-} $suite"
    done
  done
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

# Success when a production Python module has the matching pytest file
# required by this repo's bin/*.py convention. This is checked only AFTER
# explicit shell coverage, so a module with both Python and shell integration
# coverage still selects the shell suite. A module with pytest coverage only
# is classified rather than expanding the shell side to the full ALLOW set.
_py_production_has_test() {
  local path="$1" base
  case "$path" in
    bin/*.py)
      case "$path" in bin/test_*.py) return 1 ;; esac
      base="${path#bin/}"
      base="${base%.py}"
      # SABLE-y4nom.7.1 F1: convert dashes exactly as the extensionless
      # branch below does — bin/columbo-cost-prefilter.py's test is
      # test_columbo_cost_prefilter.py, and without this conversion the
      # dash-named .py class was permanently unmapped (measured escalating
      # to the full ALLOW set on every touch).
      base="${base//-/_}"
      [ -f "$REPO/bin/test_${base}.py" ] \
        || [ -f "$REPO/bin/test_${base}_integration.py" ]
      ;;
    bin/*)
      [ -f "$REPO/$path" ] || return 1
      head -1 "$REPO/$path" | grep -q 'python' || return 1
      base="${path#bin/}"
      base="${base//-/_}"
      [ -f "$REPO/bin/test_${base}.py" ] \
        || [ -f "$REPO/bin/test_${base}_lib.py" ] \
        || [ -f "$REPO/bin/test_${base}_integration.py" ]
      ;;
    *) return 1 ;;
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
# self covered file for some ALLOW suite (_covered_files), a repo source the
# golden install manifest pins a hash for (SABLE-slip0.7), a bin/test_*.py
# pytest file whose production companion (_py_test_companion) matches either
# of those (SABLE-m4exv), an EXCLUDE-listed suite's own file, a declared
# ZERO_IMPACT / DECLARED_BROAD / PY_OWNED table row, or an install-surface
# family member (SABLE-y4nom.7.1). Several of these legitimately match with
# ZERO selected suites — classified-with-nothing-to-run is a DIFFERENT
# outcome from unclassified, which is an ERROR at selection time.
_match_path() {
  local p="$1" lib suite cf matched=0 companion
  declare -A sel=()
  if [ -n "${LIB_FANOUT[$p]:-}" ]; then
    matched=1
    for suite in ${LIB_FANOUT[$p]}; do sel[$suite]=1; done
  fi
  # The golden pin rule ADDS a suite rather than claiming the path, so it runs
  # unconditionally alongside the other rules: most pinned artifacts (every
  # shared lib, every covered hook) are already matched by COVERS/LIB_FANOUT,
  # and they need the golden suite selected TOO, not instead.
  _load_golden_pins
  if [ -n "${GOLDEN_PINS[$p]:-}" ]; then
    # Non-empty GOLDEN_PINS implies the lib was sourced, so the suite name is
    # set; the fallback keeps `set -u` from turning a future refactor into an
    # unbound-variable abort mid-selection.
    local golden_suite="${GOLDEN_MANIFEST_SUITE:-test-install-golden-manifest.sh}"
    matched=1
    sel[$golden_suite]=1
  fi
  _build_covered_index
  for suite in ${COVERED_BY[$p]:-}; do
    matched=1
    sel[$suite]=1
  done
  if [ "$matched" -eq 0 ]; then
    companion="$(_py_test_companion "$p")"
    if [ -n "$companion" ]; then
      matched=1
      if [ -n "${LIB_FANOUT[$companion]:-}" ]; then
        for suite in ${LIB_FANOUT[$companion]}; do sel[$suite]=1; done
      fi
      for suite in ${COVERED_BY[$companion]:-}; do
        sel[$suite]=1
      done
    fi
  fi
  if [ "$matched" -eq 0 ] && _py_production_has_test "$p"; then
    # The Python dependency selector owns this module. Explicit COVERS were
    # already considered above; zero shell suites here is intentional.
    matched=1
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
  # SABLE-y4nom.7.1 declared classes. ZERO_IMPACT: classified, zero shell
  # suites, with a written reason in the table (the Python lane still
  # selects wherever its own graph reaches — zero-shell is not
  # zero-validation). PY_OWNED: exact entries for python-lane-owned files
  # whose test naming defeats the convention probes above. DECLARED_BROAD is
  # classified here for the completeness walk; its full-set selection is
  # applied in sable_select_impacted where the notice can name it.
  if [ -n "${ZERO_IMPACT[$p]:-}" ]; then
    matched=1
  fi
  if [ -n "${DECLARED_BROAD[$p]:-}" ]; then
    matched=1
  fi
  if [ "$matched" -eq 0 ] && in_array "$p" "${PY_OWNED[@]:-}"; then
    matched=1
  fi
  # Install-surface rule: additive, like the golden-pin rule above — a
  # skills/mm-hook/base-hook path selects the installer suite IN ADDITION to
  # any dedicated consumers, and a NEW file in those families classifies by
  # rule instead of falling to unknown.
  if _install_surface_rule_matches "$p"; then
    matched=1
    sel[$_INSTALL_SURFACE_SUITE]=1
  fi
  echo "$matched"
  printf '%s\n' "${!sel[@]}"
}

# sable_select_impacted EMITS an observability line to stderr for every
# outcome (SABLE-m4exv established the convention; SABLE-y4nom.7.1 owns the
# current outcome set): SCOPED with a suite/path count; FULL naming its
# CAUSE, which is always deliberate — declared-broad path(s) or deleted
# path(s) — because the old anonymous unmapped->FULL fallback is GONE; and
# for an existing unclassified path, "::error::" lines naming each path and
# the remediation menu with a NONZERO exit and empty stdout. The lines ride
# the GitHub-Actions-annotation convention ("::notice::"/"::error::") —
# _selected_suites in bin/sable_gate_promote_lib.py strips "::"-prefixed
# lines from the suites it parses, and its nonzero-exit branch is the
# fail-closed path for the error outcome, so stdout stays a clean suite
# list and an unclassifiable diff can never certify anything.
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
  # Preload the golden pins AND the covered-by index in THIS shell:
  # _match_path runs inside process substitutions below, and a memo first
  # populated in a subshell is lost — measured as a per-path golden reparse
  # (and a ~97-fork covered-files sweep per path) across the walk
  # (SABLE-y4nom.7.1 perf notes at _load_golden_pins / _build_covered_index).
  _load_golden_pins
  _build_covered_index
  local -a deleted=() declared_broad=()
  for p in "${paths[@]}"; do
    # SABLE-y4nom.7.1 B1 (codex round-1): ABSENCE IS CHECKED FIRST, before
    # any table row or rule can match. A changed path absent from the
    # validated tree is the DELETED class regardless of what a stale
    # COVERS/ZERO/BROAD row or a generic family rule would have said about
    # the old name — a deleted mm-hook still pattern-matched the install
    # rule and came back SCOPED (measured), which silently narrowed the
    # deletion's blast radius to one suite. A deletion deliberately selects
    # the full set; its classification row leaves with it (no tombstones).
    # -L keeps a tracked-but-broken symlink (the AGENTS.md class) counted
    # as present rather than deleted.
    if [ ! -e "$REPO/$p" ] && [ ! -L "$REPO/$p" ]; then
      deleted+=("$p")
      continue
    fi
    if [ -n "${DECLARED_BROAD[$p]:-}" ]; then
      declared_broad+=("$p")
      continue
    fi
    local -a result=()
    while IFS= read -r line; do result+=("$line"); done < <(_match_path "$p")
    matched="${result[0]}"
    if [ "$matched" -eq 0 ]; then
      # Present but unclassifiable: an ERROR naming the remediation.
      unmapped+=("$p")
      continue
    fi
    for suite in "${result[@]:1}"; do
      [ -n "$suite" ] && selected[$suite]=1
    done
  done
  if [ "${#unmapped[@]}" -gt 0 ]; then
    # SABLE-y4nom.7.1: UNKNOWN is an ERROR naming its remediation — never
    # FULL (the old conservative default had silently become the primary
    # path: 111 of 434 tracked paths escalated to all 96 suites) and never
    # zero. Empty stdout: no suite list is certified from an error.
    for p in "${unmapped[@]}"; do
      echo "::error::impact-manifest: UNKNOWN -- unclassified path: $p. Remediation: $SABLE_CLASS_REMEDIATION" >&2
    done
    return 1
  fi
  if [ "${#declared_broad[@]}" -gt 0 ] || [ "${#deleted[@]}" -gt 0 ]; then
    # Deliberate full-set selections, each naming its cause: the broad
    # class is a declared decision, the deletion class is the no-tombstones
    # contract. Either dominates any partial selection collected above.
    [ "${#declared_broad[@]}" -gt 0 ] && \
      echo "::notice::impact-manifest: FULL -- declared-broad path(s): ${declared_broad[*]}" >&2
    [ "${#deleted[@]}" -gt 0 ] && \
      echo "::notice::impact-manifest: FULL -- deleted path(s): ${deleted[*]} (a deletion selects the full set; its classification row leaves with it)" >&2
    printf '%s\n' "${ALLOW[@]}"
    return 0
  fi
  echo "::notice::impact-manifest: SCOPED -- ${#selected[@]} suite(s) from ${#paths[@]} mapped path(s)" >&2
  if [ "${#selected[@]}" -gt 0 ]; then
    printf '%s\n' "${!selected[@]}"
  fi
}

# --- Mechanical completeness check for EVERY TRACKED PATH ------------------
# History: SABLE-m4exv built this for test files as a class, whose changes
# silently fell through to the then-standard unmapped->FULL fallback on
# every compliant branch. SABLE-y4nom.7.1 retired that fallback entirely
# (UNKNOWN is now a selection ERROR; FULL is reserved for the declared-broad
# and deleted classes) and generalized the walk to all tracked paths — this
# check is what makes the error direction safe to hold, because a fully
# classified tree never produces the error on ordinary work.
#

sable_test_coverage_check() {
  # SABLE-y4nom.7.1: generalized from the two test-file globs to EVERY
  # git-tracked path. The original defect ("test files are unmapped as a
  # class") was one instance of the wider one: 111 of 434 tracked paths —
  # including production bin/, hooks/, and CI files — fell through to the
  # full-ALLOW escalation, and nothing enforced their classification. Now an
  # unclassified TRACKED path of any kind is a gate error naming the
  # remediation menu; suffix-based definitions of "code" are gone (an
  # extensionless bin/ script is a first-class governed path).
  local path matched errors=0 f line key
  local -a tracked=()
  while IFS= read -r f; do
    [ -n "$f" ] && tracked+=("$f")
  done < <(cd "$REPO" && git ls-files 2>/dev/null)
  # Preload once in THIS shell (see the _load_golden_pins and
  # _build_covered_index perf notes): the walk calls _match_path /
  # _mapped_family_sources inside substitutions, and a subshell-first load
  # is repaid on every one of the ~436 paths.
  _load_golden_pins
  _build_covered_index
  for path in "${tracked[@]}"; do
    local -a result=()
    while IFS= read -r line; do result+=("$line"); done < <(_match_path "$path")
    matched="${result[0]}"
    if [ "$matched" -ne 1 ]; then
      echo "::error::impact-manifest: $path matches no classification — an unclassified tracked path is an error, never a silent full-set escalation. Remediation: $SABLE_CLASS_REMEDIATION"
      errors=$((errors+1))
    fi
  done
  # Exactly-one-class enforcement (B3, codex round-1: the narrow static
  # pairs missed DYNAMIC rules — a planted ZERO row on a golden-pinned,
  # install-globbed skill file passed the walk). _mapped_family_sources
  # enumerates every mapped-family and automatic source for a path USING THE
  # SAME SEMANTICS AS _match_path (codex round-1 addendum: raw COVERS alone
  # missed ALLOW suite self-mapping and pytest-companion-derived mapping):
  # covered-file resolution via _covered_files (self + COVERS), the
  # companion probe, golden pins, the install rule, and the python
  # convention. Each TABLE class (ZERO_IMPACT, DECLARED_BROAD, PY_OWNED) is
  # exclusive against ALL of them and against each other — a contradiction
  # is the author's to resolve, never a precedence the tool guesses at.
  _mapped_family_sources() {
    local p="$1" f companion
    [ -n "${LIB_FANOUT[$p]:-}" ] && echo "LIB_FANOUT"
    _build_covered_index
    for f in ${COVERED_BY[$p]:-}; do
      echo "COVERED($f)"
    done
    companion="$(_py_test_companion "$p")"
    [ -n "$companion" ] && echo "PY-COMPANION($companion)"
    [ -n "${EXCLUDE[${p#hooks/test/}]:-}" ] && [ "$p" != "${p#hooks/test/}" ] && echo "EXCLUDE-OWN-FILE"
    [ -n "${GOLDEN_PINS[$p]:-}" ] && echo "GOLDEN-PIN"
    _install_surface_rule_matches "$p" && echo "INSTALL-RULE"
    _py_production_has_test "$p" && echo "PY-AUTO"
    return 0
  }
  # Table-key hygiene (codex round-1 addendum): a table row must point at a
  # PRESENT path (rows leave with their files — the no-tombstones contract's
  # other half; a stale row would otherwise sit unread forever because the
  # walk only visits tracked paths), and PY_OWNED rows must actually BE
  # Python — bin/*.py, or an extensionless file with a python shebang.
  # Without the eligibility guard any doc/shell file could be parked in
  # PY_OWNED and become zero-shell with no Python lane behind it.
  local key
  for key in "${!ZERO_IMPACT[@]}"; do
    if [ ! -e "$REPO/$key" ] && [ ! -L "$REPO/$key" ]; then
      echo "::error::impact-manifest: ZERO_IMPACT row for absent path $key — table rows leave with their files (SABLE-y4nom.7.1)"
      errors=$((errors+1))
    fi
  done
  for key in "${!DECLARED_BROAD[@]}"; do
    if [ ! -e "$REPO/$key" ] && [ ! -L "$REPO/$key" ]; then
      echo "::error::impact-manifest: DECLARED_BROAD row for absent path $key — table rows leave with their files (SABLE-y4nom.7.1)"
      errors=$((errors+1))
    fi
  done
  for key in "${PY_OWNED[@]:-}"; do
    [ -z "$key" ] && continue
    if [ ! -e "$REPO/$key" ] && [ ! -L "$REPO/$key" ]; then
      echo "::error::impact-manifest: PY_OWNED row for absent path $key — table rows leave with their files (SABLE-y4nom.7.1)"
      errors=$((errors+1))
      continue
    fi
    case "$key" in
      bin/*.py) : ;;
      bin/*)
        if ! head -1 "$REPO/$key" 2>/dev/null | grep -q 'python'; then
          echo "::error::impact-manifest: PY_OWNED row $key is not Python (no python shebang) — PY requires Python identity, pick the right class (SABLE-y4nom.7.1)"
          errors=$((errors+1))
        fi
        ;;
      *)
        # The Python selector owns bin/ ONLY (_is_python_owned in
        # sable_dev_check_lib.py) — a python-shebang file elsewhere has no
        # Python lane behind it, so the shell table may not claim Python
        # ownership for it regardless of its shebang.
        echo "::error::impact-manifest: PY_OWNED row $key is outside bin/ — the Python selector never owns it, pick the right class (SABLE-y4nom.7.1)"
        errors=$((errors+1))
        ;;
    esac
  done
  local conflicts
  for key in "${!ZERO_IMPACT[@]}"; do
    conflicts=$(_mapped_family_sources "$key")
    [ -n "${DECLARED_BROAD[$key]:-}" ] && conflicts="$conflicts DECLARED_BROAD"
    in_array "$key" "${PY_OWNED[@]:-}" && conflicts="$conflicts PY_OWNED"
    if [ -n "$(echo $conflicts)" ]; then
      echo "::error::impact-manifest: $key is ZERO_IMPACT but also classified by: $(echo $conflicts) — exactly one class per path (SABLE-y4nom.7.1)"
      errors=$((errors+1))
    fi
  done
  for key in "${!DECLARED_BROAD[@]}"; do
    conflicts=$(_mapped_family_sources "$key")
    in_array "$key" "${PY_OWNED[@]:-}" && conflicts="$conflicts PY_OWNED"
    if [ -n "$(echo $conflicts)" ]; then
      echo "::error::impact-manifest: $key is DECLARED_BROAD but also classified by: $(echo $conflicts) — exactly one class per path (SABLE-y4nom.7.1)"
      errors=$((errors+1))
    fi
  done
  for key in "${PY_OWNED[@]:-}"; do
    [ -z "$key" ] && continue
    conflicts=$(_mapped_family_sources "$key")
    if [ -n "$(echo $conflicts)" ]; then
      echo "::error::impact-manifest: $key is PY_OWNED but also classified by: $(echo $conflicts) — exactly one class per path (SABLE-y4nom.7.1)"
      errors=$((errors+1))
    fi
  done
  # B2 loud-parser guard: if this repo ships the installer but the
  # BASE_HOOK_FILES line cannot be parsed, the install-surface rule is
  # silently inert for the base-hooks family — that failure must be loud.
  if [ -f "$REPO/bin/sable-orchestration-install" ] \
     && in_array "$_INSTALL_SURFACE_SUITE" "${ALLOW[@]}" \
     && [ -z "$(_installer_base_hooks)" ]; then
    echo "::error::impact-manifest: bin/sable-orchestration-install is present but its BASE_HOOK_FILES line did not parse — the install-surface rule would be silently inert for the base-hooks family (SABLE-y4nom.7.1 B2)"
    errors=$((errors+1))
  fi
  if [ "$errors" -gt 0 ]; then
    echo "impact-manifest --check-test-coverage: $errors classification error(s)"
    return 1
  fi
  echo "impact-manifest --check-test-coverage: complete — every tracked path resolves to exactly one classification (${#tracked[@]} checked)"
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
