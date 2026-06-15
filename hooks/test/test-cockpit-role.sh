#!/usr/bin/env bash
# test-cockpit-role.sh — Verifies the cockpit role-prompt exists and carries the
# defining duties: it is mode-aware (planning + execution), reads its mode from
# the shared mechanism, and — unlike Lincoln — launches fleets.
#
# Run with:
#   bash hooks/test/test-cockpit-role.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
# v2 (SABLE-uz9.5): the main-session role lives at roles/lincoln.md — the
# session-role-anchor resolves by CLAUDE_AGENT_NAME, and the agent is lincoln.
ROLE="$REPO/templates/multi-manager/roles/lincoln.md"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
assert_grep() { if grep -qi -- "$2" "$1" 2>/dev/null; then pass "$3"; else fail "$3" "pattern not found: $2"; fi; }
assert_no_grep() { if grep -qi -- "$2" "$1" 2>/dev/null; then fail "$3" "pattern unexpectedly present: $2"; else pass "$3"; fi; }

if [ -f "$ROLE" ]; then pass "roles/lincoln.md exists"; else fail "roles/lincoln.md exists" "missing: $ROLE"; fi
if [ -f "$REPO/templates/multi-manager/roles/cockpit.md" ]; then
  fail "roles/cockpit.md retired (merged into lincoln.md)" "file still exists"
else
  pass "roles/cockpit.md retired (merged into lincoln.md)"
fi

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
# v2 (SABLE-uz9.5): the role IS Lincoln, so FRAMING wears "the strategist hat"
# rather than "the Lincoln strategist hat".
assert_grep "$ROLE" "strategist hat" "role frames FRAMING as the strategist hat"

# v2 one-window topology markers (SABLE-uz9.5 / uz9.4 option A)
assert_grep "$ROLE" "LINCOLN" "role declares the Lincoln identity"
assert_grep "$ROLE" "self-dispatch and self-push" "role: managers self-dispatch and self-push (SABLE-uz9.11)"
assert_no_grep "$ROLE" "Dispatching-for" "role drops the old DISPATCH-REQUEST relay attribution"
assert_grep "$ROLE" "run_in_background" "role spawns managers as invisible background subagents"
assert_grep "$ROLE" "Never spawn a manager in the foreground" "role forbids foreground manager spawns (chat never blocks)"
assert_grep "$ROLE" "Chuck terminal" "role reminds the operator about the Chuck terminal"
assert_grep "$ROLE" "push their own approved" "role: managers push their own approved lanes (Lincoln does not push)"
assert_grep "$ROLE" "gaudi skill" "role runs gaudi as an inline skill at ARCHITECTURE"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
