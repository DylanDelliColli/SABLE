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

# ---------- Columbo-test-spec label checks ----------

# Build a complete columbo-test-spec description — all six required sections,
# Cases includes a bullet with a Why: sub-line. Avoid embedded double quotes
# in fixtures since they terminate the bash-quoted --description argument.
COMPLETE_COLUMBO_SPEC=$'## Feature under test\nPOST /items endpoint creates an item from a JSON body.\n\n## Test file\ntests/items.skel.test.ts\n\n## Cases\n- Case name: rejects empty name\n  - Why: catches the bug where empty strings bypassed the not-null constraint\n  - Inputs: name=empty-string\n  - Expected: 422 with body error=name_required\n\n## Categories\n2, 3\n\n## Fixtures / setup\nFixtures: none.\n\n## Out of scope\nMulti-tenant rate limiting (deferred to SABLE-future).'

# Test 14: columbo-test-spec missing ## Cases → DENY mentioning Cases (manager mode)
SPEC_NO_CASES=$'## Feature under test\nPOST /items.\n\n## Test file\ntests/items.skel.test.ts\n\n## Categories\n2, 3\n\n## Fixtures / setup\nFixtures: none.\n\n## Out of scope\nNone.'
assert_deny "manager: columbo-test-spec without Cases denied" "$MANAGER_ENV" \
  "bd create --title=foo --labels=columbo-test-spec --description=\"$SPEC_NO_CASES\"" \
  "Cases"

# Test 15: columbo-test-spec missing Why: sub-line in Cases → DENY mentioning Why
SPEC_NO_WHY=$'## Feature under test\nPOST /items.\n\n## Test file\ntests/items.skel.test.ts\n\n## Cases\n- Case name: rejects empty name\n  - Inputs: name=""\n  - Expected: 422\n\n## Categories\n2, 3\n\n## Fixtures / setup\nFixtures: none.\n\n## Out of scope\nNone.'
assert_deny "manager: columbo-test-spec without Why denied" "$MANAGER_ENV" \
  "bd create --title=foo --labels=columbo-test-spec --description=\"$SPEC_NO_WHY\"" \
  "Why"

# Test 16: complete columbo-test-spec → allow (manager mode)
assert_allow "manager: columbo-test-spec complete allowed" "$MANAGER_ENV" \
  "bd create --title=foo --labels=columbo-test-spec --description=\"$COMPLETE_COLUMBO_SPEC\""

# Test 17: complete columbo-test-spec in default mode → allow (no agent identity)
assert_allow "default: columbo-test-spec complete allowed" "" \
  "bd create --title=foo --labels=columbo-test-spec --description=\"$COMPLETE_COLUMBO_SPEC\""

# Test 18: incomplete columbo-test-spec in default mode → nudge (not deny)
assert_nudge "default: columbo-test-spec incomplete nudges" "" \
  "bd create --title=foo --labels=columbo-test-spec --description=\"$SPEC_NO_CASES\"" \
  "Cases"

# Test 19: columbo-test-spec coexists with model + for-tarzan labels — complete → allow
assert_allow "manager: columbo-test-spec with sibling labels allowed" "$MANAGER_ENV" \
  "bd create --title=foo --labels=columbo-test-spec,model:sonnet,for-tarzan --description=\"$COMPLETE_COLUMBO_SPEC\""

# ---------- Columbo-test-gap label checks ----------

COMPLETE_COLUMBO_GAP=$'## Symptom\nThe existing test for process_refund only covers the success path; nothing exercises partial-failure mid-batch.\n\n## Cited test file\ntests/refund.test.ts — refund_success_block\n\n## Cited source file\nsrc/refund.ts — process_refund\n\n## Fingerprint\nfor (const item of refundBatch)\n\n## Cases to add\n- Case name: handles 503 mid-batch without losing successful refunds\n  - Why: catches silent partial-failure regression\n  - Inputs: batch of 5 items, gateway returns 200 for items 0-2, 503 for items 3-4\n  - Expected: items 0-2 marked refunded; items 3-4 retained as pending; no double-refund on retry\n\n## Categories\n5, 8\n\n## Risk if not addressed\nA partial-failure mid-batch will silently drop pending refunds, causing customer-visible refund delays and accounting drift.'

# Test 20: columbo-test-gap missing ## Fingerprint → DENY mentioning Fingerprint
GAP_NO_FP=$'## Symptom\nFoo.\n\n## Cited test file\ntests/refund.test.ts\n\n## Cited source file\nsrc/refund.ts\n\n## Cases to add\n- Case name: x\n  - Why: y\n  - Inputs: z\n  - Expected: q\n\n## Categories\n5\n\n## Risk if not addressed\nBar.'
assert_deny "manager: columbo-test-gap without Fingerprint denied" "$MANAGER_ENV" \
  "bd create --title=foo --labels=columbo-test-gap --description=\"$GAP_NO_FP\"" \
  "Fingerprint"

# Test 21: columbo-test-gap missing ## Risk if not addressed → DENY mentioning Risk
GAP_NO_RISK=$'## Symptom\nFoo.\n\n## Cited test file\ntests/refund.test.ts\n\n## Cited source file\nsrc/refund.ts\n\n## Fingerprint\nfor (const item of refundBatch)\n\n## Cases to add\n- Case name: x\n  - Why: y\n  - Inputs: z\n  - Expected: q\n\n## Categories\n5'
assert_deny "manager: columbo-test-gap without Risk denied" "$MANAGER_ENV" \
  "bd create --title=foo --labels=columbo-test-gap --description=\"$GAP_NO_RISK\"" \
  "Risk"

# Test 22: complete columbo-test-gap → allow (manager mode)
assert_allow "manager: columbo-test-gap complete allowed" "$MANAGER_ENV" \
  "bd create --title=foo --labels=columbo-test-gap --description=\"$COMPLETE_COLUMBO_GAP\""

# Test 23: complete columbo-test-gap in default mode → allow
assert_allow "default: columbo-test-gap complete allowed" "" \
  "bd create --title=foo --labels=columbo-test-gap --description=\"$COMPLETE_COLUMBO_GAP\""

# Test 24: columbo-test-gap incomplete in default mode → nudge (not deny)
assert_nudge "default: columbo-test-gap incomplete nudges" "" \
  "bd create --title=foo --labels=columbo-test-gap --description=\"$GAP_NO_FP\"" \
  "Fingerprint"

# Test 25: columbo-test-gap coexists with sibling labels — complete → allow
assert_allow "manager: columbo-test-gap with sibling labels allowed" "$MANAGER_ENV" \
  "bd create --title=foo --labels=columbo-test-gap,model:opus,for-optimus --description=\"$COMPLETE_COLUMBO_GAP\""

# Test 26: existing sherlock-finding behavior unchanged when columbo gate is also active
assert_allow "regression: sherlock-finding still allowed after columbo gate added" "$MANAGER_ENV" \
  "bd create --title=foo --labels=sherlock-finding --description=\"$COMPLETE_SHERLOCK_DESC\""

# Test 27: cross-label — bead carries BOTH columbo-test-spec and sherlock-finding.
# Both gates fire; description must satisfy both contracts. Composing the
# COMPLETE_SHERLOCK_DESC + columbo sections produces a description that
# passes everything. Confirms the gate ANDs label requirements rather than
# silently picking one and ignoring the other.
DUAL_DESC=$'## Rationale\nFoo\n\n## Evidence\n### File: src/auth.ts\n- Symbol: foo\n- Fingerprint: const foo = [\n\n## Proposed approach\nBar\n\n## Scope estimate\nS\n\n## Risk if not addressed\nBaz\n\n## Feature under test\nAuth middleware.\n\n## Test file\ntests/auth.skel.test.ts\n\n## Cases\n- Case name: rejects unsigned token\n  - Why: catches signature-bypass regression\n  - Inputs: token with empty signature\n  - Expected: 401 with reason invalid_signature\n\n## Categories\n3, 10\n\n## Fixtures / setup\nFixtures: none.\n\n## Out of scope\nRefresh-token rotation.'
assert_allow "manager: dual-label spec + sherlock-finding allowed when both contracts satisfied" "$MANAGER_ENV" \
  "bd create --title=foo --labels=columbo-test-spec,sherlock-finding --description=\"$DUAL_DESC\""

# Test 28: cross-label — same dual-label bead but missing columbo's Cases →
# DENY mentioning Cases (proves columbo gate fires even when sherlock gate
# would otherwise pass).
DUAL_NO_CASES=$'## Rationale\nFoo\n\n## Evidence\n### File: src/auth.ts\n- Symbol: foo\n- Fingerprint: const foo = [\n\n## Proposed approach\nBar\n\n## Scope estimate\nS\n\n## Risk if not addressed\nBaz\n\n## Feature under test\nAuth middleware.\n\n## Test file\ntests/auth.skel.test.ts\n\n## Categories\n3\n\n## Fixtures / setup\nFixtures: none.\n\n## Out of scope\nNone.'
assert_deny "manager: dual-label spec + sherlock-finding denied when columbo Cases missing" "$MANAGER_ENV" \
  "bd create --title=foo --labels=columbo-test-spec,sherlock-finding --description=\"$DUAL_NO_CASES\"" \
  "Cases"

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
