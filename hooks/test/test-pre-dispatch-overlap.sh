#!/usr/bin/env bash
# test-pre-dispatch-overlap.sh — first test suite for pre-dispatch-overlap.sh,
# scoped to the SABLE-uz9.9 activation inversion (orphaned contract SABLE-6zt;
# see SABLE-eaf). Overlap is the anti-collision gate for parallel workers —
# exactly the v3 risk profile (one manager-subagent, many concurrent workers in
# one session). The hook is advisory: on activation it injects an
# additionalContext overlap warning; when it stands down it emits nothing.
#
# Asserts the activation matrix:
#   - manager-subagent (agent_type=optimus) dispatch whose bead files overlap an
#     in-progress claim -> overlap warning injected.
#   - manager-subagent dispatch with no overlap -> silent.
#   - worker-type (general-purpose) and bare agent_id dispatches -> stand down.
#
# Run with:
#   bash hooks/test/test-pre-dispatch-overlap.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO/hooks/multi-manager/pre-dispatch-overlap.sh"

if [ ! -x "$HOOK" ]; then
  echo "FAIL: hook not executable at $HOOK"
  exit 2
fi

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR"' EXIT

# Registry so lib-identity resolves optimus as a manager.
AGENTS_YAML="$FIXTURE_DIR/agents.yaml"
cat > "$AGENTS_YAML" <<'YAML'
agents:
  optimus:
    type: epic_manager
  tarzan:
    type: one_off_manager
YAML

# Execution-mode fixture (unused by the subagent path, set for hermeticity so
# the live cockpit-mode.json can never leak in — cf. SABLE-wtv).
EXEC_MODE="$FIXTURE_DIR/mode-exec.json"
echo '{"mode":"execution","since":"2026-06-15"}' > "$EXEC_MODE"
NONEXISTENT_MODE="$FIXTURE_DIR/mode-absent.json"

# Stub bd:
#   show <DISP_BEAD> --json     -> a dispatch bead whose description names a file
#   list --status=in_progress   -> an in-progress bead with a WIP-CLAIMS file
# The OVERLAP_FILE env var controls whether the in-progress claim collides.
STUB_DIR="$FIXTURE_DIR/bin"
mkdir -p "$STUB_DIR"
cat > "$STUB_DIR/bd" <<'STUB'
#!/usr/bin/env bash
if [ "$1" = "show" ] && [[ "$*" == *"--json"* ]]; then
  echo '[{"id":"SABLE-disp","description":"implement hooks/foo.sh for the feature","notes":""}]'
  exit 0
fi
if [ "$1" = "list" ] && [[ "$*" == *"in_progress"* ]]; then
  echo "[{\"id\":\"SABLE-wip\",\"title\":\"active work\",\"assignee\":\"tarzan\",\"notes\":\"WIP-CLAIMS: ${OVERLAP_FILE:-}\",\"description\":\"\"}]"
  exit 0
fi
echo '[]'
exit 0
STUB
chmod +x "$STUB_DIR/bd"

# make_input <agent_id> <agent_type> <prompt>
make_input() {
  python3 -c "
import json, sys
aid, atype, prompt = sys.argv[1], sys.argv[2], sys.argv[3]
d = {'tool_name':'Agent','tool_input':{'subagent_type':'general-purpose','prompt':prompt},'hook_event_name':'PreToolUse'}
if aid: d['agent_id'] = aid
if atype: d['agent_type'] = atype
print(json.dumps(d))
" "$1" "$2" "$3"
}

# run_hook <json> <overlap_file>
run_hook() {
  printf '%s' "$1" | \
    env -u CLAUDE_AGENT_NAME -u CLAUDE_AGENT_ROLE \
        SABLE_AGENTS_YAML="$AGENTS_YAML" \
        SABLE_COCKPIT_MODE_FILE="$NONEXISTENT_MODE" \
        OVERLAP_FILE="$2" \
        PATH="$STUB_DIR:$PATH" \
        bash "$HOOK" 2>/dev/null
}

# Case 1: manager-subagent dispatch whose bead file (hooks/foo.sh) collides with
# an in-progress WIP-CLAIMS on the same file -> overlap warning injected.
OUT=$(run_hook "$(make_input a1 optimus 'Work SABLE-disp')" "hooks/foo.sh")
if printf '%s' "$OUT" | grep -q 'OVERLAP DETECTED'; then
  pass "manager-subagent dispatch overlapping an active claim injects a warning"
else
  fail "manager-subagent dispatch overlapping an active claim injects a warning" \
       "got: ${OUT:-<empty>}"
fi

# Case 1b: the warning names the colliding bead and file.
if printf '%s' "$OUT" | grep -q 'SABLE-wip' && printf '%s' "$OUT" | grep -q 'hooks/foo.sh'; then
  pass "overlap warning names the in-progress bead and file"
else
  fail "overlap warning names the in-progress bead and file" "got: ${OUT:-<empty>}"
fi

# Case 2: manager-subagent dispatch with NO file collision -> silent.
OUT=$(run_hook "$(make_input a2 optimus 'Work SABLE-disp')" "src/unrelated.ts")
if [ -z "$OUT" ]; then
  pass "manager-subagent dispatch with no overlap is silent"
else
  fail "manager-subagent dispatch with no overlap is silent" "got: $OUT"
fi

# Case 3: worker-type subagent (general-purpose) stands down even when the files
# would collide — non-managers do not dispatch, so no overlap action.
OUT=$(run_hook "$(make_input a3 general-purpose 'Work SABLE-disp')" "hooks/foo.sh")
if [ -z "$OUT" ]; then
  pass "worker-type subagent stands down (no overlap action)"
else
  fail "worker-type subagent stands down (no overlap action)" "got: $OUT"
fi

# Case 4: bare agent_id (subagent with no agent_type) stands down.
OUT=$(run_hook "$(make_input a4 '' 'Work SABLE-disp')" "hooks/foo.sh")
if [ -z "$OUT" ]; then
  pass "bare-agent_id subagent stands down (no overlap action)"
else
  fail "bare-agent_id subagent stands down (no overlap action)" "got: $OUT"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
