#!/usr/bin/env bash
# test-sable-cli.sh — the `sable` umbrella command (SABLE-ssws.4).
#
# `sable --help` is the operator map (session doors, work doors, toolbox, doc
# pointers); `sable <sub> ...` dispatches to the sable-<sub> sibling; `sable
# help <sub>` shows that tool's usage; unknown subcommands exit 2 listing what
# exists.
#
# Run with: bash hooks/test/test-sable-cli.sh

set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SABLE="$REPO/bin/sable"

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

[ -x "$SABLE" ] || { fail "bin/sable exists and is executable"; echo "Tests: 1 | Passed: 0 | Failed: 1"; exit 1; }
pass "bin/sable exists and is executable"

# --- the operator map ---
HELP="$(bash "$SABLE" --help 2>&1)"
for marker in sable-launch sable-view /sable-discover /sable-plan /sable-execute sable-msg QUICKSTART.md TMUX-AGENTS-DESIGN.md; do
  if printf '%s' "$HELP" | grep -qF -- "$marker"; then
    pass "--help mentions $marker"
  else
    fail "--help mentions $marker" "missing from help output"
  fi
done

# bare `sable` and `sable help` print the same map
[ "$(bash "$SABLE" 2>&1)" = "$HELP" ] && pass "bare sable prints the map" || fail "bare sable prints the map"
[ "$(bash "$SABLE" help 2>&1)" = "$HELP" ] && pass "sable help prints the map" || fail "sable help prints the map"

# --- dispatch: sable <sub> execs sable-<sub> ---
via_umbrella="$(bash "$SABLE" mode path 2>/dev/null)"
direct="$(bash "$REPO/bin/sable-mode" path 2>/dev/null)"
if [ -n "$via_umbrella" ] && [ "$via_umbrella" = "$direct" ]; then
  pass "sable mode path dispatches to sable-mode"
else
  fail "sable mode path dispatches to sable-mode" "umbrella=[$via_umbrella] direct=[$direct]"
fi

# --- sable help <sub> shows the tool's usage ---
SUBHELP="$(bash "$SABLE" help msg 2>&1)"
if printf '%s' "$SUBHELP" | grep -qi "usage"; then
  pass "sable help msg prints sable-msg usage"
else
  fail "sable help msg prints sable-msg usage" "got: $(printf '%s' "$SUBHELP" | head -2)"
fi

# --- unknown subcommand: exit 2, lists what exists ---
UNK_ERR="$(bash "$SABLE" frobnicate 2>&1)"; UNK_CODE=$?
if [ "$UNK_CODE" -eq 2 ] && printf '%s' "$UNK_ERR" | grep -q "launch"; then
  pass "unknown subcommand exits 2 and lists available subcommands"
else
  fail "unknown subcommand exits 2 and lists available subcommands" "code=$UNK_CODE out=[$(printf '%s' "$UNK_ERR" | head -2)]"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
