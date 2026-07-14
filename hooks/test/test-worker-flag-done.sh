#!/usr/bin/env bash
# test-worker-flag-done.sh — market-brief-package-uj22 / SABLE-5v9n: a worker's
# done-flag command must target ITS OWN pane, never whichever pane happens to
# hold the session's current/active focus. Without `-t`, `tmux set-option -p`
# resolves the target from the client's active pane — an unattached worker
# subprocess (a Bash tool call from a warm-pane claude session) is not itself
# a tmux client, so the bare form silently misroutes.
#
# Runs against a REAL tmux server on an isolated socket (-L); never touches
# the operator's session.
#
# Run with:
#   bash hooks/test/test-worker-flag-done.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
BIN="$REPO/bin/sable-worker-status"
SOCK="wfd-test-$$"
SCRATCH_BEADS_DIR=""

cleanup() {
  tmux -L "$SOCK" kill-server >/dev/null 2>&1 || true
  [ -n "$SCRATCH_BEADS_DIR" ] && rm -rf "$SCRATCH_BEADS_DIR" 2>/dev/null || true
}
trap cleanup EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

# --- fixture: a manager pane (holds operator focus) + a worker pane ---
tmux -L "$SOCK" new-session -d -s w -x 200 -y 50 'bash --noprofile --norc'
sleep 0.3
tmux -L "$SOCK" split-window -t w 'bash --noprofile --norc'
sleep 0.3

MANAGER_PANE="$(tmux -L "$SOCK" list-panes -t w -F '#{pane_id}' | sed -n 1p)"
WORKER_PANE="$(tmux -L "$SOCK" list-panes -t w -F '#{pane_id}' | sed -n 2p)"

tmux -L "$SOCK" set-option -p -t "$MANAGER_PANE" @sable_role manager
tmux -L "$SOCK" set-option -p -t "$WORKER_PANE" @sable_role worker
tmux -L "$SOCK" set-option -p -t "$WORKER_PANE" @sable_bead test-bead
tmux -L "$SOCK" set-option -p -t "$WORKER_PANE" @sable_status running

# operator "focus" is on the manager pane — the exact condition that misroutes
# the bare '-p' form (no -t) onto the wrong pane.
tmux -L "$SOCK" select-pane -t "$MANAGER_PANE"

# --- regression proof: the OLD bare form (no -t) misroutes to session focus,
#     not to the worker's own pane, even when run from a subprocess whose
#     TMUX_PANE correctly identifies the worker ---
env TMUX_PANE="$WORKER_PANE" tmux -L "$SOCK" set-option -p @sable_status done
mgr_status="$(tmux -L "$SOCK" show-option -p -t "$MANAGER_PANE" -v @sable_status 2>/dev/null || true)"
if [ "$mgr_status" = "done" ]; then
  pass "bare '-p' form (no -t) reproduces the misroute onto the focused pane"
else
  fail "bare '-p' form (no -t) reproduces the misroute onto the focused pane" "manager pane status='$mgr_status' (expected 'done' to prove the OLD bug existed)"
fi
# reset both panes for the real assertion below
tmux -L "$SOCK" set-option -pu -t "$MANAGER_PANE" @sable_status 2>/dev/null || true
tmux -L "$SOCK" set-option -p -t "$WORKER_PANE" @sable_status running

# --- the fix: explicit -t "$TMUX_PANE", run from an UNATTACHED subprocess
#     (bash -c, not tmux send-keys into the pane), must tag ONLY the worker's
#     own pane regardless of session focus ---
env TMUX_PANE="$WORKER_PANE" bash -c \
  'tmux -L "$1" set-option -p -t "$TMUX_PANE" @sable_status done' _ "$SOCK"

worker_status="$(tmux -L "$SOCK" show-option -p -t "$WORKER_PANE" -v @sable_status)"
mgr_status_after="$(tmux -L "$SOCK" show-option -p -t "$MANAGER_PANE" -v @sable_status 2>/dev/null || echo "")"

if [ "$worker_status" = "done" ]; then
  pass "explicit -t \"\$TMUX_PANE\" flags the worker's OWN pane"
else
  fail "explicit -t \"\$TMUX_PANE\" flags the worker's OWN pane" "got '$worker_status'"
fi
if [ "$mgr_status_after" != "done" ]; then
  pass "manager pane never acquires @sable_status=done"
else
  fail "manager pane never acquires @sable_status=done" "manager pane got done"
fi

# --- reap then collects the correctly-tagged worker pane, leaving the
#     manager pane (role != worker) untouched ---
# SABLE-0kj2: two independent gaps in this fixture, both fixed here.
#
# (1) SABLE-e1e3 per-repo session scoping (bin/sable_pane_lib.resolve_session):
# without SABLE_TMUX_SESSION set, --reap derives a session name from the
# calling process's real repo root (sable-SABLE) instead of using this
# fixture's actual session "w" (created above via `tmux -L "$SOCK" new-session
# -d -s w ...`) — list_workers() then scopes to a session that doesn't exist
# on the isolated socket and silently sees zero panes. Pin SABLE_TMUX_SESSION=w
# so the reap subprocess looks at the fixture's own session.
#
# (2) SABLE-1kbo real-done crosscheck (bead_closed() in bin/sable-worker-status)
# resolves @sable_bead against the beads tracker before trusting
# @sable_status=done. Make the fixture bead genuinely resolve as closed by
# creating + closing a REAL bead in an ISOLATED SCRATCH tracker (never the
# shared repo DB — SABLE-0ssz fixture-isolation class), then pointing both
# the create/close and the reap subprocess's `bd show` at that same scratch
# DB via BEADS_DB — so the assertion actually exercises the confirmed-done
# path rather than happening to pass via the crosscheck's fails-open-when
# -unresolvable fallback.
if command -v bd >/dev/null 2>&1; then
  SCRATCH_BEADS_DIR=$(mktemp -d)
  ( cd "$SCRATCH_BEADS_DIR" && BD_NON_INTERACTIVE=1 bd init --prefix=wfd \
      --non-interactive --skip-agents --skip-hooks --quiet >/dev/null 2>&1 )
  REAP_BEAD=$(cd "$SCRATCH_BEADS_DIR" && bd create --title="wfd reap-fixture bead" \
      --type=task 2>/dev/null | grep -oE '[A-Za-z][A-Za-z0-9]*-[a-zA-Z0-9]+' | head -1)
  if [ -n "$REAP_BEAD" ]; then
    ( cd "$SCRATCH_BEADS_DIR" && bd close "$REAP_BEAD" >/dev/null 2>&1 )
    tmux -L "$SOCK" set-option -p -t "$WORKER_PANE" @sable_bead "$REAP_BEAD"
  fi
fi

# CLAUDE_AGENT_NAME forced empty (SABLE-dcw2): this reap asserts the fleet-wide
# view over a manually-tagged, lane-less worker pane, so it must not inherit an
# ambient lane from the runner (a manager pane sets CLAUDE_AGENT_NAME) that the
# new own-lane default filter would use to scope the lane-less pane out.
if [ -n "$SCRATCH_BEADS_DIR" ] && [ -n "${REAP_BEAD:-}" ]; then
  SABLE_TMUX_SOCKET="$SOCK" SABLE_TMUX_SESSION=w CLAUDE_AGENT_NAME="" BEADS_DB="$SCRATCH_BEADS_DIR/.beads" python3 "$BIN" --reap >/dev/null 2>&1
else
  SABLE_TMUX_SOCKET="$SOCK" SABLE_TMUX_SESSION=w CLAUDE_AGENT_NAME="" python3 "$BIN" --reap >/dev/null 2>&1
fi
sleep 0.3
alive_worker="$(tmux -L "$SOCK" list-panes -a -F '#{pane_id}' 2>/dev/null | grep -xc "$WORKER_PANE" || true)"
alive_manager="$(tmux -L "$SOCK" list-panes -a -F '#{pane_id}' 2>/dev/null | grep -xc "$MANAGER_PANE" || true)"
if [ "$alive_worker" -eq 0 ]; then
  pass "sable-worker-status --reap collects the done worker pane"
else
  fail "sable-worker-status --reap collects the done worker pane" "worker pane still alive"
fi
if [ "$alive_manager" -eq 1 ]; then
  pass "the manager pane survives the reap sweep"
else
  fail "the manager pane survives the reap sweep" "manager pane was killed"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then exit 1; fi
exit 0
