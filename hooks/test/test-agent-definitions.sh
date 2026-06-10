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
AGENTS="optimus tarzan sherlock victor rudy columbo"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
assert_file() { if [ -f "$1" ]; then pass "$2"; else fail "$2" "missing: $1"; fi; }
assert_grep() { if grep -q -- "$2" "$1" 2>/dev/null; then pass "$3"; else fail "$3" "pattern not found: $2"; fi; }

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
  assert_grep "$DEF" "v2 invocation (one-window topology)" "$name.md carries the v2 invocation preamble"
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
assert_grep "$AGENTS_DIR/optimus.md"  "DISPATCH-REQUEST"       "optimus carries the dispatch-request protocol"
assert_grep "$AGENTS_DIR/optimus.md"  "RESIDENT"               "optimus is a resident manager (one spawn per session)"
assert_grep "$AGENTS_DIR/optimus.md"  "shift-report"           "optimus carries the shift-change protocol"
assert_grep "$AGENTS_DIR/tarzan.md"   "RESIDENT"               "tarzan is a resident manager (one spawn per session)"
assert_grep "$AGENTS_DIR/optimus.md"  "has-parent"             "optimus keeps the epic claim filter"
assert_grep "$AGENTS_DIR/optimus.md"  "APPROVE-PUSH"           "optimus carries the review verdict protocol"
assert_grep "$AGENTS_DIR/tarzan.md"   "DISPATCH-REQUEST"       "tarzan carries the dispatch-request protocol"
assert_grep "$AGENTS_DIR/tarzan.md"   "no-parent"              "tarzan keeps the orphan claim filter"
assert_grep "$AGENTS_DIR/tarzan.md"   "emergency"              "tarzan keeps emergency mode"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
