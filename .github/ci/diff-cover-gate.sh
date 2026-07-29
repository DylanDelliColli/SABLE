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
# non-zero exit from it, denies a pruning promotion unless overridden with a
# named "Coverage override: <reason>" line (mirrors hooks/multi-manager/
# pre-dispatch-model-check.sh's "Model override:" pattern). It used to REPORT
# every one of those as "does not carry the check" / "coverage regressed";
# see the PHASE ATTRIBUTION block below for why that sentence is gone and
# what replaced it (SABLE-9yjt5). The DENY itself is unchanged.
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
#
# ── PHASE ATTRIBUTION (SABLE-9yjt5) ────────────────────────────────────────
# This script does TWO independent things — run the suite, then measure the
# patch — and under a bare `set -e` a failure in EITHER left the caller with
# the same fact: "non-zero". run_coverage_floor_check then reduced that to
# `cp.returncode == 0`, and the deny message said "coverage regressed" no
# matter which phase actually broke. A worker read that literally and spent a
# full revise cycle ADDING 115 lines of tests to a branch whose real problem
# was that the run was overrunning its budget — i.e. the advice deepened the
# hole (SABLE-21rug.4).
#
# So each phase now reports itself. The DECISION does not move: every one of
# these still denies a pruning promotion, fail-closed, exactly as before. Only
# the REPORT axis changes (standing discipline 7 — decision axis ruled
# per-site, report axis always loud, the two never traded).
#
#   EXIT  MARKER                          MEANING
#   0     ok                              both phases ran; the patch cleared
#                                         --fail-under. The ONLY allow.
#   10    pytest-failed                   the suite itself is red (or errored
#                                         at collection). diff-cover NEVER RAN,
#                                         so NO coverage number exists. Adding
#                                         tests is not the remedy — fixing the
#                                         suite is.
#   11    diff-cover-under-floor          diff-cover ran, produced a real
#                                         number, and the patch was under the
#                                         floor. The ONLY code that licenses
#                                         the words "coverage regressed".
#   12    diff-cover-unavailable          diff-cover is not installed, or
#         coverage-xml-missing            coverage.py produced no XML. A
#                                         COULD-NOT-ASSESS, not a verdict.
#
# The marker is printed as a machine-readable record:
#   SABLE-COVERAGE-FLOOR-PHASE: <marker> rc=<n> [fail_under=<n>]
# Parsed by run_coverage_floor_check, which prefers the marker and falls back
# to the exit code — so an older branch's script (no marker, exit 1) still
# reads as an unattributable deny rather than a mis-attributed one.
#
# `set -e` is deliberately LIFTED around the two phases (and only there): the
# whole defect is that -e aborted before the second phase could distinguish
# itself. Setup errors below still abort hard.
set -euo pipefail

COMPARE_REF="${1:?usage: diff-cover-gate.sh <compare-ref> [fail-under]}"
FAIL_UNDER="${2:-${SABLE_COVERAGE_FLOOR_FAIL_UNDER:-80}}"

EXIT_PYTEST_FAILED=10
EXIT_DIFF_COVER_UNDER_FLOOR=11
EXIT_CANNOT_ASSESS=12

CI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$CI_DIR/../.." && pwd)"
cd "$REPO"

COVERAGE_XML="coverage-floor.xml"
trap 'rm -f "$COVERAGE_XML"' EXIT

phase() { printf 'SABLE-COVERAGE-FLOOR-PHASE: %s\n' "$*"; }

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

# ── phase 1: the suite ─────────────────────────────────────────────────────
# BOTH arms of SABLE-hauwa's scoped/full selection are bounded by the SAME rc
# capture, deliberately: which selection ran is a performance question, and
# the phase a failure is attributed to must not depend on it. (hauwa narrows
# cause (B), the budget overrun, by shrinking the run; 9yjt5 makes (B) say so
# when it still happens. They are the two halves of the same gate.)
set +e
if [[ "$SCOPE_MODE" == "scoped" && "${#SCOPE_PATHS[@]}" -gt 0 ]]; then
  python3 -m pytest "${SCOPE_PATHS[@]}" -q -p no:cacheprovider \
    --cov=bin --cov-report="xml:$COVERAGE_XML"
else
  python3 -m pytest bin/ -q -p no:cacheprovider \
    --cov=bin --cov-report="xml:$COVERAGE_XML"
fi
PYTEST_RC=$?
set -e

if [ "$PYTEST_RC" -ne 0 ]; then
  phase "pytest-failed rc=$PYTEST_RC"
  echo "diff-cover did NOT run: the suite must pass before patch coverage can be measured." >&2
  exit "$EXIT_PYTEST_FAILED"
fi

if [ ! -f "$COVERAGE_XML" ]; then
  phase "coverage-xml-missing rc=0"
  echo "pytest passed but produced no $COVERAGE_XML — coverage could not be assessed." >&2
  exit "$EXIT_CANNOT_ASSESS"
fi

if ! command -v diff-cover >/dev/null 2>&1; then
  phase "diff-cover-unavailable rc=0"
  echo "diff-cover is not installed — patch coverage could not be assessed." >&2
  exit "$EXIT_CANNOT_ASSESS"
fi

# ── phase 2: the patch measurement ─────────────────────────────────────────
set +e
diff-cover "$COVERAGE_XML" --compare-branch="$COMPARE_REF" --fail-under="$FAIL_UNDER"
DIFF_COVER_RC=$?
set -e

if [ "$DIFF_COVER_RC" -ne 0 ]; then
  phase "diff-cover-under-floor rc=$DIFF_COVER_RC fail_under=$FAIL_UNDER"
  exit "$EXIT_DIFF_COVER_UNDER_FLOOR"
fi

phase "ok rc=0 fail_under=$FAIL_UNDER"
