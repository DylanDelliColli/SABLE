#!/usr/bin/env bash
# test-edit-write-claim-reconciler.sh — Unit + integration tests for
# edit-write-claim-reconciler.sh
#
# Verifies that:
#   - PreToolUse:Edit/Write with agent_id + file_path + a transcript mentioning
#     a bead ID → the hook appends to wip_claims via `bd update --set-metadata`
#     (SABLE-szd: NOT `--notes`, which overwrites the whole field).
#   - No agent_id (manager/main-session context, not a subagent) → hook stands
#     down (no bd calls).
#   - No file_path / no transcript_path → hook stands down.
#   - Test/generated file paths (.test., .spec., __tests__, __pycache__,
#     .next/, node_modules/) are skipped — not claim-worthy overlap surface.
#   - A file already present in wip_claims is not re-appended.
#
# SABLE-lfql: the hook's bd update call must carry --sandbox — bd auto-pushes
# to the shared Dolt remote on every mutating write (create/update/close) by
# default (SABLE-rq9k), so without --sandbox this hook pushed WIP-CLAIMS
# bookkeeping to the remote as a pure hook side effect on EVERY Edit/Write
# while a bead is claimed — the exact chuck-only-convention violation behind
# the 2026-07-09 cross-fleet corruption incident.
#
# Unit tests stub `bd` on PATH. The push-prevention property is modeled with a
# stub that advances a simulated remote tip on a mutating `update` UNLESS
# --sandbox is present (same technique as the sibling SABLE-rq9k regression
# test in test-post-push-merge-notify.sh — a live dolt sql-server + file
# remote is the gold-standard fixture, but this suite stays hermetic; modeling
# the documented --sandbox contract keeps the case fast and CI-safe while
# still asserting the observable property: NO remote advance).
#
# Integration test uses a real bd sandbox (real project DB, --sandbox on every
# setup/teardown write so the scratch bead never touches the shared remote) to
# verify end-to-end behavior including the actual WIP-CLAIMS note landing.
#
# Run with:
#   bash hooks/test/test-edit-write-claim-reconciler.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO/hooks/multi-manager/edit-write-claim-reconciler.sh"

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

BD_CALL_LOG="$FIXTURE_DIR/calls.log"

STUB_DIR="$FIXTURE_DIR/bin"
mkdir -p "$STUB_DIR"
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

# A transcript JSONL mentioning a bead ID — the shape the hook scans (each
# line is JSON-parsed, then json.dumps()'d back out and regex-matched).
TRANSCRIPT="$FIXTURE_DIR/transcript.jsonl"
cat > "$TRANSCRIPT" <<'JSONL'
{"role":"user","content":"Please implement SABLE-stub: hooks/foo.sh needs updating"}
JSONL

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# make_edit_input <file_path> [agent_id] [transcript_path]
make_edit_input() {
  python3 -c "
import json, sys
file_path, agent_id, transcript = sys.argv[1], sys.argv[2], sys.argv[3]
d = {
    'tool_name': 'Edit',
    'tool_input': {'file_path': file_path},
    'hook_event_name': 'PreToolUse',
}
if agent_id:
    d['agent_id'] = agent_id
if transcript:
    d['transcript_path'] = transcript
print(json.dumps(d))
" "$1" "${2:-}" "${3:-}"
}

# run_hook <json>
run_hook() {
  : > "$BD_CALL_LOG"
  printf '%s' "$1" | env BD_CALL_LOG="$BD_CALL_LOG" PATH="$STUB_DIR:$PATH" bash "$HOOK" 2>/dev/null
}

restore_plain_stub() {
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
}

# ---------------------------------------------------------------------------
# UNIT TESTS
# ---------------------------------------------------------------------------

# --- Test 1: agent_id + file_path + transcript with bead ID → bd update --notes called ---
run_hook "$(make_edit_input "hooks/foo.sh" "agent-abc-123" "$TRANSCRIPT")" >/dev/null
if grep -q 'BD_CALLED: update' "$BD_CALL_LOG" 2>/dev/null; then
  pass "agent_id + file_path + transcript with bead ID → bd update --notes called"
else
  fail "agent_id + file_path + transcript with bead ID → bd update --notes called" \
       "bd call log: $(cat "$BD_CALL_LOG" 2>/dev/null || echo '(empty)')"
fi

# --- Test 2: WIP-CLAIMS note includes the file path ---
UPDATE_LINE=$(grep 'BD_CALLED: update' "$BD_CALL_LOG" 2>/dev/null | head -1)
if echo "$UPDATE_LINE" | grep -q 'hooks/foo.sh'; then
  pass "WIP-CLAIMS note includes the edited file path"
else
  fail "WIP-CLAIMS note includes the edited file path" "update line: ${UPDATE_LINE:-<none>}"
fi

# --- Test 3 (SABLE-lfql): the update call carries --sandbox ---
if echo "$UPDATE_LINE" | grep -q -- '--sandbox'; then
  pass "SABLE-lfql: bd update --notes call carries --sandbox (Dolt auto-push disabled)"
else
  fail "SABLE-lfql: bd update --notes call carries --sandbox (Dolt auto-push disabled)" \
       "update line: ${UPDATE_LINE:-<none>}"
fi

# --- Test 4: no agent_id (manager/main-session context) → hook stands down ---
run_hook "$(make_edit_input "hooks/foo.sh" "" "$TRANSCRIPT")" >/dev/null
if grep -q 'BD_CALLED:' "$BD_CALL_LOG" 2>/dev/null; then
  fail "no agent_id → hook stands down (no bd calls)" "bd calls: $(cat "$BD_CALL_LOG")"
else
  pass "no agent_id → hook stands down (no bd calls)"
fi

# --- Test 5: no file_path → hook stands down ---
run_hook "$(make_edit_input "" "agent-abc-123" "$TRANSCRIPT")" >/dev/null
if grep -q 'BD_CALLED:' "$BD_CALL_LOG" 2>/dev/null; then
  fail "no file_path → hook stands down (no bd calls)" "bd calls: $(cat "$BD_CALL_LOG")"
else
  pass "no file_path → hook stands down (no bd calls)"
fi

# --- Test 6: no transcript_path → hook stands down ---
run_hook "$(make_edit_input "hooks/foo.sh" "agent-abc-123" "")" >/dev/null
if grep -q 'BD_CALLED:' "$BD_CALL_LOG" 2>/dev/null; then
  fail "no transcript_path → hook stands down (no bd calls)" "bd calls: $(cat "$BD_CALL_LOG")"
else
  pass "no transcript_path → hook stands down (no bd calls)"
fi

# --- Test 7: test file path (.test.) is skipped ---
run_hook "$(make_edit_input "hooks/foo.test.sh" "agent-abc-123" "$TRANSCRIPT")" >/dev/null
if grep -q 'BD_CALLED:' "$BD_CALL_LOG" 2>/dev/null; then
  fail "test file path (.test.) is skipped (no bd calls)" "bd calls: $(cat "$BD_CALL_LOG")"
else
  pass "test file path (.test.) is skipped (no bd calls)"
fi

# --- Test 8: file already present in wip_claims metadata is not re-appended ---
cat > "$STUB_DIR/bd" <<'STUB'
#!/usr/bin/env bash
echo "BD_CALLED: $*" >> "$BD_CALL_LOG"
if [ "$1" = "show" ] && [[ "$*" == *"--json"* ]]; then
  echo '[{"id":"SABLE-stub","description":"hooks/foo.sh is the implementation","metadata":{"wip_claims":"hooks/foo.sh"}}]'
  exit 0
fi
exit 0
STUB
chmod +x "$STUB_DIR/bd"
run_hook "$(make_edit_input "hooks/foo.sh" "agent-abc-123" "$TRANSCRIPT")" >/dev/null
if grep -q 'BD_CALLED: update' "$BD_CALL_LOG" 2>/dev/null; then
  fail "file already in wip_claims → not re-appended (no bd update)" "bd calls: $(cat "$BD_CALL_LOG")"
else
  pass "file already in wip_claims → not re-appended (no bd update)"
fi
restore_plain_stub

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
printf '%s' "$(make_edit_input "hooks/foo.sh" "agent-abc-123" "$TRANSCRIPT")" | \
  env BD_CALL_LOG="$BD_CALL_LOG" DOLT_REMOTE_TIP="$LFQL_TIP" PATH="$STUB_DIR:$PATH" \
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
restore_plain_stub

# ---------------------------------------------------------------------------
# INTEGRATION TEST — real bd in the project repo
# ---------------------------------------------------------------------------
# Creates a scratch bead in the real, shared project Dolt db (--sandbox on
# every write so this test never pushes to the shared remote), a real
# transcript file mentioning it, runs the real hook, then checks WIP-CLAIMS
# was written. Closes the bead when done.

if ! command -v bd >/dev/null 2>&1; then
  echo "SKIP (integration): bd not found on PATH"
else
  SCRATCH_ID=$(bd create --sandbox \
    --title="[int-test] edit-write-claim-reconciler scratch bead" \
    --description="hooks/foo.sh is the implementation file for this scratch bead" \
    --type=task 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1)

  if [ -z "$SCRATCH_ID" ]; then
    echo "SKIP (integration): could not create scratch bead — bd create output did not match ID pattern"
  else
    echo "Integration: created scratch bead $SCRATCH_ID"
    # Add [no-test] immediately so tdd-gate won't block the close at the end.
    bd update "$SCRATCH_ID" --sandbox --notes "[no-test] integration test scratch — safe to close" 2>/dev/null || true

    INT_TRANSCRIPT="$FIXTURE_DIR/int-transcript.jsonl"
    printf '{"role":"user","content":"%s: implement the feature — hooks/foo.sh needs updating"}\n' "$SCRATCH_ID" > "$INT_TRANSCRIPT"

    # Run real hook (no stub bd on PATH) with the scratch bead ID reachable
    # via the transcript.
    INT_INPUT=$(make_edit_input "hooks/foo.sh" "agent-int-001" "$INT_TRANSCRIPT")
    printf '%s' "$INT_INPUT" | bash "$HOOK" 2>/dev/null

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
      fail "integration: bead $SCRATCH_ID has wip_claims metadata after hook run" "metadata: '$CLAIMS'"
    fi

    bd close "$SCRATCH_ID" --sandbox 2>/dev/null || true
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
