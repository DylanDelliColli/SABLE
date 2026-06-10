#!/usr/bin/env bash
# test-tdd-gate.sh — Unit tests for tdd-gate.sh, focused on the bd-close
# argument parser that decides whether [no-test] escape hatch applies.
#
# The gate parses the close command, strips flags, counts remaining tokens
# (bead IDs), and only consults the [no-test] notes hatch when count == 1.
# Any flag form that fails to strip silently inflates the count and
# bypasses the hatch — that's the regression these tests guard against.
#
# Run with:
#   bash hooks/test/test-tdd-gate.sh
#
# Strategy:
#   - Each test invokes the gate with a synthetic single-bead close command.
#   - Setup creates a fake bead lookup by writing a fixture JSON (we can't
#     mock `bd show`, so we test parsing behavior by checking exit code +
#     output: the gate exits 0 on the no-test path. If parsing strips the
#     flag correctly, count==1, the gate consults bd show, finds [no-test],
#     and exits 0 silently. If parsing fails, count==2, the hatch is
#     skipped and the gate proceeds to the evidence check.
#   - We use a session id that has no evidence file, so a no-evidence path
#     produces a deny JSON. Then: silence == hatch worked, deny JSON ==
#     hatch was skipped (parser missed the flag).

set -uo pipefail

HOOK="$(cd "$(dirname "$0")/.." && pwd)/tdd-gate.sh"

if [ ! -x "$HOOK" ]; then
  echo "FAIL: hook not executable at $HOOK"
  exit 2
fi

# Need a real, currently-open bead with [no-test] in notes for the hatch to
# fire. Use the bead this very work is being done under (SABLE-1n2) — its
# notes carry [no-test] context once we mark it. To stay self-contained,
# the test creates a throwaway test bead, marks notes, runs assertions,
# then closes it.

# Tests run as part of bd commits; can't create real beads in CI. Instead,
# stub `bd` on PATH to return a canned JSON response. The gate calls
# `bd show "$BEAD_ID" --json`; we intercept it.

PASS=0
FAIL=0
FAIL_NAMES=""

# Make a temp bin/bd that returns a canned `[no-test]` notes payload for
# any single-arg `show ... --json` call.
STUB_DIR=$(mktemp -d)
trap 'rm -rf "$STUB_DIR"' EXIT

cat > "$STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
# Stub bd that returns a single-bead [no-test] notes for show --json
if [ "$1" = "show" ] && [[ "$*" == *"--json"* ]]; then
  cat <<'JSON'
[{"id":"SABLE-stub","notes":"[no-test] stub bead for parser tests"}]
JSON
  exit 0
fi
exit 0
EOF
chmod +x "$STUB_DIR/bd"

make_input() {
  # $1 = command, $2 = session_id (use a junk id with no evidence file)
  python3 -c "
import json, sys
print(json.dumps({'tool_input': {'command': sys.argv[1]}, 'session_id': sys.argv[2]}))
" "$1" "$2"
}

# Fresh fake session id per test; never collides with real evidence.
fake_session() {
  printf 'tdd-gate-test-%s-%s' "$$" "$RANDOM"
}

# run_gate <command> → <exit-code><tab><stdout>
run_gate() {
  local command="$1"
  local sid
  sid=$(fake_session)
  local evidence="/tmp/tdd-evidence-${sid}"
  rm -f "$evidence"  # ensure no-evidence path
  local out
  out=$(make_input "$command" "$sid" | env PATH="$STUB_DIR:$PATH" bash "$HOOK" 2>/dev/null)
  echo -n "$out"
}

# assert_hatch_used <name> <command>
# Expects silent allow (gate parsed args correctly, found [no-test], exited 0).
assert_hatch_used() {
  local name="$1" command="$2"
  local out
  out=$(run_gate "$command")
  if [ -z "$out" ]; then
    PASS=$((PASS+1))
    echo "PASS: $name"
  else
    FAIL=$((FAIL+1))
    FAIL_NAMES="$FAIL_NAMES\n  $name (got: $out)"
    echo "FAIL: $name"
    echo "  Expected: silent allow (parser stripped the flag, hatch consulted)"
    echo "  Got:      $out"
  fi
}

# assert_hatch_skipped <name> <command>
# Expects deny JSON (parser failed to strip, count > 1, hatch skipped, no
# evidence → deny). Used to confirm baseline state of bugs PRE-fix.
assert_hatch_skipped() {
  local name="$1" command="$2"
  local out
  out=$(run_gate "$command")
  if echo "$out" | grep -q '"permissionDecision": "deny"'; then
    PASS=$((PASS+1))
    echo "PASS: $name"
  else
    FAIL=$((FAIL+1))
    FAIL_NAMES="$FAIL_NAMES\n  $name (got: $out)"
    echo "FAIL: $name"
    echo "  Expected: deny JSON (parser missed flag, hatch skipped)"
    echo "  Got:      $out"
  fi
}

# ---------- Currently-working forms (regression guard) ----------

assert_hatch_used "bare ID, no flags"                 "bd close SABLE-stub"
assert_hatch_used "--reason=text (equals, unquoted)"  'bd close SABLE-stub --reason=because'
assert_hatch_used "--reason=\"text\" (equals, quoted)" 'bd close SABLE-stub --reason="because of foo"'

# ---------- New: space-separated flag forms (SABLE-1n2 fix) ----------

assert_hatch_used "--reason text (space, unquoted)" \
  'bd close SABLE-stub --reason because'

assert_hatch_used "--reason \"text\" (space, double-quoted)" \
  'bd close SABLE-stub --reason "because of foo"'

assert_hatch_used "--reason '\''text'\'' (space, single-quoted)" \
  "bd close SABLE-stub --reason 'because of foo'"

assert_hatch_used "multiple flags, mixed quote styles" \
  'bd close SABLE-stub --reason "first" --suggest-next'

# ---------- Multi-bead close path (intentionally skipped — hatch only fires for single-ID) ----------

assert_hatch_skipped "two beads with --reason= still routes to evidence check" \
  'bd close SABLE-stub SABLE-other --reason=cleanup'

assert_hatch_skipped "two beads with --reason \"text\" still routes to evidence check" \
  'bd close SABLE-stub SABLE-other --reason "cleanup batch"'

# ---------- Piped / redirected / chained single-bead close (SABLE-sqz fix) ----------
# Pipes, redirects, and command chains in the close command must not inflate
# the bead-ID count. Only tokens shaped like bead IDs (e.g. SABLE-stub,
# SABLE-rjv.1, BEADS-abc) should be counted.

assert_hatch_used "single bead piped to tail" \
  'bd close SABLE-stub | tail -3'

assert_hatch_used "single bead redirected stderr to stdout" \
  'bd close SABLE-stub 2>&1'

assert_hatch_used "single bead piped + tail with stderr merge" \
  'bd close SABLE-stub 2>&1 | tail -3'

assert_hatch_used "single bead redirected to file" \
  'bd close SABLE-stub > /tmp/out'

assert_hatch_used "single bead chained with &&" \
  'bd close SABLE-stub && echo done'

assert_hatch_used "single bead chained with semicolon" \
  'bd close SABLE-stub ; echo done'

assert_hatch_used "single bead with --reason and a pipe" \
  'bd close SABLE-stub --reason "shipped" | tail -3'

# Multi-bead with pipes still routes to evidence check (preserves existing
# behavior — pipes don't magically convert multi-bead into single-bead).
assert_hatch_skipped "two beads piped to tail still routes to evidence check" \
  'bd close SABLE-stub SABLE-other 2>&1 | tail -3'

# Dotted child bead IDs are recognized (SABLE-rjv.1, SABLE-rjv.12).
assert_hatch_used "dotted child bead ID alone" \
  'bd close SABLE-rjv.1'

assert_hatch_used "dotted child bead ID piped" \
  'bd close SABLE-rjv.1 2>&1'

# ---------- New: lowercase-prefix rigs (SABLE-i2m fix) ----------
# Bead prefixes can be lowercase (twine-*, chess-*) on some rigs.
# The ID_PATTERN regex must accept any-case prefixes so the [no-test]
# escape hatch fires for single-close on lowercase IDs.

assert_hatch_used "lowercase bare ID" \
  'bd close twine-stub'

assert_hatch_used "lowercase ID with --reason" \
  'bd close twine-stub --reason "docs only"'

assert_hatch_used "lowercase ID piped" \
  'bd close twine-stub 2>&1 | tail -3'

assert_hatch_used "lowercase dotted child ID" \
  'bd close chess-ab.1'

assert_hatch_skipped "two lowercase beads still route to evidence check" \
  'bd close twine-stub twine-other --reason=cleanup'

# ---------- New: flag values that look like IDs (SABLE-3uw / SABLE-9we fix) ----------
# Flag values such as 'docs-only' or 'shipped-v2' match the bead-ID shape
# (PREFIX-suffix). They must NOT inflate ID_COUNT.

assert_hatch_used "single bead, --reason docs-only (space-separated)" \
  'bd close SABLE-stub --reason docs-only'

assert_hatch_used "single bead, --reason shipped-v2 (space-separated)" \
  'bd close SABLE-stub --reason shipped-v2'

assert_hatch_used "single bead, --reason \"docs-only\" (space, double-quoted)" \
  'bd close SABLE-stub --reason "docs-only"'

assert_hatch_used "single bead, --reason '\''docs-only'\'' (space, single-quoted)" \
  "bd close SABLE-stub --reason 'docs-only'"

assert_hatch_used "single bead, multiple flags with flag-value IDs" \
  'bd close SABLE-stub --reason docs-only --suggest-next follow-up-v2'

# Multi-bead close with flag values must still route to evidence check
assert_hatch_skipped "two beads + flag value still routes to evidence check" \
  'bd close SABLE-stub SABLE-other --reason docs-only'

# ---------- Summary ----------

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="

if [ "$FAIL" -gt 0 ]; then
  echo -e "Failed tests:$FAIL_NAMES"
  exit 1
fi
exit 0
