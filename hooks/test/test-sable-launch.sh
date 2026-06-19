#!/usr/bin/env bash
# test-sable-launch.sh — SABLE-njiv
# Unit + integration tests for bin/sable-launch: the one-command operator entry
# point that sets agent identity/role and verifies (warn-only) the teams flag,
# then exec's claude. A stub `claude` on PATH echoes the env it received so we
# can assert identity is set correctly and that exec actually happened.
#
# Run with:
#   bash hooks/test/test-sable-launch.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
LAUNCH="$REPO/bin/sable-launch"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

[ -x "$LAUNCH" ] || { fail "sable-launch is executable"; echo "Tests: 1 | Passed: 0 | Failed: 1"; exit 1; }
pass "sable-launch is executable"

# ---- Stub `claude` on PATH: echoes identity env + args, then exits 0 ----
STUB_BIN=$(mktemp -d)
cat > "$STUB_BIN/claude" <<'STUB'
#!/usr/bin/env bash
echo "STUB_EXEC=1"
echo "NAME=${CLAUDE_AGENT_NAME:-}"
echo "ROLE=${CLAUDE_AGENT_ROLE:-}"
echo "ARGS=$*"
STUB
chmod +x "$STUB_BIN/claude"

# HOME with the teams flag present in settings.json
HOME_FLAG=$(mktemp -d)
mkdir -p "$HOME_FLAG/.claude"
cat > "$HOME_FLAG/.claude/settings.json" <<'JSON'
{ "env": { "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1" } }
JSON

# HOME without the flag (empty settings.json)
HOME_NOFLAG=$(mktemp -d)
mkdir -p "$HOME_NOFLAG/.claude"
cat > "$HOME_NOFLAG/.claude/settings.json" <<'JSON'
{ "env": {} }
JSON

# run <label> <home> <env_prefix> <args...> -> sets OUT/ERR/CODE globals
run_launch() {
  local home="$1" env_prefix="$2"; shift 2
  local tmpout tmperr
  tmpout=$(mktemp); tmperr=$(mktemp)
  env -i PATH="$STUB_BIN:$PATH" HOME="$home" $env_prefix bash "$LAUNCH" "$@" >"$tmpout" 2>"$tmperr"
  CODE=$?
  OUT=$(cat "$tmpout"); ERR=$(cat "$tmperr")
  rm -f "$tmpout" "$tmperr"
}

# ---- UNIT: identity ----

run_launch "$HOME_FLAG" ""
if printf '%s' "$OUT" | grep -q "NAME=lincoln" && printf '%s' "$OUT" | grep -q "ROLE=manager"; then
  pass "default role -> CLAUDE_AGENT_NAME=lincoln, ROLE=manager, exec'd claude"
else
  fail "default role -> lincoln/manager" "out=[$OUT] err=[$ERR] code=$CODE"
fi

run_launch "$HOME_FLAG" "" chuck
if printf '%s' "$OUT" | grep -q "NAME=chuck" && printf '%s' "$OUT" | grep -q "ROLE=manager"; then
  pass "explicit role chuck -> CLAUDE_AGENT_NAME=chuck"
else
  fail "explicit role chuck" "out=[$OUT] err=[$ERR] code=$CODE"
fi

run_launch "$HOME_FLAG" "" bogus
if [ "$CODE" -ne 0 ] && ! printf '%s' "$OUT" | grep -q "STUB_EXEC=1" && printf '%s' "$ERR" | grep -qi "role"; then
  pass "unknown role -> non-zero exit, usage on stderr, claude NOT exec'd"
else
  fail "unknown role rejected" "out=[$OUT] err=[$ERR] code=$CODE"
fi

run_launch "$HOME_FLAG" "" --help
if [ "$CODE" -eq 0 ] && printf '%s' "$OUT$ERR" | grep -qi "usage" && ! printf '%s' "$OUT" | grep -q "STUB_EXEC=1"; then
  pass "--help -> exit 0, prints usage, claude NOT exec'd"
else
  fail "--help prints usage" "out=[$OUT] err=[$ERR] code=$CODE"
fi

# ---- UNIT: args passthrough ----

run_launch "$HOME_FLAG" "" lincoln --resume sess-123
if printf '%s' "$OUT" | grep -q "NAME=lincoln" && printf '%s' "$OUT" | grep -q -- "--resume sess-123"; then
  pass "args after role are passed through to claude"
else
  fail "args passthrough after role" "out=[$OUT] err=[$ERR] code=$CODE"
fi

run_launch "$HOME_FLAG" "" --dangerously-skip-permissions
if printf '%s' "$OUT" | grep -q "NAME=lincoln" && printf '%s' "$OUT" | grep -q -- "--dangerously-skip-permissions"; then
  pass "leading claude flag -> default role lincoln, flag passed through"
else
  fail "leading flag passthrough with default role" "out=[$OUT] err=[$ERR] code=$CODE"
fi

# ---- INTEGRATION: teams-flag verify (warn-only, never write) ----

run_launch "$HOME_NOFLAG" ""
if printf '%s' "$ERR" | grep -q "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" && printf '%s' "$OUT" | grep -q "NAME=lincoln"; then
  pass "missing teams flag -> warning on stderr AND still exec's claude (graceful)"
else
  fail "missing flag warns + continues" "out=[$OUT] err=[$ERR] code=$CODE"
fi

# It must NOT have written the flag into settings.json (locked decision)
if grep -q "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" "$HOME_NOFLAG/.claude/settings.json"; then
  fail "never writes settings.json" "settings.json was modified to add the flag"
else
  pass "never writes the flag into settings.json (locked decision)"
fi

run_launch "$HOME_FLAG" ""
if ! printf '%s' "$ERR" | grep -q "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"; then
  pass "flag present in settings.json -> no warning"
else
  fail "no warning when flag present" "err=[$ERR]"
fi

# Flag present in the process env (exported) also suppresses the warning
run_launch "$HOME_NOFLAG" "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1"
if ! printf '%s' "$ERR" | grep -q "Add .*CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"; then
  pass "flag present in process env -> no settings.json warning"
else
  fail "env flag suppresses warning" "err=[$ERR]"
fi

# Cleanup
rm -rf "$STUB_BIN" "$HOME_FLAG" "$HOME_NOFLAG"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
