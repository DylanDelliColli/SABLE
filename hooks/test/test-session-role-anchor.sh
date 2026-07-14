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

rm -rf "$PROJ" "$HOMETMP" "$NOPROJ"
echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
