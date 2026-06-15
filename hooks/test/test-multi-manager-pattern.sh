#!/usr/bin/env bash
# test-multi-manager-pattern.sh — doc-consistency lock for MULTI-MANAGER-PATTERN.md
# after the native-spawn conversion (SABLE-uz9.14). The canonical pattern doc
# must describe managers spawning their own workers + pushing their own lanes,
# and must NOT instruct the old DISPATCH-REQUEST coord-bead relay through Lincoln.
# Still-accurate identity/coordination sections are asserted to survive.
#
# Run with:
#   bash hooks/test/test-multi-manager-pattern.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DOC="$REPO/MULTI-MANAGER-PATTERN.md"

PASS=0; FAIL=0; FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
has()    { if grep -qiF -- "$2" "$DOC" 2>/dev/null; then pass "$1"; else fail "$1" "missing: $2"; fi; }
hasno()  { if grep -qF  -- "$2" "$DOC" 2>/dev/null; then fail "$1" "unexpectedly present: $2"; else pass "$1"; fi; }

[ -f "$DOC" ] || { echo "FAIL: $DOC missing"; exit 2; }

# Old relay removed.
hasno "no DISPATCH-REQUEST relay language"          "DISPATCH-REQUEST"
hasno "Lincoln no longer receives dispatch messages" "receiving DISPATCH-REQUEST messages from Lincoln"

# Native-spawn topology documented.
has "managers dispatch their own workers"            "dispatches its own workers"
has "managers push their own approved lanes"         "approved lanes"
has "doc names the manager push command"             "git -C <worktree> push"
has "managers self-dispatch + self-push (table)"     "self-dispatch workers and self-push"

# Still-accurate sections preserved.
has "keeps the subagent-context identity discrimination" "Subagent context discrimination"
has "keeps agent_type identity resolution"               "agent_type"
has "keeps the read guard section"                       "Read guard"
has "keeps the pre-push three-phase gate"                "three-phase gate"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
