#!/usr/bin/env bash
# test-role.sh — Verifies the lincoln role-prompt exists and carries the
# defining duties (mode-aware planning + execution, reads its mode from the
# shared mechanism, brings up the warm-pane session), and pins the manager
# role files' warm-pane mandate + lane boundaries. Complements
# test-tmux-roles.sh (which lints the tmux-native dispatch/messaging markers).
#
# Run with:
#   bash hooks/test/test-role.sh

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

# warm-pane topology markers (SABLE-bldh / tmux-only SABLE-qa4d)
assert_grep "$ROLE" "LINCOLN" "role declares the Lincoln identity"
assert_no_grep "$ROLE" "Dispatching-for" "role drops the old DISPATCH-REQUEST relay attribution"
assert_grep "$ROLE" "panes, not subagents" "role: managers are warm panes, never Agent-tool spawns"
assert_grep "$ROLE" "sable-launch" "role names sable-launch as the session door"
assert_grep "$ROLE" "sable-spawn-manager" "role stands up managers on demand (execution mode only)"
assert_grep "$ROLE" "sable-msg" "role directs managers over sable-msg"
assert_grep "$ROLE" "workers self-push" "role: workers self-push their own worktree branches (Lincoln does not push)"
assert_no_grep "$ROLE" "Chuck terminal" "role no longer reminds about a Chuck terminal (chuck is a pane)"
assert_grep "$ROLE" "run_in_background" "role spawns PLANNING producers as background subagents"
assert_grep "$ROLE" "gaudi skill" "role runs gaudi as an inline skill at ARCHITECTURE"

# --- manager role-file source prose: warm-pane mandate + lane boundaries ---
# Pin the CONVERTED state of both manager role files so relay or Agent-tool
# dispatch phrasing cannot silently return in a future merge. (The tmux
# dispatch/messaging markers are separately linted in test-tmux-roles.sh.)
for mgr in tarzan optimus; do
  MROLE="$REPO/templates/multi-manager/roles/$mgr.md"
  assert_no_grep "$MROLE" "DISPATCH-REQUEST"   "$mgr role drops the DISPATCH-REQUEST relay"
  assert_grep    "$MROLE" "not the Agent tool" "$mgr role dispatches via the spawn helper, NOT the Agent tool"
  assert_grep    "$MROLE" "sable-worker-status" "$mgr role monitors/reaps worker panes"
done
assert_grep "$REPO/templates/multi-manager/roles/optimus.md" "parent epic" "optimus keeps the epic lane boundary"
assert_grep "$REPO/templates/multi-manager/roles/tarzan.md"  "orphan"      "tarzan keeps the orphan lane boundary"
assert_grep "$REPO/templates/multi-manager/roles/tarzan.md"  "emergency"   "tarzan keeps emergency mode"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
