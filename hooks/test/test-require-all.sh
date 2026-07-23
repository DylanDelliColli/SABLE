#!/usr/bin/env bash
# test-require-all.sh — unit coverage for lib-require-all.sh (SABLE-muew7).
#
# WHAT IS UNDER TEST: require_all's per-clause reporting on a conjunction
# control. The defect this replaces is not a wrong verdict — the AND was
# always computed correctly — it is a red that cannot say WHICH conjunct
# broke. SABLE-1gnuj is the live cost: a three-clause control in
# test-ci-bd-coverage-gap.sh went red, and three agents spent an evening
# unable to tell rc, subtest-count, and Skipped-line apart because the
# control only ever emitted PASS or FAIL. See lib-require-all.sh's header for
# the full mechanism and why this is a report fix, not a predicate split.
#
# Run with:
#   bash hooks/test/test-require-all.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
TESTDIR="$REPO/hooks/test"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

[ -f "$TESTDIR/lib-require-all.sh" ] || { echo "FATAL: missing $TESTDIR/lib-require-all.sh"; exit 2; }
# shellcheck source=lib-require-all.sh
source "$TESTDIR/lib-require-all.sh"

# ---------------------------------------------------------------------------
# 1. A single failing clause (the middle one) is named; the other two are not.
# ---------------------------------------------------------------------------
require_all "three-clause fixture" "clause one" 0 "clause two" 1 "clause three" 0
if [ "$REQUIRE_ALL_OK" -eq 0 ] \
   && [ "${REQUIRE_ALL_DETAIL#*clause two}" != "$REQUIRE_ALL_DETAIL" ] \
   && [ "${REQUIRE_ALL_DETAIL#*clause one}" = "$REQUIRE_ALL_DETAIL" ] \
   && [ "${REQUIRE_ALL_DETAIL#*clause three}" = "$REQUIRE_ALL_DETAIL" ]; then
  pass "single failing clause: only that clause is named"
else
  fail "single failing clause: only that clause is named" \
       "ok=$REQUIRE_ALL_OK detail=$REQUIRE_ALL_DETAIL"
fi

# ---------------------------------------------------------------------------
# 2. Two failing clauses are BOTH named — a report that stops at the first
#    failure recreates the exact problem this bead exists to fix.
# ---------------------------------------------------------------------------
require_all "three-clause fixture" "clause one" 0 "clause two" 1 "clause three" 1
if [ "$REQUIRE_ALL_OK" -eq 0 ] \
   && [ "${REQUIRE_ALL_DETAIL#*clause two}" != "$REQUIRE_ALL_DETAIL" ] \
   && [ "${REQUIRE_ALL_DETAIL#*clause three}" != "$REQUIRE_ALL_DETAIL" ] \
   && [ "${REQUIRE_ALL_DETAIL#*clause one}" = "$REQUIRE_ALL_DETAIL" ]; then
  pass "two failing clauses: both are named, the passing one is not"
else
  fail "two failing clauses: both are named, the passing one is not" \
       "ok=$REQUIRE_ALL_OK detail=$REQUIRE_ALL_DETAIL"
fi

# ---------------------------------------------------------------------------
# 3. NEGATIVE CONTROL, load-bearing: all clauses hold -> OK, and NO per-clause
#    noise. Without this, a "fix" that always narrates every clause's state
#    would pass tests 1 and 2 above and make every green run unreadable.
# ---------------------------------------------------------------------------
require_all "three-clause fixture" "clause one" 0 "clause two" 0 "clause three" 0
if [ "$REQUIRE_ALL_OK" -eq 1 ] && [ -z "$REQUIRE_ALL_DETAIL" ]; then
  pass "negative control: all clauses holding is silent (OK=1, no detail)"
else
  fail "negative control: all clauses holding is silent (OK=1, no detail)" \
       "ok=$REQUIRE_ALL_OK detail=$REQUIRE_ALL_DETAIL"
fi

# ---------------------------------------------------------------------------
# 4. PLANT-AND-FAIL (SABLE-5lli.7 pattern): a reverted, collapsed single-
#    boolean implementation of the SAME control must fail test 1's own
#    per-clause assertion — proving that assertion actually distinguishes the
#    fixed shape from the defect it replaces, not just from an empty string.
# ---------------------------------------------------------------------------
require_all_reverted_collapsed_boolean() {
  # The pre-muew7 shape: one AND, one verdict, no per-clause detail at all —
  # exactly hooks/test/test-ci-bd-coverage-gap.sh's shape before this bead.
  local c2="$1"
  REQUIRE_ALL_OK=1
  REQUIRE_ALL_DETAIL=""
  if [ "$c2" -ne 0 ]; then
    REQUIRE_ALL_OK=0
    REQUIRE_ALL_DETAIL="the conjunction control failed"
  fi
}
require_all_reverted_collapsed_boolean 1
if [ "$REQUIRE_ALL_OK" -eq 0 ] \
   && [ "${REQUIRE_ALL_DETAIL#*clause two}" != "$REQUIRE_ALL_DETAIL" ] \
   && [ "${REQUIRE_ALL_DETAIL#*clause one}" = "$REQUIRE_ALL_DETAIL" ] \
   && [ "${REQUIRE_ALL_DETAIL#*clause three}" = "$REQUIRE_ALL_DETAIL" ]; then
  fail "PLANT: the collapsed single-boolean shape must NOT name which clause failed" \
       "it did — the plant is not exercising the defect this bead fixes: detail=$REQUIRE_ALL_DETAIL"
else
  pass "PLANT: the collapsed single-boolean shape correctly fails the per-clause assertion (proves test 1 is non-vacuous)"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
