#!/usr/bin/env bash
# test-sherlock-research.sh — Verifies Sherlock's greenfield/RESEARCH mode is
# documented in both the role prompt and the finding-bead template, so the
# cockpit can drive the planning RESEARCH substage (SABLE-ni8.6).
#
# A role/template is prose, so the "test" is presence + wiring assertions:
#   - the role declares the --research invocation and the sherlock:research
#     category, names the RESEARCH substage, and the /deep-research fallback;
#   - the bead template carries the sherlock:research category and the
#     source-citation (greenfield) exception to the fingerprint rule.
#
# Run with:
#   bash hooks/test/test-sherlock-research.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
ROLE="$REPO/templates/multi-manager/roles/sherlock.md"
TEMPLATE="$REPO/templates/sherlock-bead.md"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
assert_file() { if [ -f "$1" ]; then pass "$2"; else fail "$2" "missing: $1"; fi; }
assert_grep() { if grep -qi -- "$2" "$1" 2>/dev/null; then pass "$3"; else fail "$3" "pattern not found: $2"; fi; }

assert_file "$ROLE"     "sherlock role file exists"
assert_file "$TEMPLATE" "sherlock-bead template exists"

# role: greenfield mode wiring
assert_grep "$ROLE" "sherlock --research"   "role declares the --research invocation"
assert_grep "$ROLE" "sherlock:research"     "role lists the sherlock:research category"
assert_grep "$ROLE" "RESEARCH substage"     "role ties research mode to the RESEARCH substage"
assert_grep "$ROLE" "deep-research"         "role names the /deep-research fallback"
assert_grep "$ROLE" "greenfield"            "role frames the mode as greenfield"

# template: research category + the source-citation exception
assert_grep "$TEMPLATE" "sherlock:research" "template lists the sherlock:research category"
assert_grep "$TEMPLATE" "sources"           "template documents source citation for research findings"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
