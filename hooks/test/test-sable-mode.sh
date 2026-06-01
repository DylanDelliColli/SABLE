#!/usr/bin/env bash
# test-sable-mode.sh — Unit tests for bin/sable-mode, the cockpit mode-state
# read/write helper shared by the /plan + /execute skills, the interlock hook
# (SABLE-cav.2), and the dashboard (SABLE-cav.3).
#
# Contract under test:
#   sable-mode set <planning|execution> [--fleet a,b,c]
#       Writes the mode-state JSON {mode, since, fleet}. Rejects any mode
#       other than planning|execution WITHOUT writing the file (nonzero exit).
#   sable-mode get      Prints the bare mode word; nonzero exit if unset.
#   sable-mode show     Prints the full JSON; nonzero exit if unset.
#
# State file location is overridable via SABLE_COCKPIT_STATE so tests never
# touch the real ~/.claude/sable/state/cockpit-mode.json.
#
# Run with:
#   bash hooks/test/test-sable-mode.sh

set -uo pipefail

MODE_BIN="$(cd "$(dirname "$0")/../.." && pwd)/bin/sable-mode"

if [ ! -x "$MODE_BIN" ]; then
  echo "FAIL: sable-mode not executable at $MODE_BIN"
  exit 2
fi

PASS=0
FAIL=0
FAIL_NAMES=""

pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() {
  FAIL=$((FAIL+1))
  FAIL_NAMES="$FAIL_NAMES\n  $1"
  echo "FAIL: $1"
  [ -n "${2:-}" ] && echo "  $2"
}

assert_eq() {
  # name expected actual
  if [ "$2" = "$3" ]; then pass "$1"; else fail "$1" "expected '$2', got '$3'"; fi
}
assert_nonzero() {
  # name rc
  if [ "$2" -ne 0 ]; then pass "$1"; else fail "$1" "expected nonzero exit, got 0"; fi
}
assert_zero() {
  if [ "$2" -eq 0 ]; then pass "$1"; else fail "$1" "expected exit 0, got $2"; fi
}

# fresh, nonexistent state path per test
fresh_state() {
  SABLE_COCKPIT_STATE="$(mktemp -u)"
  export SABLE_COCKPIT_STATE
}

# ---------- set + get round-trip ----------

fresh_state
"$MODE_BIN" set planning >/dev/null 2>&1
assert_eq "set planning then get" "planning" "$("$MODE_BIN" get 2>/dev/null)"

fresh_state
"$MODE_BIN" set execution >/dev/null 2>&1
assert_eq "set execution then get" "execution" "$("$MODE_BIN" get 2>/dev/null)"

# ---------- fleet round-trip ----------

fresh_state
"$MODE_BIN" set planning --fleet sherlock,columbo >/dev/null 2>&1
SHOW="$("$MODE_BIN" show 2>/dev/null)"
assert_eq "show mode after --fleet"  "planning" "$(printf '%s' "$SHOW" | jq -r '.mode')"
assert_eq "fleet[0]"                 "sherlock" "$(printf '%s' "$SHOW" | jq -r '.fleet[0]')"
assert_eq "fleet[1]"                 "columbo"  "$(printf '%s' "$SHOW" | jq -r '.fleet[1]')"

# ---------- since timestamp present ----------

fresh_state
"$MODE_BIN" set execution >/dev/null 2>&1
SINCE="$("$MODE_BIN" show 2>/dev/null | jq -r '.since')"
if [ -n "$SINCE" ] && [ "$SINCE" != "null" ]; then
  pass "since timestamp is non-empty"
else
  fail "since timestamp is non-empty" "got '$SINCE'"
fi

# ---------- invalid mode rejected, file not written ----------

fresh_state
"$MODE_BIN" set bogus >/dev/null 2>&1
assert_nonzero "set bogus exits nonzero" "$?"
if [ ! -f "$SABLE_COCKPIT_STATE" ]; then
  pass "set bogus does not write state file"
else
  fail "set bogus does not write state file" "file was created"
fi

fresh_state
"$MODE_BIN" set >/dev/null 2>&1
assert_nonzero "set with no mode exits nonzero" "$?"

# ---------- get/show with no state set ----------

fresh_state
"$MODE_BIN" get >/dev/null 2>&1
assert_nonzero "get with no state exits nonzero" "$?"

fresh_state
"$MODE_BIN" show >/dev/null 2>&1
assert_nonzero "show with no state exits nonzero" "$?"

# ---------- overwrite: last set wins ----------

fresh_state
"$MODE_BIN" set planning >/dev/null 2>&1
"$MODE_BIN" set execution >/dev/null 2>&1
assert_eq "second set overwrites first" "execution" "$("$MODE_BIN" get 2>/dev/null)"

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
