#!/usr/bin/env bash
# test-install-multi-manager.sh — SABLE-106. install.sh installs the cockpit
# (multi-manager) tier — hooks/multi-manager/*.sh + agents.yaml — under
# --cockpit (or SABLE_MULTI_MANAGER=1); --dry-run copies nothing; a plain
# Foundation install does not pull in the cockpit tier. Real installs into temp
# HOMEs (no mocking).
#
# Run with: bash hooks/test/test-install-multi-manager.sh

set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL="$REPO/install.sh"

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

command -v bd >/dev/null 2>&1 || { echo "SKIP: bd not on PATH"; exit 0; }

TH=""; TH2=""; TH3=""
cleanup() { rm -rf "$TH" "$TH2" "$TH3" 2>/dev/null || true; }
trap cleanup EXIT
TH="$(mktemp -d)"; TH2="$(mktemp -d)"; TH3="$(mktemp -d)"

# --- INTEGRATION: --cockpit installs the multi-manager tier into a temp HOME ---
HOME="$TH" bash "$INSTALL" --cockpit >/dev/null 2>&1 || true
if [ -f "$TH/.claude/hooks/multi-manager/lib-identity.sh" ] && \
   [ -f "$TH/.claude/hooks/multi-manager/pre-dispatch-refresh.sh" ] && \
   [ -f "$TH/.claude/hooks/multi-manager/cockpit-mode-interlock.sh" ]; then
  pass "--cockpit installs the multi-manager hooks"
else
  fail "--cockpit installs the multi-manager hooks" "missing under $TH/.claude/hooks/multi-manager/"
fi
if [ -f "$TH/.claude/sable/agents.yaml" ]; then pass "--cockpit installs the agents.yaml registry"; else fail "--cockpit installs the agents.yaml registry"; fi
if grep -qE "^tools:.*Agent" "$TH/.claude/agents/optimus.md" 2>/dev/null; then pass "--cockpit ships optimus.md with the Agent tool grant"; else fail "--cockpit ships optimus.md with the Agent tool grant"; fi
# Skills install by frontmatter name (cockpit-plan -> plan, cockpit-execute -> execute).
if [ -f "$TH/.claude/skills/plan/SKILL.md" ] && [ -f "$TH/.claude/skills/execute/SKILL.md" ] && [ -f "$TH/.claude/skills/columbo/SKILL.md" ] && [ -f "$TH/.claude/skills/gaudi/SKILL.md" ]; then
  pass "--cockpit installs SABLE skills by frontmatter name (plan/execute/columbo/gaudi)"
else
  fail "--cockpit installs SABLE skills by frontmatter name (plan/execute/columbo/gaudi)" "missing under $TH/.claude/skills/"
fi
if [ -f "$TH/.claude/skills/gaudi/SMELLS.md" ] && [ -f "$TH/.claude/skills/columbo/columbo-prefilter.py" ]; then
  pass "--cockpit copies skill sibling files (gaudi/SMELLS.md, columbo prefilter)"
else
  fail "--cockpit copies skill sibling files (gaudi/SMELLS.md, columbo prefilter)" "sibling files missing"
fi

# --- DRY-RUN: copies nothing ---
out="$(HOME="$TH2" bash "$INSTALL" --cockpit --dry-run 2>&1 || true)"
if [ ! -e "$TH2/.claude/hooks/multi-manager" ]; then pass "--dry-run copies no multi-manager hooks"; else fail "--dry-run copies no multi-manager hooks" "dir was created"; fi
if [ ! -e "$TH2/.claude/skills/plan" ]; then pass "--dry-run installs no skills"; else fail "--dry-run installs no skills" "skills dir created"; fi
if echo "$out" | grep -qiE "dry.run|would (copy|install)"; then pass "--dry-run output marks it as a dry run"; else fail "--dry-run output marks it as a dry run" "got: $(echo "$out" | head -3)"; fi

# --- FOUNDATION: no --cockpit → no multi-manager tier ---
HOME="$TH3" bash "$INSTALL" >/dev/null 2>&1 || true
if [ ! -e "$TH3/.claude/hooks/multi-manager" ]; then pass "Foundation install (no --cockpit) skips the multi-manager tier"; else fail "Foundation install skips the multi-manager tier" "dir present"; fi
if [ ! -e "$TH3/.claude/skills/plan" ]; then pass "Foundation install installs no SABLE skills"; else fail "Foundation install installs no SABLE skills" "skills present"; fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
