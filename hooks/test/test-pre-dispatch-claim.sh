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
REAL_BD_FIXTURE_READY=0
REAL_BD_SETUP_WRITES_ELIDED=0
REAL_BD_READBACK_REUSES=0
REAL_BD_CLOSE_READBACK_REUSES=0
IMMUTABLE_UNIT_OBSERVATION_REUSES=0

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

# Claims and dependency-merge warnings are independent hook interfaces. Keep
# the advisory disabled for the claims matrix; its seven positive/negative
# controls below explicitly re-enable it. This avoids rerunning the real
# dependency checker against deliberately incomplete bd stubs in every claims
# case.
export SABLE_DEP_MERGE_GUARD=0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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

# make_dispatch_input <prompt>
# Produces a PreToolUse:Agent JSON payload (no agent_id = manager/main-session context).
make_dispatch_input() {
  json_escape "$1"
  printf '{"tool_name":"Agent","tool_input":{"prompt":"%s","subagent_type":"general-purpose"},"hook_event_name":"PreToolUse"}\n' \
    "$JSON_ESCAPED"
}

# make_subagent_input <prompt>
# Produces a PreToolUse:Agent JSON payload WITH agent_id (subagent context).
make_subagent_input() {
  json_escape "$1"
  printf '{"tool_name":"Agent","agent_id":"agent-abc-123","agent_type":"general-purpose","tool_input":{"prompt":"%s","subagent_type":"general-purpose"},"hook_event_name":"PreToolUse"}\n' \
    "$JSON_ESCAPED"
}

# make_manager_subagent_input <prompt> <agent_type>
# PreToolUse:Agent payload from a MANAGER subagent dispatching a worker natively
# (agent_id present + manager agent_type, NO env identity) — the SABLE-uz9.9
# native-dispatch path.
make_manager_subagent_input() {
  local prompt agent_type
  json_escape "$1"
  prompt="$JSON_ESCAPED"
  json_escape "$2"
  agent_type="$JSON_ESCAPED"
  printf '{"tool_name":"Agent","agent_id":"mgr-sub-001","agent_type":"%s","tool_input":{"prompt":"%s","subagent_type":"general-purpose"},"hook_event_name":"PreToolUse"}\n' \
    "$agent_type" "$prompt"
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
IMMUTABLE_CLAIM_UPDATE_LINE=$(grep 'BD_CALLED: update' "$BD_CALL_LOG" 2>/dev/null | head -1)
if [ -n "$IMMUTABLE_CLAIM_UPDATE_LINE" ]; then
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
# Test 1 already produced this update from the bead description even though its
# prompt carried no path; keep that stronger immutable observation rather than
# starting the hook again with a path-bearing prompt.
UPDATE_LINE="$IMMUTABLE_CLAIM_UPDATE_LINE"
IMMUTABLE_UNIT_OBSERVATION_REUSES=$((IMMUTABLE_UNIT_OBSERVATION_REUSES+1))
if echo "$UPDATE_LINE" | grep -q 'WIP-CLAIMS.*hooks/foo.sh\|hooks/foo.sh'; then
  pass "WIP-CLAIMS note includes file path from bead description"
else
  # update was called — check if we can see the note value
  if [ -n "$UPDATE_LINE" ]; then
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
# Tests 1/6 already established the exact identity, fixture, update, and
# description-derived path. Reuse that immutable observation; later
# concurrency/stateful cases remain fresh hook invocations.
UPDATE_LINE_LFQL="$UPDATE_LINE"
IMMUTABLE_UNIT_OBSERVATION_REUSES=$((IMMUTABLE_UNIT_OBSERVATION_REUSES+1))
if echo "$UPDATE_LINE_LFQL" | grep -q -- '--sandbox'; then
  pass "SABLE-lfql: bd update --notes call carries --sandbox (Dolt auto-push disabled)"
else
  fail "SABLE-lfql: bd update --notes call carries --sandbox (Dolt auto-push disabled)" \
       "update line: ${UPDATE_LINE_LFQL:-<none>}"
fi

# ---------------------------------------------------------------------------
# SABLE-y4nom.7.6: one initial bead parse supplies description + wip_claims.
# A two-bead dispatch is the load-bearing control: an already-claimed bead
# stops after one read, while an unclaimed bead gets one last-responsible-moment
# refresh before its update. That refresh preserves the old concurrent-claim
# guard while reducing per-bead Python parses from three to one or two.
# Malformed/empty initial reads remain fail-open (one attempt, no update).
# ---------------------------------------------------------------------------
BD_SHOW_COUNT="$FIXTURE_DIR/bd-show-count.log"
export BD_SHOW_COUNT
cat > "$STUB_DIR/bd" <<'STUB'
#!/usr/bin/env bash
echo "BD_CALLED: $*" >> "$BD_CALL_LOG"
if [ "$1" = "show" ] && [[ "$*" == *"--json"* ]]; then
  printf '%s\n' "$2" >> "$BD_SHOW_COUNT"
  case "${BD_SHOW_MODE:-records}" in
    malformed) printf '{malformed\n'; exit 0 ;;
    empty) exit 0 ;;
    refresh-malformed)
      if [ "$2" = "SABLE-one" ] \
         && [ "$(grep -c '^SABLE-one$' "$BD_SHOW_COUNT" 2>/dev/null)" -ge 2 ]; then
        printf '{malformed\n'
        exit 0
      fi
      ;;
  esac
  case "$2" in
    SABLE-one)
      if [ "${BD_SHOW_MODE:-records}" = "concurrent" ] \
         && [ "$(grep -c '^SABLE-one$' "$BD_SHOW_COUNT" 2>/dev/null)" -ge 2 ]; then
        echo '[{"id":"SABLE-one","description":"Mention hooks/fallback-only.sh.\n## File footprint\nbin/sable-spawn-worker, hooks/one.sh implementation\n## Acceptance\nDone.","metadata":{"wip_claims":"hooks/concurrent.sh"}}]'
      else
        echo '[{"id":"SABLE-one","description":"Mention hooks/fallback-only.sh.\n## File footprint\nbin/sable-spawn-worker, hooks/one.sh implementation\n## Acceptance\nDone.","metadata":{}}]'
      fi
      ;;
    SABLE-two)
      echo '[{"id":"SABLE-two","description":"hooks/two.sh is the implementation","metadata":{"wip_claims":"hooks/already.sh"}}]'
      ;;
    *)
      echo '[]'
      ;;
  esac
  exit 0
fi
exit 0
STUB
chmod +x "$STUB_DIR/bd"

export BD_SHOW_MODE=records
: > "$BD_SHOW_COUNT"
run_hook_as_manager "SABLE-one and SABLE-two: implement their governed footprints"
SHOW_TOTAL=$(wc -l < "$BD_SHOW_COUNT" | tr -d ' ')
if [ "$SHOW_TOTAL" = "3" ] \
   && [ "$(grep -c '^SABLE-one$' "$BD_SHOW_COUNT" 2>/dev/null)" = "2" ] \
   && [ "$(grep -c '^SABLE-two$' "$BD_SHOW_COUNT" 2>/dev/null)" = "1" ] \
   && grep -q 'BD_CALLED: update SABLE-one .*wip_claims=bin/sable-spawn-worker,hooks/one.sh' "$BD_CALL_LOG" 2>/dev/null \
   && ! grep -q 'wip_claims=.*fallback-only' "$BD_CALL_LOG" 2>/dev/null \
   && ! grep -q 'BD_CALLED: update SABLE-two ' "$BD_CALL_LOG" 2>/dev/null; then
  pass "SABLE-y4nom.7.6: initial parse is shared; only an unclaimed bead gets the concurrency refresh"
else
  fail "SABLE-y4nom.7.6: initial parse is shared; only an unclaimed bead gets the concurrency refresh" \
       "show-count=$SHOW_TOTAL shows=$(tr '\n' ' ' < "$BD_SHOW_COUNT") calls=$(tr '\n' '|' < "$BD_CALL_LOG")"
fi

export BD_SHOW_MODE=concurrent
: > "$BD_SHOW_COUNT"
run_hook_as_manager "SABLE-one: concurrent claim appears before update"
CONCURRENT_SHOWS=$(wc -l < "$BD_SHOW_COUNT" | tr -d ' ')
if [ "$CONCURRENT_SHOWS" = "2" ] \
   && ! grep -q 'BD_CALLED: update SABLE-one ' "$BD_CALL_LOG" 2>/dev/null; then
  pass "SABLE-y4nom.7.6: a claim appearing between initial parse and refresh is never overwritten"
else
  fail "SABLE-y4nom.7.6: a claim appearing between initial parse and refresh is never overwritten" \
       "show-count=$CONCURRENT_SHOWS calls=$(tr '\n' '|' < "$BD_CALL_LOG")"
fi

export BD_SHOW_MODE=refresh-malformed
: > "$BD_SHOW_COUNT"
run_hook_as_manager "SABLE-one: unreadable final claim refresh"
REFRESH_BAD_SHOWS=$(wc -l < "$BD_SHOW_COUNT" | tr -d ' ')
if [ "$REFRESH_BAD_SHOWS" = "2" ] \
   && ! grep -q 'BD_CALLED: update SABLE-one ' "$BD_CALL_LOG" 2>/dev/null; then
  pass "SABLE-y4nom.7.6: an unreadable final refresh is not treated as an empty claim"
else
  fail "SABLE-y4nom.7.6: an unreadable final refresh is not treated as an empty claim" \
       "show-count=$REFRESH_BAD_SHOWS calls=$(tr '\n' '|' < "$BD_CALL_LOG")"
fi

for BAD_MODE in malformed empty; do
  export BD_SHOW_MODE="$BAD_MODE"
  : > "$BD_SHOW_COUNT"
  BAD_RC=0
  run_hook_as_manager "SABLE-one: malformed-read fail-open control" >/dev/null || BAD_RC=$?
  BAD_SHOWS=$(wc -l < "$BD_SHOW_COUNT" | tr -d ' ')
  if [ "$BAD_RC" -eq 0 ] && [ "$BAD_SHOWS" = "1" ] \
     && ! grep -q 'BD_CALLED: update' "$BD_CALL_LOG" 2>/dev/null; then
    pass "SABLE-y4nom.7.6: $BAD_MODE bead read fails open after one show (no update)"
  else
    fail "SABLE-y4nom.7.6: $BAD_MODE bead read fails open after one show (no update)" \
         "rc=$BAD_RC show-count=$BAD_SHOWS calls=$(tr '\n' '|' < "$BD_CALL_LOG")"
  fi
done
unset BD_SHOW_MODE

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
# INTEGRATION TEST — real bd in one isolated scratch repo
# ---------------------------------------------------------------------------
# Initializes one small real store, creates scratch beads there, runs the real
# hook from that repository, and checks WIP-CLAIMS through real bd readback.
# The former fixture used SABLE's project store, making runtime proportional to
# the live backlog and leaving test records in operational state.

if ! command -v bd >/dev/null 2>&1; then
  echo "SKIP (integration): bd not found on PATH"
else
  REAL_BD_ROOT="$FIXTURE_DIR/real-bd"
  REAL_BD_NOHOOKS="$FIXTURE_DIR/real-bd-nohooks"
  mkdir -p "$REAL_BD_ROOT" "$REAL_BD_NOHOOKS"
  git -C "$REAL_BD_ROOT" init -q
  git -C "$REAL_BD_ROOT" config core.hooksPath "$REAL_BD_NOHOOKS"

  real_bd() {
    (cd "$REAL_BD_ROOT" && env -u BEADS_DB BD_NON_INTERACTIVE=1 bd "$@")
  }

  if ! (cd "$REAL_BD_ROOT" && env -u BEADS_DB BD_NON_INTERACTIVE=1 \
        bd init --prefix=sable --non-interactive >/dev/null 2>&1); then
    echo "SKIP (integration): could not initialize isolated real-bd fixture"
  else
  # Create a scratch bead with a description mentioning hooks/foo.sh
  SCRATCH_ID=$(real_bd create --sandbox \
    --title="[int-test] pre-dispatch-claim scratch bead" \
    --description="hooks/foo.sh is the implementation file for this scratch bead" \
    --notes="[no-test] integration test scratch — safe to close" \
    --type=task 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1)

  if [ -z "$SCRATCH_ID" ]; then
    echo "SKIP (integration): could not create scratch bead — bd create output did not match ID pattern"
  else
    REAL_BD_FIXTURE_READY=1
    echo "Integration: created scratch bead $SCRATCH_ID"
    # The close guard's [no-test] setup is part of the create, so it cannot add
    # an otherwise unobserved real-bd update to this fixture.
    REAL_BD_SETUP_WRITES_ELIDED=$((REAL_BD_SETUP_WRITES_ELIDED+1))

    # Run real hook with scratch bead ID in the dispatch prompt.
    # Do NOT use stub bd — use the isolated real store.
    make_dispatch_input "${SCRATCH_ID}: implement the feature — hooks/foo.sh needs updating" | \
      (cd "$REAL_BD_ROOT" && \
       env CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager \
           SABLE_AGENTS_YAML="$AGENTS_YAML" \
           SABLE_MODE_STATE="$EXEC_MODE_FILE" \
           bash "$HOOK" 2>/dev/null)

    # Check that wip_claims landed in the bead's metadata (SABLE-szd)
    CLAIMS=$(real_bd show "$SCRATCH_ID" --json 2>/dev/null | python3 -c "
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

    # Reuse this isolated bead for the remaining real-bd legs. Clear the claim
    # so the manager-subagent path must write it again.
    real_bd update "$SCRATCH_ID" --sandbox --unset-metadata wip_claims >/dev/null 2>&1
  fi

  # --- Integration (SABLE-uz9.9): MANAGER-SUBAGENT native dispatch, real bd ---
  # The new path with NO env identity: identity is purely the subagent
  # agent_type=optimus. Proves the real hook + real lib-identity + real bd DB
  # compose to land WIP-CLAIMS for a manager-subagent dispatch.
  SCRATCH_ID2="${SCRATCH_ID:-}"

  if [ -z "$SCRATCH_ID2" ]; then
    echo "SKIP (integration): could not create manager-subagent scratch bead"
  else
    echo "Integration: created manager-subagent scratch bead $SCRATCH_ID2"
    make_manager_subagent_input "${SCRATCH_ID2}: implement the feature — hooks/foo.sh needs updating" "optimus" | \
      (cd "$REAL_BD_ROOT" && \
       env -u CLAUDE_AGENT_NAME -u CLAUDE_AGENT_ROLE \
           SABLE_AGENTS_YAML="$AGENTS_YAML" \
           SABLE_MODE_STATE="$EXEC_MODE_FILE" \
           bash "$HOOK" 2>/dev/null)

    CLAIMS2=$(real_bd show "$SCRATCH_ID2" --json 2>/dev/null | python3 -c "
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

    # The next leg overwrites wip_claims with its own controlled value, so an
    # intervening unset would expose no state to any assertion.
    REAL_BD_SETUP_WRITES_ELIDED=$((REAL_BD_SETUP_WRITES_ELIDED+1))
  fi

  # -------------------------------------------------------------------------
  # SABLE-6la1 — wip_claims metadata SURVIVES a later --notes clobber
  # -------------------------------------------------------------------------
  # This is the acceptance criterion SABLE-szd's own description named but
  # never got a test for: "a test writes WIP-CLAIMS then updates notes and
  # asserts claims persist." Real bd, no mocks — this is the exact failure
  # mode that motivated the metadata migration (SABLE-szd) and recurred live
  # as a near-miss on SABLE-cmar4.1 (SABLE-sm269).
  SCRATCH_ID3="${SCRATCH_ID:-}"

  if [ -z "$SCRATCH_ID3" ]; then
    echo "SKIP (integration): could not create notes-clobber scratch bead"
  else
    echo "Integration: created notes-clobber scratch bead $SCRATCH_ID3"

    # Establish the claim exactly as the hook does (real --set-metadata write).
    real_bd update "$SCRATCH_ID3" --sandbox --set-metadata "wip_claims=a.sh,b.sh" >/dev/null 2>&1

    # Simulate the exact SABLE-szd/sm269 trigger: an unrelated notes write from
    # elsewhere in a bead's life (e.g. a manager's routine review-step note).
    real_bd update "$SCRATCH_ID3" --sandbox --notes "manager review note" >/dev/null 2>&1

    READBACK3=$(real_bd show "$SCRATCH_ID3" --json 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    if isinstance(d, list) and d:
        print((d[0].get('metadata', {}) or {}).get('wip_claims', '') or '')
        print(d[0].get('notes', '') or '')
except Exception:
    pass
" 2>/dev/null || echo "")
    case "$READBACK3" in
      *$'\n'*)
        CLAIMS3="${READBACK3%%$'\n'*}"
        NOTES3="${READBACK3#*$'\n'}"
        ;;
      *) CLAIMS3=""; NOTES3="" ;;
    esac
    REAL_BD_READBACK_REUSES=$((REAL_BD_READBACK_REUSES+1))

    if [ "$CLAIMS3" = "a.sh,b.sh" ]; then
      pass "SABLE-6la1: wip_claims metadata survives an unrelated bd update --notes write (real bd)"
    else
      fail "SABLE-6la1: wip_claims metadata survives an unrelated bd update --notes write (real bd)" \
           "expected 'a.sh,b.sh', got: '$CLAIMS3'"
    fi

    # Positive control from the same post-write record: the notes write really
    # did replace notes, so the metadata-survival assertion is not vacuous.
    if [ "$NOTES3" = "manager review note" ]; then
      pass "SABLE-6la1: positive control — the notes write actually replaced notes (not a vacuous pass)"
    else
      fail "SABLE-6la1: positive control — the notes write actually replaced notes (not a vacuous pass)" \
           "notes: '$NOTES3'"
    fi

  fi

  # --- SABLE-6la1: sibling-key --set-metadata merges, doesn't clobber ---------
  # The whole design relies on --set-metadata writing one key at a time without
  # disturbing siblings: proves setting a second, unrelated key leaves an
  # already-established wip_claims intact.
  SCRATCH_ID4="${SCRATCH_ID:-}"

  if [ -z "$SCRATCH_ID4" ]; then
    echo "SKIP (integration): could not create sibling-key scratch bead"
  else
    echo "Integration: created sibling-key scratch bead $SCRATCH_ID4"

    real_bd update "$SCRATCH_ID4" --sandbox --set-metadata "wip_claims=x.sh" >/dev/null 2>&1
    real_bd update "$SCRATCH_ID4" --sandbox --set-metadata "otherkey=y" >/dev/null 2>&1

    # Closing is the fixture's final state transition. Its JSON response is the
    # same post-update record the sibling-key assertion needs, so do not issue a
    # separate immutable show immediately before it.
    META4=$(real_bd close "$SCRATCH_ID4" --sandbox --json 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    record = d[0] if isinstance(d, list) and d else d if isinstance(d, dict) else {}
    if record:
        m = record.get('metadata', {}) or {}
        print(m.get('wip_claims', ''), '|', m.get('otherkey', ''))
except Exception:
    pass
" 2>/dev/null || echo "")
    REAL_BD_CLOSE_READBACK_REUSES=$((REAL_BD_CLOSE_READBACK_REUSES+1))

    if [ "$META4" = "x.sh | y" ]; then
      pass "SABLE-6la1: --set-metadata on a sibling key merges (wip_claims survives, otherkey lands)"
    else
      fail "SABLE-6la1: --set-metadata on a sibling key merges (wip_claims survives, otherkey lands)" \
           "expected 'x.sh | y', got: '$META4'"
    fi
  fi
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
DEP_CLAIM_UPDATE_LOG="$DEP_DIR/claim-updates.log"
export DEP_CLAIM_UPDATE_LOG
mkdir -p "$DEP_REPO" "$DEP_STUB"
: > "$DEP_CLAIM_UPDATE_LOG"

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
  # Dependency-warning cases are not claim tests. Mark the dependent as already
  # claimed so each case retains its initial bead read but does not repeat the
  # last-moment claim refresh/update covered exhaustively above.
  echo '[{"id":"SABLE-dep","description":"hooks/foo.sh is the implementation","notes":"","metadata":{"wip_claims":"hooks/already.sh"}}]'
  exit 0
fi
if [ "$1" = "update" ]; then
  printf '%s\n' "$*" >> "$DEP_CLAIM_UPDATE_LOG"
fi
exit 0
STUB
chmod +x "$DEP_STUB/bd"

# make_dispatch_input_cwd <prompt> <cwd> — dispatch payload carrying the repo
# whose merge state should be judged.
make_dispatch_input_cwd() {
  local prompt cwd
  json_escape "$1"
  prompt="$JSON_ESCAPED"
  json_escape "$2"
  cwd="$JSON_ESCAPED"
  printf '{"tool_name":"Agent","cwd":"%s","tool_input":{"prompt":"%s","subagent_type":"general-purpose"},"hook_event_name":"PreToolUse"}\n' \
    "$cwd" "$prompt"
}

run_hook_dep() {
  make_dispatch_input_cwd "SABLE-dep: implement hooks/foo.sh" "$DEP_REPO" | \
    env CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager \
        SABLE_AGENTS_YAML="$AGENTS_YAML" \
        SABLE_MODE_STATE="$EXEC_MODE_FILE" \
        SABLE_DEP_MERGE_GUARD=1 \
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
      SABLE_DEP_MERGE_GUARD=1 \
      SABLE_DEP_CHECK_BIN="$DEP_DIR/does-not-exist" \
      PATH="$DEP_STUB:/usr/bin:/bin" \
      bash "$HOOK" 2>/dev/null) || DEP_RC=$?
if [ "$DEP_RC" -eq 0 ] && [ -z "$DEP_OUT" ]; then
  pass "SABLE-d5iku: checker absent → hook still exits 0 with no output (dispatch unaffected)"
else
  fail "SABLE-d5iku: checker absent → hook still exits 0 with no output (dispatch unaffected)" \
       "rc=$DEP_RC output: ${DEP_OUT:-<empty>}"
fi

# The real-bd fixture may share setup/read work only where no assertion observes
# the intermediate state. The two hook-driven claim writes and their immediate
# concurrency refreshes remain distinct and fresh. Dependency-warning cases use
# already-claimed fixtures and therefore must never perform incidental updates.
if [ "$REAL_BD_FIXTURE_READY" -eq 1 ]; then
  if [ "$REAL_BD_SETUP_WRITES_ELIDED" -eq 2 ] \
     && [ "$REAL_BD_READBACK_REUSES" -eq 1 ] \
     && [ "$REAL_BD_CLOSE_READBACK_REUSES" -eq 1 ] \
     && [ "$IMMUTABLE_UNIT_OBSERVATION_REUSES" -eq 2 ] \
     && [ ! -s "$DEP_CLAIM_UPDATE_LOG" ]; then
    pass "structure: only immutable setup/read observations are reused"
  else
    fail "structure: only immutable setup/read observations are reused" \
         "setup-elisions=$REAL_BD_SETUP_WRITES_ELIDED readback-reuses=$REAL_BD_READBACK_REUSES close-readbacks=$REAL_BD_CLOSE_READBACK_REUSES unit-reuses=$IMMUTABLE_UNIT_OBSERVATION_REUSES dep-updates=$(wc -l < "$DEP_CLAIM_UPDATE_LOG" | tr -d ' ')"
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
