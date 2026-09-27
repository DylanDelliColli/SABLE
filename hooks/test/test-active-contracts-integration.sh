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
# Central scrub lives in lib-identity-isolation.sh so every suite shares one
# definition of "identity vars" instead of drifting copies of an unset list.
source "$REPO/hooks/test/lib-identity-isolation.sh"
sable_scrub_identity_env
unset SABLE_MODE_STATE SABLE_ACTIVE_CONTRACTS 2>/dev/null || true

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

SS='{"hook_event_name":"SessionStart"}'

# --- A real temp git repo standing in for a project checkout ------------------
RREPO="$(mktemp -d)"
git -C "$RREPO" init -q >/dev/null 2>&1
git -C "$RREPO" -c user.email=t@t -c user.name=t \
  commit --allow-empty -m init -q
mkdir -p "$RREPO/.claude/sable/roles"
# chuck's STATIC identity — deliberately describes the OLD manual-merge flow,
# exactly as it did during the gah9 incident.
printf 'CHUCK STATIC ROLE: merge landed branches with bare git merge --no-ff + git push.\n' \
    > "$RREPO/.claude/sable/roles/chuck.md"

# --- The flip choreography: write the live protocol state to disk -------------
# (fix direction 3 — the flip persists its contract change to the surface.)
( cd "$RREPO" && SABLE_ORCHESTRATION=1 "$MODE" set execution \
    --break-glass --reason "synthetic authority for active-contract test" \
    >/dev/null 2>&1 ) \
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

# --- SABLE-wx048: a mode flip must not destroy accumulated doctrine -----------
# Real composition: prior operator doctrine already on the surface, then the
# LITERAL /sable-execute section 1.5 choreography run against it. Measured
# 2026-07-30: that choreography led with `set`, which truncates, and one flip
# destroyed 25 of 29 committed lines including an entry marked BINDING ON EVERY
# INCOMING LINCOLN. The prior entries must survive, and the boot hook must still
# surface them afterwards — a contract that survives the write but not the read
# is no better than one that was erased.
FREPO="$(mktemp -d)"
git -C "$FREPO" init -q >/dev/null 2>&1
git -C "$FREPO" -c user.email=t@t -c user.name=t commit --allow-empty -m init -q
mkdir -p "$FREPO/.claude/sable/roles"
printf 'OPTIMUS STATIC ROLE: dispatch workers.\n' > "$FREPO/.claude/sable/roles/optimus.md"
# Use the SAME invocation the RREPO leg proves works. A bare `sable-mode set
# planning` HANGS here (measured: >5min, blocked with no output, in a fresh temp
# repo with no bead store) — captured separately; do not reintroduce it.
( cd "$FREPO" && SABLE_ORCHESTRATION=1 "$MODE" set execution \
    --break-glass --reason "synthetic authority for wx048 flip-preservation test" \
    >/dev/null 2>&1 ) || fail "wx048 fixture: sable-mode set execution succeeds"
( cd "$FREPO" && bash "$CONTRACT" add "PRIOR DOCTRINE: binding on every incoming lincoln, do this at boot" >/dev/null 2>&1 )
( cd "$FREPO" && bash "$CONTRACT" add "PRIOR DOCTRINE: the integration branch is codex-compatible for this run" >/dev/null 2>&1 )
before="$(cd "$FREPO" && bash "$CONTRACT" show 2>/dev/null | grep -c '^- ')"

# The literal section 1.5 sequence: the seeding call followed by its add calls.
( cd "$FREPO" && bash "$CONTRACT" set "Merges go ONLY through sable-merge-gate." >/dev/null 2>&1 )
( cd "$FREPO" && bash "$CONTRACT" add "Workers self-push their worktree branch; Chuck merges via the gate." >/dev/null 2>&1 )
after="$(cd "$FREPO" && bash "$CONTRACT" show 2>/dev/null | grep -c '^- ')"

if [ "$before" = "2" ]; then pass "flip fixture seeds prior doctrine"; else fail "flip fixture seeds prior doctrine" "before=$before"; fi
if (cd "$FREPO" && bash "$CONTRACT" show 2>/dev/null | grep -q 'binding on every incoming lincoln'); then
  pass "prior doctrine SURVIVES the sable-execute flip choreography (wx048)"
else
  fail "prior doctrine SURVIVES the sable-execute flip choreography (wx048)" "after=$after"
fi
if [ "$after" -ge "$before" ]; then pass "flip never shrinks the surface"; else fail "flip never shrinks the surface" "before=$before after=$after"; fi

# The surviving doctrine must still reach a booting pane.
out="$(cd "$FREPO" && printf '%s' "$SS" | CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager bash "$HOOK" 2>/dev/null)"
if printf '%s' "$out" | grep -q 'binding on every incoming lincoln'; then pass "surviving doctrine still surfaces at boot"; else fail "surviving doctrine still surfaces at boot" "got: ${out:0:300}"; fi

# NEGATIVE CONTROL: the destructive path still exists when chosen deliberately.
( cd "$FREPO" && bash "$CONTRACT" set --force "deliberate reset" >/dev/null 2>&1 )
n="$(cd "$FREPO" && bash "$CONTRACT" show 2>/dev/null | grep -c '^- ')"
if [ "$n" = "1" ]; then pass "set --force still resets the surface (negative control)"; else fail "set --force still resets the surface (negative control)" "count=$n"; fi

rm -rf "$FREPO"

rm -rf "$RREPO"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
