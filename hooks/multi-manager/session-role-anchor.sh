#!/usr/bin/env bash
# session-role-anchor.sh — Inject role identity from registry on SessionStart/PreCompact
# Trigger: SessionStart, PreCompact | Timeout: 3000ms
#
# Reads $CLAUDE_AGENT_NAME, locates the role file at ~/.claude/sable/roles/<name>.md,
# and injects its contents as additionalContext. Anchors identity at session start
# and re-anchors after compaction via the SessionStart:compact leg (identity
# erodes silently otherwise). The PreCompact trigger itself is a no-op — its
# hookSpecificOutput schema doesn't support additionalContext (SABLE-jiqm).
#
# ALSO stamps the pane's BOOT EPOCH (SABLE-slip0.1), which is what makes a
# cleared-but-alive manager pane distinguishable from a working one. Two
# agent-written pane options, readable from outside the pane with
# `tmux display-message -p -t <pane> '#{@sable_boot_epoch}'`:
#
#   @sable_boot_epoch   <unix-seconds>-<12 hex>  — changes on EVERY agent
#                       SessionStart (including post-/clear), never on a
#                       compaction. A consumer that sees a different value than
#                       it last observed knows the session restarted.
#   @sable_boot_agent   the resolved agent name (SABLE_AGENT_NAME-first, D13),
#                       written BEFORE the epoch AND a hard prerequisite for it
#                       (SABLE-rvbg0): if this write fails, no epoch is
#                       attempted, so a published epoch always commits a
#                       same-generation pair rather than merely following one.
#
# ABSENCE OF THE EPOCH IS NOT "WORKING" AND NOT "CLEARED" — it is UNKNOWN, and
# consumers must render it that way. A pane predating this change, a manager
# outside tmux, and a provider that dispatches the hook with no stdin payload
# all present no epoch. See the stamp block below for the full contract.
#
# Fast-exits if env var is unset (non-manager sessions unaffected).

set -euo pipefail

# SABLE-38zi: a WORKER pane carries the lane manager's CLAUDE_AGENT_NAME +
# manager role so its push fires the manager-gated for-chuck handoff — but it
# must NEVER load that manager's role-card. A worker that boots as its manager
# runs the manager operating loop and re-dispatches its own bead (duplicate
# pane, defeats SABLE_MAX_WORKERS). sable-spawn-worker stamps SABLE_WORKER_PANE=1
# on every worker pane precisely to distinguish it from a real manager pane that
# shares the same identity env; stand down unconditionally when it is set.
[ -n "${SABLE_WORKER_PANE:-}" ] && exit 0

# Manager identity must be set explicitly via launch alias. Provider-neutral
# names win; Claude aliases keep existing installations compatible.
ROLE_NAME="${SABLE_AGENT_NAME:-${CLAUDE_AGENT_NAME:-}}"
ROLE_KIND="${SABLE_AGENT_ROLE:-${CLAUDE_AGENT_ROLE:-}}"
[ -z "$ROLE_NAME" ] && exit 0
[ "$ROLE_KIND" != "manager" ] && exit 0
export ROLE_NAME

# --- Hook event resolution: read stdin ONCE (SABLE-slip0.1) ------------------
# The event used to be parsed inside the final python from ITS stdin. The
# boot-epoch stamp below has to know the event BEFORE role resolution, so stdin
# is read here, once, and the resolved event is handed to that python through
# the environment instead. A pipe cannot be read twice — whichever reader goes
# first consumes it — so this move is not optional, it is the only ordering
# that lets both consumers see the event.
#
# Deliberately placed AFTER the identity/manager gates: a non-manager session
# must still fast-exit without touching stdin at all.
#
# stdin discipline is copied from lib-hook-trace.sh's sable_trace_read_stdin
# (not sourced — that lib also writes a trace log, which is not this hook's
# business): an interactive fd is treated as "no payload" rather than blocked
# on, and a real read is bounded, so a provider that dispatches this hook with
# no stdin can never burn the 3000ms budget waiting on EOF.
HOOK_INPUT=""
if [ ! -t 0 ]; then
    if command -v timeout >/dev/null 2>&1; then
        HOOK_INPUT="$(timeout "${SABLE_ROLE_ANCHOR_STDIN_TIMEOUT:-2}" cat 2>/dev/null)" || true
    else
        HOOK_INPUT="$(cat 2>/dev/null)" || true
    fi
fi

# UNKNOWN is a THIRD value, not a synonym for SessionStart. The two consumers
# below want opposite defaults for it and each gets its own, which is only
# expressible if the ambiguity survives parsing.
HOOK_EVENT="$(HOOK_INPUT="$HOOK_INPUT" python3 -c '
import json, os
try:
    d = json.loads(os.environ.get("HOOK_INPUT", ""))
    ev = d.get("hook_event_name") if isinstance(d, dict) else None
except Exception:
    ev = None
print(ev if isinstance(ev, str) and ev else "__SABLE_EVENT_UNKNOWN__")
' 2>/dev/null || true)"
[ -z "$HOOK_EVENT" ] && HOOK_EVENT="__SABLE_EVENT_UNKNOWN__"

# --- Boot epoch stamp (SABLE-slip0.1) ---------------------------------------
# A pane whose agent session was CLEARED is indistinguishable from one that is
# working: sable-spawn-manager decides "already running" from the @sable_role
# tag (bin/sable-spawn-manager:283), and a cleared-but-alive pane still carries
# it. Measured 2026-07-30: all three manager panes had been context-cleared,
# spawn-manager reported "already running - skipping" for all three and spawned
# nothing, and lincoln had to hand-kick each over sable-msg.
#
# This hook is the fix's natural home because it is the only closed
# write-then-read-at-boot loop in the fleet: it fires on every SessionStart
# INCLUDING post-clear, on BOTH providers (~/.claude/settings.json and
# ~/.codex/hooks.json), and until now it wrote nothing observable outside the
# pane. Stamping here converts "the session restarted" from an inference into
# an EVENT, written from inside the pane by the agent's own boot.
#
# D2 (binding): the signal must be AGENT-written. Of the @sable_* pane options,
# all but @sable_status are written by the spawn TOOL and survive agent death,
# process death and pane death — there is a pane on this host carrying
# @sable_role=lincoln with pane_dead=1. A tool-written signal cannot report
# agent death, so the epoch has to be written by this hook and by nothing else.
#
# D13 (binding): identity is SABLE_AGENT_NAME-first (resolved above).
# CLAUDE_AGENT_NAME reads "lincoln" on all three Codex manager panes, so an
# epoch stamped with the Claude-side name would mislabel every one of them.
#
# D3 (binding): this writes an EVENT, never a threshold. Nothing here reads
# window_activity, pane idleness or elapsed time, because a cleared idle pane
# and a working between-turns pane are equally quiet. Three candidate
# discriminators were refuted BY MEASUREMENT and must not be reintroduced:
# /proc/<pid>/environ (a /clear resets the session inside a still-running
# process — the cleared managers' pids predated the clears by ~28h), pane
# idleness (cleared and working-between-turns panes both measure idle), and
# already_recycled's scrollback banner rule (returned False on all three live
# cleared panes). The suite's zero-capture-pane invariant enforces the last one
# structurally rather than one negative control per bad idea.
_sable_stamp_boot_epoch() {
    # A COMPACTION IS NOT A RESTART. Riding the additionalContext emit below
    # would also fire this on PreCompact, so every /compact would read as a
    # clear and Lincoln would re-kick healthy managers in a loop. Stamp on an
    # EXPLICIT SessionStart only.
    if [ "$HOOK_EVENT" != "SessionStart" ]; then
        # Fail-closed on an unidentifiable event — but loudly. Silently not
        # stamping would leave the pane looking never-booted forever, which is
        # the same false-green in the other direction. The fixed token is the
        # signal that a provider is dispatching this hook without a
        # hook_event_name payload and the epoch is therefore inert on it.
        if [ "$HOOK_EVENT" = "__SABLE_EVENT_UNKNOWN__" ]; then
            printf 'SABLE-BOOT-EPOCH-EVENT-UNKNOWN: hook payload carried no hook_event_name, so this invocation cannot be told apart from a PreCompact. No boot epoch stamped for %s. A compaction misread as a restart re-kicks a healthy manager in a loop, so this fails closed; if this fires on a real SessionStart, that provider is dispatching the hook with no stdin payload and the boot epoch is inert on it.\n' \
                "$ROLE_NAME" >&2
        fi
        return 0
    fi
    # Not in a pane (a manager session outside tmux) is the ordinary case, not
    # an error: there is nothing to stamp and nobody to read it.
    [ -n "${TMUX_PANE:-}" ] || return 0
    command -v tmux >/dev/null 2>&1 || return 0

    local ts uniq
    ts="$(date +%s 2>/dev/null || true)"
    [ -n "$ts" ] || ts=0
    # Second resolution alone is not enough: a restart can land in the same
    # second as the boot it replaces, and an epoch that fails to CHANGE across
    # a restart is exactly the signal this bead exists to provide.
    uniq="$(od -An -tx1 -N6 /dev/urandom 2>/dev/null | tr -d ' \n' || true)"
    [ -n "$uniq" ] || uniq="$(printf '%04x%04x%04x' "$((RANDOM))" "$((RANDOM))" "$(($$ & 0xffff))")"

    # ORDER IS LOAD-BEARING, AND THE FIRST WRITE IS A TRANSACTIONAL PREREQUISITE
    # (SABLE-rvbg0). Identity first, epoch last, and the epoch is attempted ONLY
    # if the identity write actually landed. Writing them in that order is not by
    # itself enough — this line used to end in `|| true`, which suppressed an
    # agent-tag failure and let a fresh epoch publish beside the PREVIOUS
    # session's name. That is precisely the mispairing the ordering exists to
    # prevent, so the ordering claim was false in exactly the case it was making
    # a promise about. The epoch is the commit point BECAUSE it is unreachable
    # when the tag write fails: a reader that sees a fresh epoch is thereby
    # guaranteed a same-generation agent tag beside it.
    if ! tmux set-option -p -t "$TMUX_PANE" @sable_boot_agent "$ROLE_NAME" 2>/dev/null; then
        # Loud, no epoch, exit zero. An unstamped pane reads as never-booted
        # downstream, which is a bad outcome — but a fresh epoch carrying a
        # stale identity is a WORSE one, because it reads as authoritative.
        printf 'SABLE-BOOT-EPOCH-STAMP-FAILED: could not write @sable_boot_agent to pane %s for %s. No epoch was stamped either — publishing one without its agent tag would pair a fresh epoch with a stale identity. That pane will read as never-booted to the restart check.\n' \
            "$TMUX_PANE" "$ROLE_NAME" >&2
        return 0
    fi
    if ! tmux set-option -p -t "$TMUX_PANE" @sable_boot_epoch "${ts}-${uniq}" 2>/dev/null; then
        # Never fail the session boot over instrumentation — but never swallow
        # it either, since an unstamped pane reads as never-booted downstream.
        # Residual, deliberately not "repaired" here: the pane keeps whatever
        # epoch the PREVIOUS boot left, now beside this boot's agent tag. The
        # tag is not rolled back, because a rollback write can fail for the same
        # reason this one did and would only add a third state. The fixed token
        # on stderr is the contract for this case.
        printf 'SABLE-BOOT-EPOCH-STAMP-FAILED: could not write @sable_boot_epoch to pane %s for %s. That pane will read as never-booted to the restart check.\n' \
            "$TMUX_PANE" "$ROLE_NAME" >&2
    fi
    return 0
}
_sable_stamp_boot_epoch

# Resolve the role PROJECT-FIRST (a project-scoped orchestration install lives in
# ./.claude) then fall back to the user-level install (~/.claude).
_PROJECT_CAND="$PWD/.claude/sable/roles/${ROLE_NAME}.md"
_USER_CAND="$HOME/.claude/sable/roles/${ROLE_NAME}.md"
ROLE_FILE=""
for _cand in "$_PROJECT_CAND" "$_USER_CAND"; do
    if [ -f "$_cand" ]; then ROLE_FILE="$_cand"; break; fi
done
[ -z "$ROLE_FILE" ] && exit 0

# SABLE-thx70: LOUD ON SHADOWING. Project-first precedence is a supported
# feature (the comment above), but a stale project-local copy silently
# outranking a freshly-edited user-level one, forever, with no event, is not
# -- it defeated six days of role-card edits fleet-wide before anyone noticed
# (measured on this bead's own optimus.md: 6 bead ids present ONLY in the
# user-level copy, never read). Precedence is UNCHANGED here; this only makes
# the disagreement visible. Fires ONLY when both candidates exist AND differ
# in content -- identical content, or only one candidate present, is the
# ordinary case and must stay silent or every boot would warn.
if [ -f "$_PROJECT_CAND" ] && [ -f "$_USER_CAND" ] && ! cmp -s "$_PROJECT_CAND" "$_USER_CAND"; then
    _proj_mtime=$(stat -c '%y' "$_PROJECT_CAND" 2>/dev/null || stat -f '%Sm' "$_PROJECT_CAND" 2>/dev/null || echo unknown)
    _user_mtime=$(stat -c '%y' "$_USER_CAND" 2>/dev/null || stat -f '%Sm' "$_USER_CAND" 2>/dev/null || echo unknown)
    printf 'SABLE-ROLE-CARD-SHADOWED: role card for %s differs between the project-local and user-level installs. Precedence is unchanged -- the project-local copy WINS -- but the user-level copy is being silently ignored:\n  WINNER   (project-local): %s (mtime: %s)\n  SHADOWED (user-level):    %s (mtime: %s)\nIf the user-level edit was intentional, it will never be read while the project-local copy exists.\n' \
        "$ROLE_NAME" "$_PROJECT_CAND" "$_proj_mtime" "$_USER_CAND" "$_user_mtime" >&2
fi

ROLE_CONTENT=$(cat "$ROLE_FILE")

# --- Live protocol state surface (SABLE-9ozz) -------------------------------
# A pane restart (/clear, crash, session limit) silently reverts this session to
# the STATIC role card above and loses every conversation-state convention — the
# merge-gate sole-path contract, interim fleet caps, the manual-relay rule while
# a hook is dark. Those lived only in the previous conversation's context, which
# the restart destroyed (the 2026-07-13 gah9 bypass: chuck's static identity
# still described the OLD manual-merge flow). Surface the LIVE protocol state
# from disk — the orchestration mode-state + an active-contracts file colocated
# with it — so a fresh boot reconciles against the current contract, not the
# historical identity. Empty surfaces => byte-identical to the legacy identity-
# only injection (nothing extra emitted). This is ALSO how fix-direction-2's
# boot reconciliation instruction reaches every manager at once, without editing
# each role card.
LIVE_MODE=""
LIVE_CONTRACTS=""
LIVE_MODE_CORRUPT=""
_lmp="$(dirname "${BASH_SOURCE[0]:-$0}")/lib-mode-path.sh"
if [ -f "$_lmp" ]; then
    # shellcheck source=lib-mode-path.sh
    . "$_lmp" 2>/dev/null || true
fi
if command -v sable_mode_state_path >/dev/null 2>&1; then
    _mode_state="$(sable_mode_state_path "$PWD" 2>/dev/null || true)"
else
    _mode_state="${SABLE_MODE_STATE:-}"
fi
if [ -n "${_mode_state:-}" ] && [ -f "$_mode_state" ]; then
    LIVE_MODE="$(STATE="$_mode_state" python3 -c '
import json, os
try:
    d = json.load(open(os.environ["STATE"]))
    if not isinstance(d, dict):
        raise ValueError("state root is not an object")
    m = d.get("mode", "")
    if m not in ("planning", "execution"):
        raise ValueError("invalid mode")
except Exception:
    print("__SABLE_STATE_CORRUPT__")
    raise SystemExit(0)
line = m
sub = d.get("substage")
if sub:
    line += " / " + sub
since = d.get("since")
if since:
    line += " (since " + since + ")"
print(line)
' 2>/dev/null || true)"
    if [ "$LIVE_MODE" = "__SABLE_STATE_CORRUPT__" ]; then
        LIVE_MODE=""
        LIVE_MODE_CORRUPT="1"
    fi
fi
# The contracts surface is colocated with the mode-state file; SABLE_ACTIVE_CONTRACTS
# overrides the file directly (parallel to SABLE_MODE_STATE for mode-state).
if [ -n "${SABLE_ACTIVE_CONTRACTS:-}" ]; then
    _contracts_file="$SABLE_ACTIVE_CONTRACTS"
elif [ -n "${_mode_state:-}" ]; then
    _contracts_file="$(dirname "$_mode_state")/active-contracts.md"
else
    _contracts_file=""
fi
if [ -n "${_contracts_file:-}" ] && [ -s "$_contracts_file" ]; then
    LIVE_CONTRACTS="$(cat "$_contracts_file" 2>/dev/null || true)"
fi

# The injection's default for an unidentifiable event is the OPPOSITE of the
# stamp's, and deliberately so. The stamp fails closed (an ambiguous event must
# not be mistaken for a restart); the injection fails open to SessionStart,
# which is the behavior that shipped before the epoch existed — an identity
# that fails to anchor is a manager operating without its role card, a strictly
# worse outcome than an additionalContext payload the provider ignores.
INJECT_EVENT="$HOOK_EVENT"
[ "$INJECT_EVENT" = "__SABLE_EVENT_UNKNOWN__" ] && INJECT_EVENT="SessionStart"

ROLE_CONTENT="$ROLE_CONTENT" LIVE_MODE="$LIVE_MODE" LIVE_MODE_CORRUPT="$LIVE_MODE_CORRUPT" LIVE_CONTRACTS="$LIVE_CONTRACTS" INJECT_EVENT="$INJECT_EVENT" python3 -c "
import json, os, sys
content = os.environ.get('ROLE_CONTENT', '')
name = os.environ.get('ROLE_NAME', '').upper()
live_mode = os.environ.get('LIVE_MODE', '').strip()
live_mode_corrupt = os.environ.get('LIVE_MODE_CORRUPT', '') == '1'
live_contracts = os.environ.get('LIVE_CONTRACTS', '').strip()

# Which event fired us (SessionStart or PreCompact), so we emit the correct
# hookEventName in hookSpecificOutput — Claude Code silently drops
# additionalContext payloads if the wrapper/event name is missing or wrong.
# Resolved in BASH now (SABLE-slip0.1) rather than re-read from stdin here: the
# boot-epoch stamp needs the same answer earlier in the script, and stdin is a
# pipe that only the first reader gets.
event = os.environ.get('INJECT_EVENT') or 'SessionStart'

# SABLE-jiqm: PreCompact's hookSpecificOutput schema does not support
# additionalContext (only UserPromptSubmit/PostToolUse/PostToolBatch/Stop do) —
# emitting it here fails Claude Code's hook JSON validation on every /compact,
# silently losing the re-anchor. Re-anchoring already happens via the
# SessionStart:compact leg (this same script, fired again post-compaction with
# hook_event_name still 'SessionStart'), so no-op here instead of emitting an
# invalid shape.
if event == 'PreCompact':
    sys.exit(0)

identity = (
    f'=== AGENT IDENTITY: {name} ===\n\n{content}\n\n=== END IDENTITY ===\n\n'
    f'You are {name}. Operate within this role. Do not act as another manager.'
)

# Only append the live-protocol block when there IS live state on disk, so a
# non-orchestration manager session stays byte-identical to the legacy injection.
if live_mode or live_mode_corrupt or live_contracts:
    parts = [
        '',
        '',
        '=== LIVE PROTOCOL STATE (SABLE-9ozz — read from disk at SessionStart) ===',
        '',
        'This block reflects the CURRENT fleet protocol. A pane restart (/clear,',
        'crash, session limit) silently reverted you to the STATIC identity above,',
        'which is only a HISTORICAL baseline. Where this live state conflicts with',
        'that identity, THIS WINS.',
        '',
    ]
    if live_mode:
        parts.append('Orchestration mode: ' + live_mode)
        parts.append('')
    if live_mode_corrupt:
        parts.append('Orchestration mode: CORRUPT STATE — authorization is unavailable.')
        parts.append('Do not dispatch or execute. Diagnose with  sable-mode show  and recover')
        parts.append('explicitly with  sable-mode clear  or a new approved mode transition.')
        parts.append('')
    if live_contracts:
        parts.append('Active contracts:')
        parts.append(live_contracts)
        parts.append('')
    parts.append('Boot reconciliation (do this BEFORE your first action): reconcile the')
    parts.append('above against your static identity, then run  bd memories  and read your')
    parts.append('for-<role> inbox. Only then act.')
    parts.append('')
    parts.append('=== END LIVE PROTOCOL STATE ===')
    identity += '\n'.join(parts)

print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': event,
        'additionalContext': identity,
    }
}))
"
