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

# JSON field reader (python3 — keeps the test jq-free, matching sable-mode; SABLE-cav.8)
jget() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }

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
assert_eq "show mode after --fleet"  "planning" "$(printf '%s' "$SHOW" | jget "['mode']")"
assert_eq "fleet[0]"                 "sherlock" "$(printf '%s' "$SHOW" | jget "['fleet'][0]")"
assert_eq "fleet[1]"                 "columbo"  "$(printf '%s' "$SHOW" | jget "['fleet'][1]")"

# ---------- since timestamp present ----------

fresh_state
"$MODE_BIN" set execution >/dev/null 2>&1
SINCE="$("$MODE_BIN" show 2>/dev/null | jget ".get('since','')")"
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

# ---------- runtime env gate (SABLE_COCKPIT) ----------
# Disabled: `set` refuses (so /plan /execute can't flip mode) and writes nothing.
fresh_state
SABLE_COCKPIT=off "$MODE_BIN" set planning >/dev/null 2>&1
assert_nonzero "set refused when SABLE_COCKPIT=off" "$?"
if [ ! -f "$SABLE_COCKPIT_STATE" ]; then pass "disabled set writes nothing"; else fail "disabled set writes nothing" "file created"; fi

fresh_state
SABLE_COCKPIT=0 "$MODE_BIN" set execution >/dev/null 2>&1
assert_nonzero "set refused when SABLE_COCKPIT=0" "$?"

fresh_state
SABLE_COCKPIT=FALSE "$MODE_BIN" set planning >/dev/null 2>&1
assert_nonzero "gate is case-insensitive (FALSE)" "$?"

# Reading is never gated — get/show still work when disabled.
fresh_state
"$MODE_BIN" set planning >/dev/null 2>&1
assert_eq "get works when disabled" "planning" "$(SABLE_COCKPIT=off "$MODE_BIN" get 2>/dev/null)"

# A non-disabling value still allows set.
fresh_state
SABLE_COCKPIT=on "$MODE_BIN" set execution >/dev/null 2>&1
assert_eq "set allowed when SABLE_COCKPIT=on" "execution" "$("$MODE_BIN" get 2>/dev/null)"

# ---------- substage axis (planning sub-state machine) ----------
# Ordered substages: framing -> research -> architecture -> test-strategy -> decomposition.
# Only meaningful in planning mode. `set planning` initializes framing.

# set planning initializes substage=framing
fresh_state
"$MODE_BIN" set planning >/dev/null 2>&1
assert_eq "set planning inits substage=framing" "framing" "$("$MODE_BIN" substage get 2>/dev/null)"

# advance walks the ordered list
fresh_state
"$MODE_BIN" set planning >/dev/null 2>&1
"$MODE_BIN" substage advance >/dev/null 2>&1
assert_eq "advance 1 -> research"       "research"      "$("$MODE_BIN" substage get 2>/dev/null)"
"$MODE_BIN" substage advance >/dev/null 2>&1
assert_eq "advance 2 -> architecture"   "architecture"  "$("$MODE_BIN" substage get 2>/dev/null)"
"$MODE_BIN" substage advance >/dev/null 2>&1
assert_eq "advance 3 -> test-strategy"  "test-strategy" "$("$MODE_BIN" substage get 2>/dev/null)"
"$MODE_BIN" substage advance >/dev/null 2>&1
assert_eq "advance 4 -> decomposition"  "decomposition" "$("$MODE_BIN" substage get 2>/dev/null)"

# advancing past decomposition is rejected (nonzero) and leaves substage unchanged
"$MODE_BIN" substage advance >/dev/null 2>&1
assert_nonzero "advance past decomposition exits nonzero" "$?"
assert_eq "substage unchanged after rejected advance" "decomposition" "$("$MODE_BIN" substage get 2>/dev/null)"

# substage set to a valid stage
fresh_state
"$MODE_BIN" set planning >/dev/null 2>&1
"$MODE_BIN" substage set architecture >/dev/null 2>&1
assert_eq "substage set architecture" "architecture" "$("$MODE_BIN" substage get 2>/dev/null)"

# substage set rejects unknown (nonzero) and does not change current substage
fresh_state
"$MODE_BIN" set planning >/dev/null 2>&1
"$MODE_BIN" substage set bogus >/dev/null 2>&1
assert_nonzero "substage set bogus exits nonzero" "$?"
assert_eq "substage unchanged after rejected set" "framing" "$("$MODE_BIN" substage get 2>/dev/null)"

# set planning re-inits substage to framing even after advancing
fresh_state
"$MODE_BIN" set planning >/dev/null 2>&1
"$MODE_BIN" substage set decomposition >/dev/null 2>&1
"$MODE_BIN" set planning >/dev/null 2>&1
assert_eq "set planning resets substage to framing" "framing" "$("$MODE_BIN" substage get 2>/dev/null)"

# execution mode has no substage — substage get exits nonzero
fresh_state
"$MODE_BIN" set execution >/dev/null 2>&1
"$MODE_BIN" substage get >/dev/null 2>&1
assert_nonzero "execution has no substage (get nonzero)" "$?"

# substage get with no state set exits nonzero
fresh_state
"$MODE_BIN" substage get >/dev/null 2>&1
assert_nonzero "substage get with no state exits nonzero" "$?"

# mode + fleet preserved across a substage advance
fresh_state
"$MODE_BIN" set planning --fleet sherlock,gaudi >/dev/null 2>&1
"$MODE_BIN" substage advance >/dev/null 2>&1
SHOW="$("$MODE_BIN" show 2>/dev/null)"
assert_eq "mode preserved after advance"  "planning" "$(printf '%s' "$SHOW" | jget "['mode']")"
assert_eq "fleet preserved after advance" "sherlock" "$(printf '%s' "$SHOW" | jget "['fleet'][0]")"

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
