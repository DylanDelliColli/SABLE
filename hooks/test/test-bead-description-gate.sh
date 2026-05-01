#!/usr/bin/env bash
# test-bead-description-gate.sh — Unit tests for bead-description-gate.sh
#
# Pipes synthetic PreToolUse:Bash JSON input to the hook and verifies the
# response (deny vs allow vs nudge). No bd or git state required.
#
# Run with:
#   bash hooks/test/test-bead-description-gate.sh
#
# Exits 0 if all pass, nonzero if any fail.

set -uo pipefail

HOOK="$(cd "$(dirname "$0")/.." && pwd)/bead-description-gate.sh"

if [ ! -x "$HOOK" ]; then
  echo "FAIL: hook not executable at $HOOK"
  exit 2
fi

PASS=0
FAIL=0
FAIL_NAMES=""

# Helpers
make_input() {
  # $1 = command string
  python3 -c "
import json, sys
cmd = sys.argv[1]
print(json.dumps({'tool_input': {'command': cmd}}))
" "$1"
}

# run_hook <env-prefix> <command>
# Outputs: <exit-code><tab><stdout>
run_hook() {
  local env_prefix="$1"
  local command="$2"
  local input
  input=$(make_input "$command")
  local out
  out=$(env -i PATH="$PATH" $env_prefix bash "$HOOK" <<< "$input" 2>/dev/null || echo "RUN_ERR:$?")
  echo -n "$out"
}

assert_allow() {
  # $1 = test name, $2 = env, $3 = command
  local name="$1" env="$2" cmd="$3"
  local out
  out=$(run_hook "$env" "$cmd")
  # Allow = empty stdout (no decision JSON emitted)
  if [ -z "$out" ]; then
    PASS=$((PASS+1))
    echo "PASS: $name"
  else
    FAIL=$((FAIL+1))
    FAIL_NAMES="$FAIL_NAMES\n  $name (got: $out)"
    echo "FAIL: $name"
    echo "  Expected: empty (allow)"
    echo "  Got:      $out"
  fi
}

assert_deny() {
  # $1 = test name, $2 = env, $3 = command, $4 = substring expected in reason
  local name="$1" env="$2" cmd="$3" expect="$4"
  local out
  out=$(run_hook "$env" "$cmd")
  if echo "$out" | grep -q '"permissionDecision": "deny"' && echo "$out" | grep -qF "$expect"; then
    PASS=$((PASS+1))
    echo "PASS: $name"
  else
    FAIL=$((FAIL+1))
    FAIL_NAMES="$FAIL_NAMES\n  $name (got: $out)"
    echo "FAIL: $name"
    echo "  Expected: deny containing '$expect'"
    echo "  Got:      $out"
  fi
}

assert_nudge() {
  # $1 = test name, $2 = env, $3 = command, $4 = substring expected
  local name="$1" env="$2" cmd="$3" expect="$4"
  local out
  out=$(run_hook "$env" "$cmd")
  if echo "$out" | grep -q '"additionalContext"' && echo "$out" | grep -qF "$expect"; then
    PASS=$((PASS+1))
    echo "PASS: $name"
  else
    FAIL=$((FAIL+1))
    FAIL_NAMES="$FAIL_NAMES\n  $name (got: $out)"
    echo "FAIL: $name"
    echo "  Expected: nudge containing '$expect'"
    echo "  Got:      $out"
  fi
}

# Build a sherlock-complete description with real newlines (matches what
# `bd create --description="..."` produces when the agent types a multi-line
# heredoc-style quoted string).
COMPLETE_SHERLOCK_DESC=$'## Rationale\nFoo\n\n## Evidence\n### File: src/auth/middleware.ts\n- Symbol: publicPaths\n- Fingerprint: const publicPaths = [\n\n## Proposed approach\nBar\n\n## Scope estimate\nS\n\n## Risk if not addressed\nBaz\n\nTest spec: src/auth/test_middleware.test.ts'

# ---------- Default mode (no agent identity) ----------

# Test 1: non-bd-create commands ignored
assert_allow "ignores non-bd-create" "" "git status"

# Test 2: epic creation skipped
assert_allow "epic exempt" "" "bd create --type=epic --title=foo --description=\"bar\""

# Test 3: missing description in default mode → nudge
assert_nudge "default: missing description nudges" "" "bd create --title=foo" "no --description flag"

# Test 4: vague description in default mode → nudge with missing list
assert_nudge "default: vague description nudges" "" "bd create --title=foo --description=\"do the thing\"" "missing"

# Test 5: full description (file path + test) in default mode → allow
assert_allow "default: complete description allowed" "" "bd create --title=foo --description=\"Update src/foo.ts. Test in src/foo.test.ts.\""

# ---------- Manager mode (CLAUDE_AGENT_NAME set) ----------

MANAGER_ENV="CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager"

# Test 6: missing description in manager mode → DENY
assert_deny "manager: missing description denied" "$MANAGER_ENV" "bd create --title=foo" "no --description"

# Test 7: vague description in manager mode → DENY with missing list
assert_deny "manager: vague description denied" "$MANAGER_ENV" "bd create --title=foo --description=\"do the thing\"" "missing"

# Test 8: full description in manager mode → allow
assert_allow "manager: complete description allowed" "$MANAGER_ENV" "bd create --title=foo --description=\"Update src/foo.ts. Test in src/foo.test.ts.\""

# Test 9: epic creation skipped in manager mode
assert_allow "manager: epic exempt" "$MANAGER_ENV" "bd create --type=epic --title=foo --description=\"bar\""

# ---------- Sherlock-finding label checks (manager mode only) ----------

# Test 10: sherlock-finding label without required sections → DENY listing them
assert_deny "manager: sherlock-finding incomplete denied" "$MANAGER_ENV" \
  "bd create --title=foo --labels=sherlock-finding --description=\"Update src/foo.ts. Test in src/foo.test.ts.\"" \
  "Rationale"

# Test 11: sherlock-finding label with all required sections → allow
assert_allow "manager: sherlock-finding complete allowed" "$MANAGER_ENV" \
  "bd create --title=foo --labels=sherlock-finding --description=\"$COMPLETE_SHERLOCK_DESC\""

# Test 12: sherlock-finding label with everything except Fingerprint → DENY mentioning Fingerprint
PARTIAL_NO_FP=$'## Rationale\nFoo\n\n## Evidence\n### File: src/auth.ts\n- Symbol: foo\n\n## Proposed approach\nBar\n\n## Scope estimate\nS\n\n## Risk if not addressed\nBaz\n\nTest spec: src/auth.test.ts'
assert_deny "manager: sherlock-finding without fingerprint denied" "$MANAGER_ENV" \
  "bd create --title=foo --labels=sherlock-finding --description=\"$PARTIAL_NO_FP\"" \
  "Fingerprint"

# Test 13: non-sherlock-finding label, complete description → allow even in manager mode
assert_allow "manager: non-sherlock label allowed when complete" "$MANAGER_ENV" \
  "bd create --title=foo --labels=bug,for-tarzan --description=\"Update src/foo.ts. Test in src/foo.test.ts.\""

# ---------- Summary ----------

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="

if [ "$FAIL" -gt 0 ]; then
  echo -e "Failed tests:$FAIL_NAMES"
  exit 1
fi
exit 0
