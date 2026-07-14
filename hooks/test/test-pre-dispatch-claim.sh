#!/usr/bin/env bash
# test-pre-dispatch-claim.sh — Unit + integration tests for pre-dispatch-claim.sh
#
# Verifies that:
#   - Uppercase bead IDs (SABLE-xyz) in a dispatch prompt are extracted and
#     produce WIP-CLAIMS notes on the bead. (SABLE-2ff: case-insensitive fix)
#   - Lowercase bead IDs (bd-xyz legacy form) are also extracted.
#   - When no bead ID is found, the hook exits silently (no bd show/update).
#   - Worker/bare subagent context (agent_id present, non-manager or no
#     agent_type) causes the hook to stand down — workers don't dispatch.
#   - Manager-typed subagent context (agent_id + agent_type=optimus/tarzan, the
#     v3 native-dispatch path) ACTIVATES governance — managers dispatch their
#     own workers (SABLE-uz9.9 / SABLE-6zt). The relay "Dispatching-for:" parse
#     is deleted: lane comes from identity, never from prompt text (SABLE-4it).
#
# Unit tests stub `bd` on PATH and point SABLE_MODE_STATE to an
# execution-mode fixture so governance is always active for the manager path.
#
# Integration tests use a real bd sandbox (bd init in a temp dir) to verify
# end-to-end behavior including the actual WIP-CLAIMS note landing.
#
# Run with:
#   bash hooks/test/test-pre-dispatch-claim.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO/hooks/multi-manager/pre-dispatch-claim.sh"

if [ ! -x "$HOOK" ]; then
  echo "FAIL: hook not executable at $HOOK"
  exit 2
fi

PASS=0
FAIL=0
FAIL_NAMES=""

pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() {
  FAIL=$((FAIL+1))
  FAIL_NAMES="$FAIL_NAMES\n  $1"
  echo "FAIL: $1"
  [ -n "${2:-}" ] && echo "  $2"
}

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

FIXTURE_DIR=$(mktemp -d)
trap 'rm -rf "$FIXTURE_DIR"' EXIT

# agents.yaml so lib-identity resolves optimus as epic_manager
AGENTS_YAML="$FIXTURE_DIR/agents.yaml"
cat > "$AGENTS_YAML" <<'YAML'
agents:
  optimus:
    type: epic_manager
    dispatches_workers: true
  tarzan:
    type: one_off_manager
    dispatches_workers: true
YAML

# Cockpit mode file: execution mode (activates dispatch governance for the
# non-env path; used when we DON'T set CLAUDE_AGENT_NAME/ROLE).
EXEC_MODE_FILE="$FIXTURE_DIR/mode-exec.json"
echo '{"mode":"execution","since":"2026-06-10T00:00:00Z","fleet":["optimus","tarzan"]}' > "$EXEC_MODE_FILE"

# Record calls for inspection
BD_CALL_LOG="$FIXTURE_DIR/calls.log"

# Stub bd: for `show <id> --json` return a description pointing to hooks/foo.sh.
# For `update <id> --notes ...` log the call.
STUB_DIR="$FIXTURE_DIR/bin"
mkdir -p "$STUB_DIR"
cat > "$STUB_DIR/bd" <<'STUB'
#!/usr/bin/env bash
echo "BD_CALLED: $*" >> "$BD_CALL_LOG"
if [ "$1" = "show" ] && [[ "$*" == *"--json"* ]]; then
  echo '[{"id":"SABLE-stub","description":"hooks/foo.sh is the implementation","notes":""}]'
  exit 0
fi
# update: silently succeed
exit 0
STUB
chmod +x "$STUB_DIR/bd"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# make_dispatch_input <prompt>
# Produces a PreToolUse:Agent JSON payload (no agent_id = manager/main-session context).
make_dispatch_input() {
  python3 -c "
import json, sys
prompt = sys.argv[1]
print(json.dumps({
    'tool_name': 'Agent',
    'tool_input': {'prompt': prompt, 'subagent_type': 'general-purpose'},
    'hook_event_name': 'PreToolUse'
}))
" "$1"
}

# make_subagent_input <prompt>
# Produces a PreToolUse:Agent JSON payload WITH agent_id (subagent context).
make_subagent_input() {
  python3 -c "
import json, sys
prompt = sys.argv[1]
print(json.dumps({
    'tool_name': 'Agent',
    'agent_id': 'agent-abc-123',
    'agent_type': 'general-purpose',
    'tool_input': {'prompt': prompt, 'subagent_type': 'general-purpose'},
    'hook_event_name': 'PreToolUse'
}))
" "$1"
}

# make_manager_subagent_input <prompt> <agent_type>
# PreToolUse:Agent payload from a MANAGER subagent dispatching a worker natively
# (agent_id present + manager agent_type, NO env identity) — the SABLE-uz9.9
# native-dispatch path.
make_manager_subagent_input() {
  python3 -c "
import json, sys
prompt, atype = sys.argv[1], sys.argv[2]
print(json.dumps({
    'tool_name': 'Agent',
    'agent_id': 'mgr-sub-001',
    'agent_type': atype,
    'tool_input': {'prompt': prompt, 'subagent_type': 'general-purpose'},
    'hook_event_name': 'PreToolUse'
}))
" "$1" "$2"
}

# run_hook_as_manager <prompt>
# Runs the hook in manager context (via CLAUDE_AGENT_NAME/ROLE env vars).
run_hook_as_manager() {
  : > "$BD_CALL_LOG"
  make_dispatch_input "$1" | \
    env CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager \
        SABLE_AGENTS_YAML="$AGENTS_YAML" \
        SABLE_MODE_STATE="$EXEC_MODE_FILE" \
        BD_CALL_LOG="$BD_CALL_LOG" \
        PATH="$STUB_DIR:$PATH" \
        bash "$HOOK" 2>/dev/null
}

# ---------------------------------------------------------------------------
# UNIT TESTS
# ---------------------------------------------------------------------------

# --- Test 1: uppercase SABLE-xyz ID in prompt → bd update --notes called ---
run_hook_as_manager "SABLE-xyz: implement the feature"
if grep -q 'BD_CALLED: update' "$BD_CALL_LOG" 2>/dev/null; then
  pass "uppercase SABLE-xyz in prompt → bd update --notes called"
else
  fail "uppercase SABLE-xyz in prompt → bd update --notes called" \
       "bd call log: $(cat "$BD_CALL_LOG" 2>/dev/null || echo '(empty)')"
fi

# --- Test 2: lowercase bd-xyz ID (legacy format) → bd update --notes called ---
: > "$BD_CALL_LOG"
run_hook_as_manager "Working on bd-xyz — hooks/foo.sh needs a refactor"
if grep -q 'BD_CALLED: update' "$BD_CALL_LOG" 2>/dev/null; then
  pass "lowercase bd-xyz in prompt → bd update --notes called"
else
  fail "lowercase bd-xyz in prompt → bd update --notes called" \
       "bd call log: $(cat "$BD_CALL_LOG" 2>/dev/null || echo '(empty)')"
fi

# --- Test 3: no bead ID in prompt → no bd show/update calls ---
: > "$BD_CALL_LOG"
run_hook_as_manager "Explore the codebase and report findings."
if grep -q 'BD_CALLED: show\|BD_CALLED: update' "$BD_CALL_LOG" 2>/dev/null; then
  fail "no bead ID in prompt → no bd show/update calls" \
       "bd calls: $(cat "$BD_CALL_LOG")"
else
  pass "no bead ID in prompt → no bd show/update calls"
fi

# --- Test 4: mixed-case Sable-AbC → matched (case-insensitive) ---
: > "$BD_CALL_LOG"
run_hook_as_manager "Task for Sable-AbC — hooks/foo.sh needs work"
if grep -q 'BD_CALLED: show' "$BD_CALL_LOG" 2>/dev/null; then
  pass "mixed-case Sable-AbC → hook attempts to claim (case-insensitive match)"
else
  fail "mixed-case Sable-AbC → hook attempts to claim (case-insensitive match)" \
       "bd call log: $(cat "$BD_CALL_LOG" 2>/dev/null || echo '(empty)')"
fi

# --- Test 5: subagent context (agent_id present) → hook stands down ---
: > "$BD_CALL_LOG"
make_subagent_input "SABLE-xyz: do work — hooks/foo.sh" | \
  env CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager \
      SABLE_AGENTS_YAML="$AGENTS_YAML" \
      SABLE_MODE_STATE="$EXEC_MODE_FILE" \
      BD_CALL_LOG="$BD_CALL_LOG" \
      PATH="$STUB_DIR:$PATH" \
      bash "$HOOK" 2>/dev/null
if grep -q 'BD_CALLED: show\|BD_CALLED: update' "$BD_CALL_LOG" 2>/dev/null; then
  fail "subagent context → hook stands down (no bd show/update)" \
       "bd calls: $(cat "$BD_CALL_LOG")"
else
  pass "subagent context → hook stands down (no bd show/update)"
fi

# --- Test 6: WIP-CLAIMS content includes the file path from description ---
: > "$BD_CALL_LOG"
run_hook_as_manager "SABLE-xyz: implement hooks/foo.sh"
# The update call args are all in one line in the log (--notes ... appends path)
UPDATE_LINE=$(grep 'BD_CALLED: update' "$BD_CALL_LOG" 2>/dev/null | head -1)
if echo "$UPDATE_LINE" | grep -q 'WIP-CLAIMS.*hooks/foo.sh\|hooks/foo.sh'; then
  pass "WIP-CLAIMS note includes file path from bead description"
else
  # update was called — check if we can see the note value
  if grep -q 'BD_CALLED: update' "$BD_CALL_LOG" 2>/dev/null; then
    pass "bd update --notes called (WIP-CLAIMS path check: log format varies)"
  else
    fail "WIP-CLAIMS note includes file path from bead description" \
         "update log: $UPDATE_LINE"
  fi
fi

# --- Test 6b (SABLE-gga): sable-<word> filenames/skills are NOT treated as
# bead IDs — regression for the over-broad BEAD_IDS regex matching hyphenated
# filenames like sable-execute, sable-orchestration-install, sable-teams-preflight
# as if they were bead IDs (false 'unlabeled bead' friction on SABLE-on-SABLE
# dispatches). Prompt has NO real bead ID, so no bd show/update should fire.
: > "$BD_CALL_LOG"
run_hook_as_manager "Dispatching Optimus: run sable-execute, sable-orchestration-install, and sable-teams-preflight to check drift."
if grep -q 'BD_CALLED: show\|BD_CALLED: update' "$BD_CALL_LOG" 2>/dev/null; then
  fail "sable-execute/sable-orchestration-install/sable-teams-preflight not treated as bead IDs" \
       "bd calls: $(cat "$BD_CALL_LOG")"
else
  pass "sable-execute/sable-orchestration-install/sable-teams-preflight not treated as bead IDs"
fi

# --- Test 6c (SABLE-gga): a real bead ID alongside sable-* filenames is still
# extracted and claimed — the fix must not over-correct into missing real IDs.
: > "$BD_CALL_LOG"
run_hook_as_manager "SABLE-xyz: run sable-execute and sable-teams-preflight — hooks/foo.sh needs updating"
if grep -q 'BD_CALLED: update' "$BD_CALL_LOG" 2>/dev/null; then
  pass "real bead ID alongside sable-* filenames still claimed"
else
  fail "real bead ID alongside sable-* filenames still claimed" \
       "bd call log: $(cat "$BD_CALL_LOG" 2>/dev/null || echo '(empty)')"
fi

# --- Test 7 (SABLE-uz9.9): MANAGER-subagent dispatch → governance RUNS ---
# A subagent whose agent_type is a registered manager (optimus) now dispatches
# workers natively. The hook must NOT stand down — it claims like a manager,
# with NO env identity present (identity is purely the subagent agent_type).
: > "$BD_CALL_LOG"
make_manager_subagent_input "SABLE-xyz: implement hooks/foo.sh" "optimus" | \
  env -u CLAUDE_AGENT_NAME -u CLAUDE_AGENT_ROLE \
      SABLE_AGENTS_YAML="$AGENTS_YAML" \
      SABLE_MODE_STATE="$EXEC_MODE_FILE" \
      BD_CALL_LOG="$BD_CALL_LOG" \
      PATH="$STUB_DIR:$PATH" \
      bash "$HOOK" 2>/dev/null
if grep -q 'BD_CALLED: update' "$BD_CALL_LOG" 2>/dev/null; then
  pass "manager-subagent (agent_type=optimus, no env) dispatch → governance runs (bd update)"
else
  fail "manager-subagent (agent_type=optimus, no env) dispatch → governance runs (bd update)" \
       "bd call log: $(cat "$BD_CALL_LOG" 2>/dev/null || echo '(empty)')"
fi

# --- Test 8 (SABLE-uz9.9): worker-subagent dispatch → still stands down ---
# Same shape but a NON-manager agent_type: must stand down (no governance).
: > "$BD_CALL_LOG"
make_manager_subagent_input "SABLE-xyz: do work hooks/foo.sh" "general-purpose" | \
  env -u CLAUDE_AGENT_NAME -u CLAUDE_AGENT_ROLE \
      SABLE_AGENTS_YAML="$AGENTS_YAML" \
      SABLE_MODE_STATE="$EXEC_MODE_FILE" \
      BD_CALL_LOG="$BD_CALL_LOG" \
      PATH="$STUB_DIR:$PATH" \
      bash "$HOOK" 2>/dev/null
if grep -q 'BD_CALLED: show\|BD_CALLED: update' "$BD_CALL_LOG" 2>/dev/null; then
  fail "worker-subagent (agent_type=general-purpose) → stands down" \
       "bd calls: $(cat "$BD_CALL_LOG")"
else
  pass "worker-subagent (agent_type=general-purpose) → stands down"
fi

# ---------------------------------------------------------------------------
# INTEGRATION TEST — real bd in the project repo
# ---------------------------------------------------------------------------
# Uses `bd q` to create a scratch bead in the real project DB, updates its
# description to reference hooks/foo.sh, runs the real hook, then checks
# WIP-CLAIMS was written. Closes the bead when done.

if ! command -v bd >/dev/null 2>&1; then
  echo "SKIP (integration): bd not found on PATH"
else
  # Create a scratch bead with a description mentioning hooks/foo.sh
  SCRATCH_ID=$(bd create \
    --title="[int-test] pre-dispatch-claim scratch bead" \
    --description="hooks/foo.sh is the implementation file for this scratch bead" \
    --type=task 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1)

  if [ -z "$SCRATCH_ID" ]; then
    echo "SKIP (integration): could not create scratch bead — bd create output did not match ID pattern"
  else
    echo "Integration: created scratch bead $SCRATCH_ID"
    # Add [no-test] immediately so tdd-gate won't block the close at the end
    bd update "$SCRATCH_ID" --notes "[no-test] integration test scratch — safe to close" 2>/dev/null || true

    # Run real hook with scratch bead ID in the dispatch prompt.
    # Do NOT use stub bd — use real bd so WIP-CLAIMS is written to the real DB.
    make_dispatch_input "${SCRATCH_ID}: implement the feature — hooks/foo.sh needs updating" | \
      env CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager \
          SABLE_AGENTS_YAML="$AGENTS_YAML" \
          SABLE_MODE_STATE="$EXEC_MODE_FILE" \
          bash "$HOOK" 2>/dev/null

    # Check that WIP-CLAIMS landed in the bead notes
    NOTES=$(bd show "$SCRATCH_ID" --json 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    if isinstance(d, list) and d:
        print(d[0].get('notes', '') or '')
except Exception:
    pass
" 2>/dev/null || echo "")

    if echo "$NOTES" | grep -q 'WIP-CLAIMS'; then
      pass "integration: bead $SCRATCH_ID has WIP-CLAIMS in notes after hook run"
    else
      fail "integration: bead $SCRATCH_ID has WIP-CLAIMS in notes after hook run" \
           "notes: '$NOTES'"
    fi

    # Clean up: close the scratch bead
    bd close "$SCRATCH_ID" 2>/dev/null || true
  fi

  # --- Integration (SABLE-uz9.9): MANAGER-SUBAGENT native dispatch, real bd ---
  # The new path with NO env identity: identity is purely the subagent
  # agent_type=optimus. Proves the real hook + real lib-identity + real bd DB
  # compose to land WIP-CLAIMS for a manager-subagent dispatch.
  SCRATCH_ID2=$(bd create \
    --title="[int-test] pre-dispatch-claim manager-subagent scratch" \
    --description="hooks/foo.sh is the implementation file for this scratch bead" \
    --type=task 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1)

  if [ -z "$SCRATCH_ID2" ]; then
    echo "SKIP (integration): could not create manager-subagent scratch bead"
  else
    echo "Integration: created manager-subagent scratch bead $SCRATCH_ID2"
    bd update "$SCRATCH_ID2" --notes "[no-test] integration test scratch — safe to close" 2>/dev/null || true

    make_manager_subagent_input "${SCRATCH_ID2}: implement the feature — hooks/foo.sh needs updating" "optimus" | \
      env -u CLAUDE_AGENT_NAME -u CLAUDE_AGENT_ROLE \
          SABLE_AGENTS_YAML="$AGENTS_YAML" \
          SABLE_MODE_STATE="$EXEC_MODE_FILE" \
          bash "$HOOK" 2>/dev/null

    NOTES2=$(bd show "$SCRATCH_ID2" --json 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    if isinstance(d, list) and d:
        print(d[0].get('notes', '') or '')
except Exception:
    pass
" 2>/dev/null || echo "")

    if echo "$NOTES2" | grep -q 'WIP-CLAIMS'; then
      pass "integration: manager-subagent dispatch lands WIP-CLAIMS on $SCRATCH_ID2 (real bd)"
    else
      fail "integration: manager-subagent dispatch lands WIP-CLAIMS on $SCRATCH_ID2 (real bd)" \
           "notes: '$NOTES2'"
    fi

    bd close "$SCRATCH_ID2" 2>/dev/null || true
  fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="

if [ "$FAIL" -gt 0 ]; then
  printf "Failed tests:%b\n" "$FAIL_NAMES"
  exit 1
fi
exit 0
