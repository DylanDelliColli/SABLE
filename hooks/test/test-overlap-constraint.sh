#!/usr/bin/env bash
# test-overlap-constraint.sh — test suite for pre-dispatch-overlap.sh
# (SABLE-jd5fj.6: overlap flips from advisory to a SCHEDULING CONSTRAINT).
# Formerly test-pre-dispatch-overlap.sh (SABLE-uz9.9 activation inversion,
# orphaned contract SABLE-6zt; see SABLE-eaf) — the activation matrix cases
# (manager vs worker vs bare agent_id) carry over unchanged; the overlap
# outcome cases are rewritten for deny/Serialize-with-allow/tag semantics.
#
# Asserts:
#   - manager-subagent dispatch whose declared footprint overlaps an
#     in-progress claim, with NO Serialize-with -> DENIED (permissionDecision).
#   - the SAME overlap, with a Serialize-with line naming the overlapping
#     bead -> ALLOWED, and both beads' serialize_with metadata is tagged.
#   - Serialize-with naming an UNRELATED bead does not launder the overlap ->
#     still DENIED.
#   - a declared '## File footprint' description section (extension-less
#     path) is honored the same as wip_claims metadata.
#   - manager-subagent dispatch with no overlap -> silent.
#   - worker-type (general-purpose) and bare agent_id dispatches -> stand down.
#
# Run with:
#   bash hooks/test/test-overlap-constraint.sh

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
# the live mode-state.json can never leak in — cf. SABLE-wtv).
EXEC_MODE="$FIXTURE_DIR/mode-exec.json"
echo '{"mode":"execution","since":"2026-06-15"}' > "$EXEC_MODE"
NONEXISTENT_MODE="$FIXTURE_DIR/mode-absent.json"

# Stub bd:
#   show <DISP_BEAD> --json     -> a dispatch bead whose description names a
#                                  file (or a '## File footprint' section when
#                                  DISP_DESC is set)
#   list --status=in_progress   -> an in-progress bead with a wip_claims
#                                  metadata claim (SABLE-szd: NOT notes —
#                                  bd update --notes overwrites the whole
#                                  field, so claims live in metadata instead)
#   update ... --set-metadata   -> logged to BD_CALL_LOG so tagging can be
#                                  asserted (no real bd write in this unit
#                                  suite — see test-overlap-dispatch-e2e.sh for
#                                  the real-bd integration coverage)
# The OVERLAP_FILE env var controls whether the in-progress claim collides.
STUB_DIR="$FIXTURE_DIR/bin"
mkdir -p "$STUB_DIR"
cat > "$STUB_DIR/bd" <<'STUB'
#!/usr/bin/env bash
if [ "$1" = "show" ] && [[ "$*" == *"--json"* ]]; then
  export DESC="${DISP_DESC:-implement hooks/foo.sh for the feature}"
  export DISP_SW="${DISP_SERIALIZE_WITH:-}"
  export DISP_NOTES="${DISP_NOTES:-}"
  python3 -c "
import json, os, sys
metadata = {}
if os.environ.get('DISP_SW', ''):
    metadata['serialize_with'] = os.environ['DISP_SW']
print(json.dumps([{'id': 'SABLE-disp', 'description': os.environ.get('DESC', ''), 'notes': os.environ.get('DISP_NOTES', ''), 'metadata': metadata}]))
"
  exit 0
fi
if [ "$1" = "list" ] && [[ "$*" == *"in_progress"* ]]; then
  echo "[{\"id\":\"SABLE-wip\",\"title\":\"active work\",\"assignee\":\"tarzan\",\"metadata\":{\"wip_claims\":\"${OVERLAP_FILE:-}\"},\"description\":\"\"}]"
  exit 0
fi
if [ "$1" = "update" ]; then
  echo "$*" >> "${BD_CALL_LOG:-/dev/null}"
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

# run_hook <json> <overlap_file> [disp_desc] [disp_serialize_with] [disp_notes]
run_hook() {
  BD_CALL_LOG="$FIXTURE_DIR/bd_calls.log"
  : > "$BD_CALL_LOG"
  printf '%s' "$1" | \
    env -u CLAUDE_AGENT_NAME -u CLAUDE_AGENT_ROLE -u SABLE_WORKER_PANE -u SABLE_BEAD \
        SABLE_AGENTS_YAML="$AGENTS_YAML" \
        SABLE_MODE_STATE="$NONEXISTENT_MODE" \
        OVERLAP_FILE="$2" \
        DISP_DESC="${3:-}" \
        DISP_SERIALIZE_WITH="${4:-}" \
        DISP_NOTES="${5:-}" \
        BD_CALL_LOG="$BD_CALL_LOG" \
        PATH="$STUB_DIR:$PATH" \
        bash "$HOOK" 2>/dev/null
}

# Case 1: manager-subagent dispatch whose bead file (hooks/foo.sh) collides
# with an in-progress WIP-CLAIMS on the same file, NO Serialize-with -> DENIED.
OUT=$(run_hook "$(make_input a1 optimus 'Work SABLE-disp')" "hooks/foo.sh")
if printf '%s' "$OUT" | grep -q '"permissionDecision": "deny"'; then
  pass "overlapping dispatch with no Serialize-with is DENIED"
else
  fail "overlapping dispatch with no Serialize-with is DENIED" "got: ${OUT:-<empty>}"
fi

# Case 1b: the deny reason names the colliding bead, the file, the
# SCHEDULING CONSTRAINT framing, and both outs.
if printf '%s' "$OUT" | grep -q 'SABLE-wip' && printf '%s' "$OUT" | grep -q 'hooks/foo.sh' \
   && printf '%s' "$OUT" | grep -q 'SCHEDULING CONSTRAINT' \
   && printf '%s' "$OUT" | grep -q 'Serialize-with'; then
  pass "deny reason names the in-progress bead, file, and both outs"
else
  fail "deny reason names the in-progress bead, file, and both outs" "got: ${OUT:-<empty>}"
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

# Case 5: a Serialize-with line naming the ACTUAL overlapping bead -> ALLOWED
# (additionalContext, no deny), and both beads' serialize_with metadata tagged.
OUT=$(run_hook "$(make_input a5 optimus $'Work SABLE-disp\nSerialize-with: SABLE-wip')" "hooks/foo.sh")
if printf '%s' "$OUT" | grep -q 'SERIALIZE-WITH ACCEPTED' && ! printf '%s' "$OUT" | grep -q '"permissionDecision": "deny"'; then
  pass "matching Serialize-with line permits the dispatch"
else
  fail "matching Serialize-with line permits the dispatch" "got: ${OUT:-<empty>}"
fi

CALLS="$(cat "$FIXTURE_DIR/bd_calls.log" 2>/dev/null || echo "")"
if printf '%s' "$CALLS" | grep -q 'SABLE-disp --sandbox --set-metadata serialize_with=SABLE-wip' \
   && printf '%s' "$CALLS" | grep -q 'SABLE-wip --sandbox --set-metadata serialize_with=SABLE-disp'; then
  pass "Serialize-with tags serialize_with metadata on BOTH beads"
else
  fail "Serialize-with tags serialize_with metadata on BOTH beads" "bd calls: ${CALLS:-<empty>}"
fi

# Case 6: Serialize-with naming an UNRELATED bead does not launder the actual
# overlap -> still DENIED.
OUT=$(run_hook "$(make_input a6 optimus $'Work SABLE-disp\nSerialize-with: SABLE-other')" "hooks/foo.sh")
if printf '%s' "$OUT" | grep -q '"permissionDecision": "deny"'; then
  pass "Serialize-with naming an unrelated bead does not launder the overlap"
else
  fail "Serialize-with naming an unrelated bead does not launder the overlap" "got: ${OUT:-<empty>}"
fi

# Case 7: a declared '## File footprint' description section (extension-less
# path) is honored the same as wip_claims metadata — the generic per-token
# regex requires a file extension and would miss it.
FOOTPRINT_DESC=$'Story.\n\n## File footprint\nbin/sable-spawn-worker (constraint surfacing), hooks/foo.sh'
OUT=$(run_hook "$(make_input a7 optimus 'Work SABLE-disp')" "bin/sable-spawn-worker" "$FOOTPRINT_DESC")
if printf '%s' "$OUT" | grep -q '"permissionDecision": "deny"' && printf '%s' "$OUT" | grep -q 'bin/sable-spawn-worker'; then
  pass "declared '## File footprint' section (extension-less path) triggers the constraint"
else
  fail "declared '## File footprint' section (extension-less path) triggers the constraint" "got: ${OUT:-<empty>}"
fi

# Case 8 (SABLE-86bsl): the dispatch bead carries ONLY the serialize_with
# METADATA field (already granted by an earlier dispatch) and NO Serialize-with
# line anywhere — not in the prompt, not in notes. Must still be PERMITTED:
# the metadata is the durable record and must not depend on the prompt/notes
# prose surviving. Red before the metadata-first read path existed.
OUT=$(run_hook "$(make_input a8 optimus 'Work SABLE-disp')" "hooks/foo.sh" "" "SABLE-wip" "")
if printf '%s' "$OUT" | grep -q 'SERIALIZE-WITH ACCEPTED' && ! printf '%s' "$OUT" | grep -q '"permissionDecision": "deny"'; then
  pass "serialize_with_read_from_metadata: metadata-only grant (no prompt/notes line) is PERMITTED"
else
  fail "serialize_with_read_from_metadata: metadata-only grant (no prompt/notes line) is PERMITTED" "got: ${OUT:-<empty>}"
fi

# Case 9 (SABLE-86bsl): backward-compat fallback — a bead authored before the
# metadata field existed, carrying the declaration ONLY as a 'Serialize-with:'
# line in its own NOTES (not the dispatch prompt) -> still PERMITTED.
OUT=$(run_hook "$(make_input a9 optimus 'Work SABLE-disp')" "hooks/foo.sh" "" "" "Serialize-with: SABLE-wip")
if printf '%s' "$OUT" | grep -q 'SERIALIZE-WITH ACCEPTED' && ! printf '%s' "$OUT" | grep -q '"permissionDecision": "deny"'; then
  pass "notes-borne Serialize-with line (legacy, no metadata field) is PERMITTED"
else
  fail "notes-borne Serialize-with line (legacy, no metadata field) is PERMITTED" "got: ${OUT:-<empty>}"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
