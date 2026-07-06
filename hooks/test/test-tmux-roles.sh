#!/usr/bin/env bash
# test-tmux-roles.sh — doc-lint for the tmux warm-pane role rewrites (SABLE-bldh.5).
#
# Asserts the four role files express the tmux-native contract and dropped the
# stale in-process Agent-tool worker-dispatch language:
#   - managers (optimus/tarzan) dispatch via sable-spawn-worker, talk via sable-msg,
#     carry the sender-framing glyph, and NO LONGER carry `run_in_background`
#     (the Agent-tool background-spawn flag) for worker dispatch.
#   - lincoln directs managers via sable-msg + the framing rule and references
#     sable-tmux (run_in_background is allowed — its PLANNING producers are still
#     Agent-tool subagents).
#   - chuck is a sable-tmux pane.
#
# Run with:  bash hooks/test/test-tmux-roles.sh

set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
ROLES="$REPO/templates/multi-manager/roles"

PASS=0; FAIL=0; FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; }

has()    { if grep -q "$2" "$ROLES/$1.md" 2>/dev/null; then pass "$1.md $3"; else fail "$1.md $3 (missing: $2)"; fi; }
lacks()  { if grep -q "$2" "$ROLES/$1.md" 2>/dev/null; then fail "$1.md $3 (found stale: $2)"; else pass "$1.md $3"; fi; }

GLYPH="⟦SABLE-MSG⟧"

# --- managers: warm-pane dispatch + messaging, no stale Agent-spawn flag ---
for mgr in optimus tarzan; do
  has   "$mgr" "sable-spawn-worker"  "dispatches via sable-spawn-worker"
  has   "$mgr" "sable-msg"           "talks to Lincoln via sable-msg"
  has   "$mgr" "$GLYPH"              "carries the sender-framing rule"
  has   "$mgr" "self-push\|SELF-PUSH" "states workers self-push"
  lacks "$mgr" "run_in_background"   "dropped the Agent-tool background-spawn flag"
  lacks "$mgr" "[-][-]has-parent"    "no invalid --has-parent bd flag (SABLE-bldh.17)"
  lacks "$mgr" "[-][-]no-parent"     "no invalid --no-parent bd flag (SABLE-bldh.17)"
  lacks "$mgr" "[-][-]no-label"      "no invalid --no-label bd flag (SABLE-bldh.17)"
done

# --- lincoln: directs managers over tmux; planning producers stay subagents ---
has "lincoln" "sable-msg"   "directs managers via sable-msg"
has "lincoln" "$GLYPH"      "carries the sender-framing rule"
has "lincoln" "sable-tmux"  "references the sable-tmux launcher"

# --- chuck: a warm pane ---
has "chuck" "sable-tmux"    "is a sable-tmux pane"

# --- SABLE-qa4d.7: no stale nested/teams topology framing remains ---
lacks "chuck" "nested/teams"       "for-chuck fallback framed as pane-unreachable, not a topology"
lacks "chuck" "teams-mode gap"     "stranded-recovery no longer blamed on a teams-mode gap"
file_lacks() { if grep -q "$2" "$1" 2>/dev/null; then fail "$3 (found stale: $2)"; else pass "$3"; fi; }
file_has()   { if grep -q "$2" "$1" 2>/dev/null; then pass "$3"; else fail "$3 (missing: $2)"; fi; }
file_lacks "$REPO/hooks/multi-manager/post-push-merge-notify.sh" "nested/teams" "post-push comment framed as pane-unreachable, not a topology"
file_lacks "$REPO/templates/multi-manager/agents.yaml" "resident manager subagents" "agents.yaml lincoln entry drops resident-subagent framing"
file_lacks "$REPO/templates/multi-manager/agents.yaml" "env-var.*terminal\|terminal (SABLE-uz9.6)" "agents.yaml no longer frames chuck as an env-var terminal"
file_has   "$REPO/templates/multi-manager/agents.yaml" "warm pane\|tmux pane" "agents.yaml describes the warm-pane execution surface"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
