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
IMMUTABLE_HOOK_OUTPUT_REUSES=0

# Make a temp dir to stage a fake `bd` shim.
TMP_DIR=$(mktemp -d)

# Set SABLE_MODE_STATE to an isolated path (SABLE-ierm) so the test doesn't read
# the ambient repo's mode-state.json. This ensures tests run consistently regardless
# of the repo's execution mode. Mirrors test-mode-interlock.sh's pattern.
SABLE_MODE_STATE="$TMP_DIR/mode-state.json"
export SABLE_MODE_STATE

trap 'rm -rf "$TMP_DIR" 2>/dev/null || true' EXIT

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
    IFS= read -r FIXTURE < "$TMP_DIR/fixtures/$ID.json"
    printf '%s\n' "$FIXTURE"
    exit 0
  fi
fi
exit 1
STUB
chmod +x "$TMP_DIR/bd"

json_escape() {
  local value="$1"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\b'/\\b}
  value=${value//$'\f'/\\f}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  value=${value//$'\t'/\\t}
  JSON_ESCAPED="$value"
}

write_fixture() {
  # $1 = bead id, $2 = comma-separated labels (or empty)
  local id="$1" labels="$2" encoded_id encoded_label json sep label
  local -a label_items=()
  json_escape "$id"
  encoded_id="$JSON_ESCAPED"
  json="[{\"id\":\"$encoded_id\",\"labels\":["
  sep=""
  if [ -n "$labels" ]; then
    IFS=',' read -r -a label_items <<< "$labels"
    for label in "${label_items[@]}"; do
      json_escape "$label"
      encoded_label="$JSON_ESCAPED"
      json="$json$sep\"$encoded_label\""
      sep=","
    done
  fi
  printf '%s]}]\n' "$json" > "$TMP_DIR/fixtures/$id.json"
}

make_input() {
  # $1 = prompt, $2 = subagent_type, $3 = model
  local prompt subtype model
  json_escape "$1"; prompt="$JSON_ESCAPED"
  json_escape "$2"; subtype="$JSON_ESCAPED"
  json_escape "$3"; model="$JSON_ESCAPED"
  if [ -n "$model" ]; then
    HOOK_INPUT_JSON="{\"tool_input\":{\"prompt\":\"$prompt\",\"subagent_type\":\"$subtype\",\"model\":\"$model\"}}"
  else
    HOOK_INPUT_JSON="{\"tool_input\":{\"prompt\":\"$prompt\",\"subagent_type\":\"$subtype\"}}"
  fi
}

run_hook() {
  # $1 = env prefix, $2 = prompt, $3 = subagent_type, $4 = model
  local env_prefix="$1"
  local prompt="$2"
  local subtype="$3"
  local model="$4"
  local input
  make_input "$prompt" "$subtype" "$model"
  input="$HOOK_INPUT_JSON"
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
    if [[ "$out" == *'"additionalContext"'* && "$out" != *'"permissionDecision"'* ]]; then
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

assert_deny_output() {
  local name="$1" out="$2" expect="$3"
  if [[ "$out" == *'"permissionDecision": "deny"'* && "$out" == *"$expect"* ]]; then
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

assert_deny() {
  local name="$1" env="$2" prompt="$3" subtype="$4" model="$5" expect="$6"
  local out
  out=$(run_hook "$env" "$prompt" "$subtype" "$model")
  assert_deny_output "$name" "$out" "$expect"
}

assert_nudge() {
  local name="$1" env="$2" prompt="$3" subtype="$4" model="$5" expect="$6"
  local out
  out=$(run_hook "$env" "$prompt" "$subtype" "$model")
  if [[ "$out" == *'"additionalContext"'* && "$out" == *"$expect"* ]]; then
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

# A malformed longer token must never backtrack into a real shorter bead id.
# SABLE-aaa is model:opus, so a false prefix extraction would deny this
# sonnet dispatch; silent allow proves none of the malformed forms was
# harvested as SABLE-aaa.
assert_allow "malformed dotted/double-hyphen ids do not yield shorter valid prefixes" \
  "$MGR_ENV" \
  "Malformed references: SABLE-aaa.1x, SABLE-aaa.1-extra, SABLE-aaa--extra, SABLE-aaa..1, SABLE-aaa.1..2, SABLE-aaa.-extra." \
  "" "sonnet"

# ---- Match cases (silent allow) ----

# Test 4: bead has model:opus, dispatch uses opus → allow
assert_allow "match opus" "$MGR_ENV" "Working on SABLE-aaa, the auth refactor" "" "opus"

# A single sentence-ending period is punctuation, not a malformed continuation.
assert_deny "sentence-ending period preserves a valid bead id" \
  "$MGR_ENV" "Working on SABLE-aaa." "" "sonnet" \
  "model:opus but dispatch chose sonnet"

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
OUT=$(run_hook "$MGR_ENV" "Dispatching for SABLE-ddd: run sable-execute, sable-orchestration-install, and sable-teams-preflight against the epic." "" "")
assert_deny_output "sable-execute/sable-teams-preflight filenames not treated as beads" "$OUT" "have no model: label"
IMMUTABLE_HOOK_OUTPUT_REUSES=$((IMMUTABLE_HOOK_OUTPUT_REUSES+1))
if [[ "$OUT" == *"sable-execute"* || "$OUT" == *"sable-orchestration-install"* || "$OUT" == *"sable-teams-preflight"* ]]; then
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

# ---- SABLE-fxv3: lowercase sable-* TOOL names (single-segment, <=6 chars —
# sable-plan, sable-doctor, sable-agents, sable-launch, sable-msg, sable-mode,
# sable-note, sable-view, sable-tmux) must not be parsed as bead IDs. These
# escape the SABLE-gga multi-segment lookahead because they have no second
# hyphen. Fix is a case-sensitive prefix match (bd issues SABLE-xxxx uppercase;
# tool names are lowercase by convention) — no re.IGNORECASE. ----

# Test 18 (fxv3 unit case 1): prompt mentions lowercase tool names sable-doctor/
# sable-plan alongside the real labeled bead SABLE-aaa (model:opus). Only the
# real bead should be extracted; since its label matches the dispatch model,
# this must be a silent allow — no spurious no-label list, no deny for the
# tool-name "beads".
assert_allow "fxv3: sable-doctor/sable-plan tool names ignored, real labeled bead extracted" \
  "$MGR_ENV" "Working on SABLE-aaa: run sable-doctor and sable-plan to check drift." "" "opus"

# Test 19 (fxv3 unit case 2): ad-hoc prompt mentioning ONLY tool names (no real
# bead ID) with an explicit dispatch model → must take the no-bead/ad-hoc path
# (silent allow), never the no-label-bead deny naming the tool names.
assert_allow "fxv3: tool-name-only prompt with explicit model takes ad-hoc no-bead path" \
  "$MGR_ENV" "Run sable-plan and sable-doctor to check drift before dispatch." "" "sonnet"

# Test 20 (fxv3 integration case, live-observed shape): prompt mentions the
# unlabeled bead SABLE-ddd alongside sable-launch/sable-plan — the exact tokens
# from the 2026-07-15 live false denial ('bead(s) [ SABLE-59t6 sable-launch
# sable-plan ] have no model: label'). End-to-end hook invocation must deny
# naming ONLY SABLE-ddd; sable-launch/sable-plan must never appear in the
# no-label-beads list or anywhere in the emitted JSON.
OUT=$(run_hook "$MGR_ENV" "Dispatching for SABLE-ddd: run sable-launch and sable-plan for the rollout." "" "")
assert_deny_output "fxv3: sable-launch/sable-plan not counted as no-label beads (live-observed shape)" "$OUT" "have no model: label"
IMMUTABLE_HOOK_OUTPUT_REUSES=$((IMMUTABLE_HOOK_OUTPUT_REUSES+1))
if [[ "$OUT" == *"sable-launch"* || "$OUT" == *"sable-plan"* ]]; then
  FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  fxv3: sable-launch/sable-plan absent from deny reason"
  echo "FAIL: fxv3: sable-launch/sable-plan absent from deny reason"
  echo "  Got: ${OUT:0:400}"
else
  PASS=$((PASS+1)); echo "PASS: fxv3: sable-launch/sable-plan absent from deny reason"
fi

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
  local prompt model
  json_escape "$1"; prompt="$JSON_ESCAPED"
  json_escape "$2"; model="$JSON_ESCAPED"
  if [ -n "$model" ]; then
    HOOK_INPUT_JSON="{\"tool_name\":\"Agent\",\"agent_id\":\"mgr-sub-001\",\"agent_type\":\"optimus\",\"tool_input\":{\"prompt\":\"$prompt\",\"subagent_type\":\"claude\",\"model\":\"$model\"},\"hook_event_name\":\"PreToolUse\"}"
  else
    HOOK_INPUT_JSON="{\"tool_name\":\"Agent\",\"agent_id\":\"mgr-sub-001\",\"agent_type\":\"optimus\",\"tool_input\":{\"prompt\":\"$prompt\",\"subagent_type\":\"claude\"},\"hook_event_name\":\"PreToolUse\"}"
  fi
}

# run_hook_subagent <prompt> <model> — no env identity; registry resolves optimus
run_hook_subagent() {
  make_subagent_input "$1" "$2"
  env -i PATH="$TMP_DIR:$PATH" TMP_DIR="$TMP_DIR" SABLE_AGENTS_YAML="$AGENTS_YAML" SABLE_MODE_STATE="$SABLE_MODE_STATE" \
      bash "$HOOK" <<< "$HOOK_INPUT_JSON" 2>/dev/null || echo "RUN_ERR:$?"
}

# Test 16 (6zt case 5): manager-subagent dispatch, bead model:opus, dispatch
# model unspecified → DENY (activation proves the gate engaged via agent_type).
OUT=$(run_hook_subagent "Working on SABLE-aaa, the auth refactor" "")
if [[ "$OUT" == *'"permissionDecision": "deny"'* && "$OUT" == *"model:opus but dispatch model is unspecified"* ]]; then
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
SNIPPET_CONTENT=$(<"$SNIPPET")
for h in pre-dispatch-claim pre-dispatch-model-check pre-dispatch-overlap pre-dispatch-preempt pre-dispatch-refresh; do
  if [[ "$SNIPPET_CONTENT" == *"$h.sh"* ]]; then
    PASS=$((PASS+1)); echo "PASS: $h.sh registered in settings-snippet.json"
  else
    FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $h.sh registered in settings-snippet.json"; echo "FAIL: $h.sh registered in settings-snippet.json"
  fi
done

# Two deny cases below have a second assertion over the exact same immutable
# hook response. Keep this structural counter load-bearing so those assertions
# cannot silently regain redundant encoder/hook/fixture reads.
if [ "$IMMUTABLE_HOOK_OUTPUT_REUSES" -eq 2 ]; then
  PASS=$((PASS+1)); echo "PASS: structure: immutable deny outputs reused twice"
else
  FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  structure: expected 2 immutable output reuses, got $IMMUTABLE_HOOK_OUTPUT_REUSES"
  echo "FAIL: structure: immutable deny outputs reused twice"
fi

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
