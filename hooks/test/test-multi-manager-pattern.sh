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

# Warm-pane topology documented (tmux-only, SABLE-qa4d).
has "managers dispatch their own worker panes"       "worker pane"
has "doc names the pane launcher"                    "sable-tmux"
has "doc names the lead<->manager messaging path"    "sable-msg"
has "workers self-push their own branches"           "self-push"
has "doc links the tmux design doc"                  "TMUX-AGENTS-DESIGN.md"
hasno "no manager git -C push lane remains"          "git -C <worktree> push"
hasno "no resident-subagent manager framing remains" "resident manager subagents"
hasno "no teams-topology default remains"            "Teams topology (default"

# Still-accurate sections preserved.
has "keeps the subagent-context identity discrimination" "Subagent context discrimination"
has "keeps agent_type identity resolution"               "agent_type"
has "keeps the read guard section"                       "Read guard"
has "keeps the pre-push three-phase gate"                "three-phase gate"

# Declared-footprint overlap is a hard scheduling constraint (SABLE-y3e1z /
# SABLE-jd5fj.6), not the former annotate-and-proceed advisory. Keep both the
# refusal and its exact operator escape visible in the canonical pattern doc.
hasno "drops stale advisory overlap heading"              "### 3. Overlap awareness (advisory, not blocking)"
hasno "drops stale annotate-instead-of-deny direction"    "the hook **annotates** rather than denies"
hasno "drops stale proceed-and-file-coord remedy"         "Decision: dispatch will proceed; file a coord bead"
has "overlap section names the scheduling constraint"     "### 3. Declared-footprint overlap is a scheduling constraint"
has "overlap section says dispatch is denied"             "the dispatch is **denied**"
has "overlap section gives the wait remedy"               "Wait for every overlapping bead to clear"
has "overlap section gives the explicit serialization remedy" 'Serialize-with: <bead-id>'
has "serialization must cover every overlap"              "Every overlapping bead must be named"
has "accepted serialization tags both beads"              '`serialize_with` metadata on both beads'
has "hook catalog classifies overlap as a hard deny"       "Hard deny (unless every overlap is explicitly serialized)"

# Poll-based inbox-injection hooks are gone from the catalog (SABLE-qa4d.6).
hasno "hook catalog drops inbox-injection"           "inbox-injection.sh"
hasno "hook catalog drops inbox-injection-precompact" "inbox-injection-precompact.sh"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
