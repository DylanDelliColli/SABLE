#!/usr/bin/env bash
# test-agent-definitions.sh — Verifies the generated custom agent definitions
# under templates/agents/ (SABLE-uz9.2, one-window topology).
#
# Two layers:
#   1. Drift check: bin/sable-build-agents regenerated into a temp dir must be
#      byte-identical to the committed templates/agents/ files. The role files
#      are the single source of truth; hand-edits to generated files (or role
#      edits without a rebuild) fail here.
#   2. Contract assertions per agent: frontmatter well-formed (name matches
#      filename, non-empty description), v2 invocation preamble present, and
#      each role's load-bearing contract markers survived the conversion.
#
# Run with:
#   bash hooks/test/test-agent-definitions.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
AGENTS_DIR="$REPO/templates/agents"
BUILDER="$REPO/bin/sable-build-agents"
# Producers only (tmux-only, SABLE-qa4d.5): managers (optimus/tarzan) are warm
# panes whose role files are injected by session-role-anchor — they have no
# Agent-tool definitions any more. chuck/lincoln/gaudi were already excluded.
AGENTS="sherlock victor rudy columbo"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
assert_file() { if [ -f "$1" ]; then pass "$2"; else fail "$2" "missing: $1"; fi; }
assert_grep() { if grep -q -- "$2" "$1" 2>/dev/null; then pass "$3"; else fail "$3" "pattern not found: $2"; fi; }
assert_no_grep() { if grep -q -- "$2" "$1" 2>/dev/null; then fail "$3" "pattern unexpectedly present: $2"; else pass "$3"; fi; }

assert_file "$BUILDER" "builder script exists"

# --- Layer 1: drift check (generated == committed) ---
TMP_OUT="$(mktemp -d)"
trap 'rm -rf "$TMP_OUT"' EXIT
if python3 "$BUILDER" --out-dir "$TMP_OUT" >/dev/null 2>&1; then
  pass "builder regenerates cleanly"
else
  fail "builder regenerates cleanly" "bin/sable-build-agents exited nonzero"
fi
for name in $AGENTS; do
  if diff -q "$TMP_OUT/$name.md" "$AGENTS_DIR/$name.md" >/dev/null 2>&1; then
    pass "$name.md committed copy matches regeneration (no drift)"
  else
    fail "$name.md committed copy matches regeneration (no drift)" "re-run bin/sable-build-agents (or revert hand-edits to templates/agents/$name.md)"
  fi
done

# --- Layer 2: per-agent contract assertions ---
for name in $AGENTS; do
  DEF="$AGENTS_DIR/$name.md"
  assert_file "$DEF" "$name.md exists"
  [ -f "$DEF" ] || continue

  # frontmatter: first line is ---, name field matches filename, description non-empty
  if [ "$(head -n1 "$DEF")" = "---" ]; then
    pass "$name.md opens with frontmatter"
  else
    fail "$name.md opens with frontmatter" "first line is not ---"
  fi
  assert_grep "$DEF" "^name: $name\$" "$name.md frontmatter name matches filename"
  if grep -q "^description: .\+" "$DEF"; then
    pass "$name.md has a non-empty description"
  else
    fail "$name.md has a non-empty description"
  fi
  assert_grep "$DEF" "GENERATED from templates/multi-manager/roles/$name.md" "$name.md carries the generated-file marker"
  # Producers carry the v2 one-window subagent preamble (they remain Agent-tool
  # planning subagents; managers are panes and have no defs).
  assert_grep "$DEF" "v2 invocation (one-window topology)" "$name.md (producer) carries the v2 invocation preamble"
done

# load-bearing contract markers per role (the conversion must not lose these)
assert_grep "$AGENTS_DIR/sherlock.md" "Fingerprint"            "sherlock keeps the fingerprint citation contract"
assert_grep "$AGENTS_DIR/sherlock.md" "sherlock:research"      "sherlock keeps the greenfield research category"
assert_grep "$AGENTS_DIR/victor.md"   "victor-validated-at"    "victor keeps the validation marker format"
assert_grep "$AGENTS_DIR/victor.md"   "first 5 runs"           "victor keeps the auto-close ramp-up gate"
assert_grep "$AGENTS_DIR/rudy.md"     "never. Catastrophic"    "rudy keeps the production prohibition"
assert_grep "$AGENTS_DIR/rudy.md"     "SABLE_RUDY_BASE_URL"    "rudy keeps the target env-var config"
assert_grep "$AGENTS_DIR/columbo.md"  "columbo-test-spec"      "columbo keeps the test-spec bead label"
assert_grep "$AGENTS_DIR/columbo.md"  "it.todo"                "columbo keeps the skeleton-file contract"

# --- SABLE-qa4d.5: managers and the teams build are GONE ---
# Managers are warm panes (role files under templates/multi-manager/roles/ are
# their identity source); the agent-teams topology was removed entirely.
assert_absent() { if [ -e "$1" ]; then fail "$2" "unexpectedly present: $1"; else pass "$2"; fi; }
assert_absent "$AGENTS_DIR/optimus.md" "no optimus agent def (managers are panes)"
assert_absent "$AGENTS_DIR/tarzan.md"  "no tarzan agent def (managers are panes)"
assert_absent "$AGENTS_DIR/chuck.md"   "no chuck agent def (merge queue is a pane)"
assert_absent "$REPO/templates/agents-teams" "no agents-teams build target remains"
assert_absent "$REPO/templates/multi-manager/cards/teams-coordination.md" "no teams coordination card remains"
# The builder no longer accepts a teams mode.
if python3 "$BUILDER" --mode teams --out-dir "$(mktemp -d)" >/dev/null 2>&1; then
  fail "builder rejects --mode (single-target build)" "exited 0"
else
  pass "builder rejects --mode (single-target build)"
fi
# The manager role files (pane identity source) still exist.
for r in lincoln optimus tarzan chuck; do
  assert_file "$REPO/templates/multi-manager/roles/$r.md" "role file $r.md still present (pane identity source)"
done

# ---------------------------------------------------------------------------
# SABLE-m22: per-agent capability preamble (implements SABLE-chk)
# ---------------------------------------------------------------------------
# Case 1: no generated def carries the stale "no Agent tool" claim — false since
# the nested-spawn spike (SABLE-d50.1, CC 2.1.173) and actively harmful (forced
# producers into single-threaded exploration).
for name in $AGENTS; do
  assert_no_grep "$AGENTS_DIR/$name.md" "no Agent tool" "$name.md preamble drops the stale 'no Agent tool' claim"
done

# Case 2: consistency — no file may carry BOTH the old and new capability strings
# (a half-regenerated def would whipsaw the agent at runtime).
for name in $AGENTS; do
  DEF="$AGENTS_DIR/$name.md"
  if grep -qi "no Agent tool" "$DEF" 2>/dev/null && grep -qi "Agent tool IS available" "$DEF" 2>/dev/null; then
    fail "$name.md not self-contradictory (not both 'no Agent tool' + 'Agent tool IS available')" "carries both strings"
  else
    pass "$name.md not self-contradictory (not both 'no Agent tool' + 'Agent tool IS available')"
  fi
done

# Case 3: producers carry the read-only-children capability text and NOT the
# manager direct-dispatch text.
for name in $AGENTS; do
  DEF="$AGENTS_DIR/$name.md"
  assert_grep    "$DEF" "Agent tool IS available"     "$name.md (producer) states the Agent tool is available"
  assert_grep    "$DEF" "READ-ONLY children"          "$name.md (producer) may spawn read-only Explore children"
  assert_grep    "$DEF" "may NOT spawn code-writing"  "$name.md (producer) may not spawn code-writing workers"
  assert_no_grep "$DEF" "dispatch your own workers"   "$name.md (producer) lacks the manager direct-dispatch text"
done

# Case 7: regeneration is idempotent — two consecutive runs are byte-identical.
IDEM_A="$(mktemp -d)"; IDEM_B="$(mktemp -d)"
python3 "$BUILDER" --out-dir "$IDEM_A" >/dev/null 2>&1
python3 "$BUILDER" --out-dir "$IDEM_B" >/dev/null 2>&1
if diff -rq "$IDEM_A" "$IDEM_B" >/dev/null 2>&1; then
  pass "regeneration is idempotent (two runs byte-identical)"
else
  fail "regeneration is idempotent (two runs byte-identical)" "consecutive builds differ"
fi
rm -rf "$IDEM_A" "$IDEM_B"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
