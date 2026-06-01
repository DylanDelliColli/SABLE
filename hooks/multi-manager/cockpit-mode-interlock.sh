#!/usr/bin/env bash
# cockpit-mode-interlock.sh — Enforce the cockpit's planning/execution boundary.
# Trigger: PreToolUse:Bash | Timeout: 3000ms
#
# This is the mechanical guarantee that makes the cockpit's two modes real
# rather than advisory persona. It governs ONLY the cockpit session.
#
#   PLANNING mode  → deny spawning execution managers (optimus/tarzan/chuck)
#                    and deny `git push` of code. You fill the pool here; you
#                    do not drain it or push from a half-formed backlog.
#   EXECUTION mode → deny spawning planning-only producers
#                    (sherlock/victor/columbo/gaudi) from the cockpit. You drain
#                    the pool here; producers belong to planning sessions.
#
# Soft override: a command carrying `--force`, or env SABLE_COCKPIT_FORCE=1,
# is always allowed. The interlock is a guardrail, not a wall.
#
# No-op unless CLAUDE_AGENT_NAME=cockpit. No-op in subagent contexts (agent_id
# present in the hook input) so dispatched workers are never governed by it.
# No-op when no mode is set. Mode is read from the mode-state file that
# bin/sable-mode owns (SABLE_COCKPIT_STATE or ~/.claude/sable/state/
# cockpit-mode.json), preferring the helper when resolvable.

set -uo pipefail

# Only the cockpit session is governed.
[ "${CLAUDE_AGENT_NAME:-}" = "cockpit" ] || exit 0

# Runtime enable gate — no-op when the cockpit is disabled (SABLE-cav.7).
case "$(printf '%s' "${SABLE_COCKPIT:-}" | tr '[:upper:]' '[:lower:]')" in
    off|0|false|no) exit 0 ;;
esac

INPUT="$(cat)"

# Subagent context — never govern dispatched workers.
AGENT_ID="$(printf '%s' "$INPUT" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('agent_id', '') or '')
except Exception:
    print('')
" 2>/dev/null)"
[ -n "$AGENT_ID" ] && exit 0

COMMAND="$(printf '%s' "$INPUT" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('tool_input', {}).get('command', '') or '')
except Exception:
    print('')
" 2>/dev/null)"
[ -z "$COMMAND" ] && exit 0

# Soft override: explicit --force flag or env opt-out.
[ "${SABLE_COCKPIT_FORCE:-}" = "1" ] && exit 0
printf '%s' "$COMMAND" | grep -qE '(^|[[:space:]])--force([[:space:]]|$)' && exit 0

# Resolve current mode. Prefer the helper; fall back to reading the file.
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
MODE_BIN="$HOOK_DIR/../../bin/sable-mode"
STATE="${SABLE_COCKPIT_STATE:-$HOME/.claude/sable/state/cockpit-mode.json}"

MODE=""
if [ -x "$MODE_BIN" ]; then
  MODE="$("$MODE_BIN" get 2>/dev/null || true)"
elif [ -f "$STATE" ]; then
  MODE="$(STATE="$STATE" python3 -c "import json,os; print(json.load(open(os.environ['STATE'])).get('mode','') or '')" 2>/dev/null || true)"
fi
[ -z "$MODE" ] && exit 0

deny() {
  # $1 = reason
  REASON="$1" python3 -c "
import json, os
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': os.environ.get('REASON', '')
    }
}))
"
  exit 0
}

# Detect an attempt to launch a given set of named agents — either by setting
# CLAUDE_AGENT_NAME=<name> on a claude invocation, or by invoking the bare
# launch alias as the first word of the command.
launches() {
  # $1 = alternation like 'optimus|tarzan|chuck'
  printf '%s' "$COMMAND" | grep -qE "CLAUDE_AGENT_NAME=($1)([[:space:]]|$)" && return 0
  printf '%s' "$COMMAND" | grep -qE "(^|[[:space:];&|])($1)([[:space:]]|$)" && return 0
  return 1
}

case "$MODE" in
  planning)
    if launches 'optimus|tarzan|chuck'; then
      deny "Cockpit is in PLANNING mode — launching execution managers (optimus/tarzan/chuck) is blocked. Run /execute to drain the pool, or append --force to override."
    fi
    if printf '%s' "$COMMAND" | grep -qE '(^|[[:space:];&|])git[[:space:]]+push([[:space:]]|$)'; then
      deny "Cockpit is in PLANNING mode — code 'git push' is blocked so you don't ship from a half-formed backlog. Run /execute first, or append --force to override."
    fi
    ;;
  execution)
    if launches 'sherlock|victor|columbo|gaudi'; then
      deny "Cockpit is in EXECUTION mode — launching planning-only producers (sherlock/victor/columbo/gaudi) from the cockpit is blocked. Run /plan for a planning session, or append --force to override."
    fi
    ;;
esac

exit 0
