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
# not a wall. On the Bash leg the env form is honored both ways: set in the
# hook process's own environment, AND as an inline `SABLE_ORCHESTRATION_FORCE=1
# <command>` prefix on the command text itself — the latter is parsed out of
# the command string because the hook runs as a separate process from whatever
# the Bash tool eventually execs, so it can't inherit an env assignment inline
# to a command it hasn't run yet. No mode set → inert. SABLE_ORCHESTRATION=off
# → inert. Mode is read from the mode-state file that bin/sable-mode owns
# (SABLE_MODE_STATE or ~/.claude/sable/state/mode-state.json), preferring the
# helper when resolvable. sable-mode invocations are exempt from the
# manager/producer launch classifier entirely — its own flags legitimately
# carry agent names (e.g. `--fleet victor`) that aren't a spawn attempt.

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

# Resolve the mode-state file from the repo the tool call runs in (the hook-input
# cwd, the dir git actually operates in — mirrors SABLE-041's push-repo resolve)
# so per-repo modes are honored and two sessions in different repos stay
# independent. Export the resolved path as SABLE_MODE_STATE so the MODE_BIN call,
# the file fallback, and the substage/tier reads all agree regardless of THIS
# hook process's own cwd. sable_mode_state_path returns an already-set
# SABLE_MODE_STATE unchanged, so a test/operator override is preserved.
# shellcheck source=lib-mode-path.sh
. "$HOOK_DIR/lib-mode-path.sh"
HOOK_CWD="$(printf '%s' "$INPUT" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('cwd', '') or '')
except Exception:
    print('')
" 2>/dev/null)"
STATE="$(sable_mode_state_path "$HOOK_CWD")"
export SABLE_MODE_STATE="$STATE"

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
# Worker-dispatch leg (tmux-native, SABLE-bldh.6): `sable-spawn-worker` stands up
# a worker pane — the EXECUTION-mode dispatch path that replaced the in-process
# Agent spawn. Gate it to execution mode regardless of which agent (lincoln OR a
# manager) invokes it, since managers now run it from their own Bash. The env
# override (SABLE_ORCHESTRATION_FORCE=1) is already handled above; a `--force`
# flag on the command also overrides.
# ---------------------------------------------------------------------------
# Extract the Bash command once — reused by both spawn legs and the main Bash
# leg below (one python3 field read instead of three).
CMD_TEXT="$(printf '%s' "$INPUT" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('tool_input', {}).get('command', '') or '')
except Exception:
    print('')
" 2>/dev/null)"

# Soft override: SABLE_ORCHESTRATION_FORCE=1 as an inline prefix on the command
# text applies to EVERY Bash leg, the spawn legs included (SABLE-pi5m defect 2).
# The top-of-script env check only sees this hook process's OWN env, which cannot
# inherit an assignment inline to a command the Bash tool has not exec'd yet, so
# parse it out of the command string here — BEFORE the spawn legs, so the
# documented override actually reaches sable-spawn-worker / sable-spawn-manager
# (previously it was parsed only in the main Bash leg, which the spawn legs
# short-circuit past, so the prefix never overrode a spawn deny).
printf '%s' "$CMD_TEXT" | grep -qE '(^|[[:space:];&|])SABLE_ORCHESTRATION_FORCE=1([[:space:]]|$)' && exit 0

# leading_cmd <command> → the first real command word (skipping any leading
# VAR=val assignment prefix), lowercased. Empty if the command is only
# assignments.
leading_cmd() {
  printf '%s' "$1" | awk '{ for (i=1;i<=NF;i++){ if ($i ~ /=/) continue; print $i; break } }' | tr '[:upper:]' '[:lower:]'
}

# is_prose_carrier <command> → true iff the leading command word legitimately
# carries agent / producer / spawn-helper NAMES inside its arguments as prose
# rather than as a launch (SABLE-pi5m). A bd --description, a sable-note body, a
# sable-msg message, and a sable-mode --fleet flag all routinely name agents and
# the spawn helpers (often with shell punctuation), and none of these commands
# ever stands up an agent — so a name appearing in their args is never a spawn or
# a launch. Shared by is_spawn_call (helper legs) and launches (name legs).
is_prose_carrier() {
  case "$(leading_cmd "$1")" in
    bd|sable-note|sable-mode|sable-msg) return 0 ;;
  esac
  return 1
}

# is_spawn_call <helper> <command> → true iff <command> genuinely invokes
# <helper> as a command, NOT merely names it inside a quoted argument (SABLE-pi5m
# defect 1). Three guards:
#   (a) prose-carrier allow-list (is_prose_carrier) — a bd / sable-note /
#       sable-msg / sable-mode command's args legitimately name the helper in
#       prose, often with shell punctuation; those are never a spawn.
#   (b) leading-word identity — if the first real command word (after any VAR=val
#       assignment prefix, which leading_cmd strips) IS the helper, it is a spawn
#       even though the position regex below cannot see past the assignment
#       (e.g. `CLAUDE_AGENT_NAME=x sable-spawn-worker …`) — SABLE-pi5m follow-up.
#   (c) command-word position — the helper must sit at the start of the command
#       or immediately after a real shell separator (; & | ( or a newline, which
#       grep's per-line ^ already covers). A plain space does NOT qualify: an
#       argument-separating space is indistinguishable from a space inside a
#       quoted string, and matching on it was the original false-positive.
is_spawn_call() {
  local helper="$1" cmd="$2"
  is_prose_carrier "$cmd" && return 1
  [ "$(leading_cmd "$cmd")" = "$helper" ] && return 0
  printf '%s' "$cmd" | grep -qE "(^|[;&|(])[[:space:]]*${helper}([[:space:]]|\$)"
}

if is_spawn_call 'sable-spawn-worker' "$CMD_TEXT"; then
  printf '%s' "$CMD_TEXT" | grep -qE '(^|[[:space:]])--force([[:space:]]|$)' && exit 0
  if [ "$MODE" != "execution" ]; then
    deny "Orchestration is in ${MODE} mode — sable-spawn-worker dispatches an execution worker and is blocked outside EXECUTION mode. Run /sable-execute, or append --force to override."
  fi
  exit 0
fi

# sable-spawn-manager stands up the execution fleet (manager windows) — gated
# to EXECUTION mode the same way (SABLE-dqhn.2). Launching a session is
# mode-neutral (lincoln only); spawning managers is not.
if is_spawn_call 'sable-spawn-manager' "$CMD_TEXT"; then
  printf '%s' "$CMD_TEXT" | grep -qE '(^|[[:space:]])--force([[:space:]]|$)' && exit 0
  if [ "$MODE" != "execution" ]; then
    deny "Orchestration is in ${MODE} mode — sable-spawn-manager stands up the execution fleet and is blocked outside EXECUTION mode. Run /sable-execute, or append --force to override."
  fi
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

COMMAND="$CMD_TEXT"   # extracted once above (shared with the spawn legs)
[ -z "$COMMAND" ] && exit 0

# Soft override: explicit --force flag on the command.
printf '%s' "$COMMAND" | grep -qE '(^|[[:space:]])--force([[:space:]]|$)' && exit 0

# NOTE: the inline SABLE_ORCHESTRATION_FORCE=1 prefix override is handled once,
# up above the spawn legs (SABLE-pi5m), so it covers every Bash leg — this main
# leg included. Any command reaching here carrying that prefix has already
# exited 0, so no second check is needed.

# sable-mode is the sanctioned mode-transition command run by /sable-plan and
# /sable-execute; its own flags legitimately carry agent/producer names (e.g.
# `--fleet victor`) that would otherwise false-positive the launch classifier
# below, which only wants the token in COMMAND position, not anywhere in the
# argument list. sable-mode itself never spawns anything, so exempt it outright.
printf '%s' "$COMMAND" | grep -qE '(^|[[:space:];&|])sable-mode([[:space:]]|$)' && exit 0

# Detect an attempt to launch a given set of named agents — either by invoking
# the bare launch alias in command-word position, or by setting
# CLAUDE_AGENT_NAME=<name> as a launch env prefix. Mirrors is_spawn_call's
# command-position discipline (SABLE-pi5m): a manager/producer NAME appearing
# inside a prose-carrying command's args — a bd --description, a sable-note body,
# a sable-msg message — is NOT a launch. The pre-fix regex let a plain space
# count as a boundary, so any name mid-sentence in quoted prose false-matched
# (e.g. `sable-msg lincoln "shipped the sherlock findings"` denied in execution;
# `sable-msg tarzan "ask optimus to take it"` denied in planning).
launches() {
  # $1 = alternation like 'optimus|tarzan|chuck'
  is_prose_carrier "$COMMAND" && return 1
  # (a) bare launch alias as the leading command word (leading_cmd strips any
  #     VAR=val assignment prefix, so `FOO=bar optimus` still resolves to optimus).
  printf '%s' "$(leading_cmd "$COMMAND")" | grep -qE "^($1)\$" && return 0
  # (b) bare launch alias in command-word position after a real shell separator.
  printf '%s' "$COMMAND" | grep -qE "(^|[;&|(])[[:space:]]*($1)([[:space:]]|\$)" && return 0
  # (c) CLAUDE_AGENT_NAME=<name> launch env prefix in command-word position (not
  #     merely a name assignment quoted inside another command's prose).
  printf '%s' "$COMMAND" | grep -qE "(^|[;&|(])[[:space:]]*CLAUDE_AGENT_NAME=($1)([[:space:]]|\$)" && return 0
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

# Resolve the planning tier (quick|full|discovery). Empty/full => the full
# substage gate applies; quick => the gate is telescoped to the single
# /sable-plan approval, so the quick-tier flow may author its 1-3 beads without
# walking all five substages; discovery (Mode 1) likewise bypasses the backlog
# gate (it authors no implementation beads — only charters + epic-intention
# shells). Fail-safe: anything other than 'quick'/'discovery' keeps the strict gate.
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
    TIER="$(get_tier)"
    if authors_backlog && [ "$TIER" != "quick" ] && [ "$TIER" != "discovery" ]; then
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
