#!/usr/bin/env bash
# diff-cover-gate.sh — strict patch-coverage gate, python half (SABLE-cmar4.5,
# story S3, locked contract: diff-cover patch-coverage semantics — strict
# patch gate, lenient project floor).
#
# This is the coverage-delta check bin/sable_coverage_floor_lib.py's pruning
# detector requires a branch to CARRY before a diff that removes a test
# function, adds a skip marker, or deletes a test file (a "pruning" diff) is
# allowed to promote — see run_coverage_floor_check / assert_coverage_floor in
# bin/sable_gate_promote_lib.py. The merge gate runs this SAME script (via a
# throwaway `git worktree add --detach` at the candidate commit) rather than
# re-implementing the invocation, so "does the branch carry the check" and
# "what does the check say" are answered by one script, runnable by hand the
# same way the gate runs it. Absence of this script on a branch, or any
# non-zero exit from it, is treated by the gate as "does not carry the check"
# and denies a pruning promotion unless overridden with a named
# "Coverage override: <reason>" line (mirrors hooks/multi-manager/
# pre-dispatch-model-check.sh's "Model override:" pattern).
#
# Deliberately STRICT-PATCH, LENIENT-PROJECT (the S3 lock): diff-cover only
# ever measures the lines the diff touches, never the project's overall
# coverage.py total, so a repo with plenty of pre-existing uncovered code is
# never penalized for it — only new/changed lines in THIS diff must clear the
# floor.
#
# IMPACT-SCOPED (SABLE-hauwa): the pytest+coverage.py run below that produces
# coverage.xml is scoped to the diff's own footprint of test suites via
# bin/tier_selection.py's --diff-cover-scope mode WHENEVER that mode can
# PROVE the scoped selection covers this diff (see
# tier_selection.build_diff_cover_scope_plan's docstring for the exact
# guarantees) — a full `pytest bin/` run takes ~887s idle on a 24-core box
# against this floor's 900s budget, so it does not fit even idle. On any
# doubt (missing/broken selector, unprovable selection, tier_selection.py
# itself absent — e.g. an older checkout) this falls back to exactly the
# full run this script always did before this fix.
#
# Usage: diff-cover-gate.sh <compare-ref> [fail-under]
#   compare-ref   the base commit/ref to diff against (required)
#   fail-under    patch-coverage percentage floor (default: $SABLE_COVERAGE_
#                 FLOOR_FAIL_UNDER, else 80)
#
# Run from anywhere; always operates on this script's own repo (bin/'s python
# half — the pytest/coverage.py half this floor has real teeth on; see the
# KNOWN-RESIDUAL note in bin/sable_coverage_floor_lib.py for the shell half).
set -euo pipefail

COMPARE_REF="${1:?usage: diff-cover-gate.sh <compare-ref> [fail-under]}"
FAIL_UNDER="${2:-${SABLE_COVERAGE_FLOOR_FAIL_UNDER:-80}}"

CI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$CI_DIR/../.." && pwd)"
cd "$REPO"

COVERAGE_XML="coverage-floor.xml"
trap 'rm -f "$COVERAGE_XML"' EXIT

# SABLE-hauwa: ask tier_selection.py for a provably-safe scoped selection.
# Guarded so a missing/broken selector script (e.g. an older checkout that
# predates this fix, or a synthetic test fixture that doesn't carry it)
# degrades to the full run below rather than aborting this script outright
# under `set -e`.
SCOPE_MODE="full"
SCOPE_PATHS=()
if [[ -f bin/tier_selection.py ]]; then
  if SCOPE_OUTPUT="$(python3 bin/tier_selection.py "--diff-cover-scope=$COMPARE_REF")"; then
    SCOPE_MODE="${SCOPE_OUTPUT%%$'\n'*}"
    if [[ "$SCOPE_MODE" == "scoped" ]]; then
      mapfile -t SCOPE_PATHS <<<"$SCOPE_OUTPUT"
      SCOPE_PATHS=("${SCOPE_PATHS[@]:1}")
    else
      SCOPE_MODE="full"
    fi
  else
    SCOPE_MODE="full"
  fi
fi

if [[ "$SCOPE_MODE" == "scoped" && "${#SCOPE_PATHS[@]}" -gt 0 ]]; then
  python3 -m pytest "${SCOPE_PATHS[@]}" -q -p no:cacheprovider \
    --cov=bin --cov-report="xml:$COVERAGE_XML"
else
  python3 -m pytest bin/ -q -p no:cacheprovider \
    --cov=bin --cov-report="xml:$COVERAGE_XML"
fi

diff-cover "$COVERAGE_XML" --compare-branch="$COMPARE_REF" --fail-under="$FAIL_UNDER"
