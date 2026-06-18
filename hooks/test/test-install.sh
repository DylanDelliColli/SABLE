#!/usr/bin/env bash
# test-install.sh — front-door integration for install.sh (SABLE-ppy / iw0).
# Verifies install.sh delegates the Orchestration tier to sable-orchestration-install,
# branches on topology (subagent vs teams), auto-merges the topology settings snippet
# idempotently + non-clobbering, prints (not writes) the teams experimental flag,
# skips the layer for Foundation, and leaves runnable installed hook copies.
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

# ---------- delegation: orchestration --subagent lands the layer ----------
TS="$(mktemp -d)"
HOME="$TS" bash "$INSTALL" --orchestration --subagent >/tmp/ti-sub.log 2>&1
SS="$TS/.claude/settings.json"
present "$TS/.claude/hooks/multi-manager/mode-interlock.sh" "subagent: delegate installed multi-manager hooks"
present "$TS/.claude/sable/agents.yaml"                     "subagent: registry installed"
present "$TS/.claude/skills/sable-plan/SKILL.md"           "subagent: /sable-plan skill installed"
present "$TS/.claude/agents-teams/chuck.md"                 "subagent: agents-teams defs always installed"
[ "$(count_marker "$SS" mode-interlock.sh)" = "2" ] && pass "subagent: interlock merged on both legs" || fail "subagent: interlock merged on both legs" "count=$(count_marker "$SS" mode-interlock.sh)"
[ "$(count_marker "$SS" inbox-injection)" -ge 1 ] && pass "subagent: poll hooks present in settings" || fail "subagent: poll hooks present" "count=$(count_marker "$SS" inbox-injection)"

# installed hook copy is runnable (no-op on benign input → exit 0)
printf '%s' '{"tool_name":"Bash","tool_input":{"command":"echo hi"}}' | bash "$TS/.claude/hooks/multi-manager/mode-interlock.sh" >/dev/null 2>&1
[ "$?" = "0" ] && pass "subagent: installed interlock copy runs" || fail "subagent: installed interlock copy runs"

# --nested is an alias for --subagent
TN2="$(mktemp -d)"
HOME="$TN2" bash "$INSTALL" --orchestration --nested >/dev/null 2>&1
present "$TN2/.claude/hooks/multi-manager/mode-interlock.sh" "--nested: hooks installed"
present "$TN2/.claude/agents-teams/chuck.md"                  "--nested: agents-teams defs installed"
rm -rf "$TN2"

# idempotent re-run: interlock count stable
HOME="$TS" bash "$INSTALL" --orchestration --subagent >/dev/null 2>&1
[ "$(count_marker "$SS" mode-interlock.sh)" = "2" ] && pass "re-run idempotent (interlock still 2)" || fail "re-run idempotent" "count=$(count_marker "$SS" mode-interlock.sh)"

# non-clobber: a pre-existing user hook survives the merge
TN="$(mktemp -d)"; mkdir -p "$TN/.claude"
printf '%s\n' '{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"bash /tmp/user-own.sh"}]}]}}' > "$TN/.claude/settings.json"
HOME="$TN" bash "$INSTALL" --orchestration --subagent >/dev/null 2>&1
grep -q 'user-own.sh' "$TN/.claude/settings.json" && pass "non-clobber: pre-existing user hook survives" || fail "non-clobber: pre-existing user hook survives"

# ---------- topology: teams adds member defs, governance hooks present, PRINTS the flag ----------
TT="$(mktemp -d)"
HOME="$TT" bash "$INSTALL" --orchestration --teams >/tmp/ti-teams.log 2>&1
TSET="$TT/.claude/settings.json"
present "$TT/.claude/agents-teams/optimus.md" "teams: member defs installed"
[ "$(count_marker "$TSET" inbox-injection)" -ge 1 ] && pass "teams: governance hooks present in settings" || fail "teams: governance hooks present" "count=$(count_marker "$TSET" inbox-injection)"
[ "$(count_marker "$TSET" mode-interlock.sh)" = "2" ] && pass "teams: interlock merged on both legs" || fail "teams: interlock merged" "count=$(count_marker "$TSET" mode-interlock.sh)"
grep -q 'CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS' /tmp/ti-teams.log && pass "teams: experimental flag PRINTED" || fail "teams: experimental flag printed"
! grep -q 'CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS' "$TSET" && pass "teams: flag NOT auto-written to settings" || fail "teams: flag not auto-written"

# ---------- default topology is teams ----------
TD="$(mktemp -d)"
HOME="$TD" bash "$INSTALL" --orchestration >/tmp/ti-default.log 2>&1
TDSET="$TD/.claude/settings.json"
present "$TD/.claude/agents-teams/chuck.md"                           "default topology: agents-teams defs installed"
[ "$(count_marker "$TDSET" inbox-injection)" -ge 1 ] && pass "default topology: governance hooks in settings" || fail "default topology: governance hooks in settings" "count=$(count_marker "$TDSET" inbox-injection)"
grep -q 'CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS' /tmp/ti-default.log && pass "default topology: teams flag PRINTED" || fail "default topology: teams flag PRINTED"
rm -rf "$TD"

# ---------- tier gate: Foundation skips the layer ----------
TF="$(mktemp -d)"
HOME="$TF" bash "$INSTALL" --foundation >/dev/null 2>&1
absent  "$TF/.claude/hooks/multi-manager" "foundation: no orchestration layer"
present "$TF/.claude/agents/optimus.md"   "foundation: still installs base agent defs"

rm -rf "$TS" "$TN" "$TT" "$TF"
echo
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
if [ "$FAIL" -gt 0 ]; then echo -e "Failed:$NAMES"; exit 1; fi
exit 0
