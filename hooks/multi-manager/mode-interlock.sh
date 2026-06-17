#!/usr/bin/env bash
# mode-interlock.sh — Enforce the orchestration planning/execution boundary.
# Trigger: PreToolUse:Bash AND PreToolUse:Agent | Timeout: 3000ms
#
# The mechanical guarantee that makes the two modes real rather than advisory
# persona. Modes flip mid-conversation via /sable-plan and /sable-execute — the state file
# is re-read on every tool call, so the boundary moves the moment the skill
# rewrites it.
#
# v3 (SABLE-4k7): the Agent leg governs the WHOLE subtree, not just the Lincoln
# main session. Once subagents can spawn agents (nested Agent tool, SABLE-d50.1),
# a manager- or producer-typed subagent could otherwise stand up the wrong fleet
# for the current mode — the boundary the interlock exists to make mechanical
# would go advisory for the entire subtree. It is enforced as an identity-aware
# matrix:
#   spawner = main (main session: no agent_id, env lincoln/cockpit)
#           | subagent (agent_id present)
#   target  = manager-typed | producer-typed | unregistered
#             (tool_input.subagent_type looked up in agents.yaml: a registered
#              manager-class type is a manager; any other registered type is a
#              producer; unregistered — Explore/general-purpose/… — is free)
#   PLANNING  → manager target: DENY any spawner. producer target: ALLOW from
#               main, DENY from a subagent. unregistered: FREE.
#   EXECUTION → manager target: ALLOW from main only, DENY from a subagent.
#               producer target: DENY any spawner. unregistered: FREE (this is
#               the lane manager worker dispatches ride on).
# Depth-3+ worker sub-delegation is deliberately ungoverned — the interlock
# guards the MODE boundary, not tree shape.
#
# The Bash leg (legacy launch aliases, git push, backlog population + the
# SABLE-kwr.3 quick-tier substage telescope) governs only the main
# session — in v3 subagents spawn via the Agent tool, not via bash claude aliases.
#
# Soft override: a Bash command carrying `--force`, or env
# SABLE_ORCHESTRATION_FORCE=1, is always allowed. The interlock is a guardrail,
# not a wall. No mode set → inert. SABLE_ORCHESTRATION=off → inert. Mode is read
# from the mode-state file that bin/sable-mode owns (SABLE_MODE_STATE or
# ~/.claude/sable/state/mode-state.json), preferring the helper when resolvable.

set -uo pipefail

# Consume stdin unconditionally so the upstream writer (e.g. a python3 subprocess
# in the test harness) can flush and exit cleanly regardless of whether this hook
# no-ops early. Without this drain, an early exit leaves the pipe read-end closed
# while the writer is still flushing, producing a BrokenPipeError (SABLE-dc0).
INPUT="$(cat)"

# Runtime enable gate — no-op when orchestration is disabled (SABLE-cav.7).
case "$(printf '%s' "${SABLE_ORCHESTRATION:-}" | tr '[:upper:]' '[:lower:]')" in
    off|0|false|no) exit 0 ;;
esac

# Env soft override applies to every leg (Agent matrix + Bash).
[ "${SABLE_ORCHESTRATION_FORCE:-}" = "1" ] && exit 0

TOOL_NAME="$(printf '%s' "$INPUT" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('tool_name', '') or '')
except Exception:
    print('')
" 2>/dev/null)"

AGENT_ID="$(printf '%s' "$INPUT" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('agent_id', '') or '')
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

# classify_target <name> → echoes "manager" | "producer" | "free".
# Looks the spawn target up in agents.yaml. Registered manager-class types are
# managers; any other registered type is a producer; unregistered (or an
# unreadable registry) is free. Fail open: registry read errors yield "free".
classify_target() {
  local name; name="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  [ -z "$name" ] && { echo "free"; return; }
  local yaml="${SABLE_AGENTS_YAML:-${HOME:-}/.claude/sable/agents.yaml}"
  [ -f "$yaml" ] || { echo "free"; return; }
  local t
  t="$(awk -v n="$name" '
    $0 == "  " n ":" { f=1; next }
    f && /^    type:/ { sub(/^    type:[ ]*/,""); sub(/[ \t#].*$/,""); print; exit }
    f && /^  [a-zA-Z0-9_-]+:/ { exit }
  ' "$yaml" 2>/dev/null)"
  [ -z "$t" ] && { echo "free"; return; }
  case " epic_manager one_off_manager integrator strategist cockpit " in
    *" $t "*) echo "manager"; return ;;
  esac
  echo "producer"
}

# ---------------------------------------------------------------------------
# Agent leg (v3): identity-aware mode-boundary matrix — governs main + subagents.
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

  # Spawner dimension: subagent (agent_id present) vs main session.
  if [ -n "$AGENT_ID" ]; then
    SPAWNER="subagent"
  else
    case "${CLAUDE_AGENT_NAME:-}" in
      lincoln|cockpit) SPAWNER="main" ;;
      *) exit 0 ;;   # other env terminal / plain session: Agent leg ungoverned
    esac
  fi

  TARGET="$(classify_target "$SUBTYPE")"
  [ "$TARGET" = "free" ] && exit 0

  case "$MODE" in
    planning)
      case "$TARGET" in
        manager)
          deny "Orchestration is in PLANNING mode — spawning an execution manager ($SUBTYPE) is blocked. You fill the pool here; you do not stand up the fleet. Run /sable-execute to drain it, or set SABLE_ORCHESTRATION_FORCE=1 to override."
          ;;
        producer)
          [ "$SPAWNER" = "subagent" ] && deny "Orchestration is in PLANNING mode — a subagent may not spawn planning producers ($SUBTYPE); producer fan-out stays under the main session. Set SABLE_ORCHESTRATION_FORCE=1 to override."
          ;;
      esac
      ;;
    execution)
      case "$TARGET" in
        manager)
          [ "$SPAWNER" = "subagent" ] && deny "Orchestration is in EXECUTION mode — only the main session spawns the manager fleet; a subagent may not spawn $SUBTYPE (managers do not clone managers). Set SABLE_ORCHESTRATION_FORCE=1 to override."
          ;;
        producer)
          deny "Orchestration is in EXECUTION mode — spawning a planning producer ($SUBTYPE) is blocked; you drain the pool here, you do not fill it. Run /sable-plan for a planning session, or set SABLE_ORCHESTRATION_FORCE=1 to override."
          ;;
      esac
      ;;
  esac
  exit 0
fi

# ---------------------------------------------------------------------------
# Bash leg: governs the main session only — legacy launch aliases, git
# push, backlog population. Subagents spawn via the Agent tool in v3, so the
# Bash leg stays main-session scoped (subagent Bash launches are a non-scenario).
# ---------------------------------------------------------------------------
case "${CLAUDE_AGENT_NAME:-}" in
  lincoln|cockpit) ;;
  *) exit 0 ;;
esac
[ -n "$AGENT_ID" ] && exit 0

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

# Resolve the planning tier (quick|full). Empty/full => the full substage gate
# applies; quick => the gate is telescoped to the single /sable-plan approval, so
# the quick-tier flow may author its 1-3 beads without walking all five substages.
# Fail-safe: anything other than the literal 'quick' keeps the strict gate.
get_tier() {
  if [ -x "$MODE_BIN" ]; then
    "$MODE_BIN" tier get 2>/dev/null || true
  elif [ -f "$STATE" ]; then
    STATE="$STATE" python3 -c "import json,os; print(json.load(open(os.environ['STATE'])).get('tier','') or '')" 2>/dev/null || true
  fi
}

case "$MODE" in
  planning)
    if launches 'optimus|tarzan|chuck'; then
      deny "Orchestration is in PLANNING mode — launching execution managers (optimus/tarzan/chuck) is blocked. Run /sable-execute to drain the pool, or append --force to override."
    fi
    if printf '%s' "$COMMAND" | grep -qE '(^|[[:space:];&|])git[[:space:]]+push([[:space:]]|$)'; then
      deny "Orchestration is in PLANNING mode — code 'git push' is blocked so you don't ship from a half-formed backlog. Run /sable-execute first, or append --force to override."
    fi
    if authors_backlog && [ "$(get_tier)" != "quick" ]; then
      SUBSTAGE="$(get_substage)"
      if [ "$SUBSTAGE" != "decomposition" ]; then
        deny "Orchestration is in PLANNING mode at substage '${SUBSTAGE:-unset}' — populating the implementation backlog (bd create --parent/--graph/--file) is blocked until substage=decomposition. The bare epic shell (bd create --type=epic) is allowed now as the planning home; producers attach their review to it. Walk the staged flow (framing → research → architecture → test-strategy → decomposition), advancing with 'sable-mode substage advance' after each human sign-off. Append --force to override."
      fi
    fi
    ;;
  execution)
    if launches 'sherlock|victor|columbo|gaudi'; then
      deny "Orchestration is in EXECUTION mode — launching planning-only producers (sherlock/victor/columbo/gaudi) is blocked. Run /sable-plan for a planning session, or append --force to override."
    fi
    ;;
esac

exit 0
