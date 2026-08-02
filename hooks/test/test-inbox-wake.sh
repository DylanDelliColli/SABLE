#!/usr/bin/env bash
# test-inbox-wake.sh — the queue payload never enters tmux; only a count-only
# wake reaches a freshly proven idle pane (SABLE-m4kyf.4).
#
# A real inbox and real watcher run against an isolated tmux socket.  Each pane
# carries a resident executable-shaped Codex child so sable-worker-status's
# process-tree seam reports LIVE; EXITED/CANNOT-ASSESS polarity has unit coverage.

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
BIN="$REPO/bin"
STATUS_COMMAND="${SABLE_WORKER_STATUS_COMMAND:-$BIN/sable-worker-status}"
SOCK="inbox-wake-$$"
SESS="wake-test"
RECIPIENT="wake-test-$$"
RECIPIENT_HELD="wake-held-$$"
RECIPIENT_BOOT="wake-boot-$$"
RECIPIENT_MISMATCH="wake-mismatch-$$"
INBOX_ROOT="/tmp/sable-$(id -u)/inbox"

PASS=0
FAIL=0
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
tmux_() { tmux -L "$SOCK" "$@"; }
cleanup() {
  tmux_ kill-server >/dev/null 2>&1 || true
  rm -rf "$INBOX_ROOT/$RECIPIENT" "$INBOX_ROOT/$RECIPIENT_HELD" \
    "$INBOX_ROOT/$RECIPIENT_BOOT" "$INBOX_ROOT/$RECIPIENT_MISMATCH"
}
trap cleanup EXIT

command -v tmux >/dev/null 2>&1 || { echo "SKIP: tmux not installed"; exit 0; }

enqueue() {
  PYTHONPATH="$BIN" python3 - "$1" <<'PY'
import sys
from sable_inbox_lib import enqueue
enqueue(sys.argv[1], "integration", "payload stays byte-exact in the queue\n;")
PY
}

pending_count() {
  PYTHONPATH="$BIN" python3 - "$1" <<'PY'
import sys
from sable_inbox_lib import pending
print(len(pending(sys.argv[1])))
PY
}

queue_intact() {
  PYTHONPATH="$BIN" python3 - "$1" <<'PY'
import sys
from sable_inbox_lib import read
messages = read(sys.argv[1])
raise SystemExit(0 if len(messages) == 1
                 and messages[0].sender == "integration"
                 and messages[0].body == "payload stays byte-exact in the queue\n;"
                 else 1)
PY
}

tag_worker() {
  tmux_ set-option -p -t "$1" @sable_role worker
  tmux_ set-option -p -t "$1" @sable_bead "$2"
  tmux_ set-option -p -t "$1" @sable_status running
}

tag_manager() {
  tmux_ set-option -p -t "$1" @sable_role "$2"
  tmux_ set-option -p -t "$1" @sable_bead ""
  tmux_ set-option -p -t "$1" @sable_status running
}

wait_for_capture() {
  target="$1"
  needle="$2"
  attempts=0
  # The boot fixture deliberately withholds its prompt for three seconds.
  # Leave enough bounded headroom for a loaded proportional-test run: the
  # wake policy is asserted by the captures, not by host scheduler latency.
  while [ "$attempts" -lt 200 ]; do
    if tmux_ capture-pane -p -J -e -t "$target" 2>/dev/null | grep -Fq "$needle"; then
      return 0
    fi
    attempts=$((attempts+1))
    sleep 0.05
  done
  return 1
}

wait_for_live() {
  target="$1"
  attempts=0
  while [ "$attempts" -lt 20 ]; do
    if SABLE_TMUX_SOCKET="$SOCK" SABLE_TMUX_SESSION="$SESS" \
        "$STATUS_COMMAND" --quiescence-pane "$target" --json 2>/dev/null \
        | python3 -c 'import json,sys; p=sys.argv[1]; d=json.load(sys.stdin); raise SystemExit(0 if any(w.get("pane") == p and w.get("state") == "LIVE" for w in d.get("panes", [])) else 1)' "$target"
    then
      return 0
    fi
    attempts=$((attempts+1))
    sleep 0.05
  done
  return 1
}

run_watcher() {
  SABLE_TMUX_SOCKET="$SOCK" SABLE_TMUX_SESSION="$SESS" \
    "$BIN/sable-inbox-watcher" "$1" "$2" claude \
      --status-command "$STATUS_COMMAND" 2>&1
}

# Proven-idle pane with a resident agent descendant.  The background process
# is the liveness fact; the literal bare prompt is the independent idle fact.
tmux_ new-session -d -s "$SESS" -x 180 -y 40 'bash --noprofile --norc'
IDLE_PANE="$(tmux_ display-message -p -t "$SESS" '#{pane_id}')"
tag_manager "$IDLE_PANE" "$RECIPIENT"
tmux_ send-keys -t "$IDLE_PANE" "bash -c 'exec -a codex sleep 45' &" Enter
tmux_ send-keys -t "$IDLE_PANE" "PS1='> '" Enter
if wait_for_capture "$IDLE_PANE" "> "; then
  pass "idle fixture rendered a composer prompt"
else
  fail "idle fixture rendered a composer prompt" "$(tmux_ capture-pane -p -t "$IDLE_PANE")"
fi

enqueue "$RECIPIENT"
disabled_capture="$(tmux_ capture-pane -p -J -t "$IDLE_PANE")"
if [ "$(pending_count "$RECIPIENT")" -eq 1 ] \
   && ! printf '%s\n' "$disabled_capture" | grep -Fq 'sable-inbox read'; then
  pass "watcher-disabled control leaves payload pending and pane unpoked"
else
  fail "watcher-disabled control leaves payload pending and pane unpoked" \
    "capture=$disabled_capture"
fi
first_out="$(run_watcher "$RECIPIENT" "$IDLE_PANE")"
first_rc=$?
first_capture="$(tmux_ capture-pane -p -J -t "$IDLE_PANE")"
poke='⟦SABLE-MSG⟧ Run sable-inbox read: 1 pending.'
first_pokes="$(printf '%s\n' "$first_capture" | grep -Fc "$poke" || true)"
if [ "$first_rc" -eq 0 ] && [ "$first_pokes" -eq 1 ] \
   && ! printf '%s' "$first_capture" | grep -Fq 'payload stays byte-exact'; then
  pass "pending inbox receives exactly one submitted count-only poke"
else
  fail "pending inbox receives exactly one submitted count-only poke" \
    "rc=$first_rc count=$first_pokes out=$first_out capture=$first_capture"
fi
if [ "$(pending_count "$RECIPIENT")" -eq 1 ] && queue_intact "$RECIPIENT"; then
  pass "first poke leaves queued payload untouched"
else
  fail "first poke leaves queued payload untouched"
fi

# Retry is deliberately harmless: the same short wake may repeat, while the
# sole queued message remains byte-identical and singular.
second_out="$(run_watcher "$RECIPIENT" "$IDLE_PANE")"
second_rc=$?
second_capture="$(tmux_ capture-pane -p -J -t "$IDLE_PANE")"
second_pokes="$(printf '%s\n' "$second_capture" | grep -Fc "$poke" || true)"
if [ "$second_rc" -eq 0 ] && [ "$second_pokes" -eq 2 ] \
   && [ "$(pending_count "$RECIPIENT")" -eq 1 ] && queue_intact "$RECIPIENT"; then
  pass "duplicate poke is harmless and does not duplicate or corrupt queue"
else
  fail "duplicate poke is harmless and does not duplicate or corrupt queue" \
    "rc=$second_rc count=$second_pokes out=$second_out"
fi

# Binding control: the same LIVE+idle pane must not be poked for an inbox whose
# registered identity belongs elsewhere (or nowhere).  Refusal precedes the
# process probe, and the transcript count proves zero injection occurred.
enqueue "$RECIPIENT_MISMATCH"
mismatch_before="$second_pokes"
mismatch_out="$(run_watcher "$RECIPIENT_MISMATCH" "$IDLE_PANE")"
mismatch_rc=$?
mismatch_capture="$(tmux_ capture-pane -p -J -t "$IDLE_PANE")"
mismatch_after="$(printf '%s\n' "$mismatch_capture" | grep -Fc "$poke" || true)"
if [ "$mismatch_rc" -eq 1 ] && [ "$mismatch_after" -eq "$mismatch_before" ] \
   && printf '%s' "$mismatch_out" | grep -q 'no registered pane is bound' \
   && queue_intact "$RECIPIENT_MISMATCH"; then
  pass "recipient-pane mismatch refuses loudly with zero injection"
else
  fail "recipient-pane mismatch refuses loudly with zero injection" \
    "rc=$mismatch_rc before=$mismatch_before after=$mismatch_after out=$mismatch_out"
fi

# Negative control: a LIVE pane whose composer holds unsubmitted text must not
# be typed over.  Its message remains pending for a later tick.
HELD_PANE="$(tmux_ split-window -d -P -F '#{pane_id}' -t "$SESS" \
  'bash --noprofile --norc')"
tag_worker "$HELD_PANE" "$RECIPIENT_HELD"
tmux_ send-keys -t "$HELD_PANE" "bash -c 'exec -a codex sleep 45' &" Enter
tmux_ send-keys -t "$HELD_PANE" "PS1='> '" Enter
wait_for_capture "$HELD_PANE" "> " || true
tmux_ send-keys -t "$HELD_PANE" -l "operator draft held here"
enqueue "$RECIPIENT_HELD"
held_out="$(run_watcher "$RECIPIENT_HELD" "$HELD_PANE")"
held_rc=$?
held_capture="$(tmux_ capture-pane -p -J -t "$HELD_PANE")"
if [ "$held_rc" -eq 1 ] && printf '%s' "$held_out" | grep -q 'REFUSED' \
   && ! printf '%s' "$held_capture" | grep -Fq "$poke" \
   && printf '%s' "$held_capture" | grep -Fq "operator draft held here"; then
  pass "held composer refuses loudly and preserves unsubmitted text"
else
  fail "held composer refuses loudly and preserves unsubmitted text" \
    "rc=$held_rc out=$held_out capture=$held_capture"
fi

# Boot-window control: a LIVE process tree without a composer refuses now;
# after that same pane renders its prompt, the next external tick succeeds.
BOOT_PANE="$(tmux_ split-window -d -P -F '#{pane_id}' -t "$SESS" \
  "bash --noprofile --norc -c \"bash -c 'exec -a codex sleep 45' & sleep 3; PS1='> '; export PS1; exec bash --noprofile --norc -i\"")"
tag_manager "$BOOT_PANE" "$RECIPIENT_BOOT"
enqueue "$RECIPIENT_BOOT"
if ! wait_for_live "$BOOT_PANE"; then
  fail "boot fixture exposed a resident agent before rendering its prompt"
fi
boot_out="$(run_watcher "$RECIPIENT_BOOT" "$BOOT_PANE")"
boot_rc=$?
if [ "$boot_rc" -eq 1 ] && printf '%s' "$boot_out" | grep -q 'fresh capture is not idle'; then
  pass "boot window refuses loudly instead of poking on a timer"
else
  fail "boot window refuses loudly instead of poking on a timer" \
    "rc=$boot_rc out=$boot_out"
fi
if wait_for_capture "$BOOT_PANE" "> "; then
  retry_out="$(run_watcher "$RECIPIENT_BOOT" "$BOOT_PANE")"
  retry_rc=$?
  retry_capture="$(tmux_ capture-pane -p -J -t "$BOOT_PANE")"
  if [ "$retry_rc" -eq 0 ] && printf '%s\n' "$retry_capture" | grep -Fq "$poke"; then
    pass "next tick retries after boot and submits one poke"
  else
    fail "next tick retries after boot and submits one poke" \
      "rc=$retry_rc out=$retry_out capture=$retry_capture"
  fi
else
  fail "boot fixture eventually rendered an idle prompt"
fi

echo "$PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
