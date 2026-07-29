#!/usr/bin/env bash
# test-ci-bd-coverage-gap.sh — INTEGRATION coverage for SABLE-jd5fj.16: the
# iron-rule real-bd suites (test-dep-merge-state.sh, test-overlap-dispatch-
# e2e.sh) must never be able to print a clean green summary when they self-
# skip their real-bd leg — a suite whose output is indistinguishable from a
# full run is exactly how this coverage gap went unnoticed (.github/workflows
# /ci-verify.yml ships no bd, SABLE-59zu, so these two legs execute ONLY at
# chuck's local combined-tree impact tier, SABLE-jd5fj.13).
#
# THE BD-ABSENT HALF reproduces the ci-verify clean room by stripping bd's
# own directory out of PATH (not blanking PATH — python3/git/mktemp etc.
# must keep resolving, or a crash would masquerade as the very defect under
# test) and running both suites for real, asserting their output NAMES the
# skipped leg and their summary is loud about it (non-zero Skipped count).
# Runs unconditionally — it does not need bd itself, only needs to be ABLE
# to remove it, which works whether or not this environment has it.
#
# The real-bd complement is NOT re-run here. Both target suites are themselves
# entries in shell-run-set.sh's ALLOW list, where their real-bd assertions run
# once authoritatively on a host that carries bd. Re-running them from this
# wrapper duplicated the two slowest real-bd E2Es (including fresh Dolt
# stores) merely to prove that "not skipped" differs from "skipped". The cheap
# polarity below owns the loud-skip transport contract; the target suites own
# their real behavior. shell-run-set --check-loud-skip also requires both
# targets to remain in ALLOW, so that complement cannot silently disappear.
#
# Run with:
#   bash hooks/test/test-ci-bd-coverage-gap.sh
#
# sable-test-load: nested-runner -- composes only the cheap bd-absent polarity; each real-bd E2E remains authoritative in ALLOW

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
TESTDIR="$REPO/hooks/test"
DEP_MERGE="$TESTDIR/test-dep-merge-state.sh"
OVERLAP="$TESTDIR/test-overlap-dispatch-e2e.sh"

# shellcheck source=lib-require-all.sh
source "$TESTDIR/lib-require-all.sh"

PASS=0; FAIL=0; SKIP=0; FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
skip() { SKIP=$((SKIP+1)); echo "SKIP: $1"; }

for f in "$DEP_MERGE" "$OVERLAP"; do
  if [ ! -f "$f" ]; then
    echo "FAIL: fixture missing: $f"
    exit 2
  fi
done

# ---------------------------------------------------------------------------
# PLANT-AND-FAIL, SABLE-bjabn (test-require-all.sh's own pattern, SABLE-5lli.7
# style): this suite's own two Skipped-count controls below (bd-absent
# test-dep-merge-state.sh / test-overlap-dispatch-e2e.sh) used to collapse
# "no Skipped: line was found at all" and "a Skipped: line was found but its
# count was zero" into ONE `if A && B; then pass; else fail "..."; fi`. Prove
# THIS SPECIFIC conversion is non-vacuous -- not just inheriting lib-require-
# all.sh's own generic proof from test-require-all.sh -- by reproducing the
# exact pre-conversion collapsed shape against the same synthetic clause
# values used by both real conversions below, and asserting the collapsed
# form cannot name which clause failed while require_all can.
# ---------------------------------------------------------------------------
_bjabn_plant_collapsed_skip_count() {
  # The pre-bjabn shape: one AND, one verdict, no per-clause detail -- exactly
  # this file's DM_NOBD_SKIPPED / OV_NOBD_SKIPPED controls before this bead.
  # c1 = "a Skipped: line was found" (0=held), c2 = "count > 0" (0=held).
  local c1="$1" c2="$2"
  if [ "$c1" -eq 0 ] && [ "$c2" -eq 0 ]; then
    _bjabn_plant_ok=1; _bjabn_plant_detail=""
  else
    _bjabn_plant_ok=0; _bjabn_plant_detail="the conjunction control failed"
  fi
}
# Synthetic case matching a REAL failure shape either conversion below can
# hit: the Skipped: line WAS found (c1=0/held) but its count was NOT > 0
# (c2=1/failed) -- two claims that disagree, which a collapsed boolean
# cannot distinguish and require_all must.
_bjabn_plant_collapsed_skip_count 0 1
require_all "plant: skip-count conjunction" \
  "a Skipped: count line was found" 0 "the Skipped count is > 0" 1
if [ "$_bjabn_plant_ok" -eq 0 ] \
   && [ "$_bjabn_plant_detail" = "the conjunction control failed" ] \
   && [ "$REQUIRE_ALL_OK" -eq 0 ] \
   && [ "${REQUIRE_ALL_DETAIL#*the Skipped count is > 0}" != "$REQUIRE_ALL_DETAIL" ] \
   && [ "${REQUIRE_ALL_DETAIL#*a Skipped: count line was found}" = "$REQUIRE_ALL_DETAIL" ]; then
  pass "PLANT: the pre-bjabn collapsed skip-count shape cannot name which clause failed, require_all does"
else
  fail "PLANT: the pre-bjabn collapsed skip-count shape cannot name which clause failed, require_all does" \
       "collapsed_ok=$_bjabn_plant_ok collapsed_detail=$_bjabn_plant_detail require_all_ok=$REQUIRE_ALL_OK require_all_detail=$REQUIRE_ALL_DETAIL"
fi
# Opposite polarity of the same synthetic shape: the line was NOT found at
# all (c1=1/failed) but a stale count happened to be > 0 (c2=0/held) -- the
# OTHER clause must be the one named, proving the plant exercises both
# directions rather than a single hardcoded outcome.
_bjabn_plant_collapsed_skip_count 1 0
require_all "plant: skip-count conjunction" \
  "a Skipped: count line was found" 1 "the Skipped count is > 0" 0
if [ "$_bjabn_plant_ok" -eq 0 ] \
   && [ "$REQUIRE_ALL_OK" -eq 0 ] \
   && [ "${REQUIRE_ALL_DETAIL#*a Skipped: count line was found}" != "$REQUIRE_ALL_DETAIL" ] \
   && [ "${REQUIRE_ALL_DETAIL#*the Skipped count is > 0}" = "$REQUIRE_ALL_DETAIL" ]; then
  pass "PLANT: opposite polarity -- the OTHER clause is named when it is the one that broke"
else
  fail "PLANT: opposite polarity -- the OTHER clause is named when it is the one that broke" \
       "require_all_ok=$REQUIRE_ALL_OK require_all_detail=$REQUIRE_ALL_DETAIL"
fi

# --- Build a bd-absent PATH the same way the ci-verify clean room is bd-
# absent: bd's own directory removed, everything else (python3, git, mktemp,
# bash) left resolvable. Blanking PATH outright would make python3/git
# unresolvable too, so a genuine crash would look identical to the loud-skip
# behavior under test and prove nothing (the same trap test-dep-merge-
# state.sh's own NOBD fixture documents and avoids).
BD_PATH="$(command -v bd 2>/dev/null || true)"
if [ -n "$BD_PATH" ]; then
  BD_DIR="$(dirname "$BD_PATH")"
  NOBD_PATH="$(printf '%s' "$PATH" | tr ':' '\n' | grep -vF "$BD_DIR" | tr '\n' ':')"
  NOBD_PATH="${NOBD_PATH%:}"
else
  # Already a bd-less environment (the real ci-verify clean room) — nothing
  # to strip, PATH already reproduces the condition under test.
  NOBD_PATH="$PATH"
fi

if [ -n "$(PATH="$NOBD_PATH" command -v bd 2>/dev/null)" ]; then
  fail "fixture: NOBD_PATH really has no bd resolvable" "bd still resolvable on the stripped PATH — the assertions below would be vacuous"
fi

# ---------------------------------------------------------------------------
# bd ABSENT: test-dep-merge-state.sh
# ---------------------------------------------------------------------------
OUT_DM_NOBD=$(env PATH="$NOBD_PATH" bash "$DEP_MERGE" 2>&1)
RC_DM_NOBD=$?
DM_NOBD_TESTS=$(printf '%s' "$OUT_DM_NOBD" | grep -oE 'Tests: [0-9]+' | tail -1 | grep -oE '[0-9]+')

if [ "$RC_DM_NOBD" -eq 0 ]; then
  pass "bd-absent: test-dep-merge-state.sh exits 0 (a self-skip is not a failure)"
else
  fail "bd-absent: test-dep-merge-state.sh exits 0 (a self-skip is not a failure)" "rc=$RC_DM_NOBD"
fi

if printf '%s' "$OUT_DM_NOBD" | grep -q 'bd half: bd not on PATH'; then
  pass "bd-absent: test-dep-merge-state.sh NAMES the skipped leg ('bd half: bd not on PATH')"
else
  fail "bd-absent: test-dep-merge-state.sh NAMES the skipped leg ('bd half: bd not on PATH')" "output: $OUT_DM_NOBD"
fi

DM_NOBD_SKIPPED=$(printf '%s' "$OUT_DM_NOBD" | grep -oE 'Skipped: [0-9]+' | tail -1 | grep -oE '[0-9]+')
# SABLE-bjabn: a red here used to collapse two independently-meaningful
# claims -- "no Skipped: count line was found at all" (a parsing/output-shape
# break) vs "a Skipped: count line WAS found but its count was zero" (a real
# behavior difference) -- into one boolean, exactly muew7's shape: a reader
# on RED could not tell which had actually happened.
[ -n "${DM_NOBD_SKIPPED:-}" ]; _dm_skip_c1=$?
[ "${DM_NOBD_SKIPPED:-0}" -gt 0 ] 2>/dev/null; _dm_skip_c2=$?
require_all "bd-absent: test-dep-merge-state.sh non-zero Skipped count" \
  "a Skipped: count line was found" "$_dm_skip_c1" \
  "the Skipped count is > 0" "$_dm_skip_c2"
if [ "$REQUIRE_ALL_OK" -eq 1 ]; then
  pass "bd-absent: test-dep-merge-state.sh summary shows a non-zero Skipped count ($DM_NOBD_SKIPPED)"
else
  fail "bd-absent: test-dep-merge-state.sh summary shows a non-zero Skipped count" \
       "$REQUIRE_ALL_DETAIL (parsed='${DM_NOBD_SKIPPED:-<none>}' output=$OUT_DM_NOBD)"
fi

# ---------------------------------------------------------------------------
# bd ABSENT: test-overlap-dispatch-e2e.sh
# ---------------------------------------------------------------------------
OUT_OV_NOBD=$(env PATH="$NOBD_PATH" bash "$OVERLAP" 2>&1)
RC_OV_NOBD=$?
OV_NOBD_TESTS=$(printf '%s' "$OUT_OV_NOBD" | grep -oE 'Tests: [0-9]+' | tail -1 | grep -oE '[0-9]+')

if [ "$RC_OV_NOBD" -eq 0 ]; then
  pass "bd-absent: test-overlap-dispatch-e2e.sh exits 0 (a self-skip is not a failure)"
else
  fail "bd-absent: test-overlap-dispatch-e2e.sh exits 0 (a self-skip is not a failure)" "rc=$RC_OV_NOBD"
fi

if printf '%s' "$OUT_OV_NOBD" | grep -q 'bd not found on PATH'; then
  pass "bd-absent: test-overlap-dispatch-e2e.sh NAMES the skipped leg ('bd not found on PATH')"
else
  fail "bd-absent: test-overlap-dispatch-e2e.sh NAMES the skipped leg ('bd not found on PATH')" "output: $OUT_OV_NOBD"
fi

OV_NOBD_SKIPPED=$(printf '%s' "$OUT_OV_NOBD" | grep -oE 'Skipped: [0-9]+' | tail -1 | grep -oE '[0-9]+')
# SABLE-bjabn: same two-claim collapse as the DM control above, converted the
# same way.
[ -n "${OV_NOBD_SKIPPED:-}" ]; _ov_skip_c1=$?
[ "${OV_NOBD_SKIPPED:-0}" -gt 0 ] 2>/dev/null; _ov_skip_c2=$?
require_all "bd-absent: test-overlap-dispatch-e2e.sh non-zero Skipped count" \
  "a Skipped: count line was found" "$_ov_skip_c1" \
  "the Skipped count is > 0" "$_ov_skip_c2"
if [ "$REQUIRE_ALL_OK" -eq 1 ]; then
  pass "bd-absent: test-overlap-dispatch-e2e.sh summary shows a non-zero Skipped count ($OV_NOBD_SKIPPED)"
else
  fail "bd-absent: test-overlap-dispatch-e2e.sh summary shows a non-zero Skipped count" \
       "$REQUIRE_ALL_DETAIL (parsed='${OV_NOBD_SKIPPED:-<none>}' output=$OUT_OV_NOBD)"
fi

skip "real-bd complement runs once in each target suite's own ALLOW entry; this wrapper does not duplicate either E2E"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL | Skipped: $SKIP"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
