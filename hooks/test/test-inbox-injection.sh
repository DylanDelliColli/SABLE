#!/usr/bin/env bash
# test-inbox-injection.sh — behavior tests for inbox-injection.sh +
# inbox-injection-precompact.sh after the SABLE-uz9.3 identity rewrite.
# Uses a stub `bd` on PATH returning fixture inbox JSON; asserts injection
# for manager identities in BOTH modes (subagent agent_type + legacy env),
# per-identity dedup within a shared session, and worker exclusion.
#
# Run with:
#   bash hooks/test/test-inbox-injection.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO/hooks/multi-manager/inbox-injection.sh"
PRECOMPACT="$REPO/hooks/multi-manager/inbox-injection-precompact.sh"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

FIXTURE_DIR="$(mktemp -d)"
SESSION="testsess-$$"
trap 'rm -rf "$FIXTURE_DIR"; rm -f "/tmp/inbox-seen-${SESSION}" "/tmp/inbox-seen-${SESSION}-"*' EXIT

cat > "$FIXTURE_DIR/agents.yaml" <<'YAML'
agents:
  optimus:
    type: epic_manager
  tarzan:
    type: one_off_manager
  sherlock:
    type: auditor
YAML
export SABLE_AGENTS_YAML="$FIXTURE_DIR/agents.yaml"

# Stub bd: `bd ready -l for-<name> --json` returns one bead addressed to <name>
mkdir -p "$FIXTURE_DIR/bin"
cat > "$FIXTURE_DIR/bin/bd" <<'STUB'
#!/usr/bin/env bash
label=""
prev=""
for a in "$@"; do
  [ "$prev" = "-l" ] && label="$a"
  prev="$a"
done
name="${label#for-}"
echo "[{\"id\": \"SABLE-${name}1\", \"title\": \"coord item for ${name}\", \"priority\": 1}]"
STUB
chmod +x "$FIXTURE_DIR/bin/bd"
export PATH="$FIXTURE_DIR/bin:$PATH"

json() { # <agent_id> <agent_type>
  python3 -c "
import json, sys
aid, atype = sys.argv[1], sys.argv[2]
d = {'tool_name': 'Bash', 'tool_input': {'command': 'echo x'}, 'session_id': '$SESSION'}
if aid: d['agent_id'] = aid
if atype: d['agent_type'] = atype
print(json.dumps(d))
" "$1" "$2"
}

run_hook() { # <json> <env_name> <env_role>
  (
    unset CLAUDE_AGENT_NAME CLAUDE_AGENT_ROLE
    [ -n "$2" ] && export CLAUDE_AGENT_NAME="$2"
    [ -n "$3" ] && export CLAUDE_AGENT_ROLE="$3"
    printf '%s' "$1" | bash "$HOOK" 2>/dev/null
  )
}

# 1. Manager subagent gets its inbox injected (the v2 change)
OUT=$(run_hook "$(json a1 optimus)" "" "")
if printf '%s' "$OUT" | grep -q "INBOX (OPTIMUS)"; then
  pass "manager subagent (optimus) receives inbox injection"
else
  fail "manager subagent (optimus) receives inbox injection" "got: ${OUT:-<empty>}"
fi
printf '%s' "$OUT" | grep -q "SABLE-optimus1" && pass "injection lists the addressed bead" || fail "injection lists the addressed bead"

# 2. Dedup: same identity, second call is silent
OUT=$(run_hook "$(json a1 optimus)" "" "")
[ -z "$OUT" ] && pass "second call deduped for same identity" || fail "second call deduped for same identity" "got: $OUT"

# 3. Sibling manager subagent in the SAME session still gets its own inbox
OUT=$(run_hook "$(json a2 tarzan)" "" "")
if printf '%s' "$OUT" | grep -q "INBOX (TARZAN)"; then
  pass "sibling manager (tarzan) has independent dedup in shared session"
else
  fail "sibling manager (tarzan) has independent dedup in shared session" "got: ${OUT:-<empty>}"
fi

# 4. Worker subagent excluded
OUT=$(run_hook "$(json a3 Explore)" "" "")
[ -z "$OUT" ] && pass "worker subagent excluded from inbox" || fail "worker subagent excluded from inbox" "got: $OUT"

# 5. Registered non-manager (sherlock) excluded
OUT=$(run_hook "$(json a4 sherlock)" "" "")
[ -z "$OUT" ] && pass "planning agent (sherlock) excluded from inbox" || fail "planning agent (sherlock) excluded from inbox" "got: $OUT"

# 6. Worker inside a manager terminal excluded (contamination fix)
OUT=$(run_hook "$(json a5 general-purpose)" "optimus" "manager")
[ -z "$OUT" ] && pass "worker in manager terminal excluded (env ignored)" || fail "worker in manager terminal excluded (env ignored)" "got: $OUT"

# 7. Legacy env manager main session still injected (dual-mode)
OUT=$(run_hook "$(json '' '')" "chuck" "manager")
if printf '%s' "$OUT" | grep -q "INBOX (CHUCK)"; then
  pass "legacy env manager (chuck) injected"
else
  fail "legacy env manager (chuck) injected" "got: ${OUT:-<empty>}"
fi

# 8. Anonymous main session silent
OUT=$(run_hook "$(json '' '')" "" "")
[ -z "$OUT" ] && pass "anonymous main session silent" || fail "anonymous main session silent" "got: $OUT"

# 9. PreCompact clears all per-identity dedup files for the session
ls "/tmp/inbox-seen-${SESSION}-"* >/dev/null 2>&1 || fail "precondition: dedup files exist" "none found"
printf '{"session_id": "%s"}' "$SESSION" | bash "$PRECOMPACT" 2>/dev/null
if ls "/tmp/inbox-seen-${SESSION}-"* >/dev/null 2>&1; then
  fail "precompact clears per-identity dedup files"
else
  pass "precompact clears per-identity dedup files"
fi

# 10. After precompact, re-injection happens (re-orientation)
OUT=$(run_hook "$(json a1 optimus)" "" "")
printf '%s' "$OUT" | grep -q "INBOX (OPTIMUS)" && pass "post-compact re-injection re-announces" || fail "post-compact re-injection re-announces" "got: ${OUT:-<empty>}"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
