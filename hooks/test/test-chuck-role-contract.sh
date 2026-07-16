#!/usr/bin/env bash
# test-chuck-role-contract.sh — locks the D3 chuck-wake trigger into
# templates/multi-manager/roles/chuck.md (SABLE-jfg6.5 / S5-U2): the prose
# stranded-recovery sweep duty is retired (promoted to the tool) and every
# wake runs `sable-reconcile-handoffs` as a standing step instead. Victor's
# review note: the prose lives in TWO places (the ":31 sweep sentence" AND the
# ":88 Exception (stranded-recovery)" block) — both must be gone.
#
# Pure grep — no bd/git/subprocess — so it needs no clean-room guard.
#
# Run with: bash hooks/test/test-chuck-role-contract.sh

set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
CHUCK="$REPO/templates/multi-manager/roles/chuck.md"

PASS=0; FAIL=0; FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
has() { if grep -qF -- "$2" "$CHUCK" 2>/dev/null; then pass "$1"; else fail "$1" "chuck.md missing: $2"; fi; }
hasno() { if grep -qF -- "$2" "$CHUCK" 2>/dev/null; then fail "$1" "chuck.md unexpectedly has: $2"; else pass "$1"; fi; }

if [ ! -f "$CHUCK" ]; then
  echo "FAIL: chuck.md not found at $CHUCK"
  exit 2
fi

# --- the prose duty is gone from BOTH locations victor flagged ---
hasno "chuck.md drops the ':31 sweep sentence' phrase"          "stranded-recovery sweep"
hasno "chuck.md drops the ':88 Exception (stranded-recovery)' block" "Exception (stranded-recovery)"

# --- the tool invocation stands in its place ---
has "chuck.md names sable-reconcile-handoffs as a standing step" "sable-reconcile-handoffs"
has "chuck.md documents every-wake cadence for the reconcile step" "EVERY wake"

# --- boundaries section still forbids hand-filing, now attributing the
#     exception to the tool rather than to Chuck's own judgment call ---
has "chuck.md still forbids Chuck hand-filing for-chuck beads" "You do not file for-chuck beads yourself"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
