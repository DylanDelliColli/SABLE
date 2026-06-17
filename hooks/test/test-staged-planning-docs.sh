#!/usr/bin/env bash
# test-staged-planning-docs.sh — Docs-consistency guard for the staged-planning
# model (SABLE-ni8.7). The five planning substages must be documented coherently
# across the three prose docs that describe the methodology and the cockpit, so a
# fresh reader of any one of them learns the gated flow.
#
# Run with:
#   bash hooks/test/test-staged-planning-docs.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

DOCS="SABLE.md ENTRY-POINTS-DESIGN.md MULTI-MANAGER-PATTERN.md"
STAGES="framing research architecture test-strategy decomposition"

for doc in $DOCS; do
  path="$REPO/$doc"
  if [ ! -f "$path" ]; then fail "$doc exists" "missing: $path"; continue; fi
  pass "$doc exists"
  for s in $STAGES; do
    if grep -qi -- "$s" "$path" 2>/dev/null; then
      pass "$doc names substage: $s"
    else
      fail "$doc names substage: $s" "pattern not found: $s"
    fi
  done
  # the five substages appear as an ordered chain somewhere in the doc (robust to
  # unrelated earlier mentions of a word like "architecture" in a large doc)
  order_ok="$(DOC="$path" python3 -c "
import os, re
text = open(os.environ['DOC']).read().lower()
chain = r'framing.*research.*architecture.*test-strategy.*decomposition'
print('ok' if re.search(chain, text, re.DOTALL) else 'no')
" 2>/dev/null)"
  if [ "$order_ok" = "ok" ]; then pass "$doc presents substages as an ordered chain"; else fail "$doc presents substages as an ordered chain" "got '$order_ok'"; fi
done

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
