#!/usr/bin/env bash
# lib-hook-trace.sh — shared hook-invocation tracer (SABLE-jfg6.1 / contract D1)
#
# The t7fm family (tdd-evidence non-capture, tdd-gate false-blocks, post-push
# notify loss) all shared one blind spot: the only invocation signal was a
# trace line that sat AFTER the hook's matcher/decision logic. Absence of that
# line therefore could not distinguish two very different failures —
#   (a) the hook was NEVER dispatched (Claude Code issue #6305), vs
#   (b) the hook fired but received EMPTY/absent stdin (issue #16047).
# That ambiguity is exactly what the t7fm classification misread (research F1).
#
# This lib gives every instrumented hook a durable, append-only, pane-reap-
# surviving ENTRY line at TRUE line 1 (before any stdin read or matcher), plus a
# single-read stdin helper that records STDIN_BYTES. Now:
#   - no ENTRY line            => the hook was never dispatched
#   - ENTRY + STDIN_BYTES=0    => dispatched, but stdin was empty/absent
#   - ENTRY + STDIN_BYTES=N>0  => dispatched with a real payload
#
# Failsafe by construction: every tracing error is swallowed so instrumentation
# can NEVER affect the hook it instruments. Kill-switch SABLE_HOOK_TRACE=0
# (parity with the legacy post-push pp_trace). Log path honors
# SABLE_HOOK_TRACE_LOG, defaulting to ~/.claude/sable/logs/hook-trace.log
# (SABLE-6cf9 fixture-isolation lesson: always redirectable for hermetic tests).
#
# Exports:
#   sable_trace_entry <hook-name>  — call FIRST, before any stdin read. Appends
#     'ENTRY ts pid=<pid> hook=<name> session-age=<n>s'. The session-age field
#     (seconds since this session's first traced hook fired) is D5's detection
#     deliverable for SABLE-ke3l; this lib maintains the session-start marker.
#   sable_trace_read_stdin         — reads stdin EXACTLY ONCE (never hangs on
#     absent stdin: fd test + bounded read), logs STDIN_BYTES=<n>, and echoes
#     the payload for the hook to consume in place of a raw `cat`.

# --- internal helpers -------------------------------------------------------

# Resolve the trace log path (honors SABLE_HOOK_TRACE_LOG override).
_sable_trace_log_path() {
  if [ -n "${SABLE_HOOK_TRACE_LOG:-}" ]; then
    printf '%s' "$SABLE_HOOK_TRACE_LOG"
  else
    printf '%s' "${HOME:-/tmp}/.claude/sable/logs/hook-trace.log"
  fi
}

# Tracing enabled? SABLE_HOOK_TRACE=0 is the kill-switch (default on).
_sable_trace_enabled() {
  [ "${SABLE_HOOK_TRACE:-1}" = "0" ] && return 1
  return 0
}

# Append one line to the trace log. Every error is swallowed (never disturbs the
# hook). No-ops entirely when tracing is disabled — so SABLE_HOOK_TRACE=0 leaves
# the log file untouched / uncreated.
_sable_trace_write() {
  _sable_trace_enabled || return 0
  local log dir
  log=$(_sable_trace_log_path) || return 0
  dir=$(dirname "$log" 2>/dev/null) || return 0
  [ -d "$dir" ] || mkdir -p "$dir" 2>/dev/null || return 0
  printf '%s\n' "$*" >> "$log" 2>/dev/null || return 0
  return 0
}

# Session-start marker path (overridable via SABLE_HOOK_TRACE_SESSION_MARKER for
# hermetic tests). Keyed by session id when one is in the environment, so the
# age reflects THIS session rather than the log file's lifetime; D5 (SABLE-ke3l)
# refines the detection semantics on top of this field.
_sable_trace_marker_path() {
  if [ -n "${SABLE_HOOK_TRACE_SESSION_MARKER:-}" ]; then
    printf '%s' "$SABLE_HOOK_TRACE_SESSION_MARKER"
    return 0
  fi
  local log dir sid
  log=$(_sable_trace_log_path)
  dir=$(dirname "$log" 2>/dev/null) || dir="${HOME:-/tmp}"
  sid="${CLAUDE_SESSION_ID:-${SABLE_SESSION_ID:-default}}"
  sid=$(printf '%s' "$sid" | tr -c 'A-Za-z0-9._-' '_' 2>/dev/null || echo default)
  printf '%s/.hook-trace-session-%s.start' "$dir" "$sid"
}

# Seconds since this session's first traced hook fired. Creates the marker on
# first call. Always prints a non-negative integer; failsafe to 0.
_sable_trace_session_age() {
  local marker now start mdir
  now=$(date +%s 2>/dev/null || echo 0)
  marker=$(_sable_trace_marker_path 2>/dev/null) || { printf '0'; return 0; }
  if [ -f "$marker" ]; then
    start=$(cat "$marker" 2>/dev/null || printf '%s' "$now")
  else
    start="$now"
    mdir=$(dirname "$marker" 2>/dev/null) || mdir=""
    if [ -n "$mdir" ]; then
      [ -d "$mdir" ] || mkdir -p "$mdir" 2>/dev/null || true
    fi
    printf '%s\n' "$now" > "$marker" 2>/dev/null || true
  fi
  case "$start" in
    ''|*[!0-9]*) start="$now" ;;
  esac
  case "$now" in
    ''|*[!0-9]*) now=0; start=0 ;;
  esac
  local age=$(( now - start ))
  [ "$age" -lt 0 ] && age=0
  printf '%s' "$age"
}

# --- public API -------------------------------------------------------------

# sable_trace_entry <hook-name>
# The FIRST executable statement a hook runs, BEFORE reading stdin or evaluating
# any matcher. Records a durable ENTRY line so absence-of-line == never-fired.
sable_trace_entry() {
  _sable_trace_enabled || return 0
  local hook="${1:-unknown}"
  local ts pid age
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo '?')
  pid=$$
  age=$(_sable_trace_session_age 2>/dev/null || echo 0)
  _sable_trace_write "ENTRY ${ts} pid=${pid} hook=${hook} session-age=${age}s"
  return 0
}

# sable_trace_read_stdin
# Reads stdin EXACTLY ONCE and echoes it verbatim for the hook to consume,
# logging STDIN_BYTES=<n>. Never hangs on absent stdin: an interactive fd is
# treated as empty, and a bounded `timeout cat` guards against a stuck pipe
# (partial bytes already written are still returned). Prints nothing extra, so
# `HOOK_INPUT=$(sable_trace_read_stdin)` is a drop-in for `HOOK_INPUT=$(cat)`.
sable_trace_read_stdin() {
  local payload=""
  if [ -t 0 ]; then
    # stdin is an interactive terminal: no piped hook payload. Treat as absent.
    payload=""
  elif command -v timeout >/dev/null 2>&1; then
    payload=$(timeout "${SABLE_HOOK_TRACE_STDIN_TIMEOUT:-5}" cat 2>/dev/null) || true
  else
    payload=$(cat 2>/dev/null) || true
  fi
  local bytes
  bytes=$(printf '%s' "$payload" | wc -c 2>/dev/null | tr -d ' ') || bytes=0
  [ -n "$bytes" ] || bytes=0
  _sable_trace_write "STDIN_BYTES=${bytes}"
  printf '%s' "$payload"
  return 0
}
