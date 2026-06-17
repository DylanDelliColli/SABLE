#!/usr/bin/env bash
# mode-interlock.sh — Enforce the cockpit's planning/execution boundary.
# Trigger: PreToolUse:Bash AND PreToolUse:Agent | Timeout: 3000ms
#
# This is the mechanical guarantee that makes the cockpit's two modes real
# rather than advisory persona. It governs ONLY the Lincoln main session
# (the cockpit seat). Modes flip mid-conversation via /plan and /execute —
# the state file is re-read on every tool call, so the boundary moves the
# moment the skill rewrites it.
#
#   PLANNING mode  → deny spawning execution managers (optimus/tarzan/chuck)
#                    — as Agent-tool subagent spawns (v2 one-window topology)
#                    or legacy Bash launch aliases — and deny `git push` of
#                    code. You fill the pool here; you do not drain it or push
#                    from a half-formed backlog. Backlog population (bd create
#                    --parent/--graph/--file) stays blocked until
#                    substage=decomposition.
#   EXECUTION mode → deny spawning planning-only producers
#                    (sherlock/victor/columbo, and legacy gaudi launches) from
#                    the cockpit. You drain the pool here; producers belong to
#                    planning. (Gaudi is a skill in v2 — skill invocations are
#                    not Agent spawns and are not governed here.)
#
# Soft override: a Bash command carrying `--force`, or env SABLE_ORCHESTRATION_FORCE=1,
# is always allowed. The interlock is a guardrail, not a wall.
#
# No-op unless CLAUDE_AGENT_NAME is lincoln (v2) or cockpit (legacy/transition).
# No-op in subagent contexts (agent_id present) so dispatched agents are never
# governed by it. No-op when no mode is set. Mode is read from the mode-state
# file that bin/sable-mode owns (SABLE_MODE_STATE or
# ~/.claude/sable/state/mode-state.json), preferring the helper when
# resolvable.

set -uo pipefail

# Consume stdin unconditionally so that the upstream writer (e.g. a python3
# subprocess in the test harness) can flush and exit cleanly regardless of
# whether this hook no-ops early. Without this drain, any early exit below
# leaves the pipe read-end closed while the writer is still flushing, which
# produces a BrokenPipeError in the writer's stderr.
INPUT="$(cat)"

# Only the Lincoln/cockpit main session is governed.
case "${CLAUDE_AGENT_NAME:-}" in
  lincoln|cockpit) ;;
  *) exit 0 ;;
esac

# Runtime enable gate — no-op when the cockpit is disabled (SABLE-cav.7).
case "$(printf '%s' "${SABLE_ORCHESTRATION:-}" | tr '[:upper:]' '[:lower:]')" in
    off|0|false|no) exit 0 ;;
esac

# Subagent context — never govern dispatched agents.
AGENT_ID="$(printf '%s' "$INPUT" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('agent_id', '') or '')
except Exception:
    print('')
" 2>/dev/null)"
[ -n "$AGENT_ID" ] && exit 0

# Env opt-out applies to every leg.
[ "${SABLE_ORCHESTRATION_FORCE:-}" = "1" ] && exit 0

TOOL_NAME="$(printf '%s' "$INPUT" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('tool_name', '') or '')
except Exception:
    print('')
" 2>/dev/null)"

# Resolve current mode. Prefer the helper; fall back to reading the file.
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
MODE_BIN="$HOOK_DIR/../../bin/sable-mode"
STATE="${SABLE_MODE_STATE:-$HOME/.claude/sable/state/mode-state.json}"

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

# ---------------------------------------------------------------------------
# Agent leg (v2): spawning named agents happens via the Agent tool.
# ---------------------------------------------------------------------------
if [ "$TOOL_NAME" = "Agent" ]; then
  SUBTYPE="$(printf '%s' "$INPUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print((d.get('tool_input') or {}).get('subagent_type', '') or '')
except Exception:
    print('')
" 2>/dev/null | tr '[:upper:]' '[:lower:]')"
  [ -z "$SUBTYPE" ] && exit 0

  case "$MODE" in
    planning)
      case "$SUBTYPE" in
        optimus|tarzan|chuck)
          deny "Orchestration is in PLANNING mode — spawning execution managers (optimus/tarzan/chuck) is blocked. Run /execute to drain the pool, or set SABLE_ORCHESTRATION_FORCE=1 to override."
          ;;
      esac
      ;;
    execution)
      case "$SUBTYPE" in
        sherlock|victor|columbo)
          deny "Orchestration is in EXECUTION mode — spawning planning-only producers (sherlock/victor/columbo) is blocked. Run /plan for a planning session, or set SABLE_ORCHESTRATION_FORCE=1 to override."
          ;;
      esac
      ;;
  esac
  exit 0
fi

# ---------------------------------------------------------------------------
# Bash leg: legacy launch aliases, git push, backlog population.
# ---------------------------------------------------------------------------
COMMAND="$(printf '%s' "$INPUT" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('tool_input', {}).get('command', '') or '')
except Exception:
    print('')
" 2>/dev/null)"
[ -z "$COMMAND" ] && exit 0

# Soft override: explicit --force flag on the command.
printf '%s' "$COMMAND" | grep -qE '(^|[[:space:]])--force([[:space:]]|$)' && exit 0

# Detect an attempt to launch a given set of named agents — either by setting
# CLAUDE_AGENT_NAME=<name> on a claude invocation, or by invoking the bare
# launch alias as the first word of the command.
launches() {
  # $1 = alternation like 'optimus|tarzan|chuck'
  printf '%s' "$COMMAND" | grep -qE "CLAUDE_AGENT_NAME=($1)([[:space:]]|$)" && return 0
  printf '%s' "$COMMAND" | grep -qE "(^|[[:space:];&|])($1)([[:space:]]|$)" && return 0
  return 1
}

# Detect an attempt to populate the implementation backlog via bd create. The
# BARE epic shell (bd create --type=epic) is intentionally allowed early — it is
# the planning home that producers (gaudi/columbo --epic) attach their gating
# review to, and it holds the framing artifact. What is gated is populating it:
# --parent (implementation children), and --graph / --file (batch backlog).
# Plain artifact-bead creates (findings, test specs, framing/question beads) the
# upstream substages produce are NOT blocked either.
authors_backlog() {
  printf '%s' "$COMMAND" | grep -qE '(^|[[:space:];&|])bd[[:space:]]+create([[:space:]]|$)' || return 1
  printf '%s' "$COMMAND" | grep -qE '(--graph|--file|--parent)' && return 0
  return 1
}

# Resolve the planning substage the same way MODE is resolved (helper preferred,
# file fallback). Empty when unset or not in planning mode.
get_substage() {
  if [ -x "$MODE_BIN" ]; then
    "$MODE_BIN" substage get 2>/dev/null || true
  elif [ -f "$STATE" ]; then
    STATE="$STATE" python3 -c "import json,os; print(json.load(open(os.environ['STATE'])).get('substage','') or '')" 2>/dev/null || true
  fi
}

case "$MODE" in
  planning)
    if launches 'optimus|tarzan|chuck'; then
      deny "Orchestration is in PLANNING mode — launching execution managers (optimus/tarzan/chuck) is blocked. Run /execute to drain the pool, or append --force to override."
    fi
    if printf '%s' "$COMMAND" | grep -qE '(^|[[:space:];&|])git[[:space:]]+push([[:space:]]|$)'; then
      deny "Orchestration is in PLANNING mode — code 'git push' is blocked so you don't ship from a half-formed backlog. Run /execute first, or append --force to override."
    fi
    if authors_backlog; then
      SUBSTAGE="$(get_substage)"
      if [ "$SUBSTAGE" != "decomposition" ]; then
        deny "Orchestration is in PLANNING mode at substage '${SUBSTAGE:-unset}' — populating the implementation backlog (bd create --parent/--graph/--file) is blocked until substage=decomposition. The bare epic shell (bd create --type=epic) is allowed now as the planning home; producers attach their review to it. Walk the staged flow (framing → research → architecture → test-strategy → decomposition), advancing with 'sable-mode substage advance' after each human sign-off. Append --force to override."
      fi
    fi
    ;;
  execution)
    if launches 'sherlock|victor|columbo|gaudi'; then
      deny "Orchestration is in EXECUTION mode — launching planning-only producers (sherlock/victor/columbo/gaudi) from the cockpit is blocked. Run /plan for a planning session, or append --force to override."
    fi
    ;;
esac

exit 0
