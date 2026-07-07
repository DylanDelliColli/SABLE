#!/usr/bin/env bash
# test-sable-msg.sh — real-tmux coverage for sable-msg: verified delivery
# (SABLE-bq93) and bead-addressed worker delivery (SABLE-6izz).
#
# Composes:
#   - a manager-role pane (a stand-in "TUI" that shows nothing for a boot
#     delay, then a bare `❯ ` prompt) to prove --interrupt survives a
#     still-booting pane, VERIFIED by grepping capture-pane for the framed
#     header AND the recorded submitted line -- not just trusting send-keys'
#     zero exit code (the exact SABLE-bq93 false-positive).
#   - a worker-tagged pane (@sable_role=worker, @sable_bead=<id>, the same
#     tags sable-spawn-worker sets) to prove --bead delivery resolves it
#     (SABLE-6izz), that an unknown bead id fails cleanly, and that plain
#     manager-role resolution is unchanged.
#
# Run with: bash hooks/test/test-sable-msg.sh

set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
BIN="$REPO/bin"
command -v tmux >/dev/null 2>&1 || { echo "SKIP: tmux not installed"; exit 0; }

SOCK="sable-msg-e2e-$$"
REC="$(mktemp -d)"

PASS=0; FAIL=0; FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
tmux_() { tmux -L "$SOCK" "$@"; }
cleanup() { tmux_ kill-server >/dev/null 2>&1; rm -rf "$REC"; }
trap cleanup EXIT

export SABLE_TMUX_SOCKET="$SOCK"
export SABLE_MSG_POLL_INTERVAL="0.1"
export SABLE_MSG_SUBMIT_TRIES="30"
export SABLE_MSG_READY_TIMEOUT="5"

# A stand-in "TUI" pane: shows nothing (not ready) for BOOT_DELAY seconds, then
# a bare `❯ ` prompt line -- exactly the shape sable_pane_lib.pane_ready looks
# for. Each submitted line is appended to $REC_FILE so we can assert on what
# actually landed as a turn (not just that send-keys exited 0).
STAND_IN="$REC/stand-in.sh"
cat > "$STAND_IN" <<'SCRIPT'
#!/usr/bin/env bash
sleep "${BOOT_DELAY:-0}"
# Drain + scroll away anything sent while booting (e.g. --interrupt's Escape):
# a real TUI redraws its own UI once ready rather than leaving pre-ready
# keystrokes' raw tty-echo bleeding into the first prompt line, which would
# otherwise defeat dispatch_landed's "does the last ❯/> line hold the
# message" box-detection (a stray leading control char keeps that line from
# ever matching, causing a false-positive "landed").
while read -t 0.2 -r -N 1 _junk; do :; done
printf '%.0s\n' $(seq 1 60)
while true; do
  printf '\xe2\x9d\xaf '
  IFS= read -r line || break
  echo "$line" >> "$REC_FILE"
done
SCRIPT
chmod +x "$STAND_IN"

# --- 1) manager pane, booting for 2s: --interrupt must not drop the turn ----
tmux_ new-session -d -s w -x 200 -y 50
tmux_ respawn-pane -k -t w "REC_FILE=$REC/optimus.txt BOOT_DELAY=2 $STAND_IN"
tmux_ set-option -p -t w @sable_role optimus
sleep 0.2   # still booting at this point

if CLAUDE_AGENT_NAME=lincoln python3 "$BIN/sable-msg" optimus "cap in force" --interrupt >/dev/null 2>&1; then
  pass "sable-msg --interrupt into a booting pane returns ok"
else
  fail "sable-msg --interrupt into a booting pane returns ok"
fi
cap="$(tmux_ capture-pane -t w -p)"
if printf '%s' "$cap" | grep -q '⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force'; then
  pass "interrupt message verified landed on a still-booting pane (SABLE-bq93)"
else
  fail "interrupt message verified landed on a still-booting pane (SABLE-bq93)" "$cap"
fi
if [ -f "$REC/optimus.txt" ] && grep -q 'cap in force' "$REC/optimus.txt"; then
  pass "message was actually SUBMITTED as a turn, not just typed"
else
  fail "message was actually SUBMITTED as a turn, not just typed"
fi

# --- 2) worker pane, tagged like sable-spawn-worker tags it -----------------
BEAD="market-brief-package-73t4"
tmux_ new-window -d -t w: -n worker -c /tmp "PS1='> ' bash --noprofile --norc"
wpane="$(tmux_ list-panes -a -F '#{pane_id} #{window_name}' | awk '$2=="worker"{print $1; exit}')"
tmux_ set-option -p -t "$wpane" @sable_role worker
tmux_ set-option -p -t "$wpane" @sable_bead "$BEAD"
tmux_ set-option -p -t "$wpane" @sable_status running
sleep 0.3

if CLAUDE_AGENT_NAME=optimus python3 "$BIN/sable-msg" "$BEAD" "hold the tree claim" --bead >/dev/null 2>&1; then
  pass "sable-msg --bead resolves a worker pane by @sable_bead (SABLE-6izz)"
else
  fail "sable-msg --bead resolves a worker pane by @sable_bead (SABLE-6izz)"
fi
wcap="$(tmux_ capture-pane -t "$wpane" -p)"
if printf '%s' "$wcap" | grep -q "⟦SABLE-MSG⟧ from=optimus to=$BEAD :: hold the tree claim"; then
  pass "bead-addressed message delivered to the correct worker pane"
else
  fail "bead-addressed message delivered to the correct worker pane" "$wcap"
fi

# --- 3) unknown bead id fails cleanly ----------------------------------------
ERRFILE="$REC/err.txt"
if python3 "$BIN/sable-msg" ghost-bead "hello" --bead --from optimus >/dev/null 2>"$ERRFILE"; then
  fail "unknown bead id errors cleanly (unexpectedly returned 0)"
else
  if grep -q "ghost-bead" "$ERRFILE"; then
    pass "unknown bead id errors cleanly"
  else
    fail "unknown bead id errors cleanly" "$(cat "$ERRFILE")"
  fi
fi

# --- 4) manager-role resolution is unchanged (regression) --------------------
if CLAUDE_AGENT_NAME=lincoln python3 "$BIN/sable-msg" optimus "still routes by role" >/dev/null 2>&1; then
  pass "manager-role resolution unchanged"
else
  fail "manager-role resolution unchanged"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
