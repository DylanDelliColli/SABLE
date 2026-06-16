#!/usr/bin/env bash
# test-sable-teams-preflight.sh — SABLE-amj.6
# Env-matrix unit tests for bin/sable-teams-preflight: SABLE_TEAMS x the
# experimental flag -> correct topology or a helpful error.
#
# Run with:
#   bash hooks/test/test-sable-teams-preflight.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PREFLIGHT="$REPO/bin/sable-teams-preflight"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

[ -x "$PREFLIGHT" ] || { fail "preflight is executable"; echo "Tests: 1 | Passed: 0 | Failed: 1"; exit 1; }
pass "preflight is executable"

# run_case <label> <env_prefix> <expect_out> <expect_exit>
run_case() {
  local label="$1" env_prefix="$2" expect_out="$3" expect_exit="$4"
  local out exit_code
  out=$(env -i PATH="$PATH" $env_prefix bash "$PREFLIGHT" 2>/dev/null)
  exit_code=$?
  if [ "$out" = "$expect_out" ] && [ "$exit_code" -eq "$expect_exit" ]; then
    pass "$label"
  else
    fail "$label" "expected [$expect_out|exit $expect_exit] got [$out|exit $exit_code]"
  fi
}

# No SABLE_TEAMS -> nested (the default topology)
run_case "unset SABLE_TEAMS -> nested"                "" "nested" 0
# SABLE_TEAMS truthy + flag present -> teams
run_case "SABLE_TEAMS=1 + flag=1 -> teams"            "SABLE_TEAMS=1 CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1" "teams" 0
run_case "SABLE_TEAMS=true + flag=1 -> teams"         "SABLE_TEAMS=true CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1" "teams" 0
run_case "SABLE_TEAMS=on + flag=1 -> teams"           "SABLE_TEAMS=on CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1" "teams" 0
# SABLE_TEAMS requested but flag absent -> error (exit 1, nothing on stdout)
run_case "SABLE_TEAMS=1 + flag absent -> error exit 1" "SABLE_TEAMS=1" "" 1
# Falsey SABLE_TEAMS -> nested
run_case "SABLE_TEAMS=0 -> nested"                     "SABLE_TEAMS=0" "nested" 0
run_case "SABLE_TEAMS=false -> nested"                 "SABLE_TEAMS=false" "nested" 0

# Error message carries the one-line fix
ERR=$(env -i PATH="$PATH" SABLE_TEAMS=1 bash "$PREFLIGHT" 2>&1 1>/dev/null)
if printf '%s' "$ERR" | grep -q "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"; then
  pass "error message names the missing flag"
else
  fail "error message names the missing flag" "got: $ERR"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
