#!/usr/bin/env bash
# test-cockpit-mode-interlock.sh — Unit+integration tests for
# cockpit-mode-interlock.sh, the PreToolUse:Bash guard that makes the cockpit's
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
#     SABLE_COCKPIT_FORCE=1.
#   - No mode set → allow (nothing to enforce).
#
# Run with:
#   bash hooks/test/test-cockpit-mode-interlock.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO/hooks/multi-manager/cockpit-mode-interlock.sh"
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
SABLE_COCKPIT_STATE="$(mktemp -u)"
export SABLE_COCKPIT_STATE
trap 'rm -f "$SABLE_COCKPIT_STATE"' EXIT

set_mode() { "$MODE_BIN" set "$1" >/dev/null 2>&1; }
clear_mode() { rm -f "$SABLE_COCKPIT_STATE"; }

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

# Default identity: the cockpit.
export CLAUDE_AGENT_NAME=cockpit
unset SABLE_COCKPIT_FORCE 2>/dev/null || true

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
out_env="$(printf '%s' '{"tool_input":{"command":"CLAUDE_AGENT_NAME=optimus claude"}}' | SABLE_COCKPIT_FORCE=1 bash "$HOOK" 2>/dev/null)"
if is_deny "$out_env"; then fail "planning SABLE_COCKPIT_FORCE=1 allows manager" "got deny"; else pass "planning SABLE_COCKPIT_FORCE=1 allows manager"; fi

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

# ---------- runtime enable gate (SABLE_COCKPIT) ----------
# In planning mode a manager spawn would normally be denied; with the cockpit
# disabled the interlock must no-op entirely (SABLE-cav.7).
set_mode planning
out_disabled="$(printf '%s' '{"tool_input":{"command":"CLAUDE_AGENT_NAME=optimus claude"}}' | SABLE_COCKPIT=off bash "$HOOK" 2>/dev/null)"
if is_deny "$out_disabled"; then fail "SABLE_COCKPIT=off no-ops the interlock" "got deny"; else pass "SABLE_COCKPIT=off no-ops the interlock"; fi

# ---------- settings-snippet registration ----------
SNIPPET="$REPO/templates/multi-manager/settings-snippet.json"
if jq -e . "$SNIPPET" >/dev/null 2>&1; then pass "settings-snippet.json is valid JSON"; else fail "settings-snippet.json is valid JSON"; fi
if grep -q 'cockpit-mode-interlock.sh' "$SNIPPET"; then pass "interlock registered in settings-snippet.json"; else fail "interlock registered in settings-snippet.json"; fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
