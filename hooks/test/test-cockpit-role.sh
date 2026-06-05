#!/usr/bin/env bash
# test-cockpit-role.sh — Verifies the cockpit role-prompt exists and carries the
# defining duties: it is mode-aware (planning + execution), reads its mode from
# the shared mechanism, and — unlike Lincoln — launches fleets.
#
# Run with:
#   bash hooks/test/test-cockpit-role.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
ROLE="$REPO/templates/multi-manager/roles/cockpit.md"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
assert_grep() { if grep -qi -- "$2" "$1" 2>/dev/null; then pass "$3"; else fail "$3" "pattern not found: $2"; fi; }

if [ -f "$ROLE" ]; then pass "roles/cockpit.md exists"; else fail "roles/cockpit.md exists" "missing: $ROLE"; fi

assert_grep "$ROLE" "cockpit"        "role declares the cockpit identity"
assert_grep "$ROLE" "planning"       "role describes planning mode"
assert_grep "$ROLE" "execution"      "role describes execution mode"
assert_grep "$ROLE" "sable-mode"     "role reads mode via sable-mode"
assert_grep "$ROLE" "background"     "role launches agents as background sessions"
assert_grep "$ROLE" "interlock"      "role references the mode interlock"

# staged-planning substages + the Lincoln/FRAMING front door
assert_grep "$ROLE" "substage" "role describes the planning substage machine"
for s in framing research architecture test-strategy decomposition; do
  assert_grep "$ROLE" "$s" "role names substage: $s"
done
assert_grep "$ROLE" "Lincoln strategist hat" "role frames FRAMING as the Lincoln strategist hat"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
