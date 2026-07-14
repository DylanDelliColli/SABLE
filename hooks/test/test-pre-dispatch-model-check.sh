#!/usr/bin/env bash
# test-pre-dispatch-model-check.sh — Unit tests for pre-dispatch-model-check.sh
#
# Stubs `bd show` via a temporary directory on PATH so we can fixture bead
# label data without touching a real beads database. Pipes synthetic
# PreToolUse:Agent JSON input to the hook and verifies the response.
#
# Run:
#   bash hooks/test/test-pre-dispatch-model-check.sh

set -uo pipefail

HOOK="$(cd "$(dirname "$0")/.." && pwd)/multi-manager/pre-dispatch-model-check.sh"

if [ ! -x "$HOOK" ]; then
  echo "FAIL: hook not executable at $HOOK"
  exit 2
fi

PASS=0
FAIL=0
FAIL_NAMES=""

# Make a temp dir to stage a fake `bd` shim.
TMP_DIR=$(mktemp -d)

# Set SABLE_MODE_STATE to an isolated path (SABLE-ierm) so the test doesn't read
# the ambient repo's mode-state.json. This ensures tests run consistently regardless
# of the repo's execution mode. Mirrors test-mode-interlock.sh's pattern.
SABLE_MODE_STATE="$(mktemp -u)"
export SABLE_MODE_STATE

trap 'rm -rf "$TMP_DIR" "$SABLE_MODE_STATE" 2>/dev/null || true' EXIT

# We'll write a small bd stub that returns canned JSON based on the bead ID
# in argv. The fixture data is keyed by bead-id in $TMP_DIR/fixtures.
mkdir -p "$TMP_DIR/fixtures"

cat > "$TMP_DIR/bd" <<'STUB'
#!/usr/bin/env bash
# Minimal bd stub: only supports `bd show <id> --json`.
# Reads fixture from $TMP_DIR/fixtures/<id>.json (passed via env).
if [ "$1" = "show" ] && [ -n "${2:-}" ]; then
  ID="$2"
  if [ -f "$TMP_DIR/fixtures/$ID.json" ]; then
    cat "$TMP_DIR/fixtures/$ID.json"
    exit 0
  fi
fi
exit 1
STUB
chmod +x "$TMP_DIR/bd"

write_fixture() {
  # $1 = bead id, $2 = comma-separated labels (or empty)
  local id="$1" labels="$2"
  python3 -c "
import json, sys
labels = sys.argv[1].split(',') if sys.argv[1] else []
print(json.dumps([{'id': sys.argv[2], 'labels': labels}]))
" "$labels" "$id" > "$TMP_DIR/fixtures/$id.json"
}

make_input() {
  # $1 = prompt, $2 = subagent_type, $3 = model
  python3 -c "
import json, sys
prompt = sys.argv[1]
subtype = sys.argv[2]
model = sys.argv[3]
out = {'tool_input': {'prompt': prompt, 'subagent_type': subtype}}
if model:
    out['tool_input']['model'] = model
print(json.dumps(out))
" "$1" "$2" "$3"
}

run_hook() {
  # $1 = env prefix, $2 = prompt, $3 = subagent_type, $4 = model
  local env_prefix="$1"
  local prompt="$2"
  local subtype="$3"
  local model="$4"
  local input
  input=$(make_input "$prompt" "$subtype" "$model")
  # Inject our bd stub onto PATH and pass TMP_DIR through so the stub can find fixtures.
  # Also pass SABLE_MODE_STATE so the test doesn't read the ambient repo's mode file.
  local out
  out=$(env -i PATH="$TMP_DIR:$PATH" TMP_DIR="$TMP_DIR" SABLE_MODE_STATE="$SABLE_MODE_STATE" $env_prefix bash "$HOOK" <<< "$input" 2>/dev/null || echo "RUN_ERR:$?")
  echo -n "$out"
}

assert_allow() {
  local name="$1" env="$2" prompt="$3" subtype="$4" model="$5"
  local out
  out=$(run_hook "$env" "$prompt" "$subtype" "$model")
  if [ -z "$out" ]; then
    PASS=$((PASS+1))
    echo "PASS: $name"
  else
    # Allow can also include additionalContext (nudge)
    if echo "$out" | grep -q '"additionalContext"' && ! echo "$out" | grep -q '"permissionDecision"'; then
      PASS=$((PASS+1))
      echo "PASS: $name (with nudge)"
    else
      FAIL=$((FAIL+1))
      FAIL_NAMES="$FAIL_NAMES\n  $name"
      echo "FAIL: $name"
      echo "  Expected: empty (or nudge-only)"
      echo "  Got:      ${out:0:300}"
    fi
  fi
}

assert_deny() {
  local name="$1" env="$2" prompt="$3" subtype="$4" model="$5" expect="$6"
  local out
  out=$(run_hook "$env" "$prompt" "$subtype" "$model")
  if echo "$out" | grep -q '"permissionDecision": "deny"' && echo "$out" | grep -qF "$expect"; then
    PASS=$((PASS+1))
    echo "PASS: $name"
  else
    FAIL=$((FAIL+1))
    FAIL_NAMES="$FAIL_NAMES\n  $name"
    echo "FAIL: $name"
    echo "  Expected: deny containing '$expect'"
    echo "  Got:      ${out:0:400}"
  fi
}

assert_nudge() {
  local name="$1" env="$2" prompt="$3" subtype="$4" model="$5" expect="$6"
  local out
  out=$(run_hook "$env" "$prompt" "$subtype" "$model")
  if echo "$out" | grep -q '"additionalContext"' && echo "$out" | grep -qF "$expect"; then
    PASS=$((PASS+1))
    echo "PASS: $name"
  else
    FAIL=$((FAIL+1))
    FAIL_NAMES="$FAIL_NAMES\n  $name"
    echo "FAIL: $name"
    echo "  Expected: nudge containing '$expect'"
    echo "  Got:      ${out:0:400}"
  fi
}

# ---- Fixtures ----
write_fixture "SABLE-aaa" "model:opus,for-optimus"
write_fixture "SABLE-bbb" "model:sonnet"
write_fixture "SABLE-ccc" "model:haiku,docs"
write_fixture "SABLE-ddd" ""    # no model: label
write_fixture "SABLE-eee" "for-tarzan,bug"  # labels but no model:

MGR_ENV="CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager"

# ---- Skip cases (no enforcement) ----

# Test 1: non-manager session → no-op
assert_allow "no manager identity → no-op" "" "Bead SABLE-aaa: do work" "" "sonnet"

# Test 2: subagent_type=Explore → skip (read-only context)
assert_allow "Explore subagent skipped" "$MGR_ENV" "Bead SABLE-aaa: explore" "Explore" ""

# Test 3: research-keyword prompt → skip
assert_allow "research-keyword prompt skipped" "$MGR_ENV" "Task: explore the auth subsystem" "general-purpose" ""

# SABLE-6qn: general-purpose is ALSO the natural subtype for an implementation
# worker, so it must NOT bypass the model gate by subtype alone — only the
# explore-PROMPT heuristic (Test 3) skips read-only work. A general-purpose
# code worker with a model mismatch is denied like any other.
assert_deny "general-purpose implementation worker is gated, not skipped (SABLE-6qn)" "$MGR_ENV" "Working on SABLE-aaa, implement the refactor" "general-purpose" "sonnet" "model:opus but dispatch chose sonnet"
assert_allow "general-purpose worker with matching model allowed (no over-deny)" "$MGR_ENV" "Working on SABLE-aaa, implement the refactor" "general-purpose" "opus"

# ---- Match cases (silent allow) ----

# Test 4: bead has model:opus, dispatch uses opus → allow
assert_allow "match opus" "$MGR_ENV" "Working on SABLE-aaa, the auth refactor" "" "opus"

# Test 5: bead has model:sonnet, dispatch uses claude-sonnet-4-6 → allow
assert_allow "match sonnet via full model id" "$MGR_ENV" "Working on SABLE-bbb" "" "claude-sonnet-4-6"

# Test 6: bead has model:haiku, dispatch uses haiku → allow
assert_allow "match haiku" "$MGR_ENV" "Working on SABLE-ccc, doc fix" "" "haiku"

# ---- Mismatch cases (deny without override) ----

# Test 7: bead has model:opus, dispatch uses sonnet, no override → deny
assert_deny "mismatch opus/sonnet without override" "$MGR_ENV" "Working on SABLE-aaa" "" "sonnet" "model:opus but dispatch chose sonnet"

# Test 8: bead has model:haiku, dispatch uses opus, no override → deny
assert_deny "mismatch haiku/opus without override" "$MGR_ENV" "Working on SABLE-ccc" "" "opus" "model:haiku but dispatch chose opus"

# Test 9: bead has model:sonnet, dispatch unspecified → deny
assert_deny "label exists but dispatch unspecified" "$MGR_ENV" "Working on SABLE-bbb" "" "" "model:sonnet but dispatch model is unspecified"

# ---- Override path (allow with reason in prompt) ----

# Test 10: mismatch + Model override line → allow
PROMPT_WITH_OVERRIDE=$'Working on SABLE-aaa.\n\nModel override: simplified to mechanical rename, stepping down\n\nDo work.'
assert_allow "mismatch with override allowed" "$MGR_ENV" "$PROMPT_WITH_OVERRIDE" "" "haiku"

# Test 11: override line case-insensitive
PROMPT_LOWER_OVERRIDE=$'Working on SABLE-aaa.\nmodel override: stepping down for cleanup task'
assert_allow "lowercase override allowed" "$MGR_ENV" "$PROMPT_LOWER_OVERRIDE" "" "haiku"

# ---- No-label paths ----

# Test 12: bead has no model: label, dispatch unspecified → deny
assert_deny "no label + no dispatch model → deny" "$MGR_ENV" "Working on SABLE-ddd" "" "" "have no model: label"

# Test 13: bead has no model: label, dispatch specifies sonnet → allow with nudge
assert_nudge "no label + explicit dispatch model → nudge" "$MGR_ENV" "Working on SABLE-eee" "" "sonnet" "Suggest"

# ---- No bead in prompt ----

# Test 14: no bead in prompt + no dispatch model → deny (force ladder)
assert_deny "no bead + no dispatch model → deny" "$MGR_ENV" "Just do this generic thing" "" "" "no model specified on Agent call"

# Test 15: no bead in prompt + explicit dispatch model → allow
assert_allow "no bead + explicit model → allow" "$MGR_ENV" "Just do this generic thing" "" "sonnet"

# ---- SABLE-gga: sable-<word> filenames/skills must NOT be extracted as bead IDs ----

# Test 15b: prompt mentions sable-* filenames alongside a real unlabeled bead →
# deny must name only the real bead, never the filenames (regression for the
# false 'unlabeled bead' deny caused by over-matching the ID regex).
assert_deny "sable-execute/sable-teams-preflight filenames not treated as beads" "$MGR_ENV" \
  "Dispatching for SABLE-ddd: run sable-execute, sable-orchestration-install, and sable-teams-preflight against the epic." \
  "" "" "have no model: label"
OUT=$(run_hook "$MGR_ENV" "Dispatching for SABLE-ddd: run sable-execute, sable-orchestration-install, and sable-teams-preflight against the epic." "" "")
if echo "$OUT" | grep -qF "sable-execute" || echo "$OUT" | grep -qF "sable-orchestration-install" || echo "$OUT" | grep -qF "sable-teams-preflight"; then
  FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  sable-* filenames absent from deny reason"
  echo "FAIL: sable-* filenames absent from deny reason"
  echo "  Got: ${OUT:0:400}"
else
  PASS=$((PASS+1)); echo "PASS: sable-* filenames absent from deny reason"
fi

# Test 15c: prompt mentions ONLY sable-* filenames (no real bead) + no dispatch
# model → treated as bead-free (ad-hoc ladder-enforcement deny), not a
# false 'unlabeled bead' deny naming the filenames.
assert_deny "sable-* filenames alone → no-bead path, not false unlabeled-bead deny" "$MGR_ENV" \
  "Run sable-execute and sable-teams-preflight to check drift." "" "" "no model specified on Agent call"

# ---- Native manager-subagent path (SABLE-6zt cases 5 & 6) ----
# In v3 a manager dispatches workers natively: identity is the subagent
# agent_type (agent_id present, NO env), resolved against the registry by
# lib-identity. Model gating must engage on this path exactly as on the legacy
# env path — otherwise the model ladder is advisory for every nested dispatch.
# subagent_type=claude is a real implementing worker (NOT in the read-only
# exempt list at the hook's line 66), so the gate stays active.

AGENTS_YAML="$TMP_DIR/agents.yaml"
cat > "$AGENTS_YAML" <<'YAML'
agents:
  optimus:
    type: epic_manager
  tarzan:
    type: one_off_manager
YAML

# make_subagent_input <prompt> <model> — agent_id + agent_type=optimus payload
make_subagent_input() {
  python3 -c "
import json, sys
prompt, model = sys.argv[1], sys.argv[2]
ti = {'prompt': prompt, 'subagent_type': 'claude'}
if model:
    ti['model'] = model
print(json.dumps({
    'tool_name': 'Agent',
    'agent_id': 'mgr-sub-001',
    'agent_type': 'optimus',
    'tool_input': ti,
    'hook_event_name': 'PreToolUse'
}))
" "$1" "$2"
}

# run_hook_subagent <prompt> <model> — no env identity; registry resolves optimus
run_hook_subagent() {
  make_subagent_input "$1" "$2" | \
    env -i PATH="$TMP_DIR:$PATH" TMP_DIR="$TMP_DIR" SABLE_AGENTS_YAML="$AGENTS_YAML" SABLE_MODE_STATE="$SABLE_MODE_STATE" \
        bash "$HOOK" 2>/dev/null || echo "RUN_ERR:$?"
}

# Test 16 (6zt case 5): manager-subagent dispatch, bead model:opus, dispatch
# model unspecified → DENY (activation proves the gate engaged via agent_type).
OUT=$(run_hook_subagent "Working on SABLE-aaa, the auth refactor" "")
if echo "$OUT" | grep -q '"permissionDecision": "deny"' && echo "$OUT" | grep -qF "model:opus but dispatch model is unspecified"; then
  PASS=$((PASS+1)); echo "PASS: manager-subagent (agent_type=optimus) missing model → deny"
else
  FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  manager-subagent (agent_type=optimus) missing model → deny"
  echo "FAIL: manager-subagent (agent_type=optimus) missing model → deny"; echo "  Got: ${OUT:0:300}"
fi

# Test 17 (6zt case 6): manager-subagent dispatch with the matching model → allow.
OUT=$(run_hook_subagent "Working on SABLE-aaa, the auth refactor" "opus")
if [ -z "$OUT" ]; then
  PASS=$((PASS+1)); echo "PASS: manager-subagent (agent_type=optimus) matching model → allow"
else
  FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  manager-subagent (agent_type=optimus) matching model → allow"
  echo "FAIL: manager-subagent (agent_type=optimus) matching model → allow"; echo "  Got: ${OUT:0:300}"
fi

# ---- SABLE-82j/2dm: settings-snippet registration completeness ----
# Every pre-dispatch-*.sh hook must be registered under PreToolUse:Agent in the
# install snippet, or it silently never fires on new installs — the gap that
# left pre-dispatch-model-check.sh unregistered while the others were present.
SNIPPET="$(cd "$(dirname "$0")/../.." && pwd)/templates/multi-manager/settings-snippet.json"
for h in pre-dispatch-claim pre-dispatch-model-check pre-dispatch-overlap pre-dispatch-preempt pre-dispatch-refresh; do
  if grep -q "$h.sh" "$SNIPPET" 2>/dev/null; then
    PASS=$((PASS+1)); echo "PASS: $h.sh registered in settings-snippet.json"
  else
    FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $h.sh registered in settings-snippet.json"; echo "FAIL: $h.sh registered in settings-snippet.json"
  fi
done

# ---- Summary ----

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="

if [ "$FAIL" -gt 0 ]; then
  echo -e "Failed tests:$FAIL_NAMES"
  exit 1
fi
exit 0
