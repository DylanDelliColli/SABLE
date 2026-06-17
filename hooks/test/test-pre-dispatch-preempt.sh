#!/usr/bin/env bash
# test-pre-dispatch-preempt.sh — behavior tests for pre-dispatch-preempt.sh
# after the SABLE-uz9.3 lane rewrite (option A: Lincoln dispatches). Uses a
# stub `bd` whose P0 contents vary by inbox label. Also regression-covers
# SABLE-mb8 (deny message must not recommend the nonexistent defer --reason
# flag) and the old argv-env bug (deny message must actually list the beads).
#
# Run with:
#   bash hooks/test/test-pre-dispatch-preempt.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO/hooks/multi-manager/pre-dispatch-preempt.sh"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR"' EXIT

cat > "$FIXTURE_DIR/agents.yaml" <<'YAML'
agents:
  optimus:
    type: epic_manager
  tarzan:
    type: one_off_manager
  lincoln:
    type: strategist
YAML
export SABLE_AGENTS_YAML="$FIXTURE_DIR/agents.yaml"

# Stub bd: P0 only in for-optimus; for-tarzan has a P1; others empty
mkdir -p "$FIXTURE_DIR/bin"
cat > "$FIXTURE_DIR/bin/bd" <<'STUB'
#!/usr/bin/env bash
label=""
prev=""
for a in "$@"; do
  [ "$prev" = "-l" ] && label="$a"
  prev="$a"
done
case "$label" in
  for-optimus) echo '[{"id": "SABLE-p0x", "title": "urgent blocker", "priority": 0}]' ;;
  for-tarzan)  echo '[{"id": "SABLE-p1y", "title": "routine item", "priority": 1}]' ;;
  *)           echo '[]' ;;
esac
STUB
chmod +x "$FIXTURE_DIR/bin/bd"
export PATH="$FIXTURE_DIR/bin:$PATH"

# Mode-state fixtures
EXEC_MODE="$FIXTURE_DIR/mode-exec.json"
PLAN_MODE="$FIXTURE_DIR/mode-plan.json"
echo '{"mode": "execution", "since": "2026-06-10"}' > "$EXEC_MODE"
echo '{"mode": "planning", "since": "2026-06-10"}' > "$PLAN_MODE"
# Deliberately-absent path for the "no mode-state file" case — must NOT fall
# back to the live ~/.claude/sable/state/mode-state.json (SABLE-wtv).
NONEXISTENT_MODE="$FIXTURE_DIR/mode-nonexistent.json"

json() { # <agent_id> <agent_type> <prompt>
  python3 -c "
import json, sys
aid, atype, prompt = sys.argv[1], sys.argv[2], sys.argv[3]
d = {'tool_name': 'Agent', 'tool_input': {'subagent_type': 'general-purpose', 'prompt': prompt, 'description': 'worker'}, 'session_id': 's1'}
if aid: d['agent_id'] = aid
if atype: d['agent_type'] = atype
print(json.dumps(d))
" "$1" "$2" "$3"
}

run_hook() { # <json> <env_name> <env_role> <mode_file>
  (
    unset CLAUDE_AGENT_NAME CLAUDE_AGENT_ROLE SABLE_MODE_FILE
    [ -n "$2" ] && export CLAUDE_AGENT_NAME="$2"
    [ -n "$3" ] && export CLAUDE_AGENT_ROLE="$3"
    [ -n "$4" ] && export SABLE_MODE_FILE="$4"
    printf '%s' "$1" | bash "$HOOK" 2>/dev/null
  )
}

assert_denied() { if printf '%s' "$2" | grep -q '"permissionDecision": "deny"'; then pass "$1"; else fail "$1" "expected deny, got: ${2:-<empty>}"; fi }
assert_allowed() { if [ -z "$2" ]; then pass "$1"; else fail "$1" "expected silent allow, got: $2"; fi }

# 1. Legacy env manager with P0 in own inbox → denied
OUT=$(run_hook "$(json '' '' 'do work')" "optimus" "manager" "")
assert_denied "legacy env optimus blocked by own P0" "$OUT"

# 2. Deny message actually lists the bead (old argv-env bug regression)
printf '%s' "$OUT" | grep -q "SABLE-p0x" && pass "deny message lists the blocking bead" || fail "deny message lists the blocking bead" "got: $OUT"

# 3. Deny message gives runnable commands (SABLE-mb8: no defer --reason)
if printf '%s' "$OUT" | grep -q -- '--reason'; then
  fail "deny message must not recommend defer --reason (SABLE-mb8)"
else
  pass "deny message must not recommend defer --reason (SABLE-mb8)"
fi

# 4. Legacy env manager with only P1 → allowed
OUT=$(run_hook "$(json '' '' 'do work')" "tarzan" "manager" "")
assert_allowed "legacy env tarzan not blocked by P1" "$OUT"

# 5. v2 main session, execution mode, attributed lane with P0 → denied
OUT=$(run_hook "$(json '' '' 'Dispatching-for: optimus
Implement SABLE-xyz in worktree wk1')" "" "" "$EXEC_MODE")
assert_denied "v2 main session blocked when optimus lane has P0" "$OUT"
printf '%s' "$OUT" | grep -q "PREEMPTION (optimus)" && pass "deny names the lane" || fail "deny names the lane" "got: $OUT"

# 6. v2 main session, execution mode, unattributed dispatch → cockpit lane, clean → allowed
OUT=$(run_hook "$(json '' '' 'Explore the repo layout')" "" "" "$EXEC_MODE")
assert_allowed "v2 unattributed dispatch defaults to clean cockpit lane" "$OUT"

# 7. v2 main session in PLANNING mode → governance inactive
OUT=$(run_hook "$(json '' '' 'Dispatching-for: optimus
anything')" "" "" "$PLAN_MODE")
assert_allowed "planning mode: pre-dispatch preemption inactive" "$OUT"

# 8. Anonymous session, no mode file → inactive (SABLE-wtv: pin to an absent
#    fixture path so the live mode-state.json cannot leak into the test).
OUT=$(run_hook "$(json '' '' 'Dispatching-for: optimus')" "" "" "$NONEXISTENT_MODE")
assert_allowed "no mode-state file: inactive outside SABLE context" "$OUT"

# 9. Manager-subagent dispatching natively (SABLE-uz9.9): governance is ACTIVE
#    on its own lane — a P0 in for-optimus preempts the dispatch. (Pre-uz9.9
#    this stood down because all subagents stood down; nested spawn now works,
#    CC 2.1.177, SABLE-uz9.8.)
OUT=$(run_hook "$(json a1 optimus 'spawn a worker on SABLE-xyz')" "" "" "$EXEC_MODE")
assert_denied "manager-subagent optimus preempted by own P0 (native dispatch)" "$OUT"
printf '%s' "$OUT" | grep -q "PREEMPTION (optimus)" && pass "manager-subagent deny names the lane" || fail "manager-subagent deny names the lane" "got: $OUT"

# 9b. Worker subagent (non-manager type) still stands down, even in execution mode.
OUT=$(run_hook "$(json a2 general-purpose 'do the work')" "" "" "$EXEC_MODE")
assert_allowed "worker subagent (general-purpose) stands down" "$OUT"

# 10. Dispatcher-typed env session (lincoln) honors Dispatching-for attribution
OUT=$(run_hook "$(json '' '' 'Dispatching-for: optimus
Implement SABLE-abc')" "lincoln" "manager" "")
assert_denied "env lincoln dispatching for optimus blocked by optimus P0" "$OUT"
printf '%s' "$OUT" | grep -q "PREEMPTION (optimus)" && pass "env dispatcher lane attribution honored" || fail "env dispatcher lane attribution honored" "got: $OUT"

# 11. Dispatcher-typed env session without attribution defaults to own lane
OUT=$(run_hook "$(json '' '' 'Explore something')" "lincoln" "manager" "")
assert_allowed "env lincoln unattributed dispatch uses own clean lane" "$OUT"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
