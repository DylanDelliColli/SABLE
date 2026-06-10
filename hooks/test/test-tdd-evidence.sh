#!/usr/bin/env bash
# test-tdd-evidence.sh — Unit tests for tdd-evidence.sh
#
# Pipes synthetic PreToolUse:Bash JSON to the hook and checks whether the
# evidence file at /tmp/tdd-evidence-<session> was appended to.
#
# Run with:
#   bash hooks/test/test-tdd-evidence.sh
#
# Each test uses a fresh fake SESSION_ID so checks are isolated.
# Cleans up the evidence file after each assertion.

set -uo pipefail

HOOK="$(cd "$(dirname "$0")/.." && pwd)/tdd-evidence.sh"

if [ ! -x "$HOOK" ]; then
  echo "FAIL: hook not executable at $HOOK"
  exit 2
fi

PASS=0
FAIL=0
FAIL_NAMES=""

# Generate a unique fake session id per test so we never collide with the
# real Claude Code session's evidence file.
fake_session() {
  printf 'tdd-evidence-test-%s-%s' "$$" "$RANDOM"
}

make_input() {
  # $1 = command, $2 = session_id
  python3 -c "
import json, sys
print(json.dumps({'tool_input': {'command': sys.argv[1]}, 'session_id': sys.argv[2]}))
" "$1" "$2"
}

# run_hook_writes <test-name> <command>
# Asserts an evidence file was written (matches a "test runner" command).
run_hook_writes() {
  local name="$1" command="$2"
  local sid evidence
  sid=$(fake_session)
  evidence="/tmp/tdd-evidence-${sid}"
  rm -f "$evidence"
  make_input "$command" "$sid" | bash "$HOOK" >/dev/null 2>&1 || true
  if [ -s "$evidence" ]; then
    PASS=$((PASS+1))
    echo "PASS: $name"
  else
    FAIL=$((FAIL+1))
    FAIL_NAMES="$FAIL_NAMES\n  $name (no evidence written for: $command)"
    echo "FAIL: $name"
    echo "  Expected: evidence file written for '$command'"
    echo "  Got:      empty/missing $evidence"
  fi
  rm -f "$evidence"
}

# run_hook_silent <test-name> <command>
# Asserts no evidence was written (command did not match a test runner).
run_hook_silent() {
  local name="$1" command="$2"
  local sid evidence
  sid=$(fake_session)
  evidence="/tmp/tdd-evidence-${sid}"
  rm -f "$evidence"
  make_input "$command" "$sid" | bash "$HOOK" >/dev/null 2>&1 || true
  if [ ! -s "$evidence" ]; then
    PASS=$((PASS+1))
    echo "PASS: $name"
  else
    FAIL=$((FAIL+1))
    FAIL_NAMES="$FAIL_NAMES\n  $name (unexpected evidence: $(cat "$evidence"))"
    echo "FAIL: $name"
    echo "  Expected: no evidence (command is not a test runner): '$command'"
    echo "  Got:      $(cat "$evidence")"
  fi
  rm -f "$evidence"
}

# ---------- Existing test runners (regression) ----------

run_hook_writes "vitest run is recognized"           "npx vitest run"
run_hook_writes "pytest is recognized"               "pytest tests/"
run_hook_writes "npm test is recognized"             "npm test"
run_hook_writes "python -m pytest is recognized"     "python -m pytest"

# ---------- New: SABLE shell test harness ----------

run_hook_writes "bash hooks/test/test-foo.sh recognized" \
  "bash hooks/test/test-foo.sh"

run_hook_writes "bash with relative path to test-foo.sh recognized" \
  "bash test-foo.sh"

run_hook_writes "bash with cd prefix to hooks/test recognized" \
  "cd /home/ddc/dev-env/SABLE && bash hooks/test/test-bead-description-gate.sh"

run_hook_writes "bash with absolute path to hooks/test recognized" \
  "bash /home/ddc/dev-env/SABLE/hooks/test/test-tdd-evidence.sh"

# ---------- Negative cases ----------

run_hook_silent "git status not recognized"          "git status"
run_hook_silent "ls not recognized"                  "ls hooks/"
run_hook_silent "bash setup.sh not recognized"       "bash setup.sh"
run_hook_silent "bash deploy-script.sh not recognized" "bash deploy-script.sh"
run_hook_silent "bd close not recognized as test"    "bd close SABLE-xxx"

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
