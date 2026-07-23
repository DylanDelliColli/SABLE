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
# THE NEGATIVE CONTROL (bd present) is the complement: with bd on PATH, both
# suites must report STRICTLY MORE subtests than their own bd-absent run
# (above) reported, with zero skips — proving the bd-absent branch above is
# a real skip of real coverage, not a suite that always reports fewer tests
# than it has. Asserted as a relative property (bd-present count > bd-absent
# count) rather than a hardcoded literal, so growing either fixture suite's
# subtest count over time does not re-red this control (SABLE-xanum — a
# literal '18/18' stood in for this property and broke the day
# test-dep-merge-state.sh legitimately grew from 18 to 20 subtests).
# Self-skips loudly if bd is not on PATH here (SABLE-59zu clean room) — this
# suite's own real-bd leg is exactly the shape it is testing for, so it is
# registered here as a fixture, not lectured about elsewhere.
#
# Run with:
#   bash hooks/test/test-ci-bd-coverage-gap.sh

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
if [ -n "${DM_NOBD_SKIPPED:-}" ] && [ "$DM_NOBD_SKIPPED" -gt 0 ]; then
  pass "bd-absent: test-dep-merge-state.sh summary shows a non-zero Skipped count ($DM_NOBD_SKIPPED)"
else
  fail "bd-absent: test-dep-merge-state.sh summary shows a non-zero Skipped count" "parsed='${DM_NOBD_SKIPPED:-<none>}' output=$OUT_DM_NOBD"
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
if [ -n "${OV_NOBD_SKIPPED:-}" ] && [ "$OV_NOBD_SKIPPED" -gt 0 ]; then
  pass "bd-absent: test-overlap-dispatch-e2e.sh summary shows a non-zero Skipped count ($OV_NOBD_SKIPPED)"
else
  fail "bd-absent: test-overlap-dispatch-e2e.sh summary shows a non-zero Skipped count" "parsed='${OV_NOBD_SKIPPED:-<none>}' output=$OUT_OV_NOBD"
fi

# ---------------------------------------------------------------------------
# NEGATIVE CONTROL: bd PRESENT — full subtest counts, zero skips. Self-skips
# loudly (never silently) if this environment has no real bd (SABLE-59zu).
# ---------------------------------------------------------------------------
if ! command -v bd >/dev/null 2>&1; then
  skip "negative control: bd not on PATH here (SABLE-59zu clean room) — the bd-absent half above already ran for real and is the assertion that matters in that environment"
else
  OUT_DM_BD=$(bash "$DEP_MERGE" 2>&1)
  RC_DM_BD=$?
  DM_BD_TESTS=$(printf '%s' "$OUT_DM_BD" | grep -oE 'Tests: [0-9]+' | tail -1 | grep -oE '[0-9]+')
  DM_BD_SKIPPED=$(printf '%s' "$OUT_DM_BD" | grep -oE 'Skipped: [0-9]+' | tail -1 | grep -oE '[0-9]+')
  [ "$RC_DM_BD" -eq 0 ]; _dm_c1=$?
  [ "${DM_BD_TESTS:-0}" -gt "${DM_NOBD_TESTS:-0}" ]; _dm_c2=$?
  [ "${DM_BD_SKIPPED:-1}" -eq 0 ]; _dm_c3=$?
  # SABLE-muew7: a conjunction's RED must name WHICH clause broke, not just
  # that the control failed — three agents burned an evening on this exact
  # control's twin (below) unable to tell rc, count, and skip-state apart.
  require_all "test-dep-merge-state.sh bd-present negative control" \
    "rc is 0" "$_dm_c1" \
    "bd-present tests > bd-absent tests" "$_dm_c2" \
    "Skipped == 0" "$_dm_c3"
  if [ "$REQUIRE_ALL_OK" -eq 1 ]; then
    pass "negative control: test-dep-merge-state.sh with bd PRESENT reports strictly more subtests than bd-absent (${DM_BD_TESTS:-<none>} > ${DM_NOBD_TESTS:-<none>}), Skipped: 0"
  else
    fail "negative control: test-dep-merge-state.sh with bd PRESENT reports strictly more subtests than bd-absent, Skipped: 0" \
         "$REQUIRE_ALL_DETAIL (rc=$RC_DM_BD bd-present tests=${DM_BD_TESTS:-<none>} bd-absent tests=${DM_NOBD_TESTS:-<none>} skipped=${DM_BD_SKIPPED:-<none>})"
  fi

  OUT_OV_BD=$(bash "$OVERLAP" 2>&1)
  RC_OV_BD=$?
  OV_BD_TESTS=$(printf '%s' "$OUT_OV_BD" | grep -oE 'Tests: [0-9]+' | tail -1 | grep -oE '[0-9]+')
  [ "$RC_OV_BD" -eq 0 ]; _ov_c1=$?
  [ "${OV_BD_TESTS:-0}" -gt "${OV_NOBD_TESTS:-0}" ]; _ov_c2=$?
  ! printf '%s' "$OUT_OV_BD" | grep -q 'Skipped:'; _ov_c3=$?
  # SABLE-muew7 / SABLE-1gnuj: THIS is the control whose collapsed single
  # boolean cost three agents an evening — chuck saw it FAIL, tarzan saw it
  # PASS on two trees, and nobody could say whether rc or the Skipped-line
  # check was the differing conjunct, because the control only ever emitted
  # PASS or FAIL. require_all's report is the fix for exactly this.
  require_all "test-overlap-dispatch-e2e.sh bd-present negative control" \
    "rc is 0" "$_ov_c1" \
    "bd-present tests > bd-absent tests" "$_ov_c2" \
    "no Skipped line at all" "$_ov_c3"
  if [ "$REQUIRE_ALL_OK" -eq 1 ]; then
    pass "negative control: test-overlap-dispatch-e2e.sh with bd PRESENT reports strictly more subtests than bd-absent (${OV_BD_TESTS:-<none>} > ${OV_NOBD_TESTS:-<none>}), no Skipped line at all"
  else
    fail "negative control: test-overlap-dispatch-e2e.sh with bd PRESENT reports strictly more subtests than bd-absent, no Skipped line at all" \
         "$REQUIRE_ALL_DETAIL (rc=$RC_OV_BD bd-present tests=${OV_BD_TESTS:-<none>} bd-absent tests=${OV_NOBD_TESTS:-<none>} output=$OUT_OV_BD)"
  fi

  # THE DISTINGUISHING PROPERTY, stated directly: the bd-absent and bd-
  # present summaries for the SAME suite must never be able to read as the
  # same result. Tests-count is the sharpest signal (7 vs 18, 0 vs 5).
  if [ "${DM_BD_TESTS:-0}" != "${DM_NOBD_TESTS:-0}" ]; then
    pass "distinguishing property: test-dep-merge-state.sh's Tests count differs between bd-absent and bd-present runs"
  else
    fail "distinguishing property: test-dep-merge-state.sh's Tests count differs between bd-absent and bd-present runs" \
         "bd-present tests=$DM_BD_TESTS bd-absent tests=$DM_NOBD_TESTS"
  fi
  if [ "${OV_BD_TESTS:-0}" != "${OV_NOBD_TESTS:-0}" ]; then
    pass "distinguishing property: test-overlap-dispatch-e2e.sh's Tests count differs between bd-absent and bd-present runs"
  else
    fail "distinguishing property: test-overlap-dispatch-e2e.sh's Tests count differs between bd-absent and bd-present runs" \
         "bd-present tests=$OV_BD_TESTS bd-absent tests=$OV_NOBD_TESTS"
  fi
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL | Skipped: $SKIP"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
