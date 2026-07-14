#!/usr/bin/env bash
# test-session-role-anchor.sh — tests the identity-injection hook (SABLE-cav.9).
#
# session-role-anchor.sh injects roles/<CLAUDE_AGENT_NAME>.md as SessionStart
# additionalContext. For project-scoped cockpit installs it must resolve the
# role PROJECT-FIRST ($PWD/.claude/sable/roles) then fall back to user-level
# (~/.claude/sable/roles). Self-gates: only CLAUDE_AGENT_ROLE=manager sessions
# with the env name set get an identity.
#
# Run with:
#   bash hooks/test/test-session-role-anchor.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO/hooks/multi-manager/session-role-anchor.sh"

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

# Hermeticity (SABLE-j3bi): this suite feeds the hook its agent env INLINE per
# invocation. If it runs INSIDE a live SABLE pane, the ambient SABLE_WORKER_PANE=1
# and CLAUDE_AGENT_NAME/ROLE leak into every un-overridden invocation (the worker
# early-exit fires; the base cases go red). Clean-room CI sets none of these, so
# the leak is invisible there. Unset them up front so the suite is hermetic
# regardless of launch context. Also clear the live-state surfaces so the base
# cases never pick up a stray real mode-state.
unset SABLE_WORKER_PANE CLAUDE_AGENT_NAME CLAUDE_AGENT_ROLE \
      SABLE_MODE_STATE SABLE_ACTIVE_CONTRACTS 2>/dev/null || true

SS='{"hook_event_name":"SessionStart"}'

# ---------- project-first resolution ----------
PROJ="$(mktemp -d)"
mkdir -p "$PROJ/.claude/sable/roles"
printf 'PROJECT_COCKPIT_ROLE_MARKER\n' > "$PROJ/.claude/sable/roles/cockpit.md"
out="$(cd "$PROJ" && printf '%s' "$SS" | CLAUDE_AGENT_NAME=cockpit CLAUDE_AGENT_ROLE=manager bash "$HOOK" 2>/dev/null)"
if printf '%s' "$out" | grep -q 'PROJECT_COCKPIT_ROLE_MARKER'; then pass "injects project-scoped role"; else fail "injects project-scoped role" "got: ${out:0:120}"; fi
if printf '%s' "$out" | grep -q 'AGENT IDENTITY: COCKPIT'; then pass "wraps with identity banner"; else fail "wraps with identity banner"; fi

# ---------- user-level fallback ----------
HOMETMP="$(mktemp -d)"
mkdir -p "$HOMETMP/.claude/sable/roles"
printf 'USER_COCKPIT_ROLE_MARKER\n' > "$HOMETMP/.claude/sable/roles/cockpit.md"
NOPROJ="$(mktemp -d)"   # cwd with no project role
out="$(cd "$NOPROJ" && printf '%s' "$SS" | HOME="$HOMETMP" CLAUDE_AGENT_NAME=cockpit CLAUDE_AGENT_ROLE=manager bash "$HOOK" 2>/dev/null)"
if printf '%s' "$out" | grep -q 'USER_COCKPIT_ROLE_MARKER'; then pass "falls back to user-scoped role"; else fail "falls back to user-scoped role" "got: ${out:0:120}"; fi

# ---------- no role anywhere → no injection ----------
out="$(cd "$NOPROJ" && printf '%s' "$SS" | HOME="$NOPROJ" CLAUDE_AGENT_NAME=cockpit CLAUDE_AGENT_ROLE=manager bash "$HOOK" 2>/dev/null)"
if [ -z "$out" ]; then pass "no role file → no injection"; else fail "no role file → no injection" "got: $out"; fi

# ---------- gates ----------
out="$(cd "$PROJ" && printf '%s' "$SS" | env -u CLAUDE_AGENT_NAME CLAUDE_AGENT_ROLE=manager bash "$HOOK" 2>/dev/null)"   # name unset
if [ -z "$out" ]; then pass "unset CLAUDE_AGENT_NAME no-ops"; else fail "unset CLAUDE_AGENT_NAME no-ops" "got: $out"; fi

out="$(cd "$PROJ" && printf '%s' "$SS" | CLAUDE_AGENT_NAME=cockpit CLAUDE_AGENT_ROLE=auditor bash "$HOOK" 2>/dev/null)"   # non-manager
if [ -z "$out" ]; then pass "non-manager role no-ops"; else fail "non-manager role no-ops" "got: $out"; fi

# ---------- SABLE-38zi: worker pane never loads a manager role-card ----------
# A worker pane carries the lane manager's CLAUDE_AGENT_NAME + manager role (so
# its push fires the manager-gated for-chuck handoff), but SABLE_WORKER_PANE=1
# marks it as a worker: the role-anchor MUST stand down, or the worker boots as
# its manager and re-dispatches its own bead (duplicate pane, defeats the cap).
out="$(cd "$PROJ" && printf '%s' "$SS" | SABLE_WORKER_PANE=1 CLAUDE_AGENT_NAME=cockpit CLAUDE_AGENT_ROLE=manager bash "$HOOK" 2>/dev/null)"
if [ -z "$out" ]; then pass "worker pane (SABLE_WORKER_PANE) no-ops"; else fail "worker pane (SABLE_WORKER_PANE) no-ops" "got: ${out:0:120}"; fi

# ---------- SABLE-9ozz: live protocol state surfaced at SessionStart ----------
# A restarted manager pane (/clear, crash, session limit) reverts to its STATIC
# role card and loses every conversation-state convention — the merge-gate sole-
# path contract, interim fleet caps, the manual-relay rule. The hook must surface
# the LIVE protocol state from disk (orchestration mode + the active-contracts
# file colocated with mode-state) so a fresh boot reconciles against the CURRENT
# contract, not the historical identity. This mirrors the gah9 bypass: chuck's
# static identity described the OLD manual-merge flow while the live contract was
# "sable-merge-gate is the sole merge path".
LS="$(mktemp -d)"
mkdir -p "$LS/.claude/sable/roles" "$LS/.claude/sable/state"
printf 'CHUCK_ROLE_MARKER\n' > "$LS/.claude/sable/roles/chuck.md"
LS_MODE="$LS/.claude/sable/state/mode-state.json"
LS_CONTRACTS="$LS/.claude/sable/state/active-contracts.md"
printf '{"mode":"execution","since":"2026-07-13T09:00:00-0700","fleet":["chuck"]}\n' > "$LS_MODE"
printf -- '- sable-merge-gate is the SOLE merge path; no bare git merge/push.\n' > "$LS_CONTRACTS"

out="$(cd "$LS" && printf '%s' "$SS" | SABLE_MODE_STATE="$LS_MODE" CLAUDE_AGENT_NAME=chuck CLAUDE_AGENT_ROLE=manager bash "$HOOK" 2>/dev/null)"
if printf '%s' "$out" | grep -q 'CHUCK_ROLE_MARKER'; then pass "9ozz: static role still injected alongside live state"; else fail "9ozz: static role still injected alongside live state" "got: ${out:0:200}"; fi
if printf '%s' "$out" | grep -q 'sable-merge-gate is the SOLE merge path'; then pass "9ozz: active contract surfaced (gah9 regression)"; else fail "9ozz: active contract surfaced (gah9 regression)" "got: ${out:0:300}"; fi
if printf '%s' "$out" | grep -q 'execution'; then pass "9ozz: live orchestration mode surfaced"; else fail "9ozz: live orchestration mode surfaced" "got: ${out:0:300}"; fi
if printf '%s' "$out" | grep -q 'LIVE PROTOCOL STATE'; then pass "9ozz: live-protocol banner delimits the surface"; else fail "9ozz: live-protocol banner delimits the surface" "got: ${out:0:200}"; fi
if printf '%s' "$out" | grep -qi 'reconcile'; then pass "9ozz: boot reconciliation instruction present"; else fail "9ozz: boot reconciliation instruction present" "got: ${out:0:300}"; fi

# contracts present via SABLE_ACTIVE_CONTRACTS override, mode absent → still surfaces
LS2="$(mktemp -d)"
mkdir -p "$LS2/.claude/sable/roles"
printf 'CHUCK_ROLE_MARKER2\n' > "$LS2/.claude/sable/roles/chuck.md"
LS2_CFILE="$LS2/contracts.md"
printf -- '- interim worker cap is 2 per manager (SABLE-p8rf pending).\n' > "$LS2_CFILE"
out="$(cd "$LS2" && printf '%s' "$SS" | SABLE_MODE_STATE="$LS2/none.json" SABLE_ACTIVE_CONTRACTS="$LS2_CFILE" CLAUDE_AGENT_NAME=chuck CLAUDE_AGENT_ROLE=manager bash "$HOOK" 2>/dev/null)"
if printf '%s' "$out" | grep -q 'interim worker cap is 2'; then pass "9ozz: SABLE_ACTIVE_CONTRACTS override surfaced"; else fail "9ozz: SABLE_ACTIVE_CONTRACTS override surfaced" "got: ${out:0:200}"; fi

# neither mode nor contracts present → NO live block (byte-parity with legacy)
LS3="$(mktemp -d)"
mkdir -p "$LS3/.claude/sable/roles"
printf 'CHUCK_ROLE_MARKER3\n' > "$LS3/.claude/sable/roles/chuck.md"
out="$(cd "$LS3" && printf '%s' "$SS" | SABLE_MODE_STATE="$LS3/none.json" SABLE_ACTIVE_CONTRACTS="$LS3/none.md" CLAUDE_AGENT_NAME=chuck CLAUDE_AGENT_ROLE=manager bash "$HOOK" 2>/dev/null)"
if printf '%s' "$out" | grep -q 'CHUCK_ROLE_MARKER3'; then pass "9ozz: role still injected with no live state"; else fail "9ozz: role still injected with no live state" "got: ${out:0:200}"; fi
if printf '%s' "$out" | grep -q 'LIVE PROTOCOL STATE'; then fail "9ozz: no live block when surfaces empty" "unexpected live block"; else pass "9ozz: no live block when surfaces empty"; fi

# worker-pane gate wins even when a live contract is present
out="$(cd "$LS" && printf '%s' "$SS" | SABLE_MODE_STATE="$LS_MODE" SABLE_WORKER_PANE=1 CLAUDE_AGENT_NAME=chuck CLAUDE_AGENT_ROLE=manager bash "$HOOK" 2>/dev/null)"
if [ -z "$out" ]; then pass "9ozz: worker pane no-ops even with live contract"; else fail "9ozz: worker pane no-ops even with live contract" "got: ${out:0:120}"; fi

rm -rf "$PROJ" "$HOMETMP" "$NOPROJ" "$LS" "$LS2" "$LS3"
echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
