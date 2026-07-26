#!/usr/bin/env bash
# test-tier-ssot.sh — unit tests for .github/ci/test-tiers.sh, the per-repo
# CI-tier SSOT + loader (SABLE-cmar4.1).
#
# Covers the bead's unit test spec: the loader parses the three declared
# tiers, rejects unknown tier names (CLI and sourced-lib entry points alike),
# and budget fields are readable. No mutation of repo state — pure read-only
# calls against the real .github/ci/test-tiers.sh.
#
# Run with:
#   bash hooks/test/test-tier-ssot.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
LOADER="$REPO/.github/ci/test-tiers.sh"

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

[ -f "$LOADER" ] || { fail "loader exists at .github/ci/test-tiers.sh"; echo "Tests: 1 | Passed: 0 | Failed: 1"; exit 1; }
pass "loader exists at .github/ci/test-tiers.sh"

# ---------- --names: parses the three declared tiers -----------------------
names_out="$(bash "$LOADER" --names 2>&1)"
if [ "$names_out" = "$(printf 'pre_push\nmerge_preview\nfull_snapshot')" ]; then
  pass "--names lists exactly the three declared tiers, in order"
else
  fail "--names lists exactly the three declared tiers, in order" "got: $names_out"
fi

# ---------- --list: each declared tier has non-empty suite membership ------
for tier in pre_push merge_preview full_snapshot; do
  list_out="$(bash "$LOADER" --list "$tier" 2>&1)"
  count=$(printf '%s\n' "$list_out" | grep -c . || true)
  if [ "$count" -gt 0 ]; then
    pass "--list $tier returns non-empty suite membership ($count suites)"
  else
    fail "--list $tier returns non-empty suite membership" "got: $list_out"
  fi
done

# ---------- merge_preview / full_snapshot alias ALLOW, not a duplicate list ----
allow_run_count=$(bash "$REPO/.github/ci/shell-run-set.sh" --manifest 2>&1 | grep -c '^RUN ' || true)
for tier in merge_preview full_snapshot; do
  tier_count=$(bash "$LOADER" --list "$tier" 2>&1 | grep -c . || true)
  if [ "$tier_count" = "$allow_run_count" ]; then
    pass "$tier suite count matches shell-run-set.sh ALLOW ($allow_run_count) — sourced, not duplicated"
  else
    fail "$tier suite count matches shell-run-set.sh ALLOW ($allow_run_count) — sourced, not duplicated" \
         "tier=$tier_count allow=$allow_run_count"
  fi
done

# ---------- --budget: budget fields are readable, positive integers --------
for tier in pre_push merge_preview full_snapshot; do
  budget="$(bash "$LOADER" --budget "$tier" 2>&1)"
  if printf '%s' "$budget" | grep -qE '^[0-9]+$' && [ "$budget" -gt 0 ]; then
    pass "--budget $tier is a readable positive-integer duration budget ($budget)"
  else
    fail "--budget $tier is a readable positive-integer duration budget" "got: $budget"
  fi
done

# ---------- unknown tier names are rejected: CLI entry points --------------
out="$(bash "$LOADER" --list bogus 2>&1)"; rc=$?
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "unknown tier 'bogus'"; then
  pass "--list bogus is rejected (rc!=0, names the bad tier)"
else
  fail "--list bogus is rejected (rc!=0, names the bad tier)" "rc=$rc out=$out"
fi

out="$(bash "$LOADER" --budget bogus 2>&1)"; rc=$?
if [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q "unknown tier 'bogus'"; then
  pass "--budget bogus is rejected (rc!=0, names the bad tier)"
else
  fail "--budget bogus is rejected (rc!=0, names the bad tier)" "rc=$rc out=$out"
fi

# ---------- unknown tier names are rejected: sourced-lib entry points ------
# Sourced in a SUBSHELL so this test's own PASS/FAIL bookkeeping (and set -u)
# can't be perturbed by the loader's globals.
lib_reject_suites="$(
  bash -c '
    set -uo pipefail
    . "'"$LOADER"'"
    sable_tier_suites bogus 2>&1
    echo "RC=$?"
  '
)"
if printf '%s' "$lib_reject_suites" | grep -q "unknown tier 'bogus'" \
   && printf '%s' "$lib_reject_suites" | grep -q '^RC=1$'; then
  pass "sourced sable_tier_suites bogus returns rc=1 + names the bad tier"
else
  fail "sourced sable_tier_suites bogus returns rc=1 + names the bad tier" "$lib_reject_suites"
fi

lib_reject_budget="$(
  bash -c '
    set -uo pipefail
    . "'"$LOADER"'"
    sable_tier_budget_sec bogus 2>&1
    echo "RC=$?"
  '
)"
if printf '%s' "$lib_reject_budget" | grep -q "unknown tier 'bogus'" \
   && printf '%s' "$lib_reject_budget" | grep -q '^RC=1$'; then
  pass "sourced sable_tier_budget_sec bogus returns rc=1 + names the bad tier"
else
  fail "sourced sable_tier_budget_sec bogus returns rc=1 + names the bad tier" "$lib_reject_budget"
fi

# ---------- sourcing the loader does NOT dispatch a CLI action -------------
# (the sourced-vs-executed guard both files rely on — SABLE-cmar4.1)
source_only="$(
  bash -c '
    set -uo pipefail
    . "'"$LOADER"'"
    echo "sourced-ok"
  '
)"
if [ "$source_only" = "sourced-ok" ]; then
  pass "sourcing the loader with no args does not dispatch a CLI action or error"
else
  fail "sourcing the loader with no args does not dispatch a CLI action or error" "$source_only"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
