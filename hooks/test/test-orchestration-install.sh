#!/usr/bin/env bash
# test-cockpit-install.sh — tests bin/sable-orchestration-install (SABLE-cav.7).
#
# Verifies: project-default scope (./.claude) + --user scope (~/.claude),
# idempotent + non-clobbering settings merge, --uninstall, and the
# missing-multi-manager-base warning. Uses SABLE_PROJECT_DIR / CLAUDE_USER_DIR
# to redirect both scopes at temp dirs so nothing touches the real config.
#
# Run with:
#   bash hooks/test/test-cockpit-install.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALLER="$REPO/bin/sable-orchestration-install"

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
exists(){ if [ -e "$1" ]; then pass "$2"; else fail "$2" "missing: $1"; fi; }

if [ ! -x "$INSTALLER" ]; then echo "FAIL: installer not executable at $INSTALLER"; exit 2; fi

count_interlock(){ python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
print(sum(1 for b in d.get('hooks',{}).get('PreToolUse',[]) if isinstance(b,dict)
          for h in b.get('hooks',[]) if 'mode-interlock.sh' in h.get('command','')))" "$1" 2>/dev/null || echo ERR; }
count_in_event(){ python3 -c "
import json,sys
d=json.load(open(sys.argv[1])); ev=sys.argv[2]; m=sys.argv[3]
print(sum(1 for b in d.get('hooks',{}).get(ev,[]) if isinstance(b,dict) for h in b.get('hooks',[]) if m in h.get('command','')))" "$1" "$2" "$3" 2>/dev/null || echo ERR; }
count_marker(){ python3 -c "
import json,sys
d=json.load(open(sys.argv[1])); m=sys.argv[2]
print(sum(1 for blocks in d.get('hooks',{}).values() if isinstance(blocks,list) for b in blocks if isinstance(b,dict) for h in b.get('hooks',[]) if m in h.get('command','')))" "$1" "$2" 2>/dev/null || echo ERR; }
valid_json(){ python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$1" 2>/dev/null; }

# ---------- project scope (explicit) ----------
P="$(mktemp -d)"
out1="$(SABLE_PROJECT_DIR="$P" bash "$INSTALLER" --project 2>&1)"
exists "$P/.claude/skills/sable-plan/SKILL.md"    "project: /plan skill installed"
exists "$P/.claude/skills/sable-execute/SKILL.md" "project: /execute skill installed"
exists "$P/.claude/sable/roles/lincoln.md"  "project: lincoln role installed"
exists "$P/.claude/sable/roles/optimus.md"  "project: optimus pane role installed (tmux-native)"
exists "$P/.claude/sable/roles/tarzan.md"   "project: tarzan pane role installed (tmux-native)"
exists "$P/.claude/sable/roles/chuck.md"    "project: chuck pane role installed (tmux-native)"
if [ ! -e "$P/.claude/agents-teams" ]; then pass "project: agents-teams defs NOT installed (tmux-only)"; else fail "project: agents-teams defs NOT installed (tmux-only)" "unexpected $P/.claude/agents-teams/"; fi
if printf '%s' "$out1" | grep -q "sable-tmux"; then pass "project: install output points at the sable-tmux bring-up"; else fail "project: install output points at the sable-tmux bring-up" "no sable-tmux mention"; fi
if [ -x "$P/.claude/hooks/multi-manager/mode-interlock.sh" ]; then pass "project: interlock hook installed+exec"; else fail "project: interlock hook installed+exec"; fi
SET="$P/.claude/settings.local.json"
exists "$SET" "project: settings.local.json created"
if valid_json "$SET"; then pass "project: settings is valid JSON"; else fail "project: settings is valid JSON"; fi
if [ "$(count_interlock "$SET")" = "2" ]; then pass "project: interlock registered on both legs (Bash+Agent)"; else fail "project: interlock registered on both legs (Bash+Agent)" "count=$(count_interlock "$SET")"; fi
exists "$P/.claude/sable/agents.yaml" "project: registry (agents.yaml) installed"
if [ -x "$P/.claude/hooks/multi-manager/session-role-anchor.sh" ]; then pass "project: identity hook installed+exec"; else fail "project: identity hook installed+exec"; fi
if [ "$(count_in_event "$SET" SessionStart session-role-anchor.sh)" = "1" ]; then pass "project: identity hook registered SessionStart"; else fail "project: identity hook registered SessionStart" "count=$(count_in_event "$SET" SessionStart session-role-anchor.sh)"; fi
if [ "$(count_in_event "$SET" PreCompact session-role-anchor.sh)" = "1" ]; then pass "project: identity hook registered PreCompact"; else fail "project: identity hook registered PreCompact"; fi

# idempotent re-run
SABLE_PROJECT_DIR="$P" bash "$INSTALLER" --project >/dev/null 2>&1
if [ "$(count_interlock "$SET")" = "2" ]; then pass "project: re-run stays idempotent"; else fail "project: re-run idempotent" "count=$(count_interlock "$SET")"; fi
if valid_json "$SET"; then pass "project: settings valid after re-run"; else fail "project: settings valid after re-run"; fi

# preserves an existing unrelated hook
python3 - "$SET" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
d['hooks']['PreToolUse'].append({'matcher':'Bash','hooks':[{'type':'command','command':'bash /tmp/other-hook.sh','timeout':1000}]})
open(sys.argv[1],'w').write(json.dumps(d,indent=2))
PY
SABLE_PROJECT_DIR="$P" bash "$INSTALLER" --project >/dev/null 2>&1
if grep -q 'other-hook.sh' "$SET"; then pass "project: preserves existing hooks"; else fail "project: preserves existing hooks"; fi

# ---------- default scope is project; there is NO topology choice ----------
P2="$(mktemp -d)"
SABLE_PROJECT_DIR="$P2" bash "$INSTALLER" >/dev/null 2>&1
exists "$P2/.claude/skills/sable-plan/SKILL.md" "default (no flag) installs into project ./.claude"
if [ ! -e "$P2/.claude/agents-teams" ]; then pass "default install: no agents-teams defs (tmux-only)"; else fail "default install: no agents-teams defs (tmux-only)" "unexpected agents-teams/"; fi
SET2="$P2/.claude/settings.local.json"
if [ "$(count_marker "$SET2" pre-push-rebase-test)" -ge 1 ]; then pass "default install: governance hooks in settings"; else fail "default install: governance hooks in settings" "count=$(count_marker "$SET2" pre-push-rebase-test)"; fi

# retired topology flags are rejected with a clear error (tmux is the only topology)
for _flag in --teams --subagent --nested; do
  PN="$(mktemp -d)"
  if SABLE_PROJECT_DIR="$PN" bash "$INSTALLER" "$_flag" >/dev/null 2>&1; then
    fail "retired topology flag $_flag is rejected" "installer exited 0"
  else
    pass "retired topology flag $_flag is rejected"
  fi
  rm -rf "$PN"
done
rm -rf "$P2"

# ---------- user scope ----------
U="$(mktemp -d)"
CLAUDE_USER_DIR="$U/.claude" bash "$INSTALLER" --user >/dev/null 2>&1
exists "$U/.claude/skills/sable-execute/SKILL.md" "user: skill installed under ~/.claude"
exists "$U/.claude/settings.json" "user: settings.json created"
if [ "$(count_interlock "$U/.claude/settings.json")" = "2" ]; then pass "user: interlock registered on both legs"; else fail "user: interlock registered on both legs" "count=$(count_interlock "$U/.claude/settings.json")"; fi

# ---------- uninstall (project) ----------
# seed a legacy agents-teams dir from a pre-tmux-only install: uninstall must still clean it
mkdir -p "$P/.claude/agents-teams"; touch "$P/.claude/agents-teams/chuck.md"
SABLE_PROJECT_DIR="$P" bash "$INSTALLER" --project --uninstall >/dev/null 2>&1
if [ ! -e "$P/.claude/skills/sable-plan/SKILL.md" ]; then pass "uninstall removes skills"; else fail "uninstall removes skills"; fi
if [ ! -e "$P/.claude/sable/agents.yaml" ]; then pass "uninstall removes registry"; else fail "uninstall removes registry"; fi
if [ ! -e "$P/.claude/sable/roles/optimus.md" ] && [ ! -e "$P/.claude/sable/roles/chuck.md" ]; then pass "uninstall removes tmux-native pane roles"; else fail "uninstall removes tmux-native pane roles"; fi
if [ ! -e "$P/.claude/agents-teams" ]; then pass "uninstall cleans up legacy agents-teams defs"; else fail "uninstall cleans up legacy agents-teams defs"; fi
if [ "$(count_interlock "$SET")" = "0" ]; then pass "uninstall de-registers interlock"; else fail "uninstall de-registers interlock" "count=$(count_interlock "$SET")"; fi
if [ "$(count_marker "$SET" session-role-anchor.sh)" = "0" ]; then pass "uninstall de-registers identity hook"; else fail "uninstall de-registers identity hook" "count=$(count_marker "$SET" session-role-anchor.sh)"; fi
if grep -q 'other-hook.sh' "$SET"; then pass "uninstall keeps unrelated hooks"; else fail "uninstall keeps unrelated hooks"; fi

# ---------- SABLE-md7: idempotent across sibling same-matcher blocks ----------
# Repro: an event already has TWO matcher='' blocks — one for `bd prime`, one for
# session-role-anchor. The installer must detect the existing registration in the
# sibling block and NOT add a second one. (Buggy add_hooks only checked the first
# same-matcher block, so it double-registered session-role-anchor.)

seed_sibling_blocks(){ python3 - "$1" <<'PY'
import json, sys
seed = {
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash", "hooks": [
        {"type": "command", "command": "bash ~/.claude/hooks/tdd-gate.sh", "timeout": 5000}]}
    ],
    "SessionStart": [
      {"matcher": "", "hooks": [
        {"type": "command", "command": "bd prime 2>/dev/null || true"}]},
      {"matcher": "", "hooks": [
        {"type": "command", "command": "bash ~/.claude/hooks/multi-manager/session-role-anchor.sh", "timeout": 3000}]}
    ],
    "PreCompact": [
      {"matcher": "", "hooks": [
        {"type": "command", "command": "bd prime 2>/dev/null || true"}]},
      {"matcher": "", "hooks": [
        {"type": "command", "command": "bash ~/.claude/hooks/multi-manager/session-role-anchor.sh", "timeout": 3000}]}
    ]
  }
}
open(sys.argv[1], 'w').write(json.dumps(seed, indent=2))
PY
}

# Unit: add_hooks stays idempotent across sibling same-matcher blocks (SessionStart only,
# run the real installer twice — count must remain 1).
PU="$(mktemp -d)"; mkdir -p "$PU/.claude"
seed_sibling_blocks "$PU/.claude/settings.json"
CLAUDE_USER_DIR="$PU/.claude" bash "$INSTALLER" --user >/dev/null 2>&1
CLAUDE_USER_DIR="$PU/.claude" bash "$INSTALLER" --user >/dev/null 2>&1
PUSET="$PU/.claude/settings.json"
if [ "$(count_in_event "$PUSET" SessionStart session-role-anchor.sh)" = "1" ]; then pass "md7: sibling-block dedup keeps identity hook once (SessionStart)"; else fail "md7: sibling-block dedup keeps identity hook once (SessionStart)" "count=$(count_in_event "$PUSET" SessionStart session-role-anchor.sh)"; fi
if valid_json "$PUSET"; then pass "md7: settings valid after sibling-block dedup"; else fail "md7: settings valid after sibling-block dedup"; fi

# Integration: real `sable-orchestration-install --user` over a realistic pre-seeded
# multi-manager user scope leaves exactly one identity registration per event and
# one interlock, and preserves the bd prime entries.
M="$(mktemp -d)"; mkdir -p "$M/.claude"
seed_sibling_blocks "$M/.claude/settings.json"
CLAUDE_USER_DIR="$M/.claude" bash "$INSTALLER" --user >/dev/null 2>&1
MSET="$M/.claude/settings.json"
if [ "$(count_in_event "$MSET" SessionStart session-role-anchor.sh)" = "1" ]; then pass "md7: multi-manager re-install — identity once (SessionStart)"; else fail "md7: multi-manager re-install — identity once (SessionStart)" "count=$(count_in_event "$MSET" SessionStart session-role-anchor.sh)"; fi
if [ "$(count_in_event "$MSET" PreCompact session-role-anchor.sh)" = "1" ]; then pass "md7: multi-manager re-install — identity once (PreCompact)"; else fail "md7: multi-manager re-install — identity once (PreCompact)" "count=$(count_in_event "$MSET" PreCompact session-role-anchor.sh)"; fi
if [ "$(count_interlock "$MSET")" = "2" ]; then pass "md7: multi-manager re-install — interlock on both legs"; else fail "md7: multi-manager re-install — interlock on both legs" "count=$(count_interlock "$MSET")"; fi
if [ "$(count_marker "$MSET" 'bd prime')" = "2" ]; then pass "md7: multi-manager re-install — bd prime preserved"; else fail "md7: multi-manager re-install — bd prime preserved" "count=$(count_marker "$MSET" 'bd prime')"; fi
if valid_json "$MSET"; then pass "md7: settings valid after multi-manager re-install"; else fail "md7: settings valid after multi-manager re-install"; fi

# ---------- SABLE-qa4d.2: no teams residue in a fresh install ----------
TMONLY="$(mktemp -d)"
TM_OUT="$(SABLE_PROJECT_DIR="$TMONLY" bash "$INSTALLER" --project 2>&1)"
if printf '%s' "$TM_OUT" | grep -q "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"; then fail "install output has no experimental-teams-flag instruction" "flag instruction still printed"; else pass "install output has no experimental-teams-flag instruction"; fi
if printf '%s' "$TM_OUT" | grep -qi "topology"; then fail "install output does not speak of topologies" "topology wording still printed"; else pass "install output does not speak of topologies"; fi
rm -rf "$TMONLY"

rm -rf "$P" "$P2" "$U" "$PU" "$M"
echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
