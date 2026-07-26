#!/usr/bin/env bash
# test-identity-hermeticity.sh — acceptance suite for the SABLE-j3bi central
# hermeticity mechanism (hooks/test/lib-identity-isolation.sh).
#
# SCOPE: this suite tests the MECHANISM itself (sable_scrub_identity_env /
# sable_tmux_spawn), not the per-suite adoption sweep across hooks/test/*.sh
# — that sweep is SABLE-a9453's job and is blocked on this mechanism landing.
# The two suites already fixed by hand before this mechanism existed
# (test-active-contracts-integration.sh, test-session-role-anchor.sh) are the
# reference implementations the mechanism generalizes; test-worker-flag-done.sh
# is the live counter-example of a call-site-only scrub (SABLE-dcw2) that the
# guard below must NOT bless.
#
# Runs against a REAL tmux server on an isolated socket (-L); never touches
# the operator's session. Real tmux, no mocks.
#
# Run with:
#   bash hooks/test/test-identity-hermeticity.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
LIB="$REPO/hooks/test/lib-identity-isolation.sh"

if [ ! -r "$LIB" ]; then
  echo "FAIL: lib not found at $LIB"
  exit 2
fi
# shellcheck source=lib-identity-isolation.sh
source "$LIB"

SOCK_BASE="ih-test-$$"
cleanup() {
  tmux -L "${SOCK_BASE}-a" kill-server >/dev/null 2>&1 || true
  tmux -L "${SOCK_BASE}-b" kill-server >/dev/null 2>&1 || true
  tmux -L "${SOCK_BASE}-c" kill-server >/dev/null 2>&1 || true
  tmux -L "${SOCK_BASE}-d1" kill-server >/dev/null 2>&1 || true
  tmux -L "${SOCK_BASE}-d2" kill-server >/dev/null 2>&1 || true
}
trap cleanup EXIT

PASS=0; FAIL=0; FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

# ==========================================================================
# 1. sable_scrub_identity_env unsets every ambient identity var
# ==========================================================================
(
  export SABLE_WORKER_PANE=1 CLAUDE_AGENT_NAME=tarzan CLAUDE_AGENT_ROLE=manager SABLE_BEAD=SABLE-fake
  sable_scrub_identity_env
  leaked="$(sable_leaked_identity_vars)"
  if [ -z "$leaked" ]; then
    exit 0
  else
    echo "$leaked"
    exit 1
  fi
)
if [ $? -eq 0 ]; then
  pass "sable_scrub_identity_env unsets SABLE_WORKER_PANE/CLAUDE_AGENT_NAME/CLAUDE_AGENT_ROLE/SABLE_BEAD"
else
  fail "sable_scrub_identity_env unsets SABLE_WORKER_PANE/CLAUDE_AGENT_NAME/CLAUDE_AGENT_ROLE/SABLE_BEAD" "still leaked after scrub"
fi

# ==========================================================================
# 2. GUARD: sable_tmux_spawn refuses loudly when identity leaked and the
#    suite did NOT scrub first — the dcw2 class this mechanism exists to
#    catch (a call-site-only scrub, or no scrub at all, before a spawn).
# ==========================================================================
SOCK_B="${SOCK_BASE}-b"
(
  export CLAUDE_AGENT_NAME=tarzan
  # deliberately do NOT call sable_scrub_identity_env first
  sable_tmux_spawn -L "$SOCK_B" new-session -d -s w -x 80 -y 24 'bash --noprofile --norc'
)
guard_rc=$?
guard_stderr="$(
  export CLAUDE_AGENT_NAME=tarzan
  sable_tmux_spawn -L "$SOCK_B" new-session -d -s w2 -x 80 -y 24 'bash --noprofile --norc' 2>&1 1>/dev/null
)"
if [ "$guard_rc" -ne 0 ]; then
  pass "sable_tmux_spawn returns non-zero when identity env leaked and unscrubbed"
else
  fail "sable_tmux_spawn returns non-zero when identity env leaked and unscrubbed" "got rc=$guard_rc"
fi
if printf '%s' "$guard_stderr" | grep -q "FATAL (SABLE-j3bi hermeticity guard)"; then
  pass "sable_tmux_spawn's refusal is loud (actionable stderr, not a silent no-op)"
else
  fail "sable_tmux_spawn's refusal is loud (actionable stderr, not a silent no-op)" "stderr: $guard_stderr"
fi
if ! tmux -L "$SOCK_B" has-session -t w 2>/dev/null; then
  pass "sable_tmux_spawn's refusal never actually spawns the contaminated pane"
else
  fail "sable_tmux_spawn's refusal never actually spawns the contaminated pane" "session 'w' exists on $SOCK_B"
fi
tmux -L "$SOCK_B" kill-server >/dev/null 2>&1 || true

# ==========================================================================
# 3. UNIT (per bead test spec): spawn a stand-in pane through the shared
#    harness with CLAUDE_AGENT_NAME deliberately set in the PARENT, then read
#    the CHILD PANE PROCESS's /proc/<pid>/environ and assert the var is
#    ABSENT — proving the scrub is positional (before spawn), not merely
#    present somewhere in the suite. Red before the central fix (i.e. red if
#    sable_tmux_spawn is replaced with a bare `tmux` call in this test).
# ==========================================================================
SOCK_C="${SOCK_BASE}-c"
(
  export CLAUDE_AGENT_NAME=tarzan SABLE_WORKER_PANE=1 CLAUDE_AGENT_ROLE=manager SABLE_BEAD=SABLE-fake
  sable_scrub_identity_env
  sable_tmux_spawn -L "$SOCK_C" new-session -d -s w -x 80 -y 24 'bash --noprofile --norc'
)
sleep 0.3
pane_pid="$(tmux -L "$SOCK_C" list-panes -t w -F '#{pane_pid}' 2>/dev/null | head -1)"
if [ -z "$pane_pid" ] || [ ! -r "/proc/$pane_pid/environ" ]; then
  fail "stand-in pane's own process environ never inherits the parent's identity vars" "could not read /proc/$pane_pid/environ"
else
  environ_text="$(tr '\0' '\n' < "/proc/$pane_pid/environ" 2>/dev/null || true)"
  if printf '%s\n' "$environ_text" | grep -qE '^(CLAUDE_AGENT_NAME|SABLE_WORKER_PANE|CLAUDE_AGENT_ROLE|SABLE_BEAD)='; then
    fail "stand-in pane's own process environ never inherits the parent's identity vars" \
      "$(printf '%s\n' "$environ_text" | grep -E '^(CLAUDE_AGENT_NAME|SABLE_WORKER_PANE|CLAUDE_AGENT_ROLE|SABLE_BEAD)=')"
  else
    pass "stand-in pane's own process environ never inherits the parent's identity vars"
  fi
fi
tmux -L "$SOCK_C" kill-server >/dev/null 2>&1 || true

# ==========================================================================
# 4. INTEGRATION — equal-verdicts double-run (real tmux, no mocks): a
#    scenario that spawns a pane through the mechanism and then reads the
#    pane's live identity back (the same shape SABLE-to8m's real check
#    takes) must produce a BYTE-IDENTICAL verdict whether the invoking shell
#    was polluted with agent identity or clean. This is the class's
#    regression check and the mechanism's own acceptance criterion.
# ==========================================================================
run_scenario() {
  local sock="$1"
  sable_scrub_identity_env
  if ! sable_tmux_spawn -L "$sock" new-session -d -s w -x 80 -y 24 'bash --noprofile --norc' >/dev/null 2>&1; then
    echo "SPAWN_REFUSED"
    return
  fi
  sleep 0.3
  local pid
  pid="$(tmux -L "$sock" list-panes -t w -F '#{pane_pid}' 2>/dev/null | head -1)"
  if [ -z "$pid" ] || [ ! -r "/proc/$pid/environ" ]; then
    echo "NO_PANE_PID"
    return
  fi
  if tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep -qE '^CLAUDE_AGENT_NAME='; then
    echo "VERDICT=IDENTITY_LEAKED"
  else
    echo "VERDICT=CLEAN"
  fi
}

SOCK_D1="${SOCK_BASE}-d1"
SOCK_D2="${SOCK_BASE}-d2"

verdict_polluted="$(
  export CLAUDE_AGENT_NAME=tarzan SABLE_WORKER_PANE=%99 SABLE_BEAD=SABLE-fake
  run_scenario "$SOCK_D1"
)"
tmux -L "$SOCK_D1" kill-server >/dev/null 2>&1 || true

verdict_clean="$(
  unset CLAUDE_AGENT_NAME SABLE_WORKER_PANE SABLE_BEAD CLAUDE_AGENT_ROLE 2>/dev/null || true
  run_scenario "$SOCK_D2"
)"
tmux -L "$SOCK_D2" kill-server >/dev/null 2>&1 || true

if [ "$verdict_polluted" = "$verdict_clean" ] && [ "$verdict_polluted" = "VERDICT=CLEAN" ]; then
  pass "double-run: identical VERDICT=CLEAN whether the invoking shell was polluted or clean"
else
  fail "double-run: identical VERDICT=CLEAN whether the invoking shell was polluted or clean" \
    "polluted='$verdict_polluted' clean='$verdict_clean'"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="

if [ "$FAIL" -gt 0 ]; then
  printf "Failed tests:%b\n" "$FAIL_NAMES"
  exit 1
fi
exit 0
