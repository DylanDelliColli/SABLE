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
#
# Central scrub lives in lib-identity-isolation.sh (SABLE-j3bi) so every
# suite shares one definition of "identity vars" instead of drifting copies
# of an unset list.
source "$REPO/hooks/test/lib-identity-isolation.sh"
sable_scrub_identity_env
unset SABLE_MODE_STATE SABLE_ACTIVE_CONTRACTS 2>/dev/null || true

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

# Present corrupt state is not the same as absent state. A restarted manager
# must see that authorization is unavailable instead of silently receiving only
# its historical role card.
printf '%s' 'not-json{' > "$LS_MODE"
out="$(cd "$LS" && printf '%s' "$SS" | SABLE_MODE_STATE="$LS_MODE" CLAUDE_AGENT_NAME=chuck CLAUDE_AGENT_ROLE=manager bash "$HOOK" 2>/dev/null)"
if printf '%s' "$out" | grep -q 'CORRUPT STATE'; then pass "dbq9p.4: corrupt mode state is loud at manager boot"; else fail "dbq9p.4: corrupt mode state is loud at manager boot" "got: ${out:0:300}"; fi
if printf '%s' "$out" | grep -q 'Do not dispatch or execute'; then pass "dbq9p.4: corrupt boot surface fails closed operationally"; else fail "dbq9p.4: corrupt boot surface fails closed operationally" "got: ${out:0:300}"; fi
printf '{"mode":"execution","since":"2026-07-13T09:00:00-0700","fleet":["chuck"]}\n' > "$LS_MODE"

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

# ---------- SABLE-jiqm: PreCompact leg must never emit additionalContext ----------
# PreCompact's hookSpecificOutput schema does not support additionalContext
# (only UserPromptSubmit/PostToolUse/PostToolBatch/Stop do) — Claude Code's hook
# JSON validation rejects it, silently losing the re-anchor on every /compact.
# The hook must no-op on this leg (empty stdout is always schema-valid) and rely
# on the SessionStart:compact leg, which already re-anchors identity correctly.
PC='{"hook_event_name":"PreCompact"}'
out="$(cd "$PROJ" && printf '%s' "$PC" | CLAUDE_AGENT_NAME=cockpit CLAUDE_AGENT_ROLE=manager bash "$HOOK" 2>/dev/null)"
if [ -z "$out" ]; then pass "jiqm: PreCompact leg no-ops (no invalid additionalContext)"; else fail "jiqm: PreCompact leg no-ops (no invalid additionalContext)" "got: ${out:0:200}"; fi
if ! printf '%s' "$out" | grep -q 'additionalContext'; then pass "jiqm: PreCompact stdout never contains additionalContext key"; else fail "jiqm: PreCompact stdout never contains additionalContext key" "got: ${out:0:200}"; fi

# SessionStart (including the post-compaction resume, which still reports
# hook_event_name=SessionStart) must be unaffected by the PreCompact no-op.
out="$(cd "$PROJ" && printf '%s' "$SS" | CLAUDE_AGENT_NAME=cockpit CLAUDE_AGENT_ROLE=manager bash "$HOOK" 2>/dev/null)"
if printf '%s' "$out" | grep -q '"hookEventName": "SessionStart"'; then pass "jiqm: SessionStart leg still emits valid hookEventName+additionalContext"; else fail "jiqm: SessionStart leg still emits valid hookEventName+additionalContext" "got: ${out:0:200}"; fi

# ---------- SABLE-thx70: LOUD ON SHADOWING ----------
# Six days of role-card edits went dark because a stale project-local copy
# silently outranked a freshly-edited user-level one with no event. Precedence
# stays project-first; the fix is that the disagreement is no longer silent.
# The negative controls are load-bearing: without them this would warn on
# every ordinary boot (either shape alone, or two byte-identical copies, is
# the normal, unremarkable case).
SHADOW="$(mktemp -d)"
mkdir -p "$SHADOW/.claude/sable/roles"
SHADOW_HOME="$(mktemp -d)"
mkdir -p "$SHADOW_HOME/.claude/sable/roles"

# both present and DIFFER -> warns, names both paths, precedence unchanged (project wins)
printf 'PROJECT_SHADOW_MARKER\n' > "$SHADOW/.claude/sable/roles/cockpit.md"
printf 'USER_SHADOW_MARKER\n' > "$SHADOW_HOME/.claude/sable/roles/cockpit.md"
out="$(cd "$SHADOW" && printf '%s' "$SS" | HOME="$SHADOW_HOME" CLAUDE_AGENT_NAME=cockpit CLAUDE_AGENT_ROLE=manager bash "$HOOK" 2>/dev/null)"
err="$(cd "$SHADOW" && printf '%s' "$SS" | HOME="$SHADOW_HOME" CLAUDE_AGENT_NAME=cockpit CLAUDE_AGENT_ROLE=manager bash "$HOOK" 2>&1 1>/dev/null)"
if printf '%s' "$out" | grep -q 'PROJECT_SHADOW_MARKER'; then pass "thx70: differing shadow still resolves project-local (precedence unchanged)"; else fail "thx70: differing shadow still resolves project-local (precedence unchanged)" "got: ${out:0:200}"; fi
if printf '%s' "$err" | grep -q 'SABLE-ROLE-CARD-SHADOWED'; then pass "thx70: differing shadow warns with fixed token"; else fail "thx70: differing shadow warns with fixed token" "got: ${err:0:300}"; fi
if printf '%s' "$err" | grep -q "$SHADOW/.claude/sable/roles/cockpit.md" && printf '%s' "$err" | grep -q "$SHADOW_HOME/.claude/sable/roles/cockpit.md"; then pass "thx70: warning names both paths"; else fail "thx70: warning names both paths" "got: ${err:0:400}"; fi

# NEGATIVE CONTROL 1: only project-local present -> no warning
rm -f "$SHADOW_HOME/.claude/sable/roles/cockpit.md"
err="$(cd "$SHADOW" && printf '%s' "$SS" | HOME="$SHADOW_HOME" CLAUDE_AGENT_NAME=cockpit CLAUDE_AGENT_ROLE=manager bash "$HOOK" 2>&1 1>/dev/null)"
if ! printf '%s' "$err" | grep -q 'SABLE-ROLE-CARD-SHADOWED'; then pass "thx70: NEGATIVE CONTROL only-project-local -> no warning"; else fail "thx70: NEGATIVE CONTROL only-project-local -> no warning" "got: ${err:0:200}"; fi

# NEGATIVE CONTROL 2: only user-level present -> no warning
rm -f "$SHADOW/.claude/sable/roles/cockpit.md"
printf 'USER_SHADOW_MARKER\n' > "$SHADOW_HOME/.claude/sable/roles/cockpit.md"
err="$(cd "$SHADOW" && printf '%s' "$SS" | HOME="$SHADOW_HOME" CLAUDE_AGENT_NAME=cockpit CLAUDE_AGENT_ROLE=manager bash "$HOOK" 2>&1 1>/dev/null)"
if ! printf '%s' "$err" | grep -q 'SABLE-ROLE-CARD-SHADOWED'; then pass "thx70: NEGATIVE CONTROL only-user-level -> no warning"; else fail "thx70: NEGATIVE CONTROL only-user-level -> no warning" "got: ${err:0:200}"; fi

# NEGATIVE CONTROL 3: both present and IDENTICAL -> no warning
printf 'USER_SHADOW_MARKER\n' > "$SHADOW/.claude/sable/roles/cockpit.md"
err="$(cd "$SHADOW" && printf '%s' "$SS" | HOME="$SHADOW_HOME" CLAUDE_AGENT_NAME=cockpit CLAUDE_AGENT_ROLE=manager bash "$HOOK" 2>&1 1>/dev/null)"
if ! printf '%s' "$err" | grep -q 'SABLE-ROLE-CARD-SHADOWED'; then pass "thx70: NEGATIVE CONTROL identical content -> no warning"; else fail "thx70: NEGATIVE CONTROL identical content -> no warning" "got: ${err:0:200}"; fi

rm -rf "$SHADOW" "$SHADOW_HOME"

# ============================================================================
# SABLE-slip0.1 — BOOT EPOCH
# ============================================================================
# A pane whose agent session was CLEARED is indistinguishable from one that is
# working: sable-spawn-manager decides "already running" from the tool-written
# @sable_role tag (bin/sable-spawn-manager:283), which a cleared-but-alive pane
# still carries. Measured 2026-07-30 — all three manager panes had been
# context-cleared, spawn-manager reported "already running - skipping" for all
# three and spawned nothing, and lincoln had to hand-kick each over
# sable-msg.
#
# The hook stamps an AGENT-WRITTEN boot epoch on every SessionStart, turning
# "the session restarted" from an inference into an event. These cases pin the
# three properties that make it trustworthy: it fires on SessionStart and NOT
# on a compaction, it carries the SABLE_-first identity, and its write path
# reads NOTHING off the screen.
# ----------------------------------------------------------------------------

EPOCH_SHIM="$(mktemp -d)"
EPOCH_HOME="$(mktemp -d)"
mkdir -p "$EPOCH_HOME/.claude/sable/roles"
printf 'EPOCH_CHUCK_ROLE_MARKER\n' > "$EPOCH_HOME/.claude/sable/roles/chuck.md"
printf 'EPOCH_OPTIMUS_ROLE_MARKER\n' > "$EPOCH_HOME/.claude/sable/roles/optimus.md"
printf 'EPOCH_LINCOLN_ROLE_MARKER\n' > "$EPOCH_HOME/.claude/sable/roles/lincoln.md"
EPOCH_ALL_LOG="$EPOCH_SHIM/all-tmux-calls.log"
: > "$EPOCH_ALL_LOG"

# A stand-in `tmux` that records argv (one call per line, TAB-separated) and
# succeeds. This is what makes the PROPERTY INVARIANT below assertable: the
# question "what does the epoch path never call?" has no answer against a real
# tmux, only against a recorder.
cat > "$EPOCH_SHIM/tmux" <<'EPOCH_TMUX_SHIM'
#!/usr/bin/env bash
printf '%s\t' "$@" >> "$SABLE_TEST_TMUX_LOG"
printf '\n'        >> "$SABLE_TEST_TMUX_LOG"
if [ -n "${SABLE_TEST_TMUX_ALL_LOG:-}" ]; then
  printf '%s\t' "$@" >> "$SABLE_TEST_TMUX_ALL_LOG"
  printf '\n'        >> "$SABLE_TEST_TMUX_ALL_LOG"
fi
# SABLE_TEST_TMUX_FAIL makes the shim refuse the write, so the suite can drive
# the hook's stamp-failure branch (a real tmux has no convenient way to fail a
# set-option on demand).
#
# SABLE_TEST_TMUX_FAIL_FIRST names a marker file and refuses only the FIRST call
# of a run, then succeeds. A whole-run switch cannot reach the one failure mode
# that matters here (SABLE-rvbg0): the AGENT-TAG write failing while the epoch
# write would still have succeeded. That is the shape in which a suppressed
# failure publishes a fresh epoch beside a stale identity.
if [ -n "${SABLE_TEST_TMUX_FAIL_FIRST:-}" ] && [ ! -e "$SABLE_TEST_TMUX_FAIL_FIRST" ]; then
  : > "$SABLE_TEST_TMUX_FAIL_FIRST"
  exit 1
fi
[ -n "${SABLE_TEST_TMUX_FAIL:-}" ] && exit 1
exit 0
EPOCH_TMUX_SHIM
chmod +x "$EPOCH_SHIM/tmux"

EPOCH_LOG=""; EPOCH_ERR=""
EPOCH_N=0
# epoch_run <payload> [VAR=VAL ...] — invoke the hook with the recorder on PATH
# and TMUX_PANE preset, leaving this call's tmux argv in $EPOCH_LOG and its
# stderr in $EPOCH_ERR. Identity is passed INLINE per invocation (never
# exported) so the suite's own scrubbed environment stays clean.
epoch_run() {
  local payload="$1"; shift
  EPOCH_N=$((EPOCH_N+1))
  EPOCH_LOG="$EPOCH_SHIM/calls.$EPOCH_N.log"; : > "$EPOCH_LOG"
  EPOCH_ERR="$EPOCH_SHIM/err.$EPOCH_N.log";   : > "$EPOCH_ERR"
  ( cd "$EPOCH_HOME" && printf '%s' "$payload" | env \
      PATH="$EPOCH_SHIM:$PATH" \
      SABLE_TEST_TMUX_LOG="$EPOCH_LOG" \
      SABLE_TEST_TMUX_ALL_LOG="$EPOCH_ALL_LOG" \
      TMUX_PANE="%99" HOME="$EPOCH_HOME" \
      "$@" bash "$HOOK" >/dev/null 2>"$EPOCH_ERR" )
  return 0
}
# epoch_opt <option-name> — the value the last epoch_run wrote for that pane
# option, or empty if it never wrote it.
epoch_opt() {
  grep -F "	$1	" "$EPOCH_LOG" 2>/dev/null | tail -1 | awk -F'\t' '{print $(NF-1)}'
}

# ---------- SessionStart stamps an epoch observable outside the pane ----------
epoch_run "$SS" SABLE_AGENT_NAME=chuck SABLE_AGENT_ROLE=manager
e1="$(epoch_opt @sable_boot_epoch)"
if [ -n "$e1" ]; then pass "slip0.1: SessionStart stamps @sable_boot_epoch"; else fail "slip0.1: SessionStart stamps @sable_boot_epoch" "no set-option recorded; calls: $(cat "$EPOCH_LOG")"; fi
if printf '%s' "$e1" | grep -Eq '^[0-9]+-[0-9a-f]+$'; then pass "slip0.1: epoch has the documented <unix-seconds>-<hex> shape"; else fail "slip0.1: epoch has the documented <unix-seconds>-<hex> shape" "got: '$e1'"; fi
# A pane option is only a liveness signal if it is READABLE FROM OUTSIDE, which
# means it must be written with an explicit -t naming this pane. The bare
# `set-option -p` form resolves against the client's ACTIVE pane instead and
# lands the stamp on whichever pane holds operator focus (SABLE-5v9n).
if grep -F '	-p	' "$EPOCH_LOG" | grep -q '	-t		*%99	'; then pass "slip0.1: stamp targets its OWN pane explicitly (-p -t \$TMUX_PANE)"; else fail "slip0.1: stamp targets its OWN pane explicitly (-p -t \$TMUX_PANE)" "calls: $(cat "$EPOCH_LOG")"; fi

# ---------- the epoch CHANGES across a restart ----------
# Two boots can land in the same second, so a second-resolution timestamp alone
# would silently fail to change — which is the one thing the signal exists to do.
epoch_run "$SS" SABLE_AGENT_NAME=chuck SABLE_AGENT_ROLE=manager
e2="$(epoch_opt @sable_boot_epoch)"
if [ -n "$e2" ] && [ "$e2" != "$e1" ]; then pass "slip0.1: a second SessionStart yields a DIFFERENT epoch"; else fail "slip0.1: a second SessionStart yields a DIFFERENT epoch" "first='$e1' second='$e2'"; fi

# ---------- NAMED HAZARD: a compaction is NOT a restart ----------
# If the stamp rode the existing additionalContext emit it would also fire on
# PreCompact, every /compact would read as a clear, and Lincoln would re-kick
# healthy managers in a loop.
epoch_run "$PC" SABLE_AGENT_NAME=chuck SABLE_AGENT_ROLE=manager
if [ -z "$(epoch_opt @sable_boot_epoch)" ]; then pass "slip0.1: NAMED HAZARD — PreCompact stamps NO epoch"; else fail "slip0.1: NAMED HAZARD — PreCompact stamps NO epoch" "got: $(epoch_opt @sable_boot_epoch)"; fi
if [ -z "$(epoch_opt @sable_boot_agent)" ]; then pass "slip0.1: PreCompact writes no pane option at all"; else fail "slip0.1: PreCompact writes no pane option at all" "calls: $(cat "$EPOCH_LOG")"; fi

# ---------- D13: identity resolves SABLE_AGENT_NAME-first ----------
# CLAUDE_AGENT_NAME reads 'lincoln' on all three Codex manager panes, so an
# epoch labelled from the Claude-side name would mislabel every manager in the
# fleet as the cockpit.
epoch_run "$SS" SABLE_AGENT_NAME=optimus SABLE_AGENT_ROLE=manager CLAUDE_AGENT_NAME=lincoln CLAUDE_AGENT_ROLE=manager
if [ "$(epoch_opt @sable_boot_agent)" = "optimus" ]; then pass "slip0.1: D13 — conflicting SABLE_/CLAUDE_ names resolve SABLE_-first"; else fail "slip0.1: D13 — conflicting SABLE_/CLAUDE_ names resolve SABLE_-first" "got: '$(epoch_opt @sable_boot_agent)' (expected optimus, not lincoln)"; fi
# Not vacuous: with only the Claude-side name set, THAT is the one recorded —
# so the case above is testing precedence, not a hardcoded answer.
epoch_run "$SS" CLAUDE_AGENT_NAME=lincoln CLAUDE_AGENT_ROLE=manager
if [ "$(epoch_opt @sable_boot_agent)" = "lincoln" ]; then pass "slip0.1: D13 CONTROL — CLAUDE_ name is still used when SABLE_ is absent"; else fail "slip0.1: D13 CONTROL — CLAUDE_ name is still used when SABLE_ is absent" "got: '$(epoch_opt @sable_boot_agent)'"; fi

# ---------- ordering: identity is committed BY the epoch ----------
# @sable_boot_agent must be written BEFORE @sable_boot_epoch, so a consumer
# that sees a fresh epoch can never pair it with the previous session's name.
epoch_run "$SS" SABLE_AGENT_NAME=chuck SABLE_AGENT_ROLE=manager
agent_line="$(grep -n -F '	@sable_boot_agent	' "$EPOCH_LOG" | head -1 | cut -d: -f1)"
epoch_line="$(grep -n -F '	@sable_boot_epoch	' "$EPOCH_LOG" | head -1 | cut -d: -f1)"
if [ -n "$agent_line" ] && [ -n "$epoch_line" ] && [ "$agent_line" -lt "$epoch_line" ]; then pass "slip0.1: @sable_boot_agent is written before @sable_boot_epoch (epoch commits the pair)"; else fail "slip0.1: @sable_boot_agent is written before @sable_boot_epoch (epoch commits the pair)" "agent@$agent_line epoch@$epoch_line"; fi

# ---------- a missing role card must NOT suppress the stamp ----------
# The pane booted whether or not its role file resolved. If a missing card
# skipped the stamp, that pane would read as never-booted forever and the
# consumer would re-kick it on every pass — the kick loop, arrived at from the
# other direction.
NOROLE="$(mktemp -d)"
EPOCH_N=$((EPOCH_N+1)); EPOCH_LOG="$EPOCH_SHIM/calls.$EPOCH_N.log"; : > "$EPOCH_LOG"
( cd "$NOROLE" && printf '%s' "$SS" | env PATH="$EPOCH_SHIM:$PATH" \
    SABLE_TEST_TMUX_LOG="$EPOCH_LOG" SABLE_TEST_TMUX_ALL_LOG="$EPOCH_ALL_LOG" \
    TMUX_PANE="%99" HOME="$NOROLE" \
    SABLE_AGENT_NAME=chuck SABLE_AGENT_ROLE=manager bash "$HOOK" >/dev/null 2>&1 )
if [ -n "$(epoch_opt @sable_boot_epoch)" ]; then pass "slip0.1: stamps even when no role card resolves (no kick loop)"; else fail "slip0.1: stamps even when no role card resolves (no kick loop)" "calls: $(cat "$EPOCH_LOG")"; fi
rm -rf "$NOROLE"

# ---------- an unidentifiable event fails CLOSED, and says so ----------
# Both providers register this hook on SessionStart AND PreCompact, and the
# event is only knowable from the stdin payload. A payload that does not name
# one cannot be told apart from a compaction, so it must not stamp — and it
# must not do that silently either, because a permanently unstamped pane is the
# same false signal in the other direction.
epoch_run '{}' SABLE_AGENT_NAME=chuck SABLE_AGENT_ROLE=manager
if [ -z "$(epoch_opt @sable_boot_epoch)" ]; then pass "slip0.1: payload with no hook_event_name stamps nothing (fails closed)"; else fail "slip0.1: payload with no hook_event_name stamps nothing (fails closed)" "got: $(epoch_opt @sable_boot_epoch)"; fi
if grep -q 'SABLE-BOOT-EPOCH-EVENT-UNKNOWN' "$EPOCH_ERR"; then pass "slip0.1: unidentifiable event is LOUD (fixed token on stderr)"; else fail "slip0.1: unidentifiable event is LOUD (fixed token on stderr)" "stderr: $(cat "$EPOCH_ERR")"; fi
# ... and the identity injection is UNAFFECTED by that fail-closed choice: an
# unanchored manager is worse than an ignored additionalContext payload, so the
# two consumers of the same ambiguity deliberately default in opposite
# directions.
epoch_run '{}' SABLE_AGENT_NAME=chuck SABLE_AGENT_ROLE=manager
out="$(cd "$EPOCH_HOME" && printf '%s' '{}' | env PATH="$EPOCH_SHIM:$PATH" SABLE_TEST_TMUX_LOG="$EPOCH_SHIM/ignore.log" TMUX_PANE="%99" HOME="$EPOCH_HOME" SABLE_AGENT_NAME=chuck SABLE_AGENT_ROLE=manager bash "$HOOK" 2>/dev/null)"
if printf '%s' "$out" | grep -q 'EPOCH_CHUCK_ROLE_MARKER'; then pass "slip0.1: fail-closed stamp does NOT suppress identity injection"; else fail "slip0.1: fail-closed stamp does NOT suppress identity injection" "got: ${out:0:200}"; fi
# NEGATIVE CONTROL for the loud token: a well-formed SessionStart must stay
# silent, or the warning is noise on every boot and gets suppressed.
epoch_run "$SS" SABLE_AGENT_NAME=chuck SABLE_AGENT_ROLE=manager
if ! grep -q 'SABLE-BOOT-EPOCH-EVENT-UNKNOWN' "$EPOCH_ERR"; then pass "slip0.1: NEGATIVE CONTROL — a named SessionStart warns about nothing"; else fail "slip0.1: NEGATIVE CONTROL — a named SessionStart warns about nothing" "stderr: $(cat "$EPOCH_ERR")"; fi

# ---------- a failed stamp is loud, and never breaks the boot ----------
# Instrumentation that can take the session down with it is worse than no
# instrumentation. But swallowing the failure silently leaves the pane reading
# as never-booted downstream, so this branch has to do both things at once.
EPOCH_N=$((EPOCH_N+1)); EPOCH_LOG="$EPOCH_SHIM/calls.$EPOCH_N.log"; : > "$EPOCH_LOG"
EPOCH_ERR="$EPOCH_SHIM/err.$EPOCH_N.log"; : > "$EPOCH_ERR"
out="$(cd "$EPOCH_HOME" && printf '%s' "$SS" | env PATH="$EPOCH_SHIM:$PATH" \
    SABLE_TEST_TMUX_LOG="$EPOCH_LOG" SABLE_TEST_TMUX_FAIL=1 \
    TMUX_PANE="%99" HOME="$EPOCH_HOME" \
    SABLE_AGENT_NAME=chuck SABLE_AGENT_ROLE=manager bash "$HOOK" 2>"$EPOCH_ERR")"
rc=$?
if [ "$rc" -eq 0 ]; then pass "slip0.1: a failed stamp does not fail the hook (boot survives instrumentation)"; else fail "slip0.1: a failed stamp does not fail the hook (boot survives instrumentation)" "exit=$rc"; fi
if printf '%s' "$out" | grep -q 'EPOCH_CHUCK_ROLE_MARKER'; then pass "slip0.1: a failed stamp still injects identity"; else fail "slip0.1: a failed stamp still injects identity" "got: ${out:0:200}"; fi
if grep -q 'SABLE-BOOT-EPOCH-STAMP-FAILED' "$EPOCH_ERR"; then pass "slip0.1: a failed stamp is LOUD (fixed token on stderr)"; else fail "slip0.1: a failed stamp is LOUD (fixed token on stderr)" "stderr: $(cat "$EPOCH_ERR")"; fi

# ---------- SABLE-rvbg0: the agent tag is a TRANSACTIONAL PREREQUISITE ----------
# Writing the identity first only makes the pair same-generation if a published
# epoch IMPLIES the tag beside it was published too. Suppressing the agent-tag
# failure and continuing broke exactly that implication: the tag write could
# fail, be swallowed, and a fresh epoch still publish — pairing a NEW epoch with
# the PREVIOUS session's identity, which is the one outcome the ordering existed
# to make impossible. A whole-run tmux failure can never catch this, because it
# fails both writes and the epoch is absent for the wrong reason; the failure has
# to be first-write-ONLY, so the epoch write would have succeeded had it been
# attempted. On that failure the contract is: attempt no epoch, say so loudly,
# and let the boot proceed anyway.
EPOCH_N=$((EPOCH_N+1)); EPOCH_LOG="$EPOCH_SHIM/calls.$EPOCH_N.log"; : > "$EPOCH_LOG"
EPOCH_ERR="$EPOCH_SHIM/err.$EPOCH_N.log"; : > "$EPOCH_ERR"
FAIL_FIRST_MARK="$EPOCH_SHIM/fail-first.$EPOCH_N"
rm -f "$FAIL_FIRST_MARK"
out="$(cd "$EPOCH_HOME" && printf '%s' "$SS" | env PATH="$EPOCH_SHIM:$PATH" \
    SABLE_TEST_TMUX_LOG="$EPOCH_LOG" SABLE_TEST_TMUX_ALL_LOG="$EPOCH_ALL_LOG" \
    SABLE_TEST_TMUX_FAIL_FIRST="$FAIL_FIRST_MARK" \
    TMUX_PANE="%99" HOME="$EPOCH_HOME" \
    SABLE_AGENT_NAME=chuck SABLE_AGENT_ROLE=manager bash "$HOOK" 2>"$EPOCH_ERR")"
rc=$?
# PRECONDITION: the shim refused the AGENT-TAG write specifically. Without this
# the arm could pass vacuously against a hook that made no tmux calls at all.
if head -1 "$EPOCH_LOG" 2>/dev/null | grep -qF '	@sable_boot_agent	'; then pass "rvbg0: PRECONDITION — the refused first call IS the agent-tag write"; else fail "rvbg0: PRECONDITION — the refused first call IS the agent-tag write" "calls: $(cat "$EPOCH_LOG")"; fi
if [ -z "$(epoch_opt @sable_boot_epoch)" ]; then pass "rvbg0: a failed agent tag publishes NO epoch"; else fail "rvbg0: a failed agent tag publishes NO epoch" "got: $(epoch_opt @sable_boot_epoch)"; fi
# Not merely unpublished — never ATTEMPTED. A second set-option here would mean
# the epoch's absence depended on tmux refusing it, not on the hook's own gate.
if ! grep -qF '	@sable_boot_epoch	' "$EPOCH_LOG"; then pass "rvbg0: the epoch write is not even ATTEMPTED after the agent tag fails"; else fail "rvbg0: the epoch write is not even ATTEMPTED after the agent tag fails" "calls: $(cat "$EPOCH_LOG")"; fi
if [ "$(wc -l < "$EPOCH_LOG")" -eq 1 ]; then pass "rvbg0: exactly one tmux call — the stamp stops at the failed prerequisite"; else fail "rvbg0: exactly one tmux call — the stamp stops at the failed prerequisite" "calls: $(cat "$EPOCH_LOG")"; fi
if grep -q 'SABLE-BOOT-EPOCH-STAMP-FAILED' "$EPOCH_ERR"; then pass "rvbg0: a failed agent tag is LOUD (same fixed token)"; else fail "rvbg0: a failed agent tag is LOUD (same fixed token)" "stderr: $(cat "$EPOCH_ERR")"; fi
if [ "$rc" -eq 0 ]; then pass "rvbg0: a failed agent tag does not fail the hook"; else fail "rvbg0: a failed agent tag does not fail the hook" "exit=$rc"; fi
if printf '%s' "$out" | grep -q 'EPOCH_CHUCK_ROLE_MARKER'; then pass "rvbg0: a failed agent tag still injects identity (boot proceeds)"; else fail "rvbg0: a failed agent tag still injects identity (boot proceeds)" "got: ${out:0:200}"; fi
# NEGATIVE CONTROL for the shim itself: the marker is consumed by the first call,
# so a SECOND run under the same env stamps normally. This proves the arm above
# measured a first-write refusal and not a permanently broken tmux.
EPOCH_N=$((EPOCH_N+1)); EPOCH_LOG="$EPOCH_SHIM/calls.$EPOCH_N.log"; : > "$EPOCH_LOG"
( cd "$EPOCH_HOME" && printf '%s' "$SS" | env PATH="$EPOCH_SHIM:$PATH" \
    SABLE_TEST_TMUX_LOG="$EPOCH_LOG" SABLE_TEST_TMUX_ALL_LOG="$EPOCH_ALL_LOG" \
    SABLE_TEST_TMUX_FAIL_FIRST="$FAIL_FIRST_MARK" \
    TMUX_PANE="%99" HOME="$EPOCH_HOME" \
    SABLE_AGENT_NAME=chuck SABLE_AGENT_ROLE=manager bash "$HOOK" >/dev/null 2>&1 )
if [ -n "$(epoch_opt @sable_boot_epoch)" ]; then pass "rvbg0: NEGATIVE CONTROL — with the agent tag succeeding, the epoch publishes"; else fail "rvbg0: NEGATIVE CONTROL — with the agent tag succeeding, the epoch publishes" "calls: $(cat "$EPOCH_LOG")"; fi

# ---------- gates that precede the stamp ----------
epoch_run "$SS" SABLE_WORKER_PANE=1 SABLE_AGENT_NAME=chuck SABLE_AGENT_ROLE=manager
if [ ! -s "$EPOCH_LOG" ]; then pass "slip0.1: worker pane stamps nothing (SABLE-38zi gate precedes)"; else fail "slip0.1: worker pane stamps nothing (SABLE-38zi gate precedes)" "calls: $(cat "$EPOCH_LOG")"; fi
epoch_run "$SS" SABLE_AGENT_NAME=chuck SABLE_AGENT_ROLE=auditor
if [ ! -s "$EPOCH_LOG" ]; then pass "slip0.1: non-manager session stamps nothing"; else fail "slip0.1: non-manager session stamps nothing" "calls: $(cat "$EPOCH_LOG")"; fi
# Outside tmux there is no pane to stamp and nobody to read it — silent no-op,
# not an error, and above all not a tmux call.
EPOCH_N=$((EPOCH_N+1)); EPOCH_LOG="$EPOCH_SHIM/calls.$EPOCH_N.log"; : > "$EPOCH_LOG"
( cd "$EPOCH_HOME" && printf '%s' "$SS" | env -u TMUX_PANE PATH="$EPOCH_SHIM:$PATH" \
    SABLE_TEST_TMUX_LOG="$EPOCH_LOG" SABLE_TEST_TMUX_ALL_LOG="$EPOCH_ALL_LOG" \
    HOME="$EPOCH_HOME" SABLE_AGENT_NAME=chuck SABLE_AGENT_ROLE=manager bash "$HOOK" >/dev/null 2>&1 )
if [ ! -s "$EPOCH_LOG" ]; then pass "slip0.1: no TMUX_PANE → no tmux call at all"; else fail "slip0.1: no TMUX_PANE → no tmux call at all" "calls: $(cat "$EPOCH_LOG")"; fi

# ---------- PROPERTY INVARIANT: the epoch path reads NOTHING off the screen ----------
# Three candidate liveness discriminators were refuted BY MEASUREMENT, and the
# most seductive of them — already_recycled's boot-banner regex over a
# capture-pane scrollback — was refuted for three independent reasons at once
# (a Claude-only banner regex, a "nothing above the banner" rule a BOXED Codex
# banner can never satisfy, and the measured fact that a Codex restart does not
# clear tmux scrollback). One invariant over every call this suite made retires
# that whole family, including the ones nobody has thought of yet, instead of
# one negative control per bad idea.
if [ -s "$EPOCH_ALL_LOG" ]; then pass "slip0.1: INVARIANT PRECONDITION — the recorder captured real calls (not vacuous)"; else fail "slip0.1: INVARIANT PRECONDITION — the recorder captured real calls (not vacuous)" "no tmux calls recorded at all"; fi
if ! grep -q 'capture-pane' "$EPOCH_ALL_LOG"; then pass "slip0.1: PROPERTY INVARIANT — the epoch path issues ZERO capture-pane calls"; else fail "slip0.1: PROPERTY INVARIANT — the epoch path issues ZERO capture-pane calls" "$(grep 'capture-pane' "$EPOCH_ALL_LOG" | head -3)"; fi
# The generalization the invariant is really making: the write path issues
# set-option and nothing else, so no screen-reading subcommand (capture-pane,
# display-message, list-panes, show-options) can creep in later either.
bad_subcmds="$(awk -F'\t' '$1 != "set-option" {print $1}' "$EPOCH_ALL_LOG" | sort -u)"
if [ -z "$bad_subcmds" ]; then pass "slip0.1: PROPERTY INVARIANT — every tmux call is set-option (no read path exists)"; else fail "slip0.1: PROPERTY INVARIANT — every tmux call is set-option (no read path exists)" "also called: $bad_subcmds"; fi

# ---------- INTEGRATION: a real tmux pane, read from OUTSIDE ----------
# Not a skip-if-absent leg. This repo is tmux-native and ci-verify installs
# tmux explicitly and hard-errors without it; a suite that printed SKIP here
# would count a vacuous run as green, which is the exact failure mode
# .github/ci/shell-run-set.sh exists to prevent.
if ! command -v tmux >/dev/null 2>&1; then
  fail "slip0.1: INTEGRATION requires tmux" "tmux is not on PATH; this suite gates a tmux-native liveness signal and will not report green without exercising it"
else
  EPOCH_SOCK="sra-epoch-$$"
  epoch_tmux() { tmux -L "$EPOCH_SOCK" "$@"; }
  epoch_cleanup() { tmux -L "$EPOCH_SOCK" kill-server >/dev/null 2>&1 || true; }
  trap epoch_cleanup EXIT

  # sable_scrub_identity_env already ran at the top of this file (line ~34),
  # which is what lets sable_tmux_spawn permit this call: the pane must NOT
  # inherit this process's real agent identity, or the hook under test would
  # boot as whoever is running the suite.
  sable_tmux_spawn -L "$EPOCH_SOCK" new-session -d -s e -x 200 -y 50 'bash --noprofile --norc'
  sleep 0.3
  EPANE="$(epoch_tmux list-panes -t e -F '#{pane_id}' | sed -n 1p)"

  # NEGATIVE CONTROL (proves D2): give the pane EVERY tool-written tag that
  # sable-spawn-manager stamps. Those survive agent death, process death and
  # pane death — there is a pane on this host carrying @sable_role=lincoln with
  # pane_dead=1 — so a signal drawn from them cannot report that the agent is
  # gone. No agent SessionStart has run here, so there must be no epoch.
  epoch_tmux set-option -p -t "$EPANE" @sable_role chuck
  epoch_tmux set-option -p -t "$EPANE" @sable_provider codex
  epoch_tmux set-option -p -t "$EPANE" @sable_class manager
  epoch_tmux set-option -p -t "$EPANE" @sable_repo "$REPO"
  epoch_tmux set-option -p -t "$EPANE" @sable_status running

  read_pane_opt() { epoch_tmux display-message -p -t "$EPANE" "#{$1}" 2>/dev/null || true; }

  if [ -z "$(read_pane_opt @sable_boot_epoch)" ]; then pass "slip0.1: NEGATIVE CONTROL — every tool-written tag present, still NO epoch (D2)"; else fail "slip0.1: NEGATIVE CONTROL — every tool-written tag present, still NO epoch (D2)" "got: $(read_pane_opt @sable_boot_epoch)"; fi
  if [ "$(read_pane_opt @sable_role)" = "chuck" ]; then pass "slip0.1: NEGATIVE CONTROL PRECONDITION — the tool-written tags really are set"; else fail "slip0.1: NEGATIVE CONTROL PRECONDITION — the tool-written tags really are set" "@sable_role='$(read_pane_opt @sable_role)'"; fi

  # Boot the hook INSIDE the pane, as the agent's own SessionStart would. The
  # pane supplies the real TMUX/TMUX_PANE, and every assertion below reads the
  # result from OUTSIDE the pane — which is the whole claim.
  EPOCH_SENT="$EPOCH_SHIM/pane-done"
  boot_in_pane() {
    local event="$1" i=0
    rm -f "$EPOCH_SENT"
    epoch_tmux send-keys -t "$EPANE" "cd '$EPOCH_HOME' && printf '%s' '{\"hook_event_name\":\"$event\"}' | SABLE_AGENT_NAME=chuck SABLE_AGENT_ROLE=manager CLAUDE_AGENT_NAME=lincoln HOME='$EPOCH_HOME' bash '$HOOK' >/dev/null 2>&1; touch '$EPOCH_SENT'" Enter
    while [ $i -lt 150 ] && [ ! -e "$EPOCH_SENT" ]; do sleep 0.1; i=$((i+1)); done
    [ -e "$EPOCH_SENT" ]
  }

  if boot_in_pane SessionStart; then pass "slip0.1: INTEGRATION — hook ran to completion inside a real pane"; else fail "slip0.1: INTEGRATION — hook ran to completion inside a real pane" "sentinel never appeared within 15s"; fi
  ie1="$(read_pane_opt @sable_boot_epoch)"
  if [ -n "$ie1" ]; then pass "slip0.1: INTEGRATION — epoch written from inside the pane is readable from OUTSIDE it"; else fail "slip0.1: INTEGRATION — epoch written from inside the pane is readable from OUTSIDE it" "option unset after boot"; fi
  if [ "$(read_pane_opt @sable_boot_agent)" = "chuck" ]; then pass "slip0.1: INTEGRATION — D13 holds in a real pane (SABLE_=chuck beats CLAUDE_=lincoln)"; else fail "slip0.1: INTEGRATION — D13 holds in a real pane (SABLE_=chuck beats CLAUDE_=lincoln)" "got: '$(read_pane_opt @sable_boot_agent)'"; fi
  if [ "$(read_pane_opt @sable_role)" = "chuck" ]; then pass "slip0.1: INTEGRATION — stamping leaves the tool-written tags intact"; else fail "slip0.1: INTEGRATION — stamping leaves the tool-written tags intact" "@sable_role='$(read_pane_opt @sable_role)'"; fi

  # SIMULATED RESTART: the same pane, same process, a fresh SessionStart —
  # which is exactly what a /clear produces (it resets the session INSIDE a
  # still-running process; the cleared managers' pids predated their clears by
  # ~28h, which is why /proc can only ever catch a crash).
  boot_in_pane SessionStart >/dev/null
  ie2="$(read_pane_opt @sable_boot_epoch)"
  if [ -n "$ie2" ] && [ "$ie2" != "$ie1" ]; then pass "slip0.1: INTEGRATION — a restart CHANGES the epoch"; else fail "slip0.1: INTEGRATION — a restart CHANGES the epoch" "before='$ie1' after='$ie2'"; fi

  # ...and the compaction leg leaves it exactly where it was.
  boot_in_pane PreCompact >/dev/null
  ie3="$(read_pane_opt @sable_boot_epoch)"
  if [ "$ie3" = "$ie2" ]; then pass "slip0.1: INTEGRATION — a compaction does NOT change the epoch"; else fail "slip0.1: INTEGRATION — a compaction does NOT change the epoch" "before='$ie2' after='$ie3'"; fi

  epoch_cleanup
  trap - EXIT
fi

rm -rf "$EPOCH_SHIM" "$EPOCH_HOME"

rm -rf "$PROJ" "$HOMETMP" "$NOPROJ" "$LS" "$LS2" "$LS3"
echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
