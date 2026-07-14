#!/usr/bin/env bash
# test-active-contracts-integration.sh — end-to-end active-protocol surface (SABLE-9ozz).
#
# INTEGRATION (real composition, no mocks): drive the REAL writer tools
# (bin/sable-mode + bin/sable-contract) against a REAL temp git repo, letting
# per-repo path resolution (git --git-common-dir) place both surfaces under
# <repo>/.claude/sable/state/, then run the REAL SessionStart hook
# (session-role-anchor.sh) with cwd = that repo and assert the injected
# additionalContext carries the live mode + the live contract.
#
# This is the regression fixture for the gah9 miss: a merge-instruction contract
# written BEFORE a (simulated) restart must PERSIST into the post-restart boot
# context — i.e. a fresh manager SessionStart surfaces it. The two writer tools
# and the reader hook must agree on the surface location with zero override env,
# purely via git resolution, or a restarted pane silently reverts to its static
# identity (which is exactly the bug).
#
# Run with:
#   bash hooks/test/test-active-contracts-integration.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO/hooks/multi-manager/session-role-anchor.sh"
MODE="$REPO/bin/sable-mode"
CONTRACT="$REPO/bin/sable-contract"

# Hermeticity (SABLE-j3bi): strip any ambient SABLE pane env so this suite is
# deterministic whether or not it runs inside a live manager/worker pane.
unset SABLE_WORKER_PANE CLAUDE_AGENT_NAME CLAUDE_AGENT_ROLE \
      SABLE_MODE_STATE SABLE_ACTIVE_CONTRACTS 2>/dev/null || true

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

SS='{"hook_event_name":"SessionStart"}'

# --- A real temp git repo standing in for a project checkout ------------------
RREPO="$(mktemp -d)"
git -C "$RREPO" init -q >/dev/null 2>&1
mkdir -p "$RREPO/.claude/sable/roles"
# chuck's STATIC identity — deliberately describes the OLD manual-merge flow,
# exactly as it did during the gah9 incident.
printf 'CHUCK STATIC ROLE: merge landed branches with bare git merge --no-ff + git push.\n' \
    > "$RREPO/.claude/sable/roles/chuck.md"

# --- The flip choreography: write the live protocol state to disk -------------
# (fix direction 3 — the flip persists its contract change to the surface.)
( cd "$RREPO" && SABLE_ORCHESTRATION=1 bash "$MODE" set execution >/dev/null 2>&1 ) \
    || fail "sable-mode set execution succeeds"
( cd "$RREPO" && bash "$CONTRACT" add \
    "sable-merge-gate is the SOLE merge path; no bare git merge/push." >/dev/null 2>&1 ) \
    || fail "sable-contract add succeeds"

# Both tools must have resolved to the SAME per-repo state dir with no override.
STATEDIR="$RREPO/.claude/sable/state"
if [ -f "$STATEDIR/mode-state.json" ]; then pass "mode-state written to per-repo state dir"; else fail "mode-state written to per-repo state dir" "missing $STATEDIR/mode-state.json"; fi
if [ -s "$STATEDIR/active-contracts.md" ]; then pass "active-contracts written to per-repo state dir"; else fail "active-contracts written to per-repo state dir" "missing/empty $STATEDIR/active-contracts.md"; fi

# --- Simulate a fresh manager SessionStart in that repo -----------------------
# No SABLE_MODE_STATE / SABLE_ACTIVE_CONTRACTS override: the hook must find both
# surfaces purely by git resolution, the same way a restarted chuck pane would.
out="$(cd "$RREPO" && printf '%s' "$SS" | CLAUDE_AGENT_NAME=chuck CLAUDE_AGENT_ROLE=manager bash "$HOOK" 2>/dev/null)"

if printf '%s' "$out" | grep -q 'CHUCK STATIC ROLE'; then pass "boot injects the static identity"; else fail "boot injects the static identity" "got: ${out:0:200}"; fi
# THE regression assertion: the pre-restart merge contract persists into the
# post-restart boot context.
if printf '%s' "$out" | grep -q 'sable-merge-gate is the SOLE merge path'; then pass "boot surfaces the live merge contract (gah9 regression)"; else fail "boot surfaces the live merge contract (gah9 regression)" "got: ${out:0:400}"; fi
if printf '%s' "$out" | grep -q 'execution'; then pass "boot surfaces the live execution mode"; else fail "boot surfaces the live execution mode" "got: ${out:0:400}"; fi
if printf '%s' "$out" | grep -q 'LIVE PROTOCOL STATE'; then pass "boot delimits live state with its banner"; else fail "boot delimits live state with its banner" "got: ${out:0:200}"; fi
if printf '%s' "$out" | grep -qi 'reconcile'; then pass "boot carries the reconciliation instruction"; else fail "boot carries the reconciliation instruction" "got: ${out:0:400}"; fi

# --- Clearing the surface removes it from the next boot -----------------------
( cd "$RREPO" && bash "$CONTRACT" clear >/dev/null 2>&1 ) || fail "sable-contract clear succeeds"
out="$(cd "$RREPO" && printf '%s' "$SS" | CLAUDE_AGENT_NAME=chuck CLAUDE_AGENT_ROLE=manager bash "$HOOK" 2>/dev/null)"
if printf '%s' "$out" | grep -q 'sable-merge-gate is the SOLE merge path'; then fail "cleared contract no longer surfaces" "still present"; else pass "cleared contract no longer surfaces"; fi
# Mode is still set, so the live block persists (mode alone keeps it).
if printf '%s' "$out" | grep -q 'execution'; then pass "mode still surfaces after contract cleared"; else fail "mode still surfaces after contract cleared" "got: ${out:0:300}"; fi

rm -rf "$RREPO"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
