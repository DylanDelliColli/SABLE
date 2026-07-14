#!/usr/bin/env bash
# test-mode-interlock.sh — Unit+integration tests for
# mode-interlock.sh, the PreToolUse:Bash guard that makes the cockpit's
# planning/execution modes a mechanical guarantee rather than advisory persona.
#
# Contract:
#   - Only governs the cockpit session (CLAUDE_AGENT_NAME=cockpit); no-op
#     otherwise and no-op in subagent contexts (agent_id present in input).
#   - PLANNING mode: deny spawning execution managers (optimus/tarzan/chuck)
#     and deny `git push` of code.
#   - EXECUTION mode: deny spawning planning-only producers
#     (sherlock/victor/columbo/gaudi) from the cockpit.
#   - Soft override: allow when the command carries --force or env
#     SABLE_ORCHESTRATION_FORCE=1.
#   - No mode set → allow (nothing to enforce).
#
# Run with:
#   bash hooks/test/test-mode-interlock.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO/hooks/multi-manager/mode-interlock.sh"
MODE_BIN="$REPO/bin/sable-mode"

if [ ! -x "$HOOK" ]; then
  echo "FAIL: hook not executable at $HOOK"
  exit 2
fi

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

# Shared temp state; set mode per test group via the real helper.
SABLE_MODE_STATE="$(mktemp -u)"
export SABLE_MODE_STATE

# Hermetic registry so the v3 Agent-leg matrix (SABLE-4k7) classifies spawn
# TARGETS by registry type: manager-typed (epic_manager/one_off_manager/
# integrator/strategist/cockpit) vs producer-typed (any other registered type)
# vs unregistered (free). Without this the matrix would read the installed
# ~/.claude registry and the suite would not be hermetic.
SABLE_AGENTS_YAML="$(mktemp)"
export SABLE_AGENTS_YAML
cat > "$SABLE_AGENTS_YAML" <<'YAML'
agents:
  optimus:
    type: epic_manager
  tarzan:
    type: one_off_manager
  chuck:
    type: integrator
  lincoln:
    type: cockpit
  sherlock:
    type: auditor
  victor:
    type: bead_validator
  columbo:
    type: test_planner
  rudy:
    type: quality_validator
YAML

trap 'rm -f "$SABLE_MODE_STATE" "$SABLE_AGENTS_YAML"' EXIT

set_mode() { "$MODE_BIN" set "$1" >/dev/null 2>&1; }
clear_mode() { rm -f "$SABLE_MODE_STATE"; }

# run_hook <command> [agent_id] → stdout
run_hook() {
  python3 -c "
import json, sys
d = {'tool_input': {'command': sys.argv[1]}}
if len(sys.argv) > 2 and sys.argv[2]:
    d['agent_id'] = sys.argv[2]
print(json.dumps(d))
" "$1" "${2:-}" | bash "$HOOK" 2>/dev/null
}

is_deny() { printf '%s' "$1" | grep -q '"permissionDecision": *"deny"'; }

assert_deny() {
  # name command [agent_id]
  local out; out="$(run_hook "$2" "${3:-}")"
  if is_deny "$out"; then pass "$1"; else fail "$1" "expected deny, got: ${out:-<empty>}"; fi
}
assert_allow() {
  local out; out="$(run_hook "$2" "${3:-}")"
  if is_deny "$out"; then fail "$1" "expected allow, got deny: $out"; else pass "$1"; fi
}

# Default identity: the cockpit. Hermetic against CLAUDE_AGENT_ROLE too — the
# invoking shell may export CLAUDE_AGENT_ROLE=manager/producer for its own pane
# identity (SABLE-tz7h.3's producer leg reads this var), and that ambient value
# must not leak into the baseline cockpit-identity assertions below.
export CLAUDE_AGENT_NAME=cockpit
unset CLAUDE_AGENT_ROLE 2>/dev/null || true
unset SABLE_ORCHESTRATION_FORCE 2>/dev/null || true

# ---------- PLANNING mode ----------
set_mode planning

assert_deny  "planning blocks optimus spawn"  'CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager claude'
assert_deny  "planning blocks tarzan spawn"   'CLAUDE_AGENT_NAME=tarzan CLAUDE_AGENT_ROLE=manager claude'
assert_deny  "planning blocks chuck spawn"    'CLAUDE_AGENT_NAME=chuck CLAUDE_AGENT_ROLE=manager claude'
assert_deny  "planning blocks alias optimus"  'optimus'
assert_deny  "planning blocks git push"       'git push'
assert_deny  "planning blocks git push origin" 'git push origin personal-tooling'
assert_allow "planning allows producer spawn" 'CLAUDE_AGENT_NAME=sherlock CLAUDE_AGENT_ROLE=auditor claude src/auth'
assert_allow "planning allows benign command" 'ls -la'
assert_allow "planning allows bd commands"    'bd ready'

# Soft override
assert_allow "planning --force allows manager" 'CLAUDE_AGENT_NAME=optimus claude --force'
out_env="$(printf '%s' '{"tool_input":{"command":"CLAUDE_AGENT_NAME=optimus claude"}}' | SABLE_ORCHESTRATION_FORCE=1 bash "$HOOK" 2>/dev/null)"
if is_deny "$out_env"; then fail "planning SABLE_ORCHESTRATION_FORCE=1 allows manager" "got deny"; else pass "planning SABLE_ORCHESTRATION_FORCE=1 allows manager"; fi

# ---------- PLANNING substage gate (backlog population) ----------
# The cockpit may stand up the BARE epic shell early (planning home for
# producers / the framing artifact), but cannot POPULATE the implementation
# backlog — --parent children, or --graph / --file batches — until substage ==
# decomposition. Plain artifact-bead creates the upstream stages need stay
# allowed throughout.
set_substage() { "$MODE_BIN" substage set "$1" >/dev/null 2>&1; }

set_mode planning   # re-initializes substage=framing

assert_allow "framing allows bare epic shell"    'bd create --type=epic --title="x"'
assert_allow "framing allows bare epic (-t)"     'bd create -t epic --title="x"'
assert_deny  "framing blocks graph create"       'bd create --graph /tmp/plan.json --description "see nodes"'
assert_deny  "framing blocks file create"        'bd create --file /tmp/beads.md'
assert_deny  "framing blocks child (--parent)"   'bd create --type=task --parent=SABLE-ni8 --title="x"'
assert_allow "framing allows plain task create"  'bd create --type=task --title="framing note" --description="src/foo.py; test spec"'
assert_allow "framing allows non-create bd"      'bd update SABLE-x --claim'

set_substage research
assert_deny  "research blocks child create"      'bd create --type=task --parent=SABLE-ni8 --title="x"'
set_substage architecture
assert_deny  "architecture blocks graph create"  'bd create --graph /tmp/plan.json'
set_substage test-strategy
assert_deny  "test-strategy blocks file create"  'bd create --file /tmp/beads.md'

set_substage decomposition
assert_allow "decomposition allows child create" 'bd create --type=task --parent=SABLE-ni8 --title="x"'
assert_allow "decomposition allows graph create" 'bd create --graph /tmp/plan.json'
assert_allow "decomposition allows file create"  'bd create --file /tmp/beads.md'

# soft override regardless of substage (use a gated shape — bare epic is always allowed)
set_mode planning   # back to framing
assert_allow "framing --force allows child create" 'bd create --type=task --parent=SABLE-ni8 --title="x" --force'

# not the cockpit → no-op even on a gated child-create in framing
out_nc="$(printf '%s' '{"tool_input":{"command":"bd create --type=task --parent=SABLE-ni8 --title=x"}}' | CLAUDE_AGENT_NAME=sherlock bash "$HOOK" 2>/dev/null)"
if is_deny "$out_nc"; then fail "framing child-create no-op when not cockpit" "got deny"; else pass "framing child-create no-op when not cockpit"; fi

# ---------- EXECUTION mode ----------
set_mode execution

assert_deny  "execution blocks sherlock spawn" 'CLAUDE_AGENT_NAME=sherlock CLAUDE_AGENT_ROLE=auditor claude src/auth'
assert_deny  "execution blocks victor spawn"   'CLAUDE_AGENT_NAME=victor CLAUDE_AGENT_ROLE=bead_validator claude'
assert_deny  "execution blocks columbo spawn"  'CLAUDE_AGENT_NAME=columbo CLAUDE_AGENT_ROLE=test_planner claude'
assert_deny  "execution blocks gaudi spawn"    'CLAUDE_AGENT_NAME=gaudi claude --audit src'
assert_allow "execution allows manager spawn"  'CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager claude'
assert_allow "execution allows git push"       'git push origin personal-tooling'
assert_allow "execution --force allows producer" 'CLAUDE_AGENT_NAME=sherlock claude --force'

# ---------- sable-mode exemption (SABLE-rsvu) ----------
# sable-mode is the sanctioned mode-transition command itself; its own flags
# legitimately carry agent/producer names (e.g. --fleet victor) that must NOT
# trip the launch classifier — a false-positive on this command would block
# the exact command /sable-plan and /sable-execute instruct callers to run.
assert_allow "execution allows sable-mode --fleet victor" 'sable-mode set planning --tier quick --fleet victor'
assert_allow "execution allows sable-mode --fleet columbo" 'sable-mode set planning --tier quick --fleet columbo'
# A genuine producer launch (not wrapped in sable-mode) is still denied.
assert_deny  "execution still blocks bare victor alias" 'victor'
set_mode planning
assert_allow "planning allows sable-mode --fleet optimus" 'sable-mode set execution --fleet optimus'
assert_deny  "planning still blocks bare optimus alias" 'optimus'
set_mode execution

# ---------- SABLE_ORCHESTRATION_FORCE=1 inline command-prefix override (SABLE-rsvu) ----------
# The top-of-script check only reads the hook process's OWN env; an assignment
# prefixed inline to the command text (as a user would type in a Bash tool
# call) only applies to the subprocess the command later execs, so it must be
# parsed out of the command string on the Bash leg too — mirroring --force.
out_inline="$(printf '%s' '{"tool_input":{"command":"SABLE_ORCHESTRATION_FORCE=1 victor"}}' | bash "$HOOK" 2>/dev/null)"
if is_deny "$out_inline"; then fail "execution SABLE_ORCHESTRATION_FORCE=1 inline prefix allows producer" "got deny: $out_inline"; else pass "execution SABLE_ORCHESTRATION_FORCE=1 inline prefix allows producer"; fi
out_inline2="$(printf '%s' '{"tool_input":{"command":"SABLE_ORCHESTRATION_FORCE=1 sable-mode set planning --fleet victor"}}' | bash "$HOOK" 2>/dev/null)"
if is_deny "$out_inline2"; then fail "execution SABLE_ORCHESTRATION_FORCE=1 inline prefix allows sable-mode" "got deny: $out_inline2"; else pass "execution SABLE_ORCHESTRATION_FORCE=1 inline prefix allows sable-mode"; fi

# ---------- No-op contexts ----------
set_mode planning

# subagent context: agent_id present → no-op even though it would otherwise deny
assert_allow "no-op in subagent (agent_id present)" 'CLAUDE_AGENT_NAME=optimus claude' "sub-123"

# not the cockpit → no-op (override the exported identity for this call only)
out_noncockpit="$(printf '%s' '{"tool_input":{"command":"git push"}}' | CLAUDE_AGENT_NAME=optimus bash "$HOOK" 2>/dev/null)"
if is_deny "$out_noncockpit"; then fail "no-op when not cockpit" "got deny"; else pass "no-op when not cockpit"; fi

out_unset="$(printf '%s' '{"tool_input":{"command":"git push"}}' | env -u CLAUDE_AGENT_NAME bash "$HOOK" 2>/dev/null)"
if is_deny "$out_unset"; then fail "no-op when agent name unset" "got deny"; else pass "no-op when agent name unset"; fi

# no mode set → allow
clear_mode
assert_allow "no mode set allows everything" 'CLAUDE_AGENT_NAME=optimus claude'

# ---------- runtime enable gate (SABLE_ORCHESTRATION) ----------
# In planning mode a manager spawn would normally be denied; with the cockpit
# disabled the interlock must no-op entirely (SABLE-cav.7).
set_mode planning
out_disabled="$(printf '%s' '{"tool_input":{"command":"CLAUDE_AGENT_NAME=optimus claude"}}' | SABLE_ORCHESTRATION=off bash "$HOOK" 2>/dev/null)"
if is_deny "$out_disabled"; then fail "SABLE_ORCHESTRATION=off no-ops the interlock" "got deny"; else pass "SABLE_ORCHESTRATION=off no-ops the interlock"; fi

# ---------- v2: lincoln identity + Agent-tool leg (SABLE-uz9.5) ----------

# agent_json <subagent_type> [agent_id] [agent_type] → hook input for an Agent spawn
agent_json() {
  python3 -c "
import json, sys
d = {'tool_name': 'Agent', 'tool_input': {'subagent_type': sys.argv[1], 'prompt': 'work', 'description': 'spawn'}}
if len(sys.argv) > 2 and sys.argv[2]:
    d['agent_id'] = sys.argv[2]
if len(sys.argv) > 3 and sys.argv[3]:
    d['agent_type'] = sys.argv[3]
print(json.dumps(d))
" "$1" "${2:-}" "${3:-}"
}

run_agent() { # <subagent_type> <env_name> [agent_id]
  agent_json "$1" "${3:-}" | CLAUDE_AGENT_NAME="$2" bash "$HOOK" 2>/dev/null
}

# run_agent_t <subagent_type> <env_name> <agent_id> <agent_type> [extra_env]
# The v3 native-dispatch shape: the spawner is a subagent (agent_id present)
# carrying its own agent_type. env_name is the inherited PARENT env identity.
run_agent_t() {
  agent_json "$1" "$3" "$4" | CLAUDE_AGENT_NAME="$2" ${5:-} bash "$HOOK" 2>/dev/null
}

set_mode planning

out="$(run_agent optimus lincoln)"
if is_deny "$out"; then pass "v2 planning blocks Agent spawn of optimus (lincoln identity)"; else fail "v2 planning blocks Agent spawn of optimus (lincoln identity)" "got: ${out:-<empty>}"; fi

out="$(run_agent sherlock lincoln)"
if is_deny "$out"; then fail "v2 planning allows Agent spawn of sherlock" "got deny"; else pass "v2 planning allows Agent spawn of sherlock"; fi

out="$(run_agent Explore lincoln)"
if is_deny "$out"; then fail "v2 planning allows Agent spawn of Explore" "got deny"; else pass "v2 planning allows Agent spawn of Explore"; fi

# lincoln identity also governed on the Bash leg
out_lb="$(printf '%s' '{"tool_input":{"command":"git push"}}' | CLAUDE_AGENT_NAME=lincoln bash "$HOOK" 2>/dev/null)"
if is_deny "$out_lb"; then pass "v2 planning blocks git push under lincoln identity"; else fail "v2 planning blocks git push under lincoln identity" "got: ${out_lb:-<empty>}"; fi

set_mode execution

out="$(run_agent sherlock lincoln)"
if is_deny "$out"; then pass "v2 execution blocks Agent spawn of sherlock"; else fail "v2 execution blocks Agent spawn of sherlock" "got: ${out:-<empty>}"; fi

out="$(run_agent victor cockpit)"
if is_deny "$out"; then pass "v2 execution blocks Agent spawn of victor (legacy cockpit name)"; else fail "v2 execution blocks Agent spawn of victor (legacy cockpit name)" "got: ${out:-<empty>}"; fi

out="$(run_agent optimus lincoln)"
if is_deny "$out"; then fail "v2 execution allows Agent spawn of optimus" "got deny"; else pass "v2 execution allows Agent spawn of optimus"; fi

# v3 (SABLE-4k7): registry-based classification catches rudy (quality_validator =
# producer) that the v2 hardcoded sherlock|victor|columbo list missed. rudy is a
# planning-session agent and must be blocked in execution.
out="$(run_agent rudy lincoln)"
if is_deny "$out"; then pass "v3 execution blocks Agent spawn of rudy (quality_validator = producer)"; else fail "v3 execution blocks Agent spawn of rudy (quality_validator = producer)" "got: ${out:-<empty>}"; fi

# Agent leg (v3 SABLE-4k7): the blanket subagent no-op is GONE — a subagent
# spawning a PRODUCER in execution is DENIED (governance extends tree-wide).
out="$(run_agent sherlock lincoln sub-9)"
if is_deny "$out"; then pass "v3 execution: subagent spawning a producer (sherlock) is DENIED"; else fail "v3 execution: subagent spawning a producer (sherlock) is DENIED" "got: ${out:-<empty>}"; fi

# Native dispatch (SABLE-uz9.13): a manager-subagent spawning its own worker is
# a subagent-context Agent call (agent_id present); the interlock must NOT block
# it in execution mode, even under the governed lincoln identity.
out="$(run_agent general-purpose lincoln mgr-sub-1)"
if is_deny "$out"; then fail "execution allows manager-subagent worker spawn (general-purpose, agent_id)" "got deny"; else pass "execution allows manager-subagent worker spawn (general-purpose, agent_id)"; fi

# Agent leg: non-cockpit identity no-op (a legacy env-manager terminal with no
# agent_id is neither the cockpit seat nor a subagent → Agent leg ungoverned).
out="$(run_agent sherlock optimus)"
if is_deny "$out"; then fail "v3 Agent leg no-op for non-cockpit env identity" "got deny"; else pass "v3 Agent leg no-op for non-cockpit env identity"; fi

# ===================================================================
# v3 enforcement matrix — SABLE-4k7 / SABLE-zf1
# The Agent leg now governs the WHOLE subtree, not just the main session.
# Spawner dimension: main (cockpit seat, no agent_id) vs subagent (agent_id).
# Target dimension (registry type): manager | producer | unregistered(free).
#   PLANNING:  manager target → DENY any spawner;
#              producer target → ALLOW main, DENY subagent; unregistered → FREE.
#   EXECUTION: manager target → ALLOW main only, DENY subagent;
#              producer target → DENY any spawner; unregistered → FREE (worker lane).
# Main-session cells (1,5) are covered by the regression assertions above.
# ===================================================================

# ---- PLANNING subagent-spawner cells ----
set_mode planning

# (2) manager-typed subagent spawning a manager → DENY
out="$(run_agent_t optimus lincoln mgr-sub-1 tarzan)"
if is_deny "$out"; then pass "planning: manager-subagent (tarzan) spawning a manager (optimus) DENIED"; else fail "planning: manager-subagent (tarzan) spawning a manager (optimus) DENIED" "got: ${out:-<empty>}"; fi

# (3) producer-typed subagent spawning a producer → DENY
out="$(run_agent_t victor lincoln prod-sub-1 sherlock)"
if is_deny "$out"; then pass "planning: producer-subagent (sherlock) spawning a producer (victor) DENIED"; else fail "planning: producer-subagent (sherlock) spawning a producer (victor) DENIED" "got: ${out:-<empty>}"; fi

# (4) producer subagent spawning Explore (unregistered) → ALLOW
out="$(run_agent_t Explore lincoln prod-sub-2 sherlock)"
if is_deny "$out"; then fail "planning: producer-subagent spawning Explore (unregistered) ALLOWED" "got deny: $out"; else pass "planning: producer-subagent spawning Explore (unregistered) ALLOWED"; fi

# ---- EXECUTION subagent-spawner cells ----
set_mode execution

# (6) manager subagent spawning a manager → DENY (only the main session spawns the fleet)
out="$(run_agent_t optimus lincoln mgr-sub-2 tarzan)"
if is_deny "$out"; then pass "execution: manager-subagent (tarzan) spawning a manager (optimus) DENIED"; else fail "execution: manager-subagent (tarzan) spawning a manager (optimus) DENIED" "got: ${out:-<empty>}"; fi

# (7) manager subagent spawning a producer → DENY (execution drains, never fills)
out="$(run_agent_t sherlock lincoln mgr-sub-3 tarzan)"
if is_deny "$out"; then pass "execution: manager-subagent (tarzan) spawning a producer (sherlock) DENIED"; else fail "execution: manager-subagent (tarzan) spawning a producer (sherlock) DENIED" "got: ${out:-<empty>}"; fi

# (8) manager subagent spawning unregistered worker types → ALLOW (THE load-bearing
# v3 allow: the lane manager worker dispatches ride on; a false deny bricks v3).
out="$(run_agent_t general-purpose lincoln mgr-sub-4 tarzan)"
if is_deny "$out"; then fail "execution: manager-subagent spawning general-purpose worker ALLOWED" "got deny: $out"; else pass "execution: manager-subagent spawning general-purpose worker ALLOWED"; fi
out="$(run_agent_t Explore lincoln mgr-sub-5 tarzan)"
if is_deny "$out"; then fail "execution: manager-subagent spawning Explore worker ALLOWED" "got deny: $out"; else pass "execution: manager-subagent spawning Explore worker ALLOWED"; fi

# (9) override: SABLE_ORCHESTRATION_FORCE=1 flips a subagent-spawner deny to allow
out="$(agent_json sherlock mgr-sub-6 tarzan | SABLE_ORCHESTRATION_FORCE=1 CLAUDE_AGENT_NAME=lincoln bash "$HOOK" 2>/dev/null)"
if is_deny "$out"; then fail "execution: SABLE_ORCHESTRATION_FORCE=1 flips subagent deny to allow" "got deny: $out"; else pass "execution: SABLE_ORCHESTRATION_FORCE=1 flips subagent deny to allow"; fi

# (10) mode-file lifecycle: missing file and malformed JSON both leave it inert
clear_mode
out="$(run_agent_t sherlock lincoln mgr-sub-7 tarzan)"
if is_deny "$out"; then fail "lifecycle: missing mode file → interlock inert (allow)" "got deny: $out"; else pass "lifecycle: missing mode file → interlock inert (allow)"; fi
printf '%s' '{broken json' > "$SABLE_MODE_STATE"
out="$(run_agent_t sherlock lincoln mgr-sub-8 tarzan)"
if is_deny "$out"; then fail "lifecycle: malformed mode file → fail open (allow)" "got deny: $out"; else pass "lifecycle: malformed mode file → fail open (allow)"; fi

# (11) mode flip mid-session honored without caching: same input, decisions track the file
set_mode planning
o1="$(run_agent sherlock lincoln)"   # planning + producer + main → allow
set_mode execution
o2="$(run_agent sherlock lincoln)"   # execution + producer + main → deny
set_mode planning
o3="$(run_agent sherlock lincoln)"   # planning again → allow
if ! is_deny "$o1" && is_deny "$o2" && ! is_deny "$o3"; then
  pass "mode flip mid-session honored without caching (allow → deny → allow)"
else
  fail "mode flip mid-session honored without caching (allow → deny → allow)" "o1=$(is_deny "$o1" && echo deny || echo allow) o2=$(is_deny "$o2" && echo deny || echo allow) o3=$(is_deny "$o3" && echo deny || echo allow)"
fi

# (12) fail open: agents.yaml unreadable → allow, and stdin is drained (no BrokenPipeError)
set_mode execution
err="$(agent_json sherlock mgr-sub-9 tarzan | SABLE_AGENTS_YAML=/nonexistent/registry.yaml CLAUDE_AGENT_NAME=lincoln bash "$HOOK" 2>&1 1>/dev/null)"
out="$(agent_json sherlock mgr-sub-9 tarzan | SABLE_AGENTS_YAML=/nonexistent/registry.yaml CLAUDE_AGENT_NAME=lincoln bash "$HOOK" 2>/dev/null)"
if is_deny "$out"; then fail "fail open: unreadable registry → allow" "got deny: $out"; else pass "fail open: unreadable registry → allow"; fi
if printf '%s' "$err" | grep -q 'BrokenPipeError'; then fail "fail open: no BrokenPipeError on unreadable registry" "$err"; else pass "fail open: no BrokenPipeError on unreadable registry"; fi

set_mode planning

# ---------- worker-dispatch leg (tmux-native, SABLE-bldh.6) ----------
# sable-spawn-worker is the execution-mode dispatch path; gate it to EXECUTION
# regardless of which agent (lincoln OR a manager) invokes it. --force overrides.
set_mode planning
assert_deny  "planning blocks sable-spawn-worker (lincoln)" 'sable-spawn-worker SABLE-x --worktree /wt'
out_mgr="$(printf '%s' '{"tool_input":{"command":"sable-spawn-worker SABLE-x --worktree /wt"}}' | CLAUDE_AGENT_NAME=optimus bash "$HOOK" 2>/dev/null)"
if is_deny "$out_mgr"; then pass "planning blocks sable-spawn-worker (manager identity)"; else fail "planning blocks sable-spawn-worker (manager identity)" "got: ${out_mgr:-<empty>}"; fi
assert_allow "planning sable-spawn-worker --force overrides" 'sable-spawn-worker SABLE-x --worktree /wt --force'

set_mode execution
assert_allow "execution allows sable-spawn-worker (lincoln)" 'sable-spawn-worker SABLE-x --worktree /wt'
out_mgr_e="$(printf '%s' '{"tool_input":{"command":"sable-spawn-worker SABLE-x --worktree /wt"}}' | CLAUDE_AGENT_NAME=optimus bash "$HOOK" 2>/dev/null)"
if is_deny "$out_mgr_e"; then fail "execution allows sable-spawn-worker (manager identity)" "got deny"; else pass "execution allows sable-spawn-worker (manager identity)"; fi
set_mode planning

# ---------- manager-spawn leg (mode-neutral launch, SABLE-dqhn.2) ----------
# sable-spawn-manager stands up the execution fleet; gate it to EXECUTION.
set_mode planning
assert_deny  "planning blocks sable-spawn-manager" 'sable-spawn-manager --all'
assert_deny  "planning blocks sable-spawn-manager (single role)" 'sable-spawn-manager optimus'
assert_allow "planning sable-spawn-manager --force overrides" 'sable-spawn-manager --all --force'

set_mode execution
assert_allow "execution allows sable-spawn-manager" 'sable-spawn-manager --all'
set_mode planning

# ---------- SABLE-dzjq: Bash-leg target classification for sable-spawn-manager ----------
# The blanket execution-only deny wrongly blocked the sanctioned planning-mode
# procedure of standing up a bounded producer pane (e.g. victor) via the same
# helper. The Bash leg now reuses classify_target on the helper's role
# argument(s) and applies the same matrix the Agent leg uses.
set_mode planning
assert_allow "dzjq: planning allows sable-spawn-manager producer role (victor)" \
  'sable-spawn-manager victor --deliverable /tmp/out.md'
assert_deny  "dzjq: planning denies sable-spawn-manager manager role (tarzan)" \
  'sable-spawn-manager tarzan'
assert_deny  "dzjq: planning denies sable-spawn-manager --all" 'sable-spawn-manager --all'
assert_allow "dzjq: planning allows sable-spawn-manager unregistered role" \
  'sable-spawn-manager nobody-registered'
out_dzjq_p="$(run_hook 'sable-spawn-manager tarzan')"
if printf '%s' "$out_dzjq_p" | grep -q 'manager pane'; then pass "dzjq: planning deny message names the manager classification"; else fail "dzjq: planning deny message names the manager classification" "got: $out_dzjq_p"; fi

set_mode execution
assert_allow "dzjq: execution allows sable-spawn-manager manager role (chuck)" \
  'sable-spawn-manager chuck'
assert_allow "dzjq: execution allows sable-spawn-manager --all" 'sable-spawn-manager --all'
assert_deny  "dzjq: execution denies sable-spawn-manager producer role (victor)" \
  'sable-spawn-manager victor --deliverable /tmp/out.md'
assert_allow "dzjq: execution allows sable-spawn-manager unregistered role" \
  'sable-spawn-manager nobody-registered'
out_dzjq_e="$(run_hook 'sable-spawn-manager victor --deliverable /tmp/out.md')"
if printf '%s' "$out_dzjq_e" | grep -q 'producer pane'; then pass "dzjq: execution deny message names the producer classification"; else fail "dzjq: execution deny message names the producer classification" "got: $out_dzjq_e"; fi

# sable-spawn-manager --force still overrides the classification (unaffected by dzjq)
set_mode planning
assert_allow "dzjq: planning sable-spawn-manager producer --force overrides" \
  'sable-spawn-manager victor --deliverable /tmp/out.md --force'
set_mode planning

# ---------- SABLE-pi5m: spawn-helper false positives + FORCE override ----------
# Two defects fixed here:
#  (1) The spawn legs matched the helper's name ANYWHERE on the command line —
#      including inside a quoted `bd create --description` or a `sable-note` body —
#      because the old regex let a plain space count as a command boundary, so the
#      name-in-prose was indistinguishable from an invocation. Now the helper must
#      sit in command-word position, and bd / sable-note / sable-mode leading
#      commands are allow-listed (their args legitimately name the helper in prose,
#      often with shell punctuation).
#  (2) The documented inline `SABLE_ORCHESTRATION_FORCE=1 <cmd>` prefix did NOT
#      override a spawn deny — it was parsed only in the main Bash leg, which the
#      spawn legs short-circuit past. It is now honored before the spawn legs.
set_mode planning

# (1) quoted-prose false positives — planning must ALLOW these
assert_allow "pi5m: planning allows bd create naming sable-spawn-worker in --description" \
  'bd create --type=task --title="x" --description="dispatch a worker via sable-spawn-worker in execution"'
assert_allow "pi5m: planning allows bd create naming helper with ; in --description" \
  'bd create --type=task --title="x" --description="repro: bd create; sable-spawn-worker fails"'
assert_allow "pi5m: planning allows sable-note naming sable-spawn-worker" \
  'sable-note "mode-interlock false positive on sable-spawn-worker prose"'
assert_allow "pi5m: planning allows sable-note naming sable-spawn-manager" \
  'sable-note "the sable-spawn-manager helper over-matched the same way"'
# decomposition: a --parent child (backlog gate open) naming the helper in prose
# — the real 2026-07-06 scenario (authoring epic children) — is ALLOWED.
set_substage decomposition
assert_allow "pi5m: decomposition allows --parent child naming sable-spawn-worker" \
  'bd create --type=task --parent=SABLE-qa4d --title="x" --description="child dispatched by sable-spawn-worker helper"'
set_mode planning   # back to framing

# (1) real invocations still DENIED in planning (command-word position preserved)
assert_deny  "pi5m: planning still blocks real sable-spawn-worker" 'sable-spawn-worker SABLE-x --worktree /wt'
assert_deny  "pi5m: planning still blocks a chained real sable-spawn-worker" 'echo hi && sable-spawn-worker SABLE-x'
assert_deny  "pi5m: planning still blocks real sable-spawn-manager" 'sable-spawn-manager --all'

# (2) inline SABLE_ORCHESTRATION_FORCE=1 prefix overrides a spawn deny
assert_allow "pi5m: inline FORCE prefix allows real sable-spawn-worker" \
  'SABLE_ORCHESTRATION_FORCE=1 sable-spawn-worker SABLE-x --worktree /wt'
assert_allow "pi5m: inline FORCE prefix allows real sable-spawn-manager" \
  'SABLE_ORCHESTRATION_FORCE=1 sable-spawn-manager --all'
# process-env FORCE form also overrides a spawn deny (top-of-script check)
out_pi5m_env="$(printf '%s' '{"tool_input":{"command":"sable-spawn-worker SABLE-x --worktree /wt"}}' | SABLE_ORCHESTRATION_FORCE=1 bash "$HOOK" 2>/dev/null)"
if is_deny "$out_pi5m_env"; then fail "pi5m: env SABLE_ORCHESTRATION_FORCE=1 allows real sable-spawn-worker" "got deny: $out_pi5m_env"; else pass "pi5m: env SABLE_ORCHESTRATION_FORCE=1 allows real sable-spawn-worker"; fi

# execution mode unaffected — real spawn still allowed; prose still allowed
set_mode execution
assert_allow "pi5m: execution still allows real sable-spawn-worker" 'sable-spawn-worker SABLE-x --worktree /wt'
assert_allow "pi5m: execution allows bd create naming sable-spawn-worker" \
  'bd create --type=task --title="x" --description="names sable-spawn-worker in prose"'
set_mode planning

# ---------- SABLE-pi5m REVISE: name-leg prose FPs + assignment-prefixed spawn slip ----------
# The launches() name legs (manager/producer aliases) had the SAME plain-space
# boundary defect the spawn legs did: a manager/producer NAME appearing mid-prose
# in a message/note/description false-matched as a launch. And is_spawn_call's
# command-position regex could not see past a leading VAR=val assignment, so an
# assignment-prefixed REAL spawn invocation slipped the deny. Fixes: launches()
# now anchors to command-word position + allow-lists prose carriers (bd /
# sable-note / sable-msg via is_prose_carrier); is_spawn_call also treats
# lead==helper as an invocation.

# --- EXECUTION: message/note prose naming each producer must be ALLOWED ---
set_mode execution
assert_allow "pi5m: execution allows sable-msg prose naming sherlock (mid-sentence)" \
  'sable-msg lincoln "shipped the sherlock findings today"'
assert_allow "pi5m: execution allows sable-msg prose naming victor" \
  'sable-msg lincoln "the victor run cleared the pool"'
assert_allow "pi5m: execution allows sable-msg prose naming columbo" \
  'sable-msg lincoln "columbo planned the test coverage"'
assert_allow "pi5m: execution allows sable-msg prose naming gaudi" \
  'sable-msg lincoln "gaudi flagged an arch smell"'
assert_allow "pi5m: execution allows sable-msg prose naming several producers" \
  'sable-msg lincoln "sherlock, victor and columbo all reported in"'
assert_allow "pi5m: execution allows sable-note naming a producer" \
  'sable-note "sherlock over-matched producer names the same way"'
assert_allow "pi5m: execution allows bd create prose naming a producer" \
  'bd create --type=task --title="x" --description="follow-up from the sherlock audit"'
# real producer launches still DENIED in execution (command-word position preserved)
assert_deny  "pi5m: execution still blocks bare sherlock alias with args" 'sherlock src/auth'
assert_deny  "pi5m: execution still blocks CLAUDE_AGENT_NAME=victor launch" 'CLAUDE_AGENT_NAME=victor claude'
assert_deny  "pi5m: execution still blocks chained real gaudi launch" 'echo hi && gaudi --audit src'

# --- PLANNING: prose naming a manager must be ALLOWED ---
set_mode planning
assert_allow "pi5m: planning allows sable-msg prose naming optimus (mid-sentence)" \
  'sable-msg tarzan "ask optimus to take the epic"'
assert_allow "pi5m: planning allows sable-msg prose naming tarzan and chuck" \
  'sable-msg lincoln "tarzan and chuck are both idle"'
assert_allow "pi5m: planning allows sable-note naming a manager" \
  'sable-note "optimus lane is backed up on SABLE-qa4d"'
assert_allow "pi5m: planning allows bd create prose naming a manager" \
  'bd create --type=task --title="x" --description="hand the review to optimus"'
assert_allow "pi5m: planning allows bd create prose with CLAUDE_AGENT_NAME= assignment (not a launch)" \
  'bd create --type=task --title="x" --description="repro: CLAUDE_AGENT_NAME=optimus claude was blocked"'
# real manager launches still DENIED in planning
assert_deny  "pi5m: planning still blocks bare optimus alias" 'optimus'
assert_deny  "pi5m: planning still blocks CLAUDE_AGENT_NAME=tarzan launch" 'CLAUDE_AGENT_NAME=tarzan claude'

# --- assignment-prefixed REAL spawn-helper invocation still DENIED in planning ---
# leading_cmd strips the VAR=val prefix, so is_spawn_call's lead==helper guard
# catches these even though the position regex cannot see past the assignment.
assert_deny  "pi5m: planning blocks assignment-prefixed sable-spawn-worker" \
  'CLAUDE_AGENT_NAME=x sable-spawn-worker SABLE-y --worktree /wt'
assert_deny  "pi5m: planning blocks assignment-prefixed sable-spawn-manager" \
  'FOO=bar sable-spawn-manager --all'
set_mode planning

# ---------- SABLE-qfvn / SABLE-ykij: git-push & sable-mode prose false-positives ----------
# The git-push deny legs and the sable-mode exemption used the SAME plain-space
# [[:space:]] boundary that pi5m removed from the name/helper legs, so quoted
# prose false-matched. Three fixes, verified here:
#  (qfvn/ykij def1) planning ALLOWS bd create / sable-note / sable-msg whose PROSE
#     merely names "git push" — is_git_push tokenizes and only a real git-push
#     invocation in command-word position is denied.
#  (qfvn) a bare `git push` and a `git -C DIR push` stay DENIED (the latter was a
#     MISS in the old adjacent-only `git[[:space:]]+push` regex — a wrong-tree push).
#  (ykij def2) the sable-mode exemption is anchored to a real LEADING sable-mode
#     command, so a bd create whose description names sable-mode no longer
#     short-circuits the main leg and bypasses the backlog-population gate.
set_mode planning

# def1: prose naming "git push" is ALLOWED in planning
assert_allow "qfvn: planning allows bd create naming git push in --description" \
  'bd create --type=task --title="x" --description="then run git push after the remote-branch cleanup"'
assert_allow "ykij: planning allows sable-note naming git push" \
  'sable-note "mode-interlock false positive: git push blocked in prose"'
assert_allow "ykij: planning allows sable-msg prose naming git push (mid-sentence)" \
  'sable-msg optimus "remember to git push after execute"'
assert_allow "qfvn: planning allows plain bd create naming git push in prose" \
  'bd create --type=task --title="note" --description="repro: git push was denied here"'

# qfvn: real pushes still DENIED in planning, including git -C DIR push (old MISS)
assert_deny  "qfvn: planning still blocks bare git push"         'git push'
assert_deny  "qfvn: planning still blocks git -C dir push"       'git -C /home/ddc/dev-environment/wk-x push'
assert_deny  "qfvn: planning still blocks chained git push"      'bd create --type=task --title="x" && git push'
assert_deny  "qfvn: planning still blocks env-prefixed git push" 'GIT_SSH_COMMAND=x git push origin wk-y'

# ---------- SABLE-f5m0: git-SUBCOMMAND false positives on a bare 'push' token ----------
# The qfvn/ykij tokenizer checked `seg[i]=='git' and 'push' in seg[i+1:]` — push
# as ANY later token in the segment, not the git SUBCOMMAND. So a git subcommand
# that merely carries a bare "push" argument (a --grep value, a commit message,
# a branch name) was wrongly DENIED in planning. is_git_push now delegates to
# sable_is_git_push (lib-identity.sh), which only matches when the first
# non-flag token after git's global flags is exactly the subcommand `push`.
assert_allow "f5m0: planning allows git log --grep push (push is a --grep value, not the subcommand)" \
  'git log --grep push'
assert_allow "f5m0: planning allows git commit -m push (push is the commit message, not the subcommand)" \
  'git commit -m push'
assert_allow "f5m0: planning allows git checkout push (a branch literally named push)" \
  'git checkout push'
assert_allow "f5m0: planning allows git branch push (naming, not pushing, a branch)" \
  'git branch push'

# f5m0: real pushes stay DENIED — subcommand precision must not soften the gate
assert_deny  "f5m0: planning still blocks bare git push"                'git push'
assert_deny  "f5m0: planning still blocks git -C DIR push"              'git -C /home/ddc/dev-environment/wk-x push'
assert_deny  "f5m0: planning still blocks env-prefixed git push"        'GIT_SSH_COMMAND=x git push origin wk-y'
assert_deny  "f5m0: planning still blocks chained git push after bd create" \
  'bd create --type=task --title="x" && git push'

# ykij def2: sable-mode exemption anchored to a real LEADING sable-mode command.
# A bd create whose description NAMES sable-mode must NOT bypass the backlog gate.
set_mode planning   # substage=framing
assert_deny  "ykij: framing blocks --parent child whose --description names sable-mode" \
  'bd create --type=task --parent=SABLE-ni8 --title="x" --description="then run sable-mode set execution"'
assert_deny  "ykij: framing blocks graph create whose --description names sable-mode" \
  'bd create --graph /tmp/plan.json --description="advance via sable-mode substage advance"'
# a REAL sable-mode invocation stays exempt (and does not trip the backlog gate)
assert_allow "ykij: framing allows a real sable-mode set command"      'sable-mode set execution'
assert_allow "ykij: framing allows a real sable-mode substage advance" 'sable-mode substage advance'
# ykij def2 corollary: a chained `git push && sable-mode …` must NOT win exemption
# for its push (leading_cmd is git, not sable-mode)
assert_deny  "ykij: planning blocks git push chained before a real sable-mode" \
  'git push && sable-mode set execution'

# execution mode: prose still allowed, real push still allowed (regression)
set_mode execution
assert_allow "qfvn: execution allows sable-msg prose naming git push" \
  'sable-msg lincoln "worker will git push its branch"'
assert_allow "qfvn: execution still allows real git push"            'git push origin wk-y'
set_mode planning

# ---------- SABLE-tz7h.3: producer identity deny-leg ----------
# CLAUDE_AGENT_ROLE=producer (sherlock/victor/columbo/gaudi/rudy) is a
# read-only analysis identity: denied from dispatching workers, standing up
# the fleet, or pushing code, regardless of MODE. Read-only ops (bead JSON
# export, repo greps, file reads) and non-producer identities running the
# identical commands stay unaffected — a regression check, not just a new
# feature check.
run_hook_role() {
  # $1=command $2=role(CLAUDE_AGENT_ROLE) $3=name(CLAUDE_AGENT_NAME, optional)
  python3 -c "
import json, sys
print(json.dumps({'tool_input': {'command': sys.argv[1]}}))
" "$1" | CLAUDE_AGENT_ROLE="$2" CLAUDE_AGENT_NAME="${3:-sherlock}" bash "$HOOK" 2>/dev/null
}
assert_deny_role() {
  local out; out="$(run_hook_role "$2" "${3:-producer}" "${4:-}")"
  if is_deny "$out"; then pass "$1"; else fail "$1" "expected deny, got: ${out:-<empty>}"; fi
}
assert_allow_role() {
  local out; out="$(run_hook_role "$2" "${3:-producer}" "${4:-}")"
  if is_deny "$out"; then fail "$1" "expected allow, got deny: $out"; else pass "$1"; fi
}

# (a) spawn-helper deny — both mode values (execution would otherwise ALLOW
# these for any other identity; the producer leg must still deny)
set_mode planning
assert_deny_role "producer denied sable-spawn-worker (planning)"  'sable-spawn-worker SABLE-x --worktree /wt'
assert_deny_role "producer denied sable-spawn-manager (planning)" 'sable-spawn-manager --all'
set_mode execution
assert_deny_role "producer denied sable-spawn-worker (execution, mode would otherwise allow)"  'sable-spawn-worker SABLE-x --worktree /wt'
assert_deny_role "producer denied sable-spawn-manager (execution, mode would otherwise allow)" 'sable-spawn-manager --all'

# (b) git push deny — both mode values, including execution where a
# non-producer's git push is allowed (see "execution allows git push" above)
assert_deny_role "producer denied git push (execution, lincoln's push is allowed here)" 'git push origin wk-prodlock'
set_mode planning
assert_deny_role "producer denied git push (planning)" 'git push'

# contract: --force does NOT override the producer leg (env-prefix only)
assert_deny_role "producer sable-spawn-worker --force still denied (no flag override)" \
  'sable-spawn-worker SABLE-x --worktree /wt --force'
assert_deny_role "producer git push --force still denied (no flag override)" \
  'git push --force origin wk-prodlock'

# (c) read-only ops explicitly allowed
assert_allow_role "producer allowed bd ready (read-only)"          'bd ready'
assert_allow_role "producer allowed bead JSON export"              'bd show SABLE-tz7h.3 --json'
assert_allow_role "producer allowed repo grep"                     'grep -rn TODO src/'
assert_allow_role "producer allowed file read"                     'cat hooks/multi-manager/mode-interlock.sh'
assert_allow_role "producer allowed git status (read-only git)"    'git status'

# override: SABLE_ORCHESTRATION_FORCE=1, env form and inline command-prefix form
out_prod_env="$(printf '%s' '{"tool_input":{"command":"sable-spawn-worker SABLE-x --worktree /wt"}}' | SABLE_ORCHESTRATION_FORCE=1 CLAUDE_AGENT_ROLE=producer CLAUDE_AGENT_NAME=sherlock bash "$HOOK" 2>/dev/null)"
if is_deny "$out_prod_env"; then fail "producer SABLE_ORCHESTRATION_FORCE=1 env overrides spawn deny" "got deny: $out_prod_env"; else pass "producer SABLE_ORCHESTRATION_FORCE=1 env overrides spawn deny"; fi
out_prod_env_push="$(printf '%s' '{"tool_input":{"command":"git push"}}' | SABLE_ORCHESTRATION_FORCE=1 CLAUDE_AGENT_ROLE=producer CLAUDE_AGENT_NAME=sherlock bash "$HOOK" 2>/dev/null)"
if is_deny "$out_prod_env_push"; then fail "producer SABLE_ORCHESTRATION_FORCE=1 env overrides git push deny" "got deny: $out_prod_env_push"; else pass "producer SABLE_ORCHESTRATION_FORCE=1 env overrides git push deny"; fi
out_prod_inline="$(printf '%s' '{"tool_input":{"command":"SABLE_ORCHESTRATION_FORCE=1 sable-spawn-worker SABLE-x --worktree /wt"}}' | CLAUDE_AGENT_ROLE=producer CLAUDE_AGENT_NAME=sherlock bash "$HOOK" 2>/dev/null)"
if is_deny "$out_prod_inline"; then fail "producer inline SABLE_ORCHESTRATION_FORCE=1 prefix overrides spawn deny" "got deny: $out_prod_inline"; else pass "producer inline SABLE_ORCHESTRATION_FORCE=1 prefix overrides spawn deny"; fi

# regression: non-producer identities (lincoln, manager) running the identical
# commands are UNAFFECTED by the new leg
set_mode execution
assert_allow "non-producer (lincoln) sable-spawn-worker unaffected (execution)" 'sable-spawn-worker SABLE-x --worktree /wt'
out_mgr_push="$(printf '%s' '{"tool_input":{"command":"git push origin wk-prodlock"}}' | CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager bash "$HOOK" 2>/dev/null)"
if is_deny "$out_mgr_push"; then fail "non-producer (manager) git push unaffected (execution)" "got deny: $out_mgr_push"; else pass "non-producer (manager) git push unaffected (execution)"; fi
set_mode planning
out_mgr_spawn="$(printf '%s' '{"tool_input":{"command":"sable-spawn-worker SABLE-x --worktree /wt --force"}}' | CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager bash "$HOOK" 2>/dev/null)"
if is_deny "$out_mgr_spawn"; then fail "non-producer (manager) sable-spawn-worker --force still honored (unaffected)" "got deny: $out_mgr_spawn"; else pass "non-producer (manager) sable-spawn-worker --force still honored (unaffected)"; fi
set_mode planning

# ---------- SABLE-qfvn / SABLE-ykij: producer leg git-push via is_git_push ----------
# The producer git-push deny leg now uses is_git_push too: prose merely NAMING
# "git push" in a read-only sable-note / sable-msg is ALLOWED, while a real push
# — including a git -C DIR push (an old adjacent-regex MISS) and a chained push —
# stays DENIED for the producer identity regardless of mode.
assert_allow_role "producer allowed sable-note naming git push (prose, read-only)" \
  'sable-note "a producer cannot git push; findings go through beads"'
assert_allow_role "producer allowed sable-msg prose naming git push" \
  'sable-msg lincoln "the worker will git push its own branch"'
assert_deny_role  "producer denied git -C dir push (old adjacent-regex MISS)" \
  'git -C /home/ddc/dev-environment/wk-x push'
assert_deny_role  "producer denied chained git push after bd create" \
  'bd create --type=task --title="x" && git push'

# SABLE-f5m0: the producer leg shares is_git_push too — a git subcommand
# carrying a bare "push" token (not the subcommand itself) must not be denied.
assert_allow_role "f5m0: producer allowed git log --grep push (push is a --grep value)" \
  'git log --grep push'
assert_allow_role "f5m0: producer allowed git commit -m push (push is the commit message)" \
  'git commit -m push'

# ---------- settings-snippet registration ----------
SNIPPET="$REPO/templates/multi-manager/settings-snippet.json"
if jq -e . "$SNIPPET" >/dev/null 2>&1; then pass "settings-snippet.json is valid JSON"; else fail "settings-snippet.json is valid JSON"; fi
if grep -q 'mode-interlock.sh' "$SNIPPET"; then pass "interlock registered in settings-snippet.json"; else fail "interlock registered in settings-snippet.json"; fi

# ---------- BrokenPipeError regression (SABLE-dc0) ----------
# Exercises the two early-exit paths (non-governed identity, subagent context)
# to confirm the hook drains stdin before exiting so the upstream python3
# writer never sees a broken pipe.  Capture stderr of each invocation and fail
# if "BrokenPipeError" appears.
_no_pipe_err() {
  local label="$1"; shift
  local err
  err="$( "$@" 2>&1 1>/dev/null )"
  if printf '%s' "$err" | grep -q 'BrokenPipeError'; then
    fail "$label" "BrokenPipeError in stderr: $err"
  else
    pass "$label"
  fi
}
set_mode planning
_no_pipe_err "no BrokenPipeError: early-exit non-governed identity" \
  bash -c 'agent_json(){ python3 -c "
import json,sys
d={\"tool_name\":\"Agent\",\"tool_input\":{\"subagent_type\":sys.argv[1],\"prompt\":\"work\",\"description\":\"spawn\"}}
if len(sys.argv)>2 and sys.argv[2]: d[\"agent_id\"]=sys.argv[2]
print(json.dumps(d))
" "$@"; }; agent_json sherlock "" | CLAUDE_AGENT_NAME=optimus bash "'"$HOOK"'" '
_no_pipe_err "no BrokenPipeError: early-exit subagent context" \
  bash -c 'agent_json(){ python3 -c "
import json,sys
d={\"tool_name\":\"Agent\",\"tool_input\":{\"subagent_type\":sys.argv[1],\"prompt\":\"work\",\"description\":\"spawn\"}}
if len(sys.argv)>2 and sys.argv[2]: d[\"agent_id\"]=sys.argv[2]
print(json.dumps(d))
" "$@"; }; SABLE_MODE_STATE="'"$SABLE_MODE_STATE"'" agent_json sherlock sub-9 | CLAUDE_AGENT_NAME=lincoln bash "'"$HOOK"'" '

# ---------- per-repo mode resolution (SABLE-5hck.3) ----------
# The interlock must read the mode of the repo the tool call runs in (hook-input
# cwd), so two sessions in different repos enforce independent modes. These cases
# run WITHOUT the global SABLE_MODE_STATE override (env -u) so the hook resolves
# from cwd. SABLE_AGENTS_YAML stays exported; CLAUDE_AGENT_NAME=cockpit is set.
mk_modes_repo() {
  local d; d="$(mktemp -d)"
  git -C "$d" init -q
  git -C "$d" -c user.email=t@t -c user.name=t commit --allow-empty -m init -q
  printf '%s\n' "$d"
}
run_hook_cwd() {
  # $1=command $2=cwd → hook stdout, run with SABLE_MODE_STATE stripped
  python3 -c "
import json, sys
print(json.dumps({'tool_input': {'command': sys.argv[1]}, 'cwd': sys.argv[2]}))
" "$1" "$2" | env -u SABLE_MODE_STATE bash "$HOOK" 2>/dev/null
}
assert_deny_cwd()  { local out; out="$(run_hook_cwd "$2" "$3")"; if is_deny "$out"; then pass "$1"; else fail "$1" "expected deny, got: ${out:-<empty>}"; fi; }
assert_allow_cwd() { local out; out="$(run_hook_cwd "$2" "$3")"; if is_deny "$out"; then fail "$1" "expected allow, got deny: $out"; else pass "$1"; fi; }

REPO_EXEC="$(mk_modes_repo)"
REPO_PLAN="$(mk_modes_repo)"
( cd "$REPO_EXEC" && env -u SABLE_MODE_STATE "$MODE_BIN" set execution >/dev/null 2>&1 )
( cd "$REPO_PLAN" && env -u SABLE_MODE_STATE "$MODE_BIN" set planning  >/dev/null 2>&1 )

# Same command, opposite verdicts depending on which repo's cwd the call carries.
assert_deny_cwd  "exec repo (by cwd) blocks producer launch" 'columbo'  "$REPO_EXEC"
assert_allow_cwd "plan repo (by cwd) allows producer launch" 'columbo'  "$REPO_PLAN"
assert_deny_cwd  "plan repo (by cwd) blocks manager launch"  'optimus'  "$REPO_PLAN"
assert_allow_cwd "exec repo (by cwd) allows manager launch"  'optimus'  "$REPO_EXEC"
assert_deny_cwd  "plan repo (by cwd) blocks git push"        'git push' "$REPO_PLAN"
assert_allow_cwd "exec repo (by cwd) allows git push"        'git push' "$REPO_EXEC"

rm -rf "$REPO_EXEC" "$REPO_PLAN"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
