#!/usr/bin/env bash
# test-lib-hook-trace.sh — unit tests for hooks/multi-manager/lib-hook-trace.sh
# (SABLE-jfg6.1 / contract D1, matrix cells S1-U1..U4, S1-U11, S1-E4).
#
# Exercises the two exported functions directly via a tiny driver that sources
# the lib under the real hook shell options (set -euo pipefail):
#   sable_trace_entry <hook>   — durable ENTRY line before any stdin read
#   sable_trace_read_stdin     — single-read stdin helper logging STDIN_BYTES=<n>
#
# Hermetic: every run pins SABLE_HOOK_TRACE_LOG + SABLE_HOOK_TRACE_SESSION_MARKER
# into a trap-cleaned tmp dir, runs headless (setsid where available, env -u TERM),
# and carries the SABLE_WORKER_PANE=zzz / CLAUDE_AGENT_NAME=zzz sentinel identity.
#
# Run:  bash hooks/test/test-lib-hook-trace.sh

set -uo pipefail

LIB="$(cd "$(dirname "$0")/.." && pwd)/multi-manager/lib-hook-trace.sh"
if [ ! -f "$LIB" ]; then
  echo "FAIL: lib not found at $LIB"
  exit 2
fi

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/lib-hook-trace-test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# Headless wrapper: prefer `setsid -w` (new session, no controlling TTY) when
# available, else run directly. env -u TERM strips any inherited terminal.
if command -v setsid >/dev/null 2>&1; then
  HEADLESS="setsid -w"
else
  HEADLESS=""
fi

# The driver sources the lib under the SAME shell options the real hooks use, so
# any set -e/-u/pipefail unsafety in the lib surfaces here. It calls entry first,
# then echoes read_stdin's payload into $CAPTURE for verbatim comparison.
DRIVER="$WORK/driver.sh"
cat > "$DRIVER" <<'DRV'
#!/usr/bin/env bash
set -euo pipefail
LIB_PATH="$1"; HOOK_NAME="$2"
# shellcheck source=/dev/null
. "$LIB_PATH"
sable_trace_entry "$HOOK_NAME"
sable_trace_read_stdin > "${CAPTURE:-/dev/null}"
DRV
chmod +x "$DRIVER"

# run_driver <log> <marker> <capture> <hook> [extra env assignments...]
# Reads stdin from whatever the caller connected. Returns the driver exit code.
run_driver() {
  local log="$1" marker="$2" cap="$3" hook="$4"; shift 4
  $HEADLESS env -u TERM \
    SABLE_WORKER_PANE=zzz CLAUDE_AGENT_NAME=zzz \
    SABLE_HOOK_TRACE_LOG="$log" \
    SABLE_HOOK_TRACE_SESSION_MARKER="$marker" \
    CAPTURE="$cap" \
    "$@" \
    bash "$DRIVER" "$LIB" "$hook"
}

# ==========================================================================
# S1-U1 — ENTRY line lands BEFORE the read-helper's STDIN_BYTES line
# ==========================================================================
U1_LOG="$WORK/u1.log"; U1_MARK="$WORK/u1.mark"; U1_CAP="$WORK/u1.cap"
printf '%s' '{"tool_input":{"command":"pytest"}}' | run_driver "$U1_LOG" "$U1_MARK" "$U1_CAP" u1hook >/dev/null 2>&1
ENTRY_LN=$(grep -n '^ENTRY ' "$U1_LOG" 2>/dev/null | head -1 | cut -d: -f1)
BYTES_LN=$(grep -n '^STDIN_BYTES=' "$U1_LOG" 2>/dev/null | head -1 | cut -d: -f1)
if [ -n "$ENTRY_LN" ] && [ -n "$BYTES_LN" ] && [ "$ENTRY_LN" -lt "$BYTES_LN" ]; then
  pass "S1-U1: ENTRY line lands before the STDIN_BYTES read-helper line"
else
  fail "S1-U1: ENTRY line lands before the STDIN_BYTES read-helper line" "log: $(cat "$U1_LOG" 2>/dev/null)"
fi

# ENTRY line carries the session-age field as a non-negative integer (D5 seam).
if grep -qE '^ENTRY .* hook=u1hook session-age=[0-9]+s$' "$U1_LOG" 2>/dev/null; then
  pass "S1-U1: ENTRY line format carries pid/hook/session-age (numeric)"
else
  fail "S1-U1: ENTRY line format carries pid/hook/session-age (numeric)" "log: $(cat "$U1_LOG" 2>/dev/null)"
fi

# ==========================================================================
# S1-U4 — a real payload is echoed VERBATIM and STDIN_BYTES matches its length
# ==========================================================================
U4_LOG="$WORK/u4.log"; U4_MARK="$WORK/u4.mark"; U4_CAP="$WORK/u4.cap"
U4_PAYLOAD='{"tool_input":{"command":"bash test-x.sh"},"session_id":"s"}'
printf '%s' "$U4_PAYLOAD" | run_driver "$U4_LOG" "$U4_MARK" "$U4_CAP" u4hook >/dev/null 2>&1
EXPECT_BYTES=$(printf '%s' "$U4_PAYLOAD" | wc -c | tr -d ' ')
if [ "$(cat "$U4_CAP" 2>/dev/null)" = "$U4_PAYLOAD" ]; then
  pass "S1-U4: read helper echoes the stdin payload verbatim for the hook to consume"
else
  fail "S1-U4: read helper echoes the stdin payload verbatim" "got: $(cat "$U4_CAP" 2>/dev/null)"
fi
if grep -qx "STDIN_BYTES=${EXPECT_BYTES}" "$U4_LOG" 2>/dev/null; then
  pass "S1-U4: STDIN_BYTES records the payload byte length ($EXPECT_BYTES)"
else
  fail "S1-U4: STDIN_BYTES records the payload byte length ($EXPECT_BYTES)" "log: $(cat "$U4_LOG" 2>/dev/null)"
fi

# ==========================================================================
# S1-U2 — EMPTY stdin logs STDIN_BYTES=0
# ==========================================================================
U2_LOG="$WORK/u2.log"; U2_MARK="$WORK/u2.mark"; U2_CAP="$WORK/u2.cap"
printf '' | run_driver "$U2_LOG" "$U2_MARK" "$U2_CAP" u2hook >/dev/null 2>&1
if grep -qx 'STDIN_BYTES=0' "$U2_LOG" 2>/dev/null; then
  pass "S1-U2: EMPTY stdin logs STDIN_BYTES=0"
else
  fail "S1-U2: EMPTY stdin logs STDIN_BYTES=0" "log: $(cat "$U2_LOG" 2>/dev/null)"
fi
# The ENTRY line is present even with empty stdin — this is the whole point:
# ENTRY + STDIN_BYTES=0 == fired-with-empty-stdin, distinct from never-fired.
if grep -q '^ENTRY ' "$U2_LOG" 2>/dev/null; then
  pass "S1-U2: ENTRY still lands on empty stdin (fired-empty is separable from never-fired)"
else
  fail "S1-U2: ENTRY still lands on empty stdin" "log: $(cat "$U2_LOG" 2>/dev/null)"
fi

# ==========================================================================
# S1-U3 — ABSENT stdin logs STDIN_BYTES=0 and NEVER hangs
# ==========================================================================
# (a) stdin redirected from /dev/null (the canonical "no hook payload").
U3_LOG="$WORK/u3.log"; U3_MARK="$WORK/u3.mark"; U3_CAP="$WORK/u3.cap"
if command -v timeout >/dev/null 2>&1; then
  timeout 10 bash -c '
    LIB="'"$LIB"'"; DRIVER="'"$DRIVER"'"; HEADLESS="'"$HEADLESS"'"
    $HEADLESS env -u TERM SABLE_WORKER_PANE=zzz CLAUDE_AGENT_NAME=zzz \
      SABLE_HOOK_TRACE_LOG="'"$U3_LOG"'" SABLE_HOOK_TRACE_SESSION_MARKER="'"$U3_MARK"'" \
      CAPTURE="'"$U3_CAP"'" bash "$DRIVER" "$LIB" u3hook </dev/null
  ' >/dev/null 2>&1
  U3_RC=$?
else
  run_driver "$U3_LOG" "$U3_MARK" "$U3_CAP" u3hook </dev/null >/dev/null 2>&1
  U3_RC=$?
fi
if [ "$U3_RC" -eq 124 ]; then
  fail "S1-U3: ABSENT stdin (/dev/null) never hangs" "driver timed out (rc=124)"
elif grep -qx 'STDIN_BYTES=0' "$U3_LOG" 2>/dev/null && grep -q '^ENTRY ' "$U3_LOG" 2>/dev/null; then
  pass "S1-U3: ABSENT stdin (/dev/null) logs STDIN_BYTES=0 and never hangs"
else
  fail "S1-U3: ABSENT stdin (/dev/null) logs STDIN_BYTES=0 and never hangs" "rc=$U3_RC log: $(cat "$U3_LOG" 2>/dev/null)"
fi

# (b) stdin fully CLOSED (fd 0 closed): must also not hang and log 0 bytes.
U3B_LOG="$WORK/u3b.log"; U3B_MARK="$WORK/u3b.mark"; U3B_CAP="$WORK/u3b.cap"
if command -v timeout >/dev/null 2>&1; then
  timeout 10 bash -c '
    LIB="'"$LIB"'"; DRIVER="'"$DRIVER"'"; HEADLESS="'"$HEADLESS"'"
    $HEADLESS env -u TERM SABLE_WORKER_PANE=zzz CLAUDE_AGENT_NAME=zzz \
      SABLE_HOOK_TRACE_LOG="'"$U3B_LOG"'" SABLE_HOOK_TRACE_SESSION_MARKER="'"$U3B_MARK"'" \
      CAPTURE="'"$U3B_CAP"'" bash "$DRIVER" "$LIB" u3bhook 0<&-
  ' >/dev/null 2>&1
  U3B_RC=$?
else
  run_driver "$U3B_LOG" "$U3B_MARK" "$U3B_CAP" u3bhook 0<&- >/dev/null 2>&1
  U3B_RC=$?
fi
if [ "$U3B_RC" -eq 124 ]; then
  fail "S1-U3: ABSENT stdin (closed fd 0) never hangs" "driver timed out (rc=124)"
elif grep -qx 'STDIN_BYTES=0' "$U3B_LOG" 2>/dev/null; then
  pass "S1-U3: ABSENT stdin (closed fd 0) logs STDIN_BYTES=0 and never hangs"
else
  fail "S1-U3: ABSENT stdin (closed fd 0) logs STDIN_BYTES=0 and never hangs" "rc=$U3B_RC log: $(cat "$U3B_LOG" 2>/dev/null)"
fi

# ==========================================================================
# S1-U11 — log path honors SABLE_HOOK_TRACE_LOG; SABLE_HOOK_TRACE=0 disables
# ==========================================================================
# Override honored: writes land in the override file, not the ~/.claude default.
U11_LOG="$WORK/u11-custom.log"; U11_MARK="$WORK/u11.mark"; U11_CAP="$WORK/u11.cap"
rm -f "$U11_LOG"
printf '%s' '{"a":1}' | run_driver "$U11_LOG" "$U11_MARK" "$U11_CAP" u11hook >/dev/null 2>&1
if [ -s "$U11_LOG" ] && grep -q '^ENTRY ' "$U11_LOG" 2>/dev/null; then
  pass "S1-U11: SABLE_HOOK_TRACE_LOG override is honored (trace lands in the override file)"
else
  fail "S1-U11: SABLE_HOOK_TRACE_LOG override is honored" "log: $(cat "$U11_LOG" 2>/dev/null)"
fi

# Kill-switch: SABLE_HOOK_TRACE=0 leaves the log file uncreated, but read_stdin
# still returns the payload so the hook keeps working.
U11D_LOG="$WORK/u11-disabled.log"; U11D_MARK="$WORK/u11d.mark"; U11D_CAP="$WORK/u11d.cap"
rm -f "$U11D_LOG"
printf '%s' '{"a":1}' | run_driver "$U11D_LOG" "$U11D_MARK" "$U11D_CAP" u11dhook SABLE_HOOK_TRACE=0 >/dev/null 2>&1
if [ ! -f "$U11D_LOG" ]; then
  pass "S1-U11: SABLE_HOOK_TRACE=0 disables tracing (log file never created)"
else
  fail "S1-U11: SABLE_HOOK_TRACE=0 disables tracing" "unexpected log: $(cat "$U11D_LOG" 2>/dev/null)"
fi
if [ "$(cat "$U11D_CAP" 2>/dev/null)" = '{"a":1}' ]; then
  pass "S1-U11: SABLE_HOOK_TRACE=0 still passes the stdin payload through to the hook"
else
  fail "S1-U11: SABLE_HOOK_TRACE=0 still passes the stdin payload through" "got: $(cat "$U11D_CAP" 2>/dev/null)"
fi

# ==========================================================================
# SABLE-kqff — session-marker sid fallback chain includes CLAUDE_CODE_SESSION_ID
# ==========================================================================
# No SABLE_HOOK_TRACE_SESSION_MARKER override here: exercise the REAL
# _sable_trace_marker_path sid resolution (CLAUDE_SESSION_ID ->
# CLAUDE_CODE_SESSION_ID -> SABLE_SESSION_ID -> default).

sanitize_sid() { printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_'; }

# (a) CLAUDE_SESSION_ID unset, CLAUDE_CODE_SESSION_ID set: the harness var must
# be picked up rather than falling back to 'default'.
KQFF_LOG="$WORK/kqff-a.log"; KQFF_CAP="$WORK/kqff-a.cap"
KQFF_UUID="cc-session-11111111-2222"
rm -f "$KQFF_LOG"
printf '%s' '{}' | $HEADLESS env -u TERM -u SABLE_HOOK_TRACE_SESSION_MARKER -u CLAUDE_SESSION_ID -u SABLE_SESSION_ID \
  SABLE_WORKER_PANE=zzz CLAUDE_AGENT_NAME=zzz \
  SABLE_HOOK_TRACE_LOG="$KQFF_LOG" \
  CLAUDE_CODE_SESSION_ID="$KQFF_UUID" \
  CAPTURE="$KQFF_CAP" \
  bash "$DRIVER" "$LIB" kqffhook >/dev/null 2>&1
KQFF_EXPECT_SID=$(sanitize_sid "$KQFF_UUID")
KQFF_MARKER="$WORK/.hook-trace-session-${KQFF_EXPECT_SID}.start"
if [ -f "$KQFF_MARKER" ]; then
  pass "SABLE-kqff: CLAUDE_CODE_SESSION_ID is used in the sid fallback (marker: $KQFF_MARKER)"
else
  fail "SABLE-kqff: CLAUDE_CODE_SESSION_ID is used in the sid fallback" "expected marker $KQFF_MARKER not found; dir listing: $(ls "$WORK" 2>/dev/null)"
fi
if ls "$WORK"/.hook-trace-session-default.start >/dev/null 2>&1; then
  fail "SABLE-kqff: sid must not fall back to 'default' when CLAUDE_CODE_SESSION_ID is set" "found default marker in $WORK"
else
  pass "SABLE-kqff: sid does not fall back to 'default' when CLAUDE_CODE_SESSION_ID is set"
fi

# (b) CLAUDE_SESSION_ID still wins over CLAUDE_CODE_SESSION_ID when both are set
# (precedence unchanged from before this fix).
KQFF_LOG_B="$WORK/kqff-b.log"; KQFF_CAP_B="$WORK/kqff-b.cap"
KQFF_LEGACY_ID="legacy-session-99"
rm -f "$KQFF_LOG_B"
printf '%s' '{}' | $HEADLESS env -u TERM -u SABLE_HOOK_TRACE_SESSION_MARKER -u SABLE_SESSION_ID \
  SABLE_WORKER_PANE=zzz CLAUDE_AGENT_NAME=zzz \
  SABLE_HOOK_TRACE_LOG="$KQFF_LOG_B" \
  CLAUDE_SESSION_ID="$KQFF_LEGACY_ID" \
  CLAUDE_CODE_SESSION_ID="should-not-be-used" \
  CAPTURE="$KQFF_CAP_B" \
  bash "$DRIVER" "$LIB" kqffhookb >/dev/null 2>&1
KQFF_EXPECT_SID_B=$(sanitize_sid "$KQFF_LEGACY_ID")
KQFF_MARKER_B="$WORK/.hook-trace-session-${KQFF_EXPECT_SID_B}.start"
if [ -f "$KQFF_MARKER_B" ]; then
  pass "SABLE-kqff: CLAUDE_SESSION_ID still takes precedence over CLAUDE_CODE_SESSION_ID"
else
  fail "SABLE-kqff: CLAUDE_SESSION_ID still takes precedence over CLAUDE_CODE_SESSION_ID" "expected marker $KQFF_MARKER_B not found; dir listing: $(ls "$WORK" 2>/dev/null)"
fi

# ==========================================================================
# S1-E4 — KILLED writer (SIGKILL mid-payload) still leaves the ENTRY line on disk
# ==========================================================================
# A writer opens a FIFO, emits a PARTIAL payload, and is SIGKILLed while the
# driver is still reading. Because sable_trace_entry runs (and flushes) BEFORE
# the stdin read, the ENTRY line is durable regardless of the writer's fate; and
# the writer's death (fd close) delivers EOF so the read never hangs.
E4_LOG="$WORK/e4.log"; E4_MARK="$WORK/e4.mark"; E4_CAP="$WORK/e4.cap"
FIFO="$WORK/e4.fifo"; mkfifo "$FIFO"

# Driver reads from the FIFO. High stdin timeout so it's the writer's death
# (EOF), not the timeout, that unblocks the read — proving the SIGKILL path.
(
  env -u TERM SABLE_WORKER_PANE=zzz CLAUDE_AGENT_NAME=zzz \
    SABLE_HOOK_TRACE_LOG="$E4_LOG" SABLE_HOOK_TRACE_SESSION_MARKER="$E4_MARK" \
    CAPTURE="$E4_CAP" SABLE_HOOK_TRACE_STDIN_TIMEOUT=30 \
    bash "$DRIVER" "$LIB" e4hook <"$FIFO"
) >/dev/null 2>&1 &
E4_DRV=$!

# Writer holds the FIFO write end open (exec 3>) so the partial payload does NOT
# get an EOF until the writer is killed.
(
  exec 3>"$FIFO"
  printf '{"tool_input":{"command":"pyt' >&3
  sleep 30
) &
E4_WR=$!
# Detach from job control so the SIGKILL below doesn't print a "Killed" line.
disown "$E4_WR" 2>/dev/null || true

sleep 1
kill -9 "$E4_WR" 2>/dev/null || true

# Bounded wait on the driver so a regression (a genuine hang) fails loudly.
E4_WAITED=0
while kill -0 "$E4_DRV" 2>/dev/null; do
  sleep 0.5
  E4_WAITED=$((E4_WAITED+1))
  if [ "$E4_WAITED" -ge 20 ]; then
    kill -9 "$E4_DRV" 2>/dev/null || true
    break
  fi
done
wait "$E4_DRV" 2>/dev/null || true

if [ "$E4_WAITED" -ge 20 ]; then
  fail "S1-E4: killed-writer (SIGKILL mid-payload) — driver hung instead of EOF-ing"
elif grep -q '^ENTRY ' "$E4_LOG" 2>/dev/null; then
  pass "S1-E4: killed-writer (SIGKILL mid-payload) still leaves the ENTRY line on disk"
else
  fail "S1-E4: killed-writer (SIGKILL mid-payload) still leaves the ENTRY line on disk" "log: $(cat "$E4_LOG" 2>/dev/null)"
fi
# The partial payload that DID arrive is still recorded (STDIN_BYTES present).
if grep -q '^STDIN_BYTES=' "$E4_LOG" 2>/dev/null; then
  pass "S1-E4: partial payload from the killed writer is still read + logged (STDIN_BYTES present)"
else
  fail "S1-E4: partial payload from the killed writer is still read + logged" "log: $(cat "$E4_LOG" 2>/dev/null)"
fi

# ==========================================================================
# Summary
# ==========================================================================
echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="

if [ "$FAIL" -gt 0 ]; then
  printf "Failed tests:%b\n" "$FAIL_NAMES"
  exit 1
fi
exit 0
