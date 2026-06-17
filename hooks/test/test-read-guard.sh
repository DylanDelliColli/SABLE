#!/usr/bin/env bash
# test-read-guard.sh — behavior tests for hooks/multi-manager/read-guard.sh
# after the SABLE-uz9.3 identity rewrite. Feeds crafted PreToolUse JSON on
# stdin and asserts deny/allow for both identity modes (agent_type subagent
# context and legacy env context).
#
# Run with:
#   bash hooks/test/test-read-guard.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO/hooks/multi-manager/read-guard.sh"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR"' EXIT
cat > "$FIXTURE_DIR/agents.yaml" <<'YAML'
agents:
  optimus:
    type: epic_manager
  tarzan:
    type: one_off_manager
  chuck:
    type: integrator
  lincoln:
    type: strategist
  sherlock:
    type: auditor
YAML
export SABLE_AGENTS_YAML="$FIXTURE_DIR/agents.yaml"

# json <agent_id> <agent_type> <command> — build hook input
json() {
  python3 -c "
import json, sys
aid, atype, cmd = sys.argv[1], sys.argv[2], sys.argv[3]
d = {'tool_name': 'Bash', 'tool_input': {'command': cmd}, 'session_id': 's1'}
if aid: d['agent_id'] = aid
if atype: d['agent_type'] = atype
print(json.dumps(d))
" "$1" "$2" "$3"
}

# run_hook <json> <env_name> <env_role> → stdout of hook
run_hook() {
  local input="$1" env_name="$2" env_role="$3"
  (
    unset CLAUDE_AGENT_NAME CLAUDE_AGENT_ROLE
    [ -n "$env_name" ] && export CLAUDE_AGENT_NAME="$env_name"
    [ -n "$env_role" ] && export CLAUDE_AGENT_ROLE="$env_role"
    printf '%s' "$input" | bash "$HOOK" 2>/dev/null
  )
}

assert_denied() { # <label> <output>
  if printf '%s' "$2" | grep -q '"permissionDecision": "deny"'; then pass "$1"; else fail "$1" "expected deny, got: ${2:-<empty>}"; fi
}
assert_allowed() { # <label> <output>
  if [ -z "$2" ]; then pass "$1"; else fail "$1" "expected silent allow, got: $2"; fi
}

# --- v2 subagent-context identities (agent_type) ---
OUT=$(run_hook "$(json a1 optimus 'bd ready -l for-tarzan')" "" "")
assert_denied "subagent optimus denied querying for-tarzan" "$OUT"

OUT=$(run_hook "$(json a1 optimus 'bd ready -l for-optimus')" "" "")
assert_allowed "subagent optimus allowed own inbox" "$OUT"

OUT=$(run_hook "$(json a1 optimus 'bd list -l for-coord')" "" "")
assert_allowed "subagent optimus allowed umbrella coord label" "$OUT"

OUT=$(run_hook "$(json a2 lincoln 'bd ready -l for-optimus')" "" "")
assert_allowed "subagent lincoln has cross-inbox read exception" "$OUT"

OUT=$(run_hook "$(json a3 sherlock 'bd ready -l for-optimus')" "" "")
assert_allowed "registered non-manager (sherlock) not subject to guard" "$OUT"

OUT=$(run_hook "$(json a4 Explore 'bd ready -l for-optimus')" "" "")
assert_allowed "unregistered worker not subject to guard" "$OUT"

# --- contamination fix: worker inside a manager terminal ---
OUT=$(run_hook "$(json a5 Explore 'bd ready -l for-tarzan')" "optimus" "manager")
assert_allowed "worker in optimus terminal resolves as worker (env ignored)" "$OUT"

# --- legacy env-context identities (dual-mode) ---
OUT=$(run_hook "$(json '' '' 'bd ready -l for-tarzan')" "optimus" "manager")
assert_denied "legacy env optimus denied querying for-tarzan" "$OUT"

OUT=$(run_hook "$(json '' '' 'bd ready -l for-chuck')" "chuck" "manager")
assert_allowed "legacy env chuck allowed own inbox" "$OUT"

OUT=$(run_hook "$(json '' '' 'bd ready -l for-optimus')" "lincoln" "manager")
assert_allowed "legacy env lincoln cross-inbox exception" "$OUT"

# SABLE-xt6: Seward retired — the cross-inbox exemption is gone. An env seward
# (manager via the legacy ROLE=manager escape) is now subject to the guard like
# any other manager: it may read its own inbox, not a peer's.
OUT=$(run_hook "$(json '' '' 'bd ready -l for-tarzan')" "seward" "manager")
assert_denied "retired seward (env manager) subject to guard — no cross-inbox bypass (SABLE-xt6)" "$OUT"

# A subagent claiming the now-unregistered agent_type=seward behaves like any
# unregistered worker (the guard governs managers only) — it is not special.
OUT=$(run_hook "$(json a8 seward 'bd ready -l for-optimus')" "" "")
assert_allowed "retired seward (agent_type) not subject to guard — like unregistered (SABLE-xt6)" "$OUT"

# --- non-matching commands stay silent ---
OUT=$(run_hook "$(json a6 optimus 'git status')" "" "")
assert_allowed "non-bd command ignored" "$OUT"

OUT=$(run_hook "$(json a7 optimus 'bd show SABLE-123')" "" "")
assert_allowed "bd show ignored (not ready/list)" "$OUT"

# --- anonymous session ---
OUT=$(run_hook "$(json '' '' 'bd ready -l for-optimus')" "" "")
assert_allowed "anonymous main session not subject to guard" "$OUT"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
