#!/usr/bin/env bash
# test-thesis-docs.sh — lock the warm-pane evolution into the methodology
# thesis docs (SABLE-uz9.17; tmux-only SABLE-qa4d). SABLE.md §6 and its public
# mirror README.md §6 must document that orchestrated execution runs as warm
# tmux panes — operator → manager panes → per-bead worker panes — and must
# link to the pattern doc.
#
# Run with: bash hooks/test/test-thesis-docs.sh

set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"

PASS=0; FAIL=0; FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
has() { if grep -qiF -- "$3" "$REPO/$2" 2>/dev/null; then pass "$1"; else fail "$1" "$2 missing: $3"; fi; }
hasno() { if grep -qF -- "$3" "$REPO/$2" 2>/dev/null; then fail "$1" "$2 unexpectedly has: $3"; else pass "$1"; fi; }

for doc in SABLE.md README.md; do
  has "$doc documents the warm-pane evolution"           "$doc" "warm-pane evolution"
  has "$doc names the pane launcher"                     "$doc" "sable-tmux"
  has "$doc frames operator -> manager panes -> workers" "$doc" "operator → manager panes → per-bead worker panes"
  has "$doc links to the multi-manager pattern doc"      "$doc" "MULTI-MANAGER-PATTERN.md"
  hasno "$doc drops the one-window evolution framing"    "$doc" "one-window evolution"
  hasno "$doc drops the nest-dispatch model"             "$doc" "nest-dispatch"
done

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
