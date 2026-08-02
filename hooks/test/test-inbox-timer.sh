#!/usr/bin/env bash
# test-inbox-timer.sh — host enumerator delegates readiness to the existing
# watcher, covers managers + workers, and heartbeats only assessable sweeps
# (SABLE-albyd).

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
BIN="$REPO/bin"
STATUS="${SABLE_WORKER_STATUS_COMMAND:-$BIN/sable-worker-status}"
SOCK="inbox-timer-$$"
SESS="timer-test"
ROOT="$(mktemp -d "${TMPDIR:-/tmp}/sable-inbox-timer.XXXXXX")/inbox"
MANAGER="timer-manager-$$"
WORKER="timer-worker-$$"
POKE='⟦SABLE-MSG⟧ Run sable-inbox read: 1 pending.'

PASS=0
FAIL=0
pass() { PASS=$((PASS + 1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
tmux_() { tmux -L "$SOCK" "$@"; }
cleanup() {
  tmux_ kill-server >/dev/null 2>&1 || true
  rm -rf "$(dirname "$ROOT")"
}
trap cleanup EXIT

command -v tmux >/dev/null 2>&1 || { echo "SKIP: tmux not installed"; exit 0; }

inbox_python() {
  SABLE_TEST=1 SABLE_TEST_INBOX_ROOT="$ROOT" PYTHONPATH="$BIN" python3 - "$@"
}

enqueue() {
  inbox_python "$1" <<'PY'
import sys
from sable_inbox_lib import enqueue
enqueue(sys.argv[1], "integration", "payload stays in the file queue")
PY
}

pending_count() {
  inbox_python "$1" <<'PY'
import sys
from sable_inbox_lib import pending
print(len(pending(sys.argv[1])))
PY
}

heartbeat_fresh() {
  inbox_python <<'PY'
from sable_inbox_lib import drain_health
raise SystemExit(0 if drain_health().fresh else 1)
PY
}

wait_for_capture() {
  target="$1"
  needle="$2"
  attempts=0
  while [ "$attempts" -lt 100 ]; do
    tmux_ capture-pane -p -J -e -t "$target" 2>/dev/null | grep -Fq "$needle" && return 0
    attempts=$((attempts + 1))
    sleep 0.05
  done
  return 1
}

tag_manager() {
  tmux_ set-option -p -t "$1" @sable_role "$2"
  tmux_ set-option -p -t "$1" @sable_bead ""
  tmux_ set-option -p -t "$1" @sable_provider claude
  tmux_ set-option -p -t "$1" @sable_status running
}

tag_worker() {
  tmux_ set-option -p -t "$1" @sable_role worker
  tmux_ set-option -p -t "$1" @sable_bead "$2"
  tmux_ set-option -p -t "$1" @sable_provider claude
  tmux_ set-option -p -t "$1" @sable_status running
}

run_timer() {
  SABLE_TEST=1 SABLE_TEST_INBOX_ROOT="$ROOT" \
    "$BIN/sable-inbox-timer" --once --socket "$SOCK" \
      --interval-seconds 30 --status-command "$1" 2>&1
}

tmux_ new-session -d -s "$SESS" -x 180 -y 40 'bash --noprofile --norc'
MANAGER_PANE="$(tmux_ display-message -p -t "$SESS" '#{pane_id}')"
tag_manager "$MANAGER_PANE" "$MANAGER"
tmux_ send-keys -t "$MANAGER_PANE" "bash -c 'exec -a codex sleep 45' &" Enter
tmux_ send-keys -t "$MANAGER_PANE" "PS1='> '" Enter
wait_for_capture "$MANAGER_PANE" "> " || fail "manager fixture rendered idle prompt"

WORKER_PANE="$(tmux_ split-window -d -P -F '#{pane_id}' -t "$SESS" 'bash --noprofile --norc')"
tag_worker "$WORKER_PANE" "$WORKER"
tmux_ send-keys -t "$WORKER_PANE" "bash -c 'exec -a codex sleep 45' &" Enter
tmux_ send-keys -t "$WORKER_PANE" "PS1='> '" Enter
wait_for_capture "$WORKER_PANE" "> " || fail "worker fixture rendered idle prompt"
tmux_ send-keys -t "$WORKER_PANE" -l "operator draft held here"

enqueue "$MANAGER"
enqueue "$WORKER"

first_out="$(run_timer "$STATUS")"
first_rc=$?
manager_capture="$(tmux_ capture-pane -p -J -t "$MANAGER_PANE")"
worker_capture="$(tmux_ capture-pane -p -J -t "$WORKER_PANE")"
if [ "$first_rc" -eq 0 ] \
   && [ "$(printf '%s\n' "$manager_capture" | grep -Fc "$POKE" || true)" -eq 1 ] \
   && ! printf '%s\n' "$worker_capture" | grep -Fq "$POKE" \
   && printf '%s\n' "$worker_capture" | grep -Fq "operator draft held here"; then
  pass "one sweep pokes idle manager and delegates held-worker refusal"
else
  fail "one sweep pokes idle manager and delegates held-worker refusal" \
    "rc=$first_rc out=$first_out manager=$manager_capture worker=$worker_capture"
fi

if [ "$(pending_count "$MANAGER")" -eq 1 ] \
   && [ "$(pending_count "$WORKER")" -eq 1 ] \
   && heartbeat_fresh; then
  pass "assessable sweep preserves both payloads and publishes fresh heartbeat"
else
  fail "assessable sweep preserves both payloads and publishes fresh heartbeat"
fi

second_out="$(run_timer "$STATUS")"
second_rc=$?
manager_capture="$(tmux_ capture-pane -p -J -t "$MANAGER_PANE")"
if [ "$second_rc" -eq 0 ] \
   && [ "$(printf '%s\n' "$manager_capture" | grep -Fc "$POKE" || true)" -eq 2 ] \
   && [ "$(pending_count "$MANAGER")" -eq 1 ] \
   && [ "$(pending_count "$WORKER")" -eq 1 ]; then
  pass "second tick is idempotent for queue bytes and may repeat count-only poke"
else
  fail "second tick is idempotent for queue bytes and may repeat count-only poke" \
    "rc=$second_rc out=$second_out manager=$manager_capture"
fi

heartbeat_before="$(inbox_python <<'PY'
from sable_inbox_lib import _inbox_root
print((_inbox_root() / '.drain-heartbeat.json').read_text())
PY
)"
bad_out="$(run_timer /bin/false)"
bad_rc=$?
heartbeat_after="$(inbox_python <<'PY'
from sable_inbox_lib import _inbox_root
print((_inbox_root() / '.drain-heartbeat.json').read_text())
PY
)"
manager_after="$(tmux_ capture-pane -p -J -t "$MANAGER_PANE")"
if [ "$bad_rc" -eq 2 ] \
   && printf '%s' "$bad_out" | grep -Fq "CANNOT-ASSESS" \
   && [ "$heartbeat_before" = "$heartbeat_after" ] \
   && [ "$(printf '%s\n' "$manager_after" | grep -Fc "$POKE" || true)" -eq 2 ]; then
  pass "unreadable host PID evidence refuses before poke and leaves heartbeat unchanged"
else
  fail "unreadable host PID evidence refuses before poke and leaves heartbeat unchanged" \
    "rc=$bad_rc out=$bad_out"
fi

echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
