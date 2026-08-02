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
INT_TRANSCRIPT="$FIXTURE_DIR/int-transcript.jsonl"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Encode the six immutable request shapes in one Python process. Keeping
# json.dumps preserves exact path/agent escaping; reusing the resulting strings
# is safe because later tests mutate bd/stub state, never these hook inputs.
EDIT_INPUT_ENCODER_TRACE="$FIXTURE_DIR/edit-input-encoder.trace"
: > "$EDIT_INPUT_ENCODER_TRACE"
mapfile -t EDIT_INPUTS < <(python3 - "$TRANSCRIPT" "$INT_TRANSCRIPT" "$EDIT_INPUT_ENCODER_TRACE" <<'PYEOF'
import json
import sys

transcript, int_transcript, trace_path = sys.argv[1:]
with open(trace_path, "a", encoding="utf-8") as trace:
    trace.write("encode\n")


def edit_input(file_path, agent_id, transcript_path):
    data = {
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path},
        "hook_event_name": "PreToolUse",
    }
    if agent_id:
        data["agent_id"] = agent_id
    if transcript_path:
        data["transcript_path"] = transcript_path
    return json.dumps(data)


for values in (
    ("hooks/foo.sh", "agent-abc-123", transcript),
    ("hooks/foo.sh", "", transcript),
    ("", "agent-abc-123", transcript),
    ("hooks/foo.sh", "agent-abc-123", ""),
    ("hooks/foo.test.sh", "agent-abc-123", transcript),
    ("hooks/foo.sh", "agent-int-001", int_transcript),
):
    print(edit_input(*values))
PYEOF
)

EDIT_INPUT_USES=0
EDIT_INPUT_NORMAL_USES=0
use_edit_input() {
  EDIT_INPUT_USES=$((EDIT_INPUT_USES + 1))
  case "$1" in
    normal)        EDIT_INPUT="${EDIT_INPUTS[0]}"; EDIT_INPUT_NORMAL_USES=$((EDIT_INPUT_NORMAL_USES + 1)) ;;
    no-agent)      EDIT_INPUT="${EDIT_INPUTS[1]}" ;;
    no-file)       EDIT_INPUT="${EDIT_INPUTS[2]}" ;;
    no-transcript) EDIT_INPUT="${EDIT_INPUTS[3]}" ;;
    test-file)     EDIT_INPUT="${EDIT_INPUTS[4]}" ;;
    integration)  EDIT_INPUT="${EDIT_INPUTS[5]}" ;;
    *) echo "FATAL: unknown cached edit input: $1" >&2; exit 2 ;;
  esac
}

# run_hook <json>
run_hook() {
  : > "$BD_CALL_LOG"
  printf '%s' "$1" | env BD_CALL_LOG="$BD_CALL_LOG" PATH="$STUB_DIR:$PATH" bash "$HOOK" 2>/dev/null
}

# A snapshot is fresh after each hook mutation boundary, then reused by every
# assertion about that immutable post-run log. This avoids re-running
# grep/head pipelines without allowing observations to cross mutations.
BD_LOG_SNAPSHOTS=0
snapshot_bd_log() {
  BD_LOG_SNAPSHOTS=$((BD_LOG_SNAPSHOTS + 1))
  BD_LOG_SNAPSHOT=""
  [ -f "$BD_CALL_LOG" ] && BD_LOG_SNAPSHOT="$(<"$BD_CALL_LOG")"
}
bd_log_has_update() { [[ "$BD_LOG_SNAPSHOT" == *"BD_CALLED: update"* ]]; }

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
use_edit_input normal
run_hook "$EDIT_INPUT" >/dev/null
snapshot_bd_log
if bd_log_has_update; then
  pass "agent_id + file_path + transcript with bead ID → bd update --notes called"
else
  fail "agent_id + file_path + transcript with bead ID → bd update --notes called" \
       "bd call log: $(cat "$BD_CALL_LOG" 2>/dev/null || echo '(empty)')"
fi

# --- Test 2: WIP-CLAIMS note includes the file path ---
UPDATE_LINE=""
while IFS= read -r line; do
  [[ "$line" == *"BD_CALLED: update"* ]] && { UPDATE_LINE="$line"; break; }
done <<< "$BD_LOG_SNAPSHOT"
if [[ "$UPDATE_LINE" == *"hooks/foo.sh"* ]]; then
  pass "WIP-CLAIMS note includes the edited file path"
else
  fail "WIP-CLAIMS note includes the edited file path" "update line: ${UPDATE_LINE:-<none>}"
fi

# --- Test 3 (SABLE-lfql): the update call carries --sandbox ---
if [[ "$UPDATE_LINE" == *"--sandbox"* ]]; then
  pass "SABLE-lfql: bd update --notes call carries --sandbox (Dolt auto-push disabled)"
else
  fail "SABLE-lfql: bd update --notes call carries --sandbox (Dolt auto-push disabled)" \
       "update line: ${UPDATE_LINE:-<none>}"
fi

# --- Test 4: no agent_id (manager/main-session context) → hook stands down ---
use_edit_input no-agent
run_hook "$EDIT_INPUT" >/dev/null
snapshot_bd_log
if [ -n "$BD_LOG_SNAPSHOT" ]; then
  fail "no agent_id → hook stands down (no bd calls)" "bd calls: $(cat "$BD_CALL_LOG")"
else
  pass "no agent_id → hook stands down (no bd calls)"
fi

# --- Test 5: no file_path → hook stands down ---
use_edit_input no-file
run_hook "$EDIT_INPUT" >/dev/null
snapshot_bd_log
if [ -n "$BD_LOG_SNAPSHOT" ]; then
  fail "no file_path → hook stands down (no bd calls)" "bd calls: $(cat "$BD_CALL_LOG")"
else
  pass "no file_path → hook stands down (no bd calls)"
fi

# --- Test 6: no transcript_path → hook stands down ---
use_edit_input no-transcript
run_hook "$EDIT_INPUT" >/dev/null
snapshot_bd_log
if [ -n "$BD_LOG_SNAPSHOT" ]; then
  fail "no transcript_path → hook stands down (no bd calls)" "bd calls: $(cat "$BD_CALL_LOG")"
else
  pass "no transcript_path → hook stands down (no bd calls)"
fi

# --- Test 7: test file path (.test.) is skipped ---
use_edit_input test-file
run_hook "$EDIT_INPUT" >/dev/null
snapshot_bd_log
if [ -n "$BD_LOG_SNAPSHOT" ]; then
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
use_edit_input normal
run_hook "$EDIT_INPUT" >/dev/null
snapshot_bd_log
if bd_log_has_update; then
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
use_edit_input normal
printf '%s' "$EDIT_INPUT" | \
  env BD_CALL_LOG="$BD_CALL_LOG" DOLT_REMOTE_TIP="$LFQL_TIP" PATH="$STUB_DIR:$PATH" \
  bash "$HOOK" 2>/dev/null
snapshot_bd_log

# Precondition: the WIP-CLAIMS update really was attempted (so a pass below is
# not vacuous from the hook exiting before bd update).
if bd_log_has_update; then
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

INTEGRATION_INPUT_USED=0
if ! command -v bd >/dev/null 2>&1; then
  echo "SKIP (integration): bd not found on PATH"
else
  SCRATCH_ID=$(bd create --sandbox --silent \
    --title="[int-test] edit-write-claim-reconciler scratch bead" \
    --description="hooks/foo.sh is the implementation file for this scratch bead" \
    --notes="[no-test] integration test scratch — safe to close" \
    --type=task 2>/dev/null)

  if [[ ! "$SCRATCH_ID" =~ ^[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+$ ]]; then
    echo "SKIP (integration): could not create scratch bead — bd create output did not match ID pattern"
  else
    echo "Integration: created scratch bead $SCRATCH_ID"

    printf '{"role":"user","content":"%s: implement the feature — hooks/foo.sh needs updating"}\n' "$SCRATCH_ID" > "$INT_TRANSCRIPT"

    # Run real hook (no stub bd on PATH) with the scratch bead ID reachable
    # via the transcript.
    use_edit_input integration
    INTEGRATION_INPUT_USED=1
    printf '%s' "$EDIT_INPUT" | bash "$HOOK" 2>/dev/null

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

# Load-bearing reuse control. Six immutable payloads are encoded by one Python
# process, the common payload is consumed at three distinct stub-state
# boundaries, and seven post-hook logs are freshly snapshotted exactly once per
# mutation boundary. A future per-case encoder or repeated grep observation
# makes these counts diverge.
EXPECTED_EDIT_INPUT_USES=$((7 + INTEGRATION_INPUT_USED))
EDIT_INPUT_ENCODER_RUNS=0
while IFS= read -r encoder_event; do
  [ "$encoder_event" = "encode" ] && EDIT_INPUT_ENCODER_RUNS=$((EDIT_INPUT_ENCODER_RUNS + 1))
done < "$EDIT_INPUT_ENCODER_TRACE"
if [ "$EDIT_INPUT_ENCODER_RUNS" -eq 1 ] \
   && [ "${#EDIT_INPUTS[@]}" -eq 6 ] \
   && [ "$EDIT_INPUT_USES" -eq "$EXPECTED_EDIT_INPUT_USES" ] \
   && [ "$EDIT_INPUT_NORMAL_USES" -eq 3 ] \
   && [ "$BD_LOG_SNAPSHOTS" -eq 7 ]; then
  pass "immutable hook inputs are batch-encoded once and each mutated stub log is observed once"
else
  fail "immutable hook inputs are batch-encoded once and each mutated stub log is observed once" \
    "encoders=$EDIT_INPUT_ENCODER_RUNS payloads=${#EDIT_INPUTS[@]} uses=$EDIT_INPUT_USES normal_uses=$EDIT_INPUT_NORMAL_USES snapshots=$BD_LOG_SNAPSHOTS"
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
