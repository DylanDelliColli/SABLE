#!/usr/bin/env bash
# lib-evidence-key.sh — single source of truth for the TDD evidence file path
# (SABLE-jfg6.4 / contract D4).
#
# The t7fm family's "tfkv" failure was a writer/reader key mismatch: the evidence
# WRITER (tdd-evidence.sh) and the gate READER (tdd-gate.sh) each derived the
# /tmp/tdd-evidence-<...> path with their own inline expression. Any drift between
# the two — a suffix rule applied on one side but not the other, or an empty
# session id handled differently — silently split the pair so a real green run
# wrote at one path while the gate looked at another (false-deny) or, worse, two
# unrelated runs collided on one garbage path (false-permissive). Consolidating
# the derivation into ONE function that both consumers call makes drift
# structurally impossible: same input => byte-identical path, always.
#
# It also closes the hccq trap. Agent-subagent and gc-managed session types
# (research F3; Claude Code #34692 WONTFIX) can present with NO CLAUDE_SESSION_ID
# at all — only CLAUDE_CODE_SESSION_ID may exist, or neither. The old inline
# expression interpolated an empty session id straight into the path, yielding
# "/tmp/tdd-evidence-" (or "/tmp/tdd-evidence--<aid>") — a single garbage path
# shared by every empty-session process on the box. This function instead
# substitutes a deterministic, non-empty ppid-derived token for an absent
# session (the same fallback shape tree-claim.sh already uses for "session
# identity unknowable"), so absent-session trees get their own key and never
# collide on the empty-session path.
#
# PURE by design: callers pass the session id and agent id they already hold
# (the hooks parse them from the PreToolUse JSON; bin/sable-test resolves its
# own via sable_resolve_session_id below). This function itself reads NO
# session environment variable — that is what keeps IT out of the hccq trap.
#
# SABLE-0w0ou correction: an earlier version of this comment claimed the gate
# "sees an empty JSON session_id, not CLAUDE_CODE_SESSION_ID" and used that to
# justify never substituting CLAUDE_CODE_SESSION_ID anywhere. That premise is
# false for every hooks-fire session (every warm-pane worker, per
# ~/.claude/settings.json wiring tdd-evidence.sh/tdd-gate.sh to PreToolUse):
# the PreToolUse JSON's session_id IS populated there, and it is
# byte-for-byte CLAUDE_CODE_SESSION_ID — verified empirically (the evidence
# file the hook writes is named /tmp/tdd-evidence-<CLAUDE_CODE_SESSION_ID>),
# the same invariant tree-claim.sh/sable-claim already rely on and test. The
# real defect was bin/sable-test checking ONLY CLAUDE_SESSION_ID and never
# CLAUDE_CODE_SESSION_ID, so an env-only caller gave up on a signal that was
# actually available and correct, and fell all the way to the ppid fallback
# while the reader read the real id — a false-deny, not the false-permissive
# collision this comment used to warn about. sable_resolve_session_id() below
# is the ONE place that now walks CLAUDE_SESSION_ID -> CLAUDE_CODE_SESSION_ID
# for such callers; this function stays pure and unaware of either variable.

# sable_evidence_key <session_id> <agent_id>
# Print the absolute TDD evidence-file path for the given identity.
#
#   session_id present, agent_id present -> /tmp/tdd-evidence-<sid>-<aid>
#   session_id present, agent_id empty   -> /tmp/tdd-evidence-<sid>
#   session_id ABSENT (either agent_id)  -> ppid token replaces <sid>:
#                                           /tmp/tdd-evidence-ppid-<PPID>[-<aid>]
#
# Agreement note: the absent-session fallback keys on $PPID, which every consumer
# resolves — including inside the $(sable_evidence_key ...) command substitution
# they all use — to the PARENT of the consumer process, i.e. the session
# controller that spawned it. Sibling consumers of one session (the tdd-evidence
# / tdd-gate hook pair, or a tool invoked beside them) therefore derive the SAME
# token. It is deterministic and non-empty, never the empty-session garbage path.
sable_evidence_key() {
  local sid="${1:-}"
  local aid="${2:-}"
  local base="/tmp/tdd-evidence"

  if [ -z "$sid" ]; then
    sid="ppid-${PPID:-0}"
  fi

  if [ -n "$aid" ]; then
    printf '%s-%s-%s' "$base" "$sid" "$aid"
  else
    printf '%s-%s' "$base" "$sid"
  fi
}

# sable_resolve_session_id
# Resolve THIS process's session identity from the environment, for callers
# that have no PreToolUse hook JSON to read (bin/sable-test is the only
# consumer today; any future env-only writer should call this too, rather
# than re-deriving its own fallback order — SABLE-0w0ou is exactly what
# happens when a second derivation drifts from the first). Checks, in order:
#   1. $CLAUDE_SESSION_ID
#   2. $CLAUDE_CODE_SESSION_ID — SABLE-hccq: the env var Claude Code actually
#      exports into a Bash tool call's environment, and (SABLE-0w0ou) the
#      SAME value every PreToolUse hook's JSON session_id carries in a
#      hooks-fire session. CLAUDE_SESSION_ID is checked first only in case
#      some environment sets it; it is unset in practice.
# Prints the resolved id, or nothing if both are absent — the caller then
# passes empty straight to sable_evidence_key, which supplies the
# deterministic ppid fallback (reserved for genuinely no-identity-available
# sessions: no hook ever ran to supply one, and neither env var is set).
sable_resolve_session_id() {
  if [ -n "${CLAUDE_SESSION_ID:-}" ]; then
    printf '%s' "$CLAUDE_SESSION_ID"
    return 0
  fi
  if [ -n "${CLAUDE_CODE_SESSION_ID:-}" ]; then
    printf '%s' "$CLAUDE_CODE_SESSION_ID"
    return 0
  fi
  printf '%s' ""
}
