#!/usr/bin/env bash
# test-install-multi-manager.sh — SABLE-106. install.sh installs the cockpit
# (multi-manager) tier — hooks/multi-manager/*.sh + agents.yaml — under
# --orchestration (or SABLE_ORCHESTRATION=1); --dry-run copies nothing; a plain
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

# --- INTEGRATION: the plain install lands the multi-manager layer (no tiers) ---
HOME="$TH" bash "$INSTALL" >/dev/null 2>&1 || true
if [ -f "$TH/.claude/hooks/multi-manager/lib-identity.sh" ] && \
   [ -f "$TH/.claude/hooks/multi-manager/pre-dispatch-refresh.sh" ] && \
   [ -f "$TH/.claude/hooks/multi-manager/mode-interlock.sh" ]; then
  pass "--orchestration installs the multi-manager hooks"
else
  fail "--orchestration installs the multi-manager hooks" "missing under $TH/.claude/hooks/multi-manager/"
fi
if [ -f "$TH/.claude/sable/agents.yaml" ]; then pass "--orchestration installs the agents.yaml registry"; else fail "--orchestration installs the agents.yaml registry"; fi
# Managers are warm panes (SABLE-qa4d.5): their role files install under
# sable/roles/, and no manager agent def lands in agents/.
if [ -f "$TH/.claude/sable/roles/optimus.md" ] && [ ! -e "$TH/.claude/agents/optimus.md" ]; then pass "--orchestration ships optimus as a pane role, not an agent def"; else fail "--orchestration ships optimus as a pane role, not an agent def"; fi
# Skills install by frontmatter name (sable-plan -> plan, sable-execute -> execute).
if [ -f "$TH/.claude/skills/sable-plan/SKILL.md" ] && [ -f "$TH/.claude/skills/sable-execute/SKILL.md" ] && [ -f "$TH/.claude/skills/columbo/SKILL.md" ] && [ -f "$TH/.claude/skills/gaudi/SKILL.md" ]; then
  pass "--orchestration installs SABLE skills by frontmatter name (plan/execute/columbo/gaudi)"
else
  fail "--orchestration installs SABLE skills by frontmatter name (plan/execute/columbo/gaudi)" "missing under $TH/.claude/skills/"
fi
if [ -f "$TH/.claude/skills/gaudi/SMELLS.md" ] && [ -f "$TH/.claude/skills/columbo/columbo-prefilter.py" ]; then
  pass "--orchestration copies skill sibling files (gaudi/SMELLS.md, columbo prefilter)"
else
  fail "--orchestration copies skill sibling files (gaudi/SMELLS.md, columbo prefilter)" "sibling files missing"
fi

# --- DRY-RUN: copies nothing ---
out="$(HOME="$TH2" bash "$INSTALL" --dry-run 2>&1 || true)"
if [ ! -e "$TH2/.claude/hooks/multi-manager" ]; then pass "--dry-run copies no multi-manager hooks"; else fail "--dry-run copies no multi-manager hooks" "dir was created"; fi
if [ ! -e "$TH2/.claude/skills/sable-plan" ]; then pass "--dry-run installs no skills"; else fail "--dry-run installs no skills" "skills dir created"; fi
if echo "$out" | grep -qiE "dry.run|would (copy|install)"; then pass "--dry-run output marks it as a dry run"; else fail "--dry-run output marks it as a dry run" "got: $(echo "$out" | head -3)"; fi

# --- RETIRED TIER FLAGS: rejected, install nothing (SABLE-ssws.1) ---
for _flag in --orchestration --foundation; do
  if HOME="$TH3" bash "$INSTALL" "$_flag" >/dev/null 2>&1; then
    fail "retired tier flag $_flag is rejected" "install.sh exited 0"
  else
    pass "retired tier flag $_flag is rejected"
  fi
done
if [ ! -e "$TH3/.claude/hooks/multi-manager" ]; then pass "retired tier flags install nothing"; else fail "retired tier flags install nothing" "dir present"; fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
