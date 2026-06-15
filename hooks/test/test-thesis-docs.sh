#!/usr/bin/env bash
# test-thesis-docs.sh — lock the one-window nested-manager evolution into the
# methodology thesis docs (SABLE-uz9.17). SABLE.md §6 and its public mirror
# README.md §6 must document that the v2 orchestrator can be a resident manager
# subagent that nest-dispatches its own workers, and must link to the pattern doc.
#
# Run with: bash hooks/test/test-thesis-docs.sh

set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"

PASS=0; FAIL=0; FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
has() { if grep -qiF -- "$3" "$REPO/$2" 2>/dev/null; then pass "$1"; else fail "$1" "$2 missing: $3"; fi; }

for doc in SABLE.md README.md; do
  has "$doc documents the one-window evolution"          "$doc" "one-window evolution"
  has "$doc names the resident-manager nesting model"    "$doc" "nest-dispatch"
  has "$doc frames the multi-tier operator->managers->workers" "$doc" "operator → resident managers → per-lane workers"
  has "$doc links to the multi-manager pattern doc"      "$doc" "MULTI-MANAGER-PATTERN.md"
done

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
