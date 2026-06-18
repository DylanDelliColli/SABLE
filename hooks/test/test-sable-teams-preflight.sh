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

# ---- Helpers ----

# run_case_with_home <label> <env_prefix> <home_dir> <expect_out> <expect_exit> [<check_stderr_contains>]
# Runs the preflight with an isolated PATH+HOME (no other env leakage).
run_case_with_home() {
  local label="$1" env_prefix="$2" home_dir="$3" expect_out="$4" expect_exit="$5" check_stderr="${6:-}"
  local out err exit_code tmpout tmperr
  tmpout=$(mktemp)
  tmperr=$(mktemp)
  env -i PATH="$PATH" HOME="$home_dir" $env_prefix bash "$PREFLIGHT" >"$tmpout" 2>"$tmperr"
  exit_code=$?
  out=$(cat "$tmpout")
  err=$(cat "$tmperr")
  rm -f "$tmpout" "$tmperr"
  local ok=1
  [ "$out" = "$expect_out" ] || ok=0
  [ "$exit_code" -eq "$expect_exit" ] || ok=0
  if [ -n "$check_stderr" ]; then
    printf '%s' "$err" | grep -q "$check_stderr" || ok=0
  fi
  if [ "$ok" -eq 1 ]; then
    pass "$label"
  else
    fail "$label" "expected [out=$expect_out|exit $expect_exit] got [out=$out|exit $exit_code] stderr=[$err]"
  fi
}

# run_case <label> <env_prefix> <expect_out> <expect_exit>
# Uses an empty HOME (no agents-teams/ anywhere). Suitable for flag-absent and falsey SABLE_TEAMS cases.
EMPTY_HOME=$(mktemp -d)
run_case() {
  local label="$1" env_prefix="$2" expect_out="$3" expect_exit="$4"
  run_case_with_home "$label" "$env_prefix" "$EMPTY_HOME" "$expect_out" "$expect_exit"
}

# Set up temp HOME dirs for defs-on-disk tests
TMPDIR_NO_DEFS=$(mktemp -d)
TMPDIR_WITH_DEFS=$(mktemp -d)
mkdir -p "$TMPDIR_WITH_DEFS/.claude/agents-teams"

# ---- Tests ----

# No SABLE_TEAMS -> nested (the default topology)
run_case "unset SABLE_TEAMS -> nested"                "" "nested" 0
# SABLE_TEAMS truthy + flag present + defs present -> teams
run_case_with_home "SABLE_TEAMS=1 + flag=1 -> teams"            "SABLE_TEAMS=1 CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1" "$TMPDIR_WITH_DEFS" "teams" 0
run_case_with_home "SABLE_TEAMS=true + flag=1 -> teams"         "SABLE_TEAMS=true CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1" "$TMPDIR_WITH_DEFS" "teams" 0
run_case_with_home "SABLE_TEAMS=on + flag=1 -> teams"           "SABLE_TEAMS=on CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1" "$TMPDIR_WITH_DEFS" "teams" 0
# SABLE_TEAMS requested but flag absent -> error (exit 1, nothing on stdout)
run_case "SABLE_TEAMS=1 + flag absent -> error exit 1" "SABLE_TEAMS=1" "" 1
# Falsey SABLE_TEAMS -> nested
run_case "SABLE_TEAMS=0 -> nested"                     "SABLE_TEAMS=0" "nested" 0
run_case "SABLE_TEAMS=false -> nested"                 "SABLE_TEAMS=false" "nested" 0

# Error message carries the one-line fix (flag-absent error path)
ERR=$(env -i PATH="$PATH" HOME="$EMPTY_HOME" SABLE_TEAMS=1 bash "$PREFLIGHT" 2>&1 1>/dev/null)
if printf '%s' "$ERR" | grep -q "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"; then
  pass "error message names the missing flag"
else
  fail "error message names the missing flag" "got: $ERR"
fi

# ---- agents-teams/ defs-on-disk checks (SABLE-1qt) ----

# Case: SABLE_TEAMS=1 + flag=1 + agents-teams/ absent in HOME -> outputs "nested", exit 0, stderr hint
run_case_with_home \
  "SABLE_TEAMS=1 + flag=1 + agents-teams/ absent -> nested (exit 0)" \
  "SABLE_TEAMS=1 CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1" \
  "$TMPDIR_NO_DEFS" \
  "nested" 0 "agents-teams"

# Case: SABLE_TEAMS=1 + flag=1 + agents-teams/ present in HOME -> outputs "teams", exit 0
run_case_with_home \
  "SABLE_TEAMS=1 + flag=1 + agents-teams/ present -> teams (exit 0)" \
  "SABLE_TEAMS=1 CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1" \
  "$TMPDIR_WITH_DEFS" \
  "teams" 0

# ---- Default flip: SABLE_TEAMS unset -> teams-when-available (SABLE-c2j.3) ----

# Case 1: unset SABLE_TEAMS + flag set + defs present -> teams (exit 0)
run_case_with_home \
  "unset SABLE_TEAMS + flag=1 + defs present -> teams" \
  "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1" \
  "$TMPDIR_WITH_DEFS" \
  "teams" 0

# Case 2: unset SABLE_TEAMS + flag set + defs absent -> nested (exit 0, stderr defs hint)
run_case_with_home \
  "unset SABLE_TEAMS + flag=1 + defs absent -> nested (defs hint)" \
  "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1" \
  "$TMPDIR_NO_DEFS" \
  "nested" 0 "agents-teams"

# Case 3: unset SABLE_TEAMS + flag absent -> nested (exit 0)
run_case \
  "unset SABLE_TEAMS + flag absent -> nested" \
  "" \
  "nested" 0

# Case 4: SABLE_TEAMS=off + flag=1 + defs present -> nested (explicit opt-out wins)
run_case_with_home \
  "SABLE_TEAMS=off + flag=1 + defs present -> nested (opt-out)" \
  "SABLE_TEAMS=off CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1" \
  "$TMPDIR_WITH_DEFS" \
  "nested" 0

# Cleanup
rm -rf "$TMPDIR_NO_DEFS" "$TMPDIR_WITH_DEFS" "$EMPTY_HOME"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
