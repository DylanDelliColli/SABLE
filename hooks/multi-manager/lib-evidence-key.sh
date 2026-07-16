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
# (the hooks parse them from the PreToolUse JSON; bin/sable-test reads
# CLAUDE_SESSION_ID from the environment). This function reads NO session
# environment variable itself — that is what keeps it out of the hccq trap. It
# must never quietly substitute CLAUDE_CODE_SESSION_ID for a missing
# CLAUDE_SESSION_ID: that value is exactly the mismatch source (the gate sees an
# empty JSON session_id, not CLAUDE_CODE_SESSION_ID), so an absent session must
# fall through to the ppid token, not to a different env var.

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
