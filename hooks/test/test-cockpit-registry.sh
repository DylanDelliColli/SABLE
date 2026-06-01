#!/usr/bin/env bash
# test-cockpit-registry.sh — Verifies the cockpit agent is registered in
# templates/multi-manager/agents.yaml and that bin/sable-agents parses the
# registry and surfaces it (the SABLE-cav.1 acceptance: "agents.yaml still
# parses and bin/sable-agents prints the cockpit agent").
#
# Run with:
#   bash hooks/test/test-cockpit-registry.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
REGISTRY="$REPO/templates/multi-manager/agents.yaml"
AGENTS_BIN="$REPO/bin/sable-agents"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

# sable-agents --json emits the parsed registry; if the YAML failed to parse
# into the cockpit entry this is empty/absent.
OUT="$(python3 "$AGENTS_BIN" --json --registry "$REGISTRY" 2>/dev/null)"

if printf '%s' "$OUT" | jq -e '.cockpit' >/dev/null 2>&1; then
  pass "cockpit present in registry"
else
  fail "cockpit present in registry" "sable-agents --json has no .cockpit key"
fi

assert_field() {
  # name jq-path expected
  local got
  got="$(printf '%s' "$OUT" | jq -r "$2" 2>/dev/null)"
  if [ "$got" = "$3" ]; then pass "$1"; else fail "$1" "expected '$3', got '$got'"; fi
}

assert_field "cockpit type is cockpit"        '.cockpit.type'             "cockpit"
assert_field "cockpit cross_inbox_read true"   '.cockpit.cross_inbox_read' "true"
assert_field "cockpit role_prompt path"        '.cockpit.role_prompt'      "roles/cockpit.md"

# The existing roster must still be intact (no accidental clobber).
for a in optimus tarzan chuck lincoln sherlock victor rudy columbo; do
  if printf '%s' "$OUT" | jq -e ".$a" >/dev/null 2>&1; then
    pass "existing agent $a still present"
  else
    fail "existing agent $a still present"
  fi
done

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
