#!/usr/bin/env bash
# test-quickstart-cockpit.sh — SABLE-uz9.18. QUICKSTART.md must document the
# cockpit (multi-manager) install path and the one-window workflow so a new
# adopter can climb from Foundation to swarm.
#
# Run with: bash hooks/test/test-quickstart-cockpit.sh

set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DOC="$REPO/QUICKSTART.md"

PASS=0; FAIL=0; FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
has() { if grep -qiF -- "$2" "$DOC" 2>/dev/null; then pass "$1"; else fail "$1" "missing: $2"; fi; }

[ -f "$DOC" ] || { echo "FAIL: $DOC missing"; exit 2; }

has "documents the --orchestration install flag"        "install.sh --orchestration"
has "mentions the SABLE_ORCHESTRATION env toggle"  "SABLE_ORCHESTRATION=1"
has "documents the --dry-run flag"                 "--dry-run"
has "has a Climbing to orchestration section"      "Climbing to orchestration"
has "names the multi-manager hooks install dir"    ".claude/hooks/multi-manager"
has "names the agents.yaml registry install"       ".claude/sable/agents.yaml"
has "names the skills (slash command) install"      ".claude/skills"
has "points at sable-mode for orchestration verify" "sable-mode get"
has "frames /sable-plan and /sable-execute"        "/sable-execute"
has "links to the pattern doc for depth"           "MULTI-MANAGER-PATTERN.md"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
