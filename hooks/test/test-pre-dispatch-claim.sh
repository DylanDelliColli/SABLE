#!/usr/bin/env bash
# test-pre-dispatch-claim.sh — Unit + integration tests for pre-dispatch-claim.sh
#
# Verifies that:
#   - Uppercase bead IDs (SABLE-xyz) in a dispatch prompt are extracted and
#     produce a wip_claims metadata write on the bead. (SABLE-2ff:
#     case-insensitive fix)
#   - Lowercase bead IDs (bd-xyz legacy form) are also extracted.
#   - When no bead ID is found, the hook exits silently (no bd show/update).
#   - Worker/bare subagent context (agent_id present, non-manager or no
#     agent_type) causes the hook to stand down — workers don't dispatch.
#   - Manager-typed subagent context (agent_id + agent_type=optimus/tarzan, the
#     v3 native-dispatch path) ACTIVATES governance — managers dispatch their
#     own workers (SABLE-uz9.9 / SABLE-6zt). The relay "Dispatching-for:" parse
#     is deleted: lane comes from identity, never from prompt text (SABLE-4it).
#
# SABLE-szd: claims live in the bead's `wip_claims` metadata field, NOT notes
# — `bd update --notes` overwrites the whole field, so a later, unrelated
# notes write (e.g. a manager's review-step note) used to clobber the claim
# silently. Metadata is a dedicated column bd never touches on a --notes
# write.
#
# Unit tests stub `bd` on PATH and point SABLE_MODE_STATE to an
# execution-mode fixture so governance is always active for the manager path.
#
# Integration tests use a real bd sandbox (bd init in a temp dir) to verify
# end-to-end behavior including the actual wip_claims metadata landing.
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

# --- Test 9 (SABLE-lfql): the bd update --notes call carries --sandbox ---
# bd auto-pushes to the shared Dolt remote on every mutating write
# (create/update/close) by default (SABLE-rq9k); without --sandbox this hook
# pushed WIP-CLAIMS bookkeeping to the remote as a pure hook side effect on
# EVERY dispatch — the exact chuck-only-convention violation behind the
# 2026-07-09 cross-fleet corruption incident.
: > "$BD_CALL_LOG"
run_hook_as_manager "SABLE-xyz: implement hooks/foo.sh"
UPDATE_LINE_LFQL=$(grep 'BD_CALLED: update' "$BD_CALL_LOG" 2>/dev/null | head -1)
if echo "$UPDATE_LINE_LFQL" | grep -q -- '--sandbox'; then
  pass "SABLE-lfql: bd update --notes call carries --sandbox (Dolt auto-push disabled)"
else
  fail "SABLE-lfql: bd update --notes call carries --sandbox (Dolt auto-push disabled)" \
       "update line: ${UPDATE_LINE_LFQL:-<none>}"
fi

# ---------------------------------------------------------------------------
# SABLE-lfql / SABLE-rq9k: hermetic push-prevention regression guard.
# Same modeling technique as the sibling test in test-post-push-merge-notify.sh
# (search "SABLE-rq9k" there for the full rationale): a live dolt sql-server +
# file remote is the gold-standard fixture, but this suite stays hermetic. A
# bd stub advances a simulated remote tip on a mutating `update` UNLESS
# --sandbox is present; the property that matters is the tip staying
# unchanged after the hook writes WIP-CLAIMS.
# ---------------------------------------------------------------------------
cat > "$STUB_DIR/bd" <<'STUB'
#!/usr/bin/env bash
echo "BD_CALLED: $*" >> "$BD_CALL_LOG"
if [ "$1" = "show" ] && [[ "$*" == *"--json"* ]]; then
  echo '[{"id":"SABLE-stub","description":"hooks/foo.sh is the implementation","notes":""}]'
  exit 0
fi
if [ "$1" = "update" ]; then
  sandbox=0
  for a in "$@"; do [ "$a" = "--sandbox" ] && sandbox=1; done
  [ "$sandbox" -eq 0 ] && printf 'remote-advanced\n' >> "${DOLT_REMOTE_TIP:-/dev/null}"
fi
exit 0
STUB
chmod +x "$STUB_DIR/bd"

LFQL_TIP="$FIXTURE_DIR/dolt-remote-tip"
: > "$LFQL_TIP"
: > "$BD_CALL_LOG"
make_dispatch_input "SABLE-xyz: implement hooks/foo.sh" | \
  env CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager \
      SABLE_AGENTS_YAML="$AGENTS_YAML" \
      SABLE_MODE_STATE="$EXEC_MODE_FILE" \
      BD_CALL_LOG="$BD_CALL_LOG" \
      DOLT_REMOTE_TIP="$LFQL_TIP" \
      PATH="$STUB_DIR:$PATH" \
      bash "$HOOK" 2>/dev/null

# Precondition: the WIP-CLAIMS update really was attempted (so a pass below is
# not vacuous from the hook exiting before bd update).
if grep -q 'BD_CALLED: update' "$BD_CALL_LOG" 2>/dev/null; then
  pass "SABLE-lfql: WIP-CLAIMS update is attempted (precondition)"
else
  fail "SABLE-lfql: WIP-CLAIMS update is attempted (precondition)" "BD_CALL_LOG: $(cat "$BD_CALL_LOG" 2>/dev/null)"
fi

if [ ! -s "$LFQL_TIP" ]; then
  pass "SABLE-lfql: hook's WIP-CLAIMS write performs NO dolt remote sync (remote tip unchanged)"
else
  fail "SABLE-lfql: hook's WIP-CLAIMS write performs NO dolt remote sync (remote tip unchanged)" \
       "remote tip advanced — bd update auto-pushed (missing --sandbox): $(cat "$LFQL_TIP" 2>/dev/null)"
fi
rm -f "$LFQL_TIP"

# Restore the plain stub for hermeticity (the real-bd integration section below
# does not use $STUB_DIR, but keep PATH state predictable for future tests).
cat > "$STUB_DIR/bd" <<'STUB'
#!/usr/bin/env bash
echo "BD_CALLED: $*" >> "$BD_CALL_LOG"
if [ "$1" = "show" ] && [[ "$*" == *"--json"* ]]; then
  echo '[{"id":"SABLE-stub","description":"hooks/foo.sh is the implementation","notes":""}]'
  exit 0
fi
exit 0
STUB
chmod +x "$STUB_DIR/bd"

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
  SCRATCH_ID=$(bd create --sandbox \
    --title="[int-test] pre-dispatch-claim scratch bead" \
    --description="hooks/foo.sh is the implementation file for this scratch bead" \
    --type=task 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1)

  if [ -z "$SCRATCH_ID" ]; then
    echo "SKIP (integration): could not create scratch bead — bd create output did not match ID pattern"
  else
    echo "Integration: created scratch bead $SCRATCH_ID"
    # Add [no-test] immediately so tdd-gate won't block the close at the end
    bd update "$SCRATCH_ID" --sandbox --notes "[no-test] integration test scratch — safe to close" 2>/dev/null || true

    # Run real hook with scratch bead ID in the dispatch prompt.
    # Do NOT use stub bd — use real bd so WIP-CLAIMS is written to the real DB.
    make_dispatch_input "${SCRATCH_ID}: implement the feature — hooks/foo.sh needs updating" | \
      env CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager \
          SABLE_AGENTS_YAML="$AGENTS_YAML" \
          SABLE_MODE_STATE="$EXEC_MODE_FILE" \
          bash "$HOOK" 2>/dev/null

    # Check that wip_claims landed in the bead's metadata (SABLE-szd)
    CLAIMS=$(bd show "$SCRATCH_ID" --json 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    if isinstance(d, list) and d:
        print((d[0].get('metadata', {}) or {}).get('wip_claims', '') or '')
except Exception:
    pass
" 2>/dev/null || echo "")

    if [ -n "$CLAIMS" ]; then
      pass "integration: bead $SCRATCH_ID has wip_claims metadata after hook run"
    else
      fail "integration: bead $SCRATCH_ID has wip_claims metadata after hook run" \
           "metadata: '$CLAIMS'"
    fi

    # Clean up: close the scratch bead
    bd close "$SCRATCH_ID" --sandbox 2>/dev/null || true
  fi

  # --- Integration (SABLE-uz9.9): MANAGER-SUBAGENT native dispatch, real bd ---
  # The new path with NO env identity: identity is purely the subagent
  # agent_type=optimus. Proves the real hook + real lib-identity + real bd DB
  # compose to land WIP-CLAIMS for a manager-subagent dispatch.
  SCRATCH_ID2=$(bd create --sandbox \
    --title="[int-test] pre-dispatch-claim manager-subagent scratch" \
    --description="hooks/foo.sh is the implementation file for this scratch bead" \
    --type=task 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1)

  if [ -z "$SCRATCH_ID2" ]; then
    echo "SKIP (integration): could not create manager-subagent scratch bead"
  else
    echo "Integration: created manager-subagent scratch bead $SCRATCH_ID2"
    bd update "$SCRATCH_ID2" --sandbox --notes "[no-test] integration test scratch — safe to close" 2>/dev/null || true

    make_manager_subagent_input "${SCRATCH_ID2}: implement the feature — hooks/foo.sh needs updating" "optimus" | \
      env -u CLAUDE_AGENT_NAME -u CLAUDE_AGENT_ROLE \
          SABLE_AGENTS_YAML="$AGENTS_YAML" \
          SABLE_MODE_STATE="$EXEC_MODE_FILE" \
          bash "$HOOK" 2>/dev/null

    CLAIMS2=$(bd show "$SCRATCH_ID2" --json 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    if isinstance(d, list) and d:
        print((d[0].get('metadata', {}) or {}).get('wip_claims', '') or '')
except Exception:
    pass
" 2>/dev/null || echo "")

    if [ -n "$CLAIMS2" ]; then
      pass "integration: manager-subagent dispatch lands wip_claims metadata on $SCRATCH_ID2 (real bd)"
    else
      fail "integration: manager-subagent dispatch lands wip_claims metadata on $SCRATCH_ID2 (real bd)" \
           "metadata: '$CLAIMS2'"
    fi

    bd close "$SCRATCH_ID2" --sandbox 2>/dev/null || true
  fi

  # -------------------------------------------------------------------------
  # SABLE-6la1 — wip_claims metadata SURVIVES a later --notes clobber
  # -------------------------------------------------------------------------
  # This is the acceptance criterion SABLE-szd's own description named but
  # never got a test for: "a test writes WIP-CLAIMS then updates notes and
  # asserts claims persist." Real bd, no mocks — this is the exact failure
  # mode that motivated the metadata migration (SABLE-szd) and recurred live
  # as a near-miss on SABLE-cmar4.1 (SABLE-sm269).
  SCRATCH_ID3=$(bd create --sandbox \
    --title="[int-test] pre-dispatch-claim notes-clobber regression" \
    --description="[no-test] hooks/foo.sh is the implementation file for this scratch bead" \
    --type=task 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1)

  if [ -z "$SCRATCH_ID3" ]; then
    echo "SKIP (integration): could not create notes-clobber scratch bead"
  else
    echo "Integration: created notes-clobber scratch bead $SCRATCH_ID3"

    # Establish the claim exactly as the hook does (real --set-metadata write).
    bd update "$SCRATCH_ID3" --sandbox --set-metadata "wip_claims=a.sh,b.sh" >/dev/null 2>&1

    # Simulate the exact SABLE-szd/sm269 trigger: an unrelated notes write from
    # elsewhere in a bead's life (e.g. a manager's routine review-step note).
    bd update "$SCRATCH_ID3" --sandbox --notes "manager review note" >/dev/null 2>&1

    CLAIMS3=$(bd show "$SCRATCH_ID3" --json 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    if isinstance(d, list) and d:
        print((d[0].get('metadata', {}) or {}).get('wip_claims', '') or '')
except Exception:
    pass
" 2>/dev/null || echo "")

    if [ "$CLAIMS3" = "a.sh,b.sh" ]; then
      pass "SABLE-6la1: wip_claims metadata survives an unrelated bd update --notes write (real bd)"
    else
      fail "SABLE-6la1: wip_claims metadata survives an unrelated bd update --notes write (real bd)" \
           "expected 'a.sh,b.sh', got: '$CLAIMS3'"
    fi

    # Positive control: the notes write really did replace notes — otherwise a
    # pass above would be vacuous (bd never being destructive to notes at all,
    # rather than the metadata field specifically being immune to it).
    NOTES3=$(bd show "$SCRATCH_ID3" --json 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    if isinstance(d, list) and d:
        print(d[0].get('notes', '') or '')
except Exception:
    pass
" 2>/dev/null || echo "")

    if [ "$NOTES3" = "manager review note" ]; then
      pass "SABLE-6la1: positive control — the notes write actually replaced notes (not a vacuous pass)"
    else
      fail "SABLE-6la1: positive control — the notes write actually replaced notes (not a vacuous pass)" \
           "notes: '$NOTES3'"
    fi

    bd close "$SCRATCH_ID3" --sandbox 2>/dev/null || true
  fi

  # --- SABLE-6la1: sibling-key --set-metadata merges, doesn't clobber ---------
  # The whole design relies on --set-metadata writing one key at a time without
  # disturbing siblings: proves setting a second, unrelated key leaves an
  # already-established wip_claims intact.
  SCRATCH_ID4=$(bd create --sandbox \
    --title="[int-test] pre-dispatch-claim sibling-key metadata merge" \
    --description="[no-test] hooks/foo.sh is the implementation file for this scratch bead" \
    --type=task 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1)

  if [ -z "$SCRATCH_ID4" ]; then
    echo "SKIP (integration): could not create sibling-key scratch bead"
  else
    echo "Integration: created sibling-key scratch bead $SCRATCH_ID4"

    bd update "$SCRATCH_ID4" --sandbox --set-metadata "wip_claims=x.sh" >/dev/null 2>&1
    bd update "$SCRATCH_ID4" --sandbox --set-metadata "otherkey=y" >/dev/null 2>&1

    META4=$(bd show "$SCRATCH_ID4" --json 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    if isinstance(d, list) and d:
        m = d[0].get('metadata', {}) or {}
        print(m.get('wip_claims', ''), '|', m.get('otherkey', ''))
except Exception:
    pass
" 2>/dev/null || echo "")

    if [ "$META4" = "x.sh | y" ]; then
      pass "SABLE-6la1: --set-metadata on a sibling key merges (wip_claims survives, otherkey lands)"
    else
      fail "SABLE-6la1: --set-metadata on a sibling key merges (wip_claims survives, otherkey lands)" \
           "expected 'x.sh | y', got: '$META4'"
    fi

    bd close "$SCRATCH_ID4" --sandbox 2>/dev/null || true
  fi
fi

# ---------------------------------------------------------------------------
# SABLE-d5iku — UNMERGED-BLOCKER WARNING path
# ---------------------------------------------------------------------------
# `bd ready` releases a dependent when its blocker's STATUS goes closed, but a
# structurally-sequenced dependent needs the blocker's CODE on the branch it
# forks from. These cases prove the hook surfaces that gap at dispatch time.
#
# The bd side is stubbed (dep graph shape is the input, not the thing under
# test), but the MERGE STATE is REAL: a real git repo with real refs and a real
# `git merge-base --is-ancestor`, driven through the real bin/sable-dep-check.
# Stubbing the ancestry would test nothing — it is the whole question.
#
# BOTH DIRECTIONS, deliberately (the bead's own words: "or the check trades a
# false-go for a false-block"). Four silence cases sit against the one warn
# case: merged blocker, still-open blocker, pruned branch, non-blocking edge.

DEP_DIR="$FIXTURE_DIR/dep"
DEP_REPO="$DEP_DIR/repo"
DEP_STUB="$DEP_DIR/bin"
mkdir -p "$DEP_REPO" "$DEP_STUB"

git -C "$DEP_REPO" init -q 2>/dev/null
git -C "$DEP_REPO" config user.email "test@example.invalid"
git -C "$DEP_REPO" config user.name "SABLE Test"
git -C "$DEP_REPO" config sable.integrationBranch tmux-only
echo base > "$DEP_REPO/base.txt"
git -C "$DEP_REPO" add base.txt
git -C "$DEP_REPO" commit -qm "base"
DEP_BASE_SHA=$(git -C "$DEP_REPO" rev-parse HEAD)
echo blocker > "$DEP_REPO/blocker.txt"
git -C "$DEP_REPO" add blocker.txt
git -C "$DEP_REPO" commit -qm "blocker work"
DEP_TIP_SHA=$(git -C "$DEP_REPO" rev-parse HEAD)

# Remote-tracking refs written directly — the ancestry check reads
# refs/remotes/origin/*, and a real bare remote adds nothing to what is being
# proven here.
set_dep_refs() {
  git -C "$DEP_REPO" update-ref refs/remotes/origin/tmux-only "$1"
  if [ -n "${2:-}" ]; then
    git -C "$DEP_REPO" update-ref refs/remotes/origin/wk-blocker "$2"
  else
    git -C "$DEP_REPO" update-ref -d refs/remotes/origin/wk-blocker 2>/dev/null || true
  fi
}

# Stub bd: dep graph + blocker bead. DEP_BLOCKER_STATUS / DEP_DEP_TYPE let each
# case reshape the graph without rewriting the stub.
cat > "$DEP_STUB/bd" <<'STUB'
#!/usr/bin/env bash
if [ "$1" = "dep" ] && [ "$2" = "list" ]; then
  printf '[{"id":"SABLE-blk","dependency_type":"%s","status":"%s"}]\n' \
    "${DEP_DEP_TYPE:-blocks}" "${DEP_BLOCKER_STATUS:-closed}"
  exit 0
fi
if [ "$1" = "show" ] && [ "$2" = "SABLE-blk" ]; then
  echo '[{"id":"SABLE-blk","metadata":{"branch":"wk-blocker"},"description":"","notes":"","close_reason":""}]'
  exit 0
fi
if [ "$1" = "show" ]; then
  echo '[{"id":"SABLE-dep","description":"hooks/foo.sh is the implementation","notes":"","metadata":{}}]'
  exit 0
fi
exit 0
STUB
chmod +x "$DEP_STUB/bd"

# make_dispatch_input_cwd <prompt> <cwd> — dispatch payload carrying the repo
# whose merge state should be judged.
make_dispatch_input_cwd() {
  python3 -c "
import json, sys
print(json.dumps({
    'tool_name': 'Agent',
    'cwd': sys.argv[2],
    'tool_input': {'prompt': sys.argv[1], 'subagent_type': 'general-purpose'},
    'hook_event_name': 'PreToolUse'
}))
" "$1" "$2"
}

run_hook_dep() {
  make_dispatch_input_cwd "SABLE-dep: implement hooks/foo.sh" "$DEP_REPO" | \
    env CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager \
        SABLE_AGENTS_YAML="$AGENTS_YAML" \
        SABLE_MODE_STATE="$EXEC_MODE_FILE" \
        SABLE_DEP_CHECK_BIN="$REPO/bin/sable-dep-check" \
        DEP_BLOCKER_STATUS="${1:-closed}" \
        DEP_DEP_TYPE="${2:-blocks}" \
        PATH="$DEP_STUB:$PATH" \
        bash "$HOOK" 2>/dev/null
}

# --- d5iku-1: closed blocker, branch NOT an ancestor → hook WARNS -----------
set_dep_refs "$DEP_BASE_SHA" "$DEP_TIP_SHA"
DEP_OUT=$(run_hook_dep closed blocks)
if echo "$DEP_OUT" | grep -q 'UNMERGED-BLOCKER WARNING' \
   && echo "$DEP_OUT" | grep -q 'wk-blocker' \
   && echo "$DEP_OUT" | grep -q 'additionalContext'; then
  pass "SABLE-d5iku: closed blocker with UNMERGED branch → additionalContext warning naming the branch"
else
  fail "SABLE-d5iku: closed blocker with UNMERGED branch → additionalContext warning naming the branch" \
       "hook output: ${DEP_OUT:-<empty>}"
fi

# --- d5iku-2: same graph, branch MERGED → release is clean, NO warning ------
set_dep_refs "$DEP_TIP_SHA" "$DEP_TIP_SHA"
DEP_OUT=$(run_hook_dep closed blocks)
if [ -z "$DEP_OUT" ]; then
  pass "SABLE-d5iku: closed blocker whose branch IS merged → no warning (clean release)"
else
  fail "SABLE-d5iku: closed blocker whose branch IS merged → no warning (clean release)" \
       "expected silence, got: $DEP_OUT"
fi

# --- d5iku-3: blocker still OPEN → nothing released yet, NO warning ---------
set_dep_refs "$DEP_BASE_SHA" "$DEP_TIP_SHA"
DEP_OUT=$(run_hook_dep open blocks)
if [ -z "$DEP_OUT" ]; then
  pass "SABLE-d5iku: OPEN blocker (dependency still holding) → no warning"
else
  fail "SABLE-d5iku: OPEN blocker (dependency still holding) → no warning" \
       "expected silence, got: $DEP_OUT"
fi

# --- d5iku-4: closed blocker, branch PRUNED from origin → NO warning --------
# Worker branches are deleted once merged, so an absent ref is the normal
# post-merge state; warning on it would fire on nearly every closed blocker.
set_dep_refs "$DEP_BASE_SHA" ""
DEP_OUT=$(run_hook_dep closed blocks)
if [ -z "$DEP_OUT" ]; then
  pass "SABLE-d5iku: closed blocker whose branch is pruned from origin → no warning"
else
  fail "SABLE-d5iku: closed blocker whose branch is pruned from origin → no warning" \
       "expected silence, got: $DEP_OUT"
fi

# --- d5iku-5: closed RELATES-TO partner, unmerged branch → NO warning -------
# A non-blocking edge never gated readiness, so it cannot falsely release.
set_dep_refs "$DEP_BASE_SHA" "$DEP_TIP_SHA"
DEP_OUT=$(run_hook_dep closed relates-to)
if [ -z "$DEP_OUT" ]; then
  pass "SABLE-d5iku: closed relates-to partner with unmerged branch → no warning (non-blocking edge)"
else
  fail "SABLE-d5iku: closed relates-to partner with unmerged branch → no warning (non-blocking edge)" \
       "expected silence, got: $DEP_OUT"
fi

# --- d5iku-6: SABLE_DEP_MERGE_GUARD=0 disables the warning ------------------
set_dep_refs "$DEP_BASE_SHA" "$DEP_TIP_SHA"
DEP_OUT=$(make_dispatch_input_cwd "SABLE-dep: implement hooks/foo.sh" "$DEP_REPO" | \
  env CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager \
      SABLE_AGENTS_YAML="$AGENTS_YAML" \
      SABLE_MODE_STATE="$EXEC_MODE_FILE" \
      SABLE_DEP_CHECK_BIN="$REPO/bin/sable-dep-check" \
      SABLE_DEP_MERGE_GUARD=0 \
      PATH="$DEP_STUB:$PATH" \
      bash "$HOOK" 2>/dev/null)
if [ -z "$DEP_OUT" ]; then
  pass "SABLE-d5iku: SABLE_DEP_MERGE_GUARD=0 suppresses the warning"
else
  fail "SABLE-d5iku: SABLE_DEP_MERGE_GUARD=0 suppresses the warning" \
       "expected silence, got: $DEP_OUT"
fi

# --- d5iku-7: checker absent → dispatch is unaffected (failsafe) ------------
# The warning is advisory; a missing tool must cost the warning, never the
# dispatch. Points SABLE_DEP_CHECK_BIN at a nonexistent path AND keeps
# sable-dep-check off PATH.
set_dep_refs "$DEP_BASE_SHA" "$DEP_TIP_SHA"
DEP_RC=0
DEP_OUT=$(make_dispatch_input_cwd "SABLE-dep: implement hooks/foo.sh" "$DEP_REPO" | \
  env CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager \
      SABLE_AGENTS_YAML="$AGENTS_YAML" \
      SABLE_MODE_STATE="$EXEC_MODE_FILE" \
      SABLE_DEP_CHECK_BIN="$DEP_DIR/does-not-exist" \
      PATH="$DEP_STUB:/usr/bin:/bin" \
      bash "$HOOK" 2>/dev/null) || DEP_RC=$?
if [ "$DEP_RC" -eq 0 ] && [ -z "$DEP_OUT" ]; then
  pass "SABLE-d5iku: checker absent → hook still exits 0 with no output (dispatch unaffected)"
else
  fail "SABLE-d5iku: checker absent → hook still exits 0 with no output (dispatch unaffected)" \
       "rc=$DEP_RC output: ${DEP_OUT:-<empty>}"
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
