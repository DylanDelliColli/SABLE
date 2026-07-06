#!/usr/bin/env bash
# test-install.sh — front-door integration for install.sh (SABLE-ppy / iw0; tmux-only SABLE-qa4d).
# Verifies install.sh delegates the Orchestration tier to sable-orchestration-install
# (no topology fork — the tmux warm-pane layer is the only one), auto-merges the
# settings snippet idempotently + non-clobbering, rejects the retired topology
# flags, skips the layer for Foundation, and leaves runnable installed hook copies.
# Runs install.sh against scratch HOMEs (real bd/dolt/python on PATH).
set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL="$REPO/install.sh"
PASS=0; FAIL=0; NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); NAMES="$NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
present(){ [ -e "$1" ] && pass "$2" || fail "$2" "missing: $1"; }
absent(){ [ ! -e "$1" ] && pass "$2" || fail "$2" "present: $1"; }
count_marker(){ python3 -c "
import json,sys
d=json.load(open(sys.argv[1])); m=sys.argv[2]
print(sum(1 for bl in d.get('hooks',{}).values() if isinstance(bl,list) for b in bl if isinstance(b,dict) for h in b.get('hooks',[]) if m in (h.get('command','') or '')))" "$1" "$2" 2>/dev/null || echo ERR; }

# ---------- delegation: --orchestration lands the layer (no topology flag) ----------
TS="$(mktemp -d)"
HOME="$TS" bash "$INSTALL" --orchestration >/tmp/ti-orch.log 2>&1
SS="$TS/.claude/settings.json"
present "$TS/.claude/hooks/multi-manager/mode-interlock.sh" "orchestration: delegate installed multi-manager hooks"
present "$TS/.claude/sable/agents.yaml"                     "orchestration: registry installed"
present "$TS/.claude/skills/sable-plan/SKILL.md"            "orchestration: /sable-plan skill installed"
present "$TS/.claude/sable/roles/optimus.md"                "orchestration: pane roles installed"
absent  "$TS/.claude/agents-teams"                          "orchestration: no agents-teams defs (tmux-only)"
[ "$(count_marker "$SS" mode-interlock.sh)" = "2" ] && pass "orchestration: interlock merged on both legs" || fail "orchestration: interlock merged on both legs" "count=$(count_marker "$SS" mode-interlock.sh)"
[ "$(count_marker "$SS" pre-push-rebase-test)" -ge 1 ] && pass "orchestration: governance hooks present in settings" || fail "orchestration: governance hooks present" "count=$(count_marker "$SS" pre-push-rebase-test)"
grep -q 'sable-tmux' /tmp/ti-orch.log && pass "orchestration: output points at the sable-tmux bring-up" || fail "orchestration: output points at the sable-tmux bring-up"
! grep -q 'CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS' /tmp/ti-orch.log && pass "orchestration: no experimental-teams-flag instruction printed" || fail "orchestration: no experimental-teams-flag instruction printed"

# installed hook copy is runnable (no-op on benign input → exit 0)
printf '%s' '{"tool_name":"Bash","tool_input":{"command":"echo hi"}}' | bash "$TS/.claude/hooks/multi-manager/mode-interlock.sh" >/dev/null 2>&1
[ "$?" = "0" ] && pass "orchestration: installed interlock copy runs" || fail "orchestration: installed interlock copy runs"

# idempotent re-run: interlock count stable
HOME="$TS" bash "$INSTALL" --orchestration >/dev/null 2>&1
[ "$(count_marker "$SS" mode-interlock.sh)" = "2" ] && pass "re-run idempotent (interlock still 2)" || fail "re-run idempotent" "count=$(count_marker "$SS" mode-interlock.sh)"

# non-clobber: a pre-existing user hook survives the merge
TN="$(mktemp -d)"; mkdir -p "$TN/.claude"
printf '%s\n' '{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"bash /tmp/user-own.sh"}]}]}}' > "$TN/.claude/settings.json"
HOME="$TN" bash "$INSTALL" --orchestration >/dev/null 2>&1
grep -q 'user-own.sh' "$TN/.claude/settings.json" && pass "non-clobber: pre-existing user hook survives" || fail "non-clobber: pre-existing user hook survives"

# ---------- retired topology flags are rejected (tmux is the only topology) ----------
for _flag in --teams --subagent --nested; do
  TR="$(mktemp -d)"
  if HOME="$TR" bash "$INSTALL" --orchestration "$_flag" >/dev/null 2>&1; then
    fail "retired topology flag $_flag is rejected" "install.sh exited 0"
  else
    pass "retired topology flag $_flag is rejected"
  fi
  absent "$TR/.claude/hooks/multi-manager" "retired flag $_flag installs nothing"
  rm -rf "$TR"
done

# ---------- tier gate: Foundation skips the layer ----------
TF="$(mktemp -d)"
HOME="$TF" bash "$INSTALL" --foundation >/dev/null 2>&1
absent  "$TF/.claude/hooks/multi-manager" "foundation: no orchestration layer"
present "$TF/.claude/agents/sherlock.md"  "foundation: still installs base agent defs"

rm -rf "$TS" "$TN" "$TF"
echo
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
if [ "$FAIL" -gt 0 ]; then echo -e "Failed:$NAMES"; exit 1; fi
exit 0
