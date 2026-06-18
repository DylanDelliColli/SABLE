#!/usr/bin/env bash
# test-teams-settings-snippet.sh — SABLE-amj.5 / SABLE-c2j.1
# The teams hook wiring (settings-snippet-teams.json) must register the shared
# guard hooks AND the governance hooks (read-guard, model-check, inbox-injection).
# Governance parity with the nested topology is required (SABLE-c2j decision 1):
# inbox-injection is a harmless no-op under SendMessage; read-guard and
# model-check are transport-agnostic safety guards that teams needs equally.
#
# Run with:
#   bash hooks/test/test-teams-settings-snippet.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SNIPPET="$REPO/templates/multi-manager/settings-snippet-teams.json"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

if [ ! -f "$SNIPPET" ]; then
  fail "teams settings snippet exists" "missing: $SNIPPET"
  echo "Tests: 1 | Passed: 0 | Failed: 1"; exit 1
fi
pass "teams settings snippet exists"

# Valid JSON
if python3 -c "import json; json.load(open('$SNIPPET'))" 2>/dev/null; then
  pass "teams snippet is valid JSON"
else
  fail "teams snippet is valid JSON" "python json.load failed"
fi

present() { if grep -q -- "$1" "$SNIPPET"; then pass "teams snippet registers $1"; else fail "teams snippet registers $1" "not found"; fi; }
absent()  { if grep -q -- "$1" "$SNIPPET"; then fail "teams snippet OMITS $1" "unexpectedly present"; else pass "teams snippet omits $1"; fi; }

# Shared guard hooks must be present (transport-agnostic)
for h in \
  mode-interlock.sh \
  tree-claim.sh \
  pre-push-rebase-test.sh \
  pre-dispatch-preempt.sh \
  pre-dispatch-refresh.sh \
  pre-dispatch-claim.sh \
  pre-dispatch-overlap.sh \
  edit-write-claim-reconciler.sh \
  post-push-merge-notify.sh \
  session-role-anchor.sh; do
  present "$h"
done

# Governance hooks must be present (parity with nested topology — SABLE-c2j.1)
present "inbox-injection.sh"
present "inbox-injection-precompact.sh"
present "read-guard.sh"
present "pre-dispatch-model-check.sh"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
