#!/usr/bin/env bash
# test-cockpit-skills.sh — Integration test for the /plan and /execute mode-flip
# skills (SABLE-cav.1 acceptance: "/plan writes cockpit-mode.json with
# mode=planning and loads the planning persona; /execute writes mode=execution
# and loads the overseer persona").
#
# A skill body is prose Claude executes, so the "integration" verified here is:
#   1. each skill file exists with the right invocation name,
#   2. each skill is WIRED to the shared mechanism (`sable-mode set <mode>`),
#   3. each skill loads its persona (planning producers / execution overseer),
#   4. the documented mechanism, run end-to-end, produces the correct
#      mode-state file (exercised through bin/sable-mode against a temp state).
#
# Run with:
#   bash hooks/test/test-cockpit-skills.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PLAN_SKILL="$REPO/skills/cockpit-plan/SKILL.md"
EXEC_SKILL="$REPO/skills/cockpit-execute/SKILL.md"
MODE_BIN="$REPO/bin/sable-mode"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

assert_file() { if [ -f "$1" ]; then pass "$2"; else fail "$2" "missing: $1"; fi; }
assert_grep() {
  # file pattern name
  if grep -qi -- "$2" "$1" 2>/dev/null; then pass "$3"; else fail "$3" "pattern not found: $2"; fi
}

# 1. files exist
assert_file "$PLAN_SKILL" "/plan skill file exists"
assert_file "$EXEC_SKILL" "/execute skill file exists"

# 2. invocation name in frontmatter
assert_grep "$PLAN_SKILL" "name: plan"    "/plan declares name: plan"
assert_grep "$EXEC_SKILL" "name: execute" "/execute declares name: execute"

# 3. wired to the shared mechanism
assert_grep "$PLAN_SKILL" "sable-mode set planning"  "/plan invokes sable-mode set planning"
assert_grep "$EXEC_SKILL" "sable-mode set execution" "/execute invokes sable-mode set execution"

# 4. persona loaded
assert_grep "$PLAN_SKILL" "planning"  "/plan loads the planning persona"
assert_grep "$PLAN_SKILL" "producer"  "/plan references the Tier-2 producers"
assert_grep "$EXEC_SKILL" "execution" "/execute loads the execution persona"
assert_grep "$EXEC_SKILL" "oversee"   "/execute references overseeing the managers"

# 5. end-to-end mechanism: the documented command flips state correctly
STATE_TMP="$(mktemp -u)"
SABLE_COCKPIT_STATE="$STATE_TMP" "$MODE_BIN" set planning --fleet sherlock,columbo,gaudi,victor >/dev/null 2>&1
assert_planning="$(SABLE_COCKPIT_STATE="$STATE_TMP" "$MODE_BIN" get 2>/dev/null)"
if [ "$assert_planning" = "planning" ]; then pass "documented /plan mechanism yields mode=planning"; else fail "documented /plan mechanism yields mode=planning" "got '$assert_planning'"; fi

SABLE_COCKPIT_STATE="$STATE_TMP" "$MODE_BIN" set execution --fleet optimus,tarzan,chuck >/dev/null 2>&1
assert_exec="$(SABLE_COCKPIT_STATE="$STATE_TMP" "$MODE_BIN" get 2>/dev/null)"
if [ "$assert_exec" = "execution" ]; then pass "documented /execute mechanism yields mode=execution"; else fail "documented /execute mechanism yields mode=execution" "got '$assert_exec'"; fi
rm -f "$STATE_TMP"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
