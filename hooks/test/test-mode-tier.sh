#!/usr/bin/env bash
# test-mode-tier.sh — planning tier (quick|full) + interlock telescoping (SABLE-kwr.3).
# Unit: sable-mode set/get/default/validate the tier. Integration: the interlock
# allows backlog authoring under quick tier (single-gate telescope) but still
# blocks it under full tier before decomposition, and fails safe when tier is absent.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)"
MODE_BIN="$REPO/bin/sable-mode"
HOOK="$REPO/hooks/multi-manager/mode-interlock.sh"
STATE="$(mktemp -u)"
export SABLE_MODE_STATE="$STATE"
trap 'rm -f "$STATE"' EXIT
fails=0
ok(){ printf '  ok  %s\n' "$1"; }
no(){ printf '  FAIL %s — %s\n' "$1" "${2:-}"; fails=$((fails+1)); }
eq(){ [ "$2" = "$3" ] && ok "$1" || no "$1" "want '$3' got '$2'"; }

BACKLOG_JSON='{"tool_name":"Bash","tool_input":{"command":"bd create --parent SABLE-x --title t"}}'
is_deny(){ printf '%s' "$1" | grep -q '"permissionDecision": "deny"'; }
fire(){ printf '%s' "$BACKLOG_JSON" | CLAUDE_AGENT_NAME=lincoln bash "$HOOK" 2>/dev/null; }

# --- unit: sable-mode tier set / get / default / validate ---
"$MODE_BIN" set planning --tier quick >/dev/null 2>&1
eq "quick tier stored + read" "$("$MODE_BIN" tier get 2>/dev/null)" "quick"
"$MODE_BIN" set planning --fleet a,b >/dev/null 2>&1
eq "tier defaults to full" "$("$MODE_BIN" tier get 2>/dev/null)" "full"
"$MODE_BIN" set planning --tier bogus >/dev/null 2>&1
eq "invalid tier rejected (exit 2)" "$?" "2"

# --- integration: quick tier telescopes the backlog gate at framing ---
"$MODE_BIN" set planning --tier quick >/dev/null 2>&1
if is_deny "$(fire)"; then no "quick tier allows backlog at framing" "got deny"; else ok "quick tier allows backlog at framing"; fi

# --- integration: full tier still blocks before decomposition ---
"$MODE_BIN" set planning --tier full >/dev/null 2>&1
if is_deny "$(fire)"; then ok "full tier blocks backlog at framing"; else no "full tier blocks backlog at framing" "no deny"; fi

# --- integration: full tier allows once at decomposition ---
"$MODE_BIN" substage set decomposition >/dev/null 2>&1
if is_deny "$(fire)"; then no "full tier allows backlog at decomposition" "got deny"; else ok "full tier allows backlog at decomposition"; fi

# --- fail-safe: tier absent behaves as full (blocks at framing) ---
printf '%s\n' '{"mode":"planning","since":"x","fleet":[],"substage":"framing"}' > "$STATE"
if is_deny "$(fire)"; then ok "absent tier fails safe to full (blocks)"; else no "absent tier fails safe to full (blocks)" "no deny"; fi

# --- discovery sub-mode (SABLE-7v1r.4): lightest planning sub-mode ---
"$MODE_BIN" set planning --tier discovery >/dev/null 2>&1
eq "discovery tier stored + read" "$("$MODE_BIN" tier get 2>/dev/null)" "discovery"
# no backlog gate: even an impl-child create is allowed at framing
if is_deny "$(fire)"; then no "discovery tier allows backlog (no gate)" "got deny"; else ok "discovery tier allows backlog (no gate)"; fi
# bare epic-intention shell allowed (matches the quick/full epic-shell rule)
EPIC_JSON='{"tool_name":"Bash","tool_input":{"command":"bd create --type=epic --title intent"}}'
if is_deny "$(printf '%s' "$EPIC_JSON" | CLAUDE_AGENT_NAME=lincoln bash "$HOOK" 2>/dev/null)"; then no "discovery allows bare epic-intention shell" "got deny"; else ok "discovery allows bare epic-intention shell"; fi
# planning blocks STILL apply under discovery: git push + manager launch denied
PUSH_JSON='{"tool_name":"Bash","tool_input":{"command":"git push"}}'
if is_deny "$(printf '%s' "$PUSH_JSON" | CLAUDE_AGENT_NAME=lincoln bash "$HOOK" 2>/dev/null)"; then ok "discovery still blocks git push"; else no "discovery still blocks git push" "no deny"; fi
MGR_JSON='{"tool_name":"Bash","tool_input":{"command":"optimus"}}'
if is_deny "$(printf '%s' "$MGR_JSON" | CLAUDE_AGENT_NAME=lincoln bash "$HOOK" 2>/dev/null)"; then ok "discovery still blocks manager launch"; else no "discovery still blocks manager launch" "no deny"; fi

if [ "$fails" -eq 0 ]; then printf 'PASS test-mode-tier\n'; else printf 'FAIL test-mode-tier (%d)\n' "$fails"; exit 1; fi
