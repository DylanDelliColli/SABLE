#!/usr/bin/env bash
# lib-identity.sh — shared identity resolution for multi-manager hooks (SABLE-uz9.3)
#
# Resolves WHO is acting from, in priority order:
#   1. Hook input JSON: agent_id present => subagent context. Identity is the
#      agent_type field (the agent-definition name; verified present on
#      CC 2.1.170, spike SABLE-uz9.1). Env vars are the PARENT session's
#      identity in this context and MUST be ignored — this closes the
#      "subagent contamination" hole documented in MULTI-MANAGER-PATTERN.md.
#   2. CLAUDE_AGENT_NAME / CLAUDE_AGENT_ROLE env vars (legacy terminal
#      launches — Chuck's holdout terminal and any pre-v2 alias). Dual-mode
#      support is a hard requirement of SABLE-uz9.3.
#
# Usage (from a hook that already captured its stdin):
#   source "$(dirname "${BASH_SOURCE[0]}")/lib-identity.sh"
#   sable_resolve_identity "$HOOK_INPUT_JSON"
#
# Sets (always, possibly empty/zero):
#   SABLE_ID_NAME         lowercase agent name ("optimus", "sherlock", "explore", "")
#   SABLE_ID_TYPE         registry type from agents.yaml ("epic_manager", ...) or ""
#   SABLE_ID_SOURCE       agent_type | env | none
#   SABLE_ID_IS_SUBAGENT  1 if hook input carried agent_id, else 0
#   SABLE_ID_IS_MANAGER   1 if the identity should receive manager-hook behavior
#   SABLE_ID_IS_REGISTERED 1 if the name has an agents.yaml entry
#
# Manager-ness:
#   - registry type in: epic_manager one_off_manager integrator strategist cockpit
#   - OR legacy: env-sourced identity with CLAUDE_AGENT_ROLE=manager and no
#     registry entry (an adopter's custom alias keeps working unchanged)
#   Unregistered subagent types (Explore, general-purpose, code-reviewer, ...)
#   are workers: never managers, hooks stand down for them.
#
# Registry path: ~/.claude/sable/agents.yaml (override with SABLE_AGENTS_YAML,
# used by tests). Parsed with awk — no python-yaml dependency.

sable_resolve_identity() {
  local json="${1:-}"
  SABLE_ID_NAME=""
  SABLE_ID_TYPE=""
  SABLE_ID_SOURCE="none"
  SABLE_ID_IS_SUBAGENT=0
  SABLE_ID_IS_MANAGER=0
  SABLE_ID_IS_REGISTERED=0

  local parsed agent_id agent_type
  parsed=$(printf '%s' "$json" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print(d.get('agent_id', '') or '')
print(d.get('agent_type', '') or '')
" 2>/dev/null) || parsed=""
  agent_id=$(printf '%s\n' "$parsed" | sed -n '1p')
  agent_type=$(printf '%s\n' "$parsed" | sed -n '2p')

  if [ -n "$agent_id" ]; then
    SABLE_ID_IS_SUBAGENT=1
    if [ -n "$agent_type" ]; then
      SABLE_ID_NAME=$(printf '%s' "$agent_type" | tr '[:upper:]' '[:lower:]')
      SABLE_ID_SOURCE="agent_type"
    fi
    # NOTE: env deliberately not consulted — it belongs to the parent session.
  elif [ -n "${CLAUDE_AGENT_NAME:-}" ]; then
    SABLE_ID_NAME=$(printf '%s' "$CLAUDE_AGENT_NAME" | tr '[:upper:]' '[:lower:]')
    SABLE_ID_SOURCE="env"
  fi

  [ -z "$SABLE_ID_NAME" ] && return 0

  local yaml="${SABLE_AGENTS_YAML:-$HOME/.claude/sable/agents.yaml}"
  if [ -f "$yaml" ]; then
    SABLE_ID_TYPE=$(awk -v name="$SABLE_ID_NAME" '
      $0 == "  " name ":" { found = 1; next }
      found && /^    type:/ { sub(/^    type:[ ]*/, ""); sub(/[ \t#].*$/, ""); print; exit }
      found && /^  [a-zA-Z0-9_-]+:/ { exit }
    ' "$yaml" 2>/dev/null)
  fi
  [ -n "$SABLE_ID_TYPE" ] && SABLE_ID_IS_REGISTERED=1

  case " epic_manager one_off_manager integrator strategist cockpit " in
    *" $SABLE_ID_TYPE "*) SABLE_ID_IS_MANAGER=1 ;;
  esac

  # Legacy escape: custom env-launched manager alias not (yet) in the registry.
  if [ "$SABLE_ID_IS_MANAGER" -eq 0 ] && [ "$SABLE_ID_SOURCE" = "env" ] \
     && [ "${CLAUDE_AGENT_ROLE:-}" = "manager" ] && [ "$SABLE_ID_IS_REGISTERED" -eq 0 ]; then
    SABLE_ID_IS_MANAGER=1
  fi

  return 0
}

# sable_resolve_dispatch_lane <hook-input-json>
#
# For PreToolUse:Agent / PostToolUse:Agent hooks. Decides whether pre-dispatch
# governance applies to this Agent call and which manager LANE it belongs to.
#
# Lanes (SABLE-uz9.4, option A — Lincoln dispatches):
#   - Legacy env-launched manager terminal: governance active, lane = the
#     manager itself (today's behavior, unchanged).
#   - v2 one-window main session: governance active only when the cockpit
#     mode-state file says mode=execution. Lane = the "Dispatching-for: <name>"
#     attribution line in the dispatch prompt; defaults to "cockpit" when the
#     dispatch isn't on a manager's behalf (e.g. Lincoln's own utility spawns).
#   - Subagent contexts: stand down (subagents cannot dispatch on current
#     builds — spike SABLE-uz9.1; if that changes, revisit deliberately).
#
# Sets: SABLE_DISPATCH_ACTIVE (0|1), SABLE_DISPATCH_LANE (lowercase name or "").
# Mode-state path override for tests: SABLE_COCKPIT_MODE_FILE.
sable_resolve_dispatch_lane() {
  local json="${1:-}"
  SABLE_DISPATCH_ACTIVE=0
  SABLE_DISPATCH_LANE=""

  sable_resolve_identity "$json"

  [ "$SABLE_ID_IS_SUBAGENT" -eq 1 ] && return 0

  if [ "$SABLE_ID_SOURCE" = "env" ]; then
    if [ "$SABLE_ID_IS_MANAGER" -eq 1 ]; then
      SABLE_DISPATCH_ACTIVE=1
      SABLE_DISPATCH_LANE="$SABLE_ID_NAME"
    fi
    return 0
  fi

  local mode_file="${SABLE_COCKPIT_MODE_FILE:-$HOME/.claude/sable/state/cockpit-mode.json}"
  [ -f "$mode_file" ] || return 0
  local mode
  mode=$(MODE_FILE="$mode_file" python3 -c "
import json, os
try:
    print(json.load(open(os.environ['MODE_FILE'])).get('mode', ''))
except Exception:
    print('')
" 2>/dev/null)
  [ "$mode" = "execution" ] || return 0

  SABLE_DISPATCH_ACTIVE=1
  SABLE_DISPATCH_LANE=$(printf '%s' "$json" | python3 -c "
import json, sys, re
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
prompt = (d.get('tool_input') or {}).get('prompt', '') or ''
m = re.search(r'^Dispatching-for:[ \t]*([a-zA-Z0-9_-]+)', prompt, re.M | re.I)
print(m.group(1).lower() if m else 'cockpit')
" 2>/dev/null)
  [ -z "$SABLE_DISPATCH_LANE" ] && SABLE_DISPATCH_LANE="cockpit"
  return 0
}
