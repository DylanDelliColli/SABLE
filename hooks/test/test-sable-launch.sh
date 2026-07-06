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

# ---- UNIT: session form (no-arg = bring up + attach; SABLE-ssws.2) ----
# Stub tmux records argv; has-session behavior driven by STUB_HAS_SESSION.
cat > "$STUB_BIN/tmux" <<'STUB'
#!/usr/bin/env bash
[ -n "${TMUX_LOG:-}" ] && echo "tmux $*" >> "$TMUX_LOG"
case "$1" in
  has-session)      exit "${STUB_HAS_SESSION:-1}" ;;
  attach|attach-session) echo "STUB_ATTACH=1"; exit 0 ;;
  display-message)  echo "not-sable"; exit 0 ;;
esac
exit 0
STUB
chmod +x "$STUB_BIN/tmux"
# Recording sable-tmux stand-in (selected via SABLE_TMUX_BIN)
cat > "$STUB_BIN/sable-tmux-rec" <<'STUB'
#!/usr/bin/env bash
[ -n "${ST_LOG:-}" ] && echo "sable-tmux $*" >> "$ST_LOG"
exit 0
STUB
chmod +x "$STUB_BIN/sable-tmux-rec"

WORK="$(mktemp -d)"
# 1. no session yet -> creates via sable-tmux --autostart, then attaches
run_launch "$HOME_FLAG" "TMUX_LOG=$WORK/t1.log ST_LOG=$WORK/s1.log STUB_HAS_SESSION=1 SABLE_TMUX_BIN=$STUB_BIN/sable-tmux-rec"
if [ "$CODE" -eq 0 ] && printf '%s' "$OUT" | grep -q "STUB_ATTACH=1" && grep -q -- "--autostart" "$WORK/s1.log" 2>/dev/null; then
  pass "no-arg + no session -> sable-tmux --autostart then attach"
else
  fail "no-arg + no session -> sable-tmux --autostart then attach" "out=[$OUT] err=[$ERR] code=$CODE s1=[$(cat "$WORK/s1.log" 2>/dev/null)]"
fi

# 2. session already exists -> reuses (no sable-tmux), attaches
run_launch "$HOME_FLAG" "TMUX_LOG=$WORK/t2.log ST_LOG=$WORK/s2.log STUB_HAS_SESSION=0 SABLE_TMUX_BIN=$STUB_BIN/sable-tmux-rec"
if [ "$CODE" -eq 0 ] && printf '%s' "$OUT" | grep -q "STUB_ATTACH=1" && [ ! -s "$WORK/s2.log" ]; then
  pass "no-arg + existing session -> reuse (no re-create) then attach"
else
  fail "no-arg + existing session -> reuse then attach" "out=[$OUT] err=[$ERR] code=$CODE s2=[$(cat "$WORK/s2.log" 2>/dev/null)]"
fi

# 3. --no-attach -> brings up only, never attaches
run_launch "$HOME_FLAG" "TMUX_LOG=$WORK/t3.log ST_LOG=$WORK/s3.log STUB_HAS_SESSION=1 SABLE_TMUX_BIN=$STUB_BIN/sable-tmux-rec" --no-attach
if [ "$CODE" -eq 0 ] && ! printf '%s' "$OUT" | grep -q "STUB_ATTACH=1" && grep -q -- "--autostart" "$WORK/s3.log" 2>/dev/null; then
  pass "--no-attach brings up the session without attaching"
else
  fail "--no-attach brings up the session without attaching" "out=[$OUT] err=[$ERR] code=$CODE"
fi

# 4. tmux missing -> clear error naming tmux, non-zero exit
EMPTY_BIN="$(mktemp -d)"
ln -s "$(command -v bash)" "$EMPTY_BIN/bash"
OUT4="$(env -i PATH="$EMPTY_BIN" HOME="$HOME_FLAG" bash "$LAUNCH" 2>&1)"; CODE4=$?
if [ "$CODE4" -ne 0 ] && printf '%s' "$OUT4" | grep -qi "tmux"; then
  pass "missing tmux -> non-zero exit with a message naming tmux"
else
  fail "missing tmux -> non-zero exit with a message naming tmux" "out=[$OUT4] code=$CODE4"
fi
rm -rf "$EMPTY_BIN"

# ---- UNIT: role form (advanced single-pane door — unchanged) ----

run_launch "$HOME_FLAG" "" lincoln
if printf '%s' "$OUT" | grep -q "NAME=lincoln" && printf '%s' "$OUT" | grep -q "ROLE=manager"; then
  pass "explicit role lincoln -> CLAUDE_AGENT_NAME=lincoln, ROLE=manager, exec'd claude"
else
  fail "explicit role lincoln -> lincoln/manager" "out=[$OUT] err=[$ERR] code=$CODE"
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

# ---- INTEGRATION: no teams-flag machinery remains (tmux-only, SABLE-qa4d) ----

run_launch "$HOME_NOFLAG" "" lincoln
if ! printf '%s' "$ERR" | grep -q "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" && printf '%s' "$OUT" | grep -q "NAME=lincoln"; then
  pass "no teams-flag warning is ever printed (tmux-only)"
else
  fail "no teams-flag warning is ever printed (tmux-only)" "out=[$OUT] err=[$ERR] code=$CODE"
fi

# It must not have touched settings.json
if grep -q "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" "$HOME_NOFLAG/.claude/settings.json"; then
  fail "never writes settings.json" "settings.json was modified to add the flag"
else
  pass "never writes settings.json"
fi

# Static: the launcher itself carries no teams-flag machinery
if ! grep -q "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" "$LAUNCH"; then
  pass "sable-launch carries no teams-flag machinery"
else
  fail "sable-launch carries no teams-flag machinery" "flag still referenced in $LAUNCH"
fi

# ---- INTEGRATION: real tmux on an isolated socket (SABLE-ssws.2) ----
if command -v tmux >/dev/null 2>&1; then
  SOCK="sable-launch-test-$$"
  ISESS="slaunch"
  int_env=(SABLE_TMUX_SOCKET="$SOCK" SABLE_TMUX_SESSION="$ISESS" SABLE_TMUX_PANE_CMD="bash"
           SABLE_DISPATCH_READY_TIMEOUT=0 SABLE_DISPATCH_SUBMIT_TRIES=1 SABLE_DISPATCH_POLL_INTERVAL=0.1)
  if env "${int_env[@]}" bash "$LAUNCH" --no-attach >/dev/null 2>&1; then
    pass "integration: sable-launch --no-attach exits 0"
  else
    fail "integration: sable-launch --no-attach exits 0"
  fi
  if tmux -L "$SOCK" has-session -t "$ISESS" 2>/dev/null; then
    pass "integration: session created"
  else
    fail "integration: session created"
  fi
  NROLES="$(tmux -L "$SOCK" list-panes -s -t "$ISESS" -F '#{@sable_role}' 2>/dev/null | grep -c .)"
  if [ "$NROLES" = "4" ]; then
    pass "integration: four role-tagged panes"
  else
    fail "integration: four role-tagged panes" "got $NROLES"
  fi
  if env "${int_env[@]}" bash "$LAUNCH" --no-attach >/dev/null 2>&1; then
    pass "integration: second run reuses the existing session (exit 0)"
  else
    fail "integration: second run reuses the existing session (exit 0)"
  fi
  tmux -L "$SOCK" kill-server 2>/dev/null || true
else
  echo "SKIP: tmux not available — integration cases skipped"
fi

# Cleanup
rm -rf "$STUB_BIN" "$HOME_FLAG" "$HOME_NOFLAG"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
