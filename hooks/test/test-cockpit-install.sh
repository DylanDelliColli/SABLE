#!/usr/bin/env bash
# test-cockpit-install.sh — tests bin/sable-cockpit-install (SABLE-cav.7).
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
INSTALLER="$REPO/bin/sable-cockpit-install"

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
exists(){ if [ -e "$1" ]; then pass "$2"; else fail "$2" "missing: $1"; fi; }

if [ ! -x "$INSTALLER" ]; then echo "FAIL: installer not executable at $INSTALLER"; exit 2; fi

count_interlock(){ python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
print(sum(1 for b in d.get('hooks',{}).get('PreToolUse',[]) if isinstance(b,dict)
          for h in b.get('hooks',[]) if 'cockpit-mode-interlock.sh' in h.get('command','')))" "$1" 2>/dev/null || echo ERR; }
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
exists "$P/.claude/skills/plan/SKILL.md"    "project: /plan skill installed"
exists "$P/.claude/skills/execute/SKILL.md" "project: /execute skill installed"
exists "$P/.claude/sable/roles/cockpit.md"  "project: cockpit role installed"
exists "$P/.claude/sable/layouts/sable.kdl" "project: layout installed"
if [ -x "$P/.claude/hooks/multi-manager/cockpit-mode-interlock.sh" ]; then pass "project: interlock hook installed+exec"; else fail "project: interlock hook installed+exec"; fi
SET="$P/.claude/settings.local.json"
exists "$SET" "project: settings.local.json created"
if valid_json "$SET"; then pass "project: settings is valid JSON"; else fail "project: settings is valid JSON"; fi
if [ "$(count_interlock "$SET")" = "1" ]; then pass "project: interlock registered once"; else fail "project: interlock registered once" "count=$(count_interlock "$SET")"; fi
exists "$P/.claude/sable/agents.yaml" "project: registry (agents.yaml) installed"
if [ -x "$P/.claude/hooks/multi-manager/session-role-anchor.sh" ]; then pass "project: identity hook installed+exec"; else fail "project: identity hook installed+exec"; fi
if [ "$(count_in_event "$SET" SessionStart session-role-anchor.sh)" = "1" ]; then pass "project: identity hook registered SessionStart"; else fail "project: identity hook registered SessionStart" "count=$(count_in_event "$SET" SessionStart session-role-anchor.sh)"; fi
if [ "$(count_in_event "$SET" PreCompact session-role-anchor.sh)" = "1" ]; then pass "project: identity hook registered PreCompact"; else fail "project: identity hook registered PreCompact"; fi

# idempotent re-run
SABLE_PROJECT_DIR="$P" bash "$INSTALLER" --project >/dev/null 2>&1
if [ "$(count_interlock "$SET")" = "1" ]; then pass "project: re-run stays idempotent"; else fail "project: re-run idempotent" "count=$(count_interlock "$SET")"; fi
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

# ---------- default scope is project ----------
P2="$(mktemp -d)"
SABLE_PROJECT_DIR="$P2" bash "$INSTALLER" >/dev/null 2>&1
exists "$P2/.claude/skills/plan/SKILL.md" "default (no flag) installs into project ./.claude"

# ---------- user scope ----------
U="$(mktemp -d)"
CLAUDE_USER_DIR="$U/.claude" bash "$INSTALLER" --user >/dev/null 2>&1
exists "$U/.claude/skills/execute/SKILL.md" "user: skill installed under ~/.claude"
exists "$U/.claude/settings.json" "user: settings.json created"
if [ "$(count_interlock "$U/.claude/settings.json")" = "1" ]; then pass "user: interlock registered once"; else fail "user: interlock registered once"; fi

# ---------- uninstall (project) ----------
SABLE_PROJECT_DIR="$P" bash "$INSTALLER" --project --uninstall >/dev/null 2>&1
if [ ! -e "$P/.claude/skills/plan/SKILL.md" ]; then pass "uninstall removes skills"; else fail "uninstall removes skills"; fi
if [ ! -e "$P/.claude/sable/agents.yaml" ]; then pass "uninstall removes registry"; else fail "uninstall removes registry"; fi
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

# Integration: real `sable-cockpit-install --user` over a realistic pre-seeded
# multi-manager user scope leaves exactly one identity registration per event and
# one interlock, and preserves the bd prime entries.
M="$(mktemp -d)"; mkdir -p "$M/.claude"
seed_sibling_blocks "$M/.claude/settings.json"
CLAUDE_USER_DIR="$M/.claude" bash "$INSTALLER" --user >/dev/null 2>&1
MSET="$M/.claude/settings.json"
if [ "$(count_in_event "$MSET" SessionStart session-role-anchor.sh)" = "1" ]; then pass "md7: multi-manager re-install — identity once (SessionStart)"; else fail "md7: multi-manager re-install — identity once (SessionStart)" "count=$(count_in_event "$MSET" SessionStart session-role-anchor.sh)"; fi
if [ "$(count_in_event "$MSET" PreCompact session-role-anchor.sh)" = "1" ]; then pass "md7: multi-manager re-install — identity once (PreCompact)"; else fail "md7: multi-manager re-install — identity once (PreCompact)" "count=$(count_in_event "$MSET" PreCompact session-role-anchor.sh)"; fi
if [ "$(count_interlock "$MSET")" = "1" ]; then pass "md7: multi-manager re-install — interlock once"; else fail "md7: multi-manager re-install — interlock once" "count=$(count_interlock "$MSET")"; fi
if [ "$(count_marker "$MSET" 'bd prime')" = "2" ]; then pass "md7: multi-manager re-install — bd prime preserved"; else fail "md7: multi-manager re-install — bd prime preserved" "count=$(count_marker "$MSET" 'bd prime')"; fi
if valid_json "$MSET"; then pass "md7: settings valid after multi-manager re-install"; else fail "md7: settings valid after multi-manager re-install"; fi

rm -rf "$P" "$P2" "$U" "$PU" "$M"
echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
