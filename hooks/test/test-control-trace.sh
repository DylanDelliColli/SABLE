#!/usr/bin/env bash
# test-control-trace.sh — behavior tests for control-trace.sh (SABLE-jfg6.2 /
# contract D2, matrix cases S4-U1 + the S1-U11 hermetic/headless discipline)
#
# The hook is DELIBERATELY self-contained (no lib-hook-trace.sh) so it can
# corroborate the D1 trace lib independently. Its whole job is: always log,
# never touch stdin, never block, never fail. These tests pin that contract.
#
# Run with:
#   bash hooks/test/test-control-trace.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO/hooks/multi-manager/control-trace.sh"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

FIXTURE_DIR="$(mktemp -d)"
trap 'rm -rf "$FIXTURE_DIR"' EXIT

# 1. Fingerprint precondition: the hook file must exist (pre-change it did not).
[ -f "$HOOK" ] && pass "control-trace.sh exists" || fail "control-trace.sh exists"

# 2. Invoked with EMPTY stdin: appends its fire line, exits 0.
LOG1="$FIXTURE_DIR/log1.log"
printf '' | SABLE_CONTROL_TRACE_LOG="$LOG1" bash "$HOOK"
RC1=$?
if [ "$RC1" -eq 0 ] && [ -f "$LOG1" ] && [ "$(wc -l < "$LOG1")" -eq 1 ]; then
  pass "empty stdin: appends one fire line, exits 0"
else
  fail "empty stdin: appends one fire line, exits 0" "rc=$RC1 log=$(cat "$LOG1" 2>/dev/null)"
fi

# 3. Invoked with NO stdin connected (</dev/null): appends and exits 0 immediately.
LOG2="$FIXTURE_DIR/log2.log"
SABLE_CONTROL_TRACE_LOG="$LOG2" bash "$HOOK" < /dev/null
RC2=$?
if [ "$RC2" -eq 0 ] && [ -f "$LOG2" ] && [ "$(wc -l < "$LOG2")" -eq 1 ]; then
  pass "no stdin: appends fire line, exits 0 immediately"
else
  fail "no stdin: appends fire line, exits 0 immediately" "rc=$RC2 log=$(cat "$LOG2" 2>/dev/null)"
fi

# 4. Log line shape: 'ts pid tool-hint' — three whitespace-separated fields.
LINE=$(head -1 "$LOG2" 2>/dev/null)
FIELDS=$(printf '%s' "$LINE" | wc -w)
if [ "$FIELDS" -ge 3 ]; then
  pass "log line has ts pid tool-hint shape"
else
  fail "log line has ts pid tool-hint shape" "got: $LINE"
fi

# 5. pid field is numeric (a real invocation pid, not a placeholder).
PID_FIELD=$(printf '%s' "$LINE" | awk '{print $2}')
case "$PID_FIELD" in
  ''|*[!0-9]*) fail "pid field is numeric" "got: $PID_FIELD" ;;
  *) pass "pid field is numeric" ;;
esac

# 6. Log path override honored: the default log must not be touched when an
#    override is given (SABLE-6cf9 fixture-isolation discipline).
DEFAULT_LOG="$HOME/.claude/sable/logs/control-trace.log"
DEFAULT_LOG_MTIME_BEFORE=""
[ -f "$DEFAULT_LOG" ] && DEFAULT_LOG_MTIME_BEFORE=$(stat -c %Y "$DEFAULT_LOG" 2>/dev/null || stat -f %m "$DEFAULT_LOG" 2>/dev/null)
LOG3="$FIXTURE_DIR/log3.log"
SABLE_CONTROL_TRACE_LOG="$LOG3" bash "$HOOK" < /dev/null >/dev/null 2>&1
DEFAULT_LOG_MTIME_AFTER=""
[ -f "$DEFAULT_LOG" ] && DEFAULT_LOG_MTIME_AFTER=$(stat -c %Y "$DEFAULT_LOG" 2>/dev/null || stat -f %m "$DEFAULT_LOG" 2>/dev/null)
if [ -f "$LOG3" ] && [ "$DEFAULT_LOG_MTIME_BEFORE" = "$DEFAULT_LOG_MTIME_AFTER" ]; then
  pass "SABLE_CONTROL_TRACE_LOG override honored (default log untouched)"
else
  fail "SABLE_CONTROL_TRACE_LOG override honored (default log untouched)" "log3 exists=$([ -f "$LOG3" ] && echo yes || echo no) default before=$DEFAULT_LOG_MTIME_BEFORE after=$DEFAULT_LOG_MTIME_AFTER"
fi

# 7. Repeated invocations append, never truncate.
LOG4="$FIXTURE_DIR/log4.log"
SABLE_CONTROL_TRACE_LOG="$LOG4" bash "$HOOK" < /dev/null >/dev/null 2>&1
SABLE_CONTROL_TRACE_LOG="$LOG4" bash "$HOOK" < /dev/null >/dev/null 2>&1
if [ "$(wc -l < "$LOG4")" -eq 2 ]; then
  pass "repeated invocations append, never truncate"
else
  fail "repeated invocations append, never truncate" "lines=$(wc -l < "$LOG4")"
fi

# 8. Never blocks on an open (non-EOF) stdin pipe — guards against a future
#    regression that adds a stdin read to a hook whose whole contract is
#    zero stdin dependency.
LOG5="$FIXTURE_DIR/log5.log"
if command -v mkfifo >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1; then
  FIFO="$FIXTURE_DIR/hang.fifo"
  mkfifo "$FIFO" 2>/dev/null
  # Open the fifo read-write on fd 3 in this shell so the open itself never
  # blocks (no separate writer/EOF needed), then feed it to the hook as stdin.
  RC5=1
  if exec 3<> "$FIFO" 2>/dev/null; then
    SABLE_CONTROL_TRACE_LOG="$LOG5" timeout 5 bash "$HOOK" <&3
    RC5=$?
    exec 3<&-
  fi
  rm -f "$FIFO"
  if [ "$RC5" -eq 0 ]; then
    pass "never blocks on an open (non-EOF) stdin pipe"
  else
    fail "never blocks on an open (non-EOF) stdin pipe" "rc=$RC5"
  fi
else
  pass "never blocks on an open (non-EOF) stdin pipe (skipped: mkfifo/timeout unavailable)"
fi

# 9. Always exits 0 even when the log directory cannot be created (a
#    read-only parent) — the exit-0 contract must not depend on log success.
if [ "$(id -u)" -ne 0 ]; then
  RO_DIR="$FIXTURE_DIR/readonly"
  mkdir -p "$RO_DIR"
  chmod 500 "$RO_DIR"
  SABLE_CONTROL_TRACE_LOG="$RO_DIR/nested/control-trace.log" bash "$HOOK" < /dev/null >/dev/null 2>&1
  RC9=$?
  chmod 700 "$RO_DIR"
  if [ "$RC9" -eq 0 ]; then
    pass "exits 0 even when the log dir is not writable"
  else
    fail "exits 0 even when the log dir is not writable" "rc=$RC9"
  fi
else
  pass "exits 0 even when the log dir is not writable (skipped: running as root)"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
