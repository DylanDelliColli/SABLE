#!/usr/bin/env bash
# test-cockpit-layout.sh — Verifies the Zellij layout (sable.kdl) and the
# sable-cockpit launch helper (SABLE-cav.4).
#
# zellij is not assumed to be installed, so we do NOT shell out to its real KDL
# parser. Instead we structurally validate the layout (both pane commands
# present, balanced braces, layout/pane keywords) and exercise the helper's
# path resolution + its no-zellij fallback path directly.
#
# Run with:
#   bash hooks/test/test-cockpit-layout.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
LAYOUT="$REPO/templates/multi-manager/layouts/sable.kdl"
HELPER="$REPO/bin/sable-cockpit"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
assert_file() { if [ -f "$1" ]; then pass "$2"; else fail "$2" "missing: $1"; fi; }
assert_exec() { if [ -x "$1" ]; then pass "$2"; else fail "$2" "not executable: $1"; fi; }
assert_grep() { if grep -qi -- "$2" "$1" 2>/dev/null; then pass "$3"; else fail "$3" "pattern not found: $2"; fi; }

# ---------- layout file ----------
assert_file "$LAYOUT" "sable.kdl exists"
assert_grep "$LAYOUT" "layout"                     "layout declares a layout block"
assert_grep "$LAYOUT" "pane"                        "layout has panes"
assert_grep "$LAYOUT" "CLAUDE_AGENT_NAME=cockpit"   "left pane launches the cockpit identity"
assert_grep "$LAYOUT" "claude"                      "left pane runs claude"
assert_grep "$LAYOUT" "sable-status"                "right pane runs the dashboard"

# balanced braces (lightweight KDL sanity without zellij)
if [ -f "$LAYOUT" ]; then
  if python3 -c "
import sys
t = open('$LAYOUT').read()
o, c = t.count('{'), t.count('}')
sys.exit(0 if (o == c and o > 0) else 1)
"; then pass "layout braces balanced"; else fail "layout braces balanced"; fi
fi

# ---------- launch helper ----------
assert_file "$HELPER" "sable-cockpit helper exists"
assert_exec "$HELPER" "sable-cockpit is executable"
assert_grep "$HELPER" "zellij" "helper references zellij"

# --help works
if "$HELPER" --help >/dev/null 2>&1; then pass "helper --help exits 0"; else fail "helper --help exits 0"; fi

# --print-layout resolves an existing layout file (default repo fallback)
resolved="$("$HELPER" --print-layout 2>/dev/null)"
if [ -n "$resolved" ] && [ -f "$resolved" ]; then pass "helper resolves an existing layout path"; else fail "helper resolves an existing layout path" "got '$resolved'"; fi

# --print-layout honors the override
resolved_override="$(SABLE_COCKPIT_LAYOUT="$LAYOUT" "$HELPER" --print-layout 2>/dev/null)"
if [ "$resolved_override" = "$LAYOUT" ]; then pass "helper honors SABLE_COCKPIT_LAYOUT override"; else fail "helper honors SABLE_COCKPIT_LAYOUT override" "got '$resolved_override'"; fi

# no-zellij fallback: zellij is absent here, so a bare launch must explain the
# manual two-pane workaround and exit nonzero (not crash).
out="$("$HELPER" 2>&1)"; rc=$?
if [ "$rc" -ne 0 ]; then pass "bare launch without zellij exits nonzero"; else fail "bare launch without zellij exits nonzero"; fi
printf '%s' "$out" | grep -qi 'zellij' && printf '%s' "$out" | grep -qi 'sable-status' \
  && pass "no-zellij message gives the manual fallback" \
  || fail "no-zellij message gives the manual fallback" "got: $out"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
