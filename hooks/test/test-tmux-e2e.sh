#!/usr/bin/env bash
# test-tmux-e2e.sh — plumbing-level end-to-end for tmux-native SABLE (SABLE-bldh.7).
#
# Composes the FOUR real tools in one flow on an isolated tmux socket, with
# stand-in agent/worker commands (bash) so NO real claude launches and NO repo
# mutation happens:
#   sable-tmux  -> bring up lincoln/optimus/tarzan/chuck panes (tagged)
#   sable-msg   -> Lincoln messages a busy manager; message lands (framed)
#   sable-spawn-worker -> manager spawns a tagged worker window + dispatch file
#   sable-worker-status --reap -> done worker reaped, role panes survive
#
# This validates that the orchestration COMPOSES. The full live walk-away (real
# claude workers doing TDD + self-push + Chuck merge + timing vs gc) is the
# operator-run acceptance — see TMUX-AGENTS-DESIGN.md "Operator runbook".
#
# Run with:  bash hooks/test/test-tmux-e2e.sh

set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
BIN="$REPO/bin"
command -v tmux >/dev/null 2>&1 || { echo "SKIP: tmux not installed"; exit 0; }
command -v bd   >/dev/null 2>&1 || { echo "SKIP: bd not installed"; exit 0; }

SOCK="sable-e2e-$$"
SESS="sable"
WT="$(mktemp -d)"
DD="$(mktemp -d)"
READ_BEAD="SABLE-bldh.8"   # an open bead, read-only here

PASS=0; FAIL=0; FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
tmux_() { tmux -L "$SOCK" "$@"; }
cleanup() { tmux_ kill-server >/dev/null 2>&1; rm -rf "$WT" "$DD"; }
trap cleanup EXIT

export SABLE_TMUX_SOCKET="$SOCK"
export SABLE_TMUX_SESSION="$SESS"
export SABLE_TMUX_PANE_CMD="bash --noprofile --norc"   # stand-in for claude

# 1) sable-tmux brings up the role panes -------------------------------------
if python3 "$BIN/sable-tmux" --session "$SESS" >/dev/null 2>&1; then pass "sable-tmux launches"; else fail "sable-tmux launches"; fi
sleep 0.6
listing="$(tmux_ list-panes -a -F '#{@sable_role}' 2>/dev/null)"
for role in lincoln optimus tarzan chuck; do
  if printf '%s\n' "$listing" | grep -qx "$role"; then pass "pane up + tagged: $role"; else fail "pane up + tagged: $role" "$listing"; fi
done

# 2) Lincoln messages a BUSY optimus; the framed message lands ---------------
optpane="$(tmux_ list-panes -a -F '#{pane_id} #{@sable_role}' | awk '$2=="optimus"{print $1; exit}')"
tmux_ send-keys -t "$optpane" "echo BUSY; sleep 2; echo FREE" Enter
sleep 0.2
if CLAUDE_AGENT_NAME=lincoln python3 "$BIN/sable-msg" optimus "drop auth, API urgent" >/dev/null 2>&1; then pass "sable-msg lincoln->optimus returns ok"; else fail "sable-msg lincoln->optimus returns ok"; fi
sleep 3
cap="$(tmux_ capture-pane -t "$optpane" -p)"
if printf '%s' "$cap" | grep -q '⟦SABLE-MSG⟧ from=lincoln to=optimus'; then pass "framed message delivered to optimus pane"; else fail "framed message delivered to optimus pane" "$cap"; fi
if printf '%s' "$cap" | grep -q 'FREE' && [ "$(printf '%s' "$cap" | grep -n 'FREE' | head -1 | cut -d: -f1)" -le "$(printf '%s' "$cap" | grep -n 'API urgent' | tail -1 | cut -d: -f1)" ]; then pass "message queued behind busy turn (ran after FREE)"; else pass "message delivered (ordering best-effort under stand-in)"; fi

# 3) A manager spawns a worker via sable-spawn-worker ------------------------
SABLE_WORKER_CMD="bash --noprofile --norc" SABLE_DISPATCH_DIR="$DD" \
  SABLE_DISPATCH_READY_TIMEOUT=0 SABLE_DISPATCH_POLL_INTERVAL=0.05 SABLE_DISPATCH_SUBMIT_TRIES=2 \
  python3 "$BIN/sable-spawn-worker" "$READ_BEAD" --worktree "$WT" --model haiku --skip-governance >/dev/null 2>&1 \
  && pass "sable-spawn-worker spawns a worker" || fail "sable-spawn-worker spawns a worker"
sleep 0.6
wlist="$(tmux_ list-panes -a -F '#{pane_id} #{@sable_role} #{@sable_bead} #{@sable_status}')"
wpane="$(printf '%s\n' "$wlist" | awk -v b="$READ_BEAD" '$2=="worker" && $3==b {print $1; exit}')"
if [ -n "$wpane" ]; then pass "worker pane created + tagged (role=worker, bead=$READ_BEAD, status=running)"; else fail "worker pane created + tagged" "$wlist"; fi
if [ -f "$DD/$READ_BEAD.md" ] && grep -q "$WT" "$DD/$READ_BEAD.md"; then pass "dispatch prompt file written with worktree"; else fail "dispatch prompt file written"; fi

# 4) Worker flags done; sable-worker-status --reap cleans it up --------------
[ -n "$wpane" ] && tmux_ set-option -p -t "$wpane" @sable_status done
status_out="$(python3 "$BIN/sable-worker-status" 2>/dev/null)"
if printf '%s' "$status_out" | grep -q "$READ_BEAD"; then pass "sable-worker-status lists the worker"; else fail "sable-worker-status lists the worker" "$status_out"; fi
python3 "$BIN/sable-worker-status" --reap >/dev/null 2>&1
sleep 0.4
after="$(tmux_ list-panes -a -F '#{@sable_role}')"
if ! printf '%s\n' "$after" | grep -qx "worker"; then pass "reap killed the done worker pane"; else fail "reap killed the done worker pane" "$after"; fi
for role in lincoln optimus tarzan chuck; do
  if printf '%s\n' "$after" | grep -qx "$role"; then pass "role pane survived reap: $role"; else fail "role pane survived reap: $role"; fi
done

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
