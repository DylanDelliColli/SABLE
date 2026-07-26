#!/usr/bin/env bash
# tree-claim.sh — PreToolUse:Bash entrypoint for the tree-claim gate.
# Trigger: PreToolUse:Bash | Timeout: 3000ms
#
# All the gate's logic lives in tree-claim-impl.sh. This file exists for one
# reason: to decide what it MEANS when that logic cannot run (SABLE-k2h0m).
#
# THE PROBLEM. A hook that exits non-zero without emitting deny JSON is
# treated as ALLOW, so the failure mode of a guard is total permissiveness.
# tree-claim-impl.sh embeds six python programs inside double-quoted bash
# strings; one stray double quote anywhere in any of them — including in a
# comment — makes the WHOLE FILE unparseable and `bash tree-claim-impl.sh`
# exits 2 before executing a line. That happened live: 84 suite assertions
# flipped from deny to allow at once, and nothing said a word. A guard that
# cannot distinguish "nothing to deny" from "I could not run" is a false-green
# generator.
#
# WHY A SEPARATE FILE. A single-file hook CANNOT fail closed on its own syntax
# error: bash parses the entire file before executing its first line, so any
# self-check inside it is dead code exactly when it is needed. The check has to
# live in a different frame. Hence this entrypoint — which is why it must stay
# SMALL and free of embedded python, heredocs, and anything else that makes
# quoting fragile. It is the one frame nothing else can guard; the test suite
# asserts that property rather than trusting this comment.
#
# THE DECIDED DIRECTION: DENY, WITH THE MEANS OF REPAIR PRESERVED.
# A claim gate that could not evaluate has not established that the write is
# safe. "We could not check" is not the same claim as "this is permitted", so
# the honest answer is refusal (the same fail-closed reasoning SABLE-jd5fj.5
# applied to its freeze read). The counter-argument is real, though, and it is
# acute here: if a broken tree-claim.sh blocks all git writes, it blocks the
# writes needed to FIX tree-claim.sh. A guard must not remove the means of its
# own repair. Three properties resolve that:
#
#   1. NARROW. Only commands that look like a git index write are refused.
#      Everything else — editors, tests, `bash -n`, `git status`, `git push` —
#      still runs, so the fix can be written and validated normally. The
#      degraded classifier is deliberately crude (a regex over the raw hook
#      input, no python, no jq): in degraded mode we cannot trust the parser,
#      and over-matching errs toward refusal, which is the safe direction.
#   2. BREAK-GLASS. SABLE_TREE_CLAIM_OVERRIDE=1 — the gate's existing,
#      documented escape hatch — is honoured here from the hook's environment
#      OR as an inline prefix in the command text. The inline form matters: a
#      PreToolUse hook is a separate process, so a `VAR=1 git commit` prefix
#      typed at the command line never reaches this script's environment, and
#      an escape hatch you cannot reach from where you type is not an escape
#      hatch. Both forms are recorded in the transcript and announced.
#   3. LOUD. Every degraded decision — allow and deny alike — prints the fixed
#      token SABLE-TREE-CLAIM-DEGRADED to stderr and carries additionalContext
#      or a reason. Silence was the worst property of the old mode.
#
# BLAST RADIUS, stated deliberately. This gate attributes a write to the repo
# the command actually targets, and SABLE-vx4aj tracks cases where that
# attribution reaches unrelated repos. So while the impl is broken, refusals
# can land on writes outside this checkout. That is bounded by (1) and (2)
# above and lasts only as long as the breakage — whereas the fail-open mode it
# replaces was unbounded, silent, and indefinite.

set -uo pipefail

HOOK_INPUT=$(cat 2>/dev/null) || HOOK_INPUT=""

SELF_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd) || SELF_DIR="."
IMPL="$SELF_DIR/tree-claim-impl.sh"

# Run the real gate. Every normal path of the impl exits 0 (allow, deny and
# no-op alike), so a non-zero exit means it could not run: unparseable,
# missing, truncated, or crashed mid-flight. Its stdout is discarded in that
# case — a half-emitted decision is not a decision.
IMPL_OUT=$(printf '%s' "$HOOK_INPUT" | bash "$IMPL")
IMPL_RC=$?
if [ "$IMPL_RC" -eq 0 ]; then
  [ -n "$IMPL_OUT" ] && printf '%s\n' "$IMPL_OUT"
  exit 0
fi

# ---------------------------------------------------------------------------
# Degraded mode: the gate did not run.
# ---------------------------------------------------------------------------
# Distinguish the two shapes of breakage for the diagnostic only; both fail
# closed. `bash -n` is affordable here because we are already broken.
if [ ! -f "$IMPL" ]; then
  BREAKAGE="missing (partial or truncated install?)"
elif ! bash -n "$IMPL" 2>/dev/null; then
  BREAKAGE="unparseable (syntax error — check the embedded python quoting)"
else
  BREAKAGE="crashed at runtime (exit $IMPL_RC)"
fi

# Strip characters that would break the hand-built JSON below. This file emits
# JSON with printf on purpose: in degraded mode python3 may be exactly what is
# unavailable, and the emitter must not share a failure mode with the thing it
# is reporting on.
SAFE_IMPL=${IMPL//[\"\\]/}
SAFE_BREAKAGE=${BREAKAGE//[\"\\]/}

printf 'SABLE-TREE-CLAIM-DEGRADED: tree-claim gate could not run — %s is %s. Git index writes are being REFUSED (fail closed) until it is repaired; break glass with SABLE_TREE_CLAIM_OVERRIDE=1.\n' \
  "$SAFE_IMPL" "$SAFE_BREAKAGE" >&2

emit_allow() {
  printf '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "additionalContext": "%s"}}\n' "$1"
  exit 0
}

emit_deny() {
  printf '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "%s"}}\n' "$1"
  exit 0
}

# Break glass: the override in this process's environment, or typed inline in
# the command itself (see note 2 in the header).
if [ "${SABLE_TREE_CLAIM_OVERRIDE:-}" = "1" ] ||
   printf '%s' "$HOOK_INPUT" | grep -q 'SABLE_TREE_CLAIM_OVERRIDE=1'; then
  emit_allow "tree-claim: DEGRADED — the gate could not run ($SAFE_IMPL is $SAFE_BREAKAGE) and SABLE_TREE_CLAIM_OVERRIDE=1 allowed this write through UNCHECKED. No claim was read, written or refreshed, so nothing is protecting this checkout from a concurrent session right now. Repair the hook."
fi

# Crude, dependency-free classifier over the raw hook input. It cannot parse
# the command (that is the machinery that just failed), so it asks only
# whether the input plausibly contains an index-mutating git invocation. It
# over-matches by design — the cost of a false refusal is one override, the
# cost of a false allow is the incident this bead was filed for.
if printf '%s' "$HOOK_INPUT" | grep -Eq 'git[^"]*\b(add|commit|rm|mv|reset|restore)\b'; then
  emit_deny "tree-claim: DEGRADED — REFUSING this git index write because the claim gate could not run ($SAFE_IMPL is $SAFE_BREAKAGE), so nothing has established that this write is safe against another session sharing the checkout. This is deliberate: a guard that cannot evaluate must not report success. TO REPAIR: non-git commands are unaffected, so edit and validate the hook normally (bash -n $SAFE_IMPL), then land the fix with SABLE_TREE_CLAIM_OVERRIDE=1 prefixed to your git commands. TO OVERRIDE: same prefix — it is recorded and announced, not silent."
fi

emit_allow "tree-claim: DEGRADED — the claim gate could not run ($SAFE_IMPL is $SAFE_BREAKAGE). This command was allowed because it does not look like a git index write, but the checkout is UNGUARDED: git index writes are being refused until the hook is repaired (break glass with SABLE_TREE_CLAIM_OVERRIDE=1). Repair it before relying on claim protection."
