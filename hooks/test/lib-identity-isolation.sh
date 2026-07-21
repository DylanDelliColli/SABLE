#!/usr/bin/env bash
# lib-identity-isolation.sh — central hermeticity mechanism for hooks/test/*.sh
# suites that spawn stand-in tmux panes (SABLE-j3bi).
#
# THE MECHANISM (confirmed by tarzan 2026-07-21, tightened-triage standard):
# tmux new-session/new-window/split-window/respawn-pane fork the new pane's
# shell from the LAUNCHING process's environment at spawn time. A suite run
# from inside a live SABLE agent pane — the NORMAL case, since agents are
# exactly who run these suites — leaks CLAUDE_AGENT_NAME / SABLE_WORKER_PANE /
# CLAUDE_AGENT_ROLE / SABLE_BEAD into every stand-in pane it creates. A
# downstream identity check (SABLE-to8m, reading /proc/<pid>/environ) then
# CORRECTLY refuses an action against a pane whose real inherited identity
# contradicts the role under test. The suite reports a false RED that
# describes nothing wrong with the product — see SABLE-4nr0q / SABLE-frq7e /
# SABLE-j3bi for the (already expensive) history of workers misdirected by it.
#
# *** THE ONE RULE THAT MATTERS — POSITIONAL, NOT PRESENCE ***
# The scrub must happen BEFORE THE FIRST tmux pane-spawning call in the
# suite. Scrubbing at a LATER subprocess call site (e.g. only on the tool
# invocation under test) cannot retroactively clean an already-spawned
# pane's inherited environ. hooks/test/test-worker-flag-done.sh forces
# CLAUDE_AGENT_NAME="" on its --reap call (SABLE-dcw2) and is STILL
# deterministically non-hermetic, because the pane it's testing was spawned
# earlier in the file with the ambient identity intact. A guard that merely
# greps a suite for the PRESENCE of an unset would bless that line as
# correct; it is not.
#
# USAGE — source this file, call sable_scrub_identity_env ONCE before your
# suite's first tmux call, and route every pane-spawning tmux invocation
# through sable_tmux_spawn instead of calling tmux directly:
#
#   source "$REPO/hooks/test/lib-identity-isolation.sh"
#   sable_scrub_identity_env
#   sable_tmux_spawn -L "$SOCK" new-session -d -s w -x 200 -y 50 'bash --noprofile --norc'
#
# sable_tmux_spawn is the creation-time GUARD: if any identity var is still
# present in the environment when it is called (i.e. the suite skipped or
# mis-positioned the scrub), it FAILS LOUDLY — a non-zero exit and an
# actionable stderr message — instead of silently spawning a contaminated
# pane and letting a misleading red surface somewhere downstream. This is
# what makes the class impossible to reintroduce by accident: the failure
# happens at the exact call site that would otherwise leak, not as an
# inference from an unrelated assertion later in the suite.
#
# Non-spawning tmux calls (set-option, show-option, list-panes, select-pane,
# kill-server, ...) do not fork a new process environment and do not need to
# go through sable_tmux_spawn — call tmux directly for those, as suites
# already do.

_SABLE_IDENTITY_VARS="SABLE_WORKER_PANE CLAUDE_AGENT_NAME CLAUDE_AGENT_ROLE SABLE_BEAD"

# sable_scrub_identity_env — unset the ambient SABLE/Claude identity vars in
# THIS shell so nothing spawned after this call (directly or via
# sable_tmux_spawn) can inherit them. Call this before the first
# pane-spawning tmux call, not after.
sable_scrub_identity_env() {
  # shellcheck disable=SC2086
  unset $_SABLE_IDENTITY_VARS 2>/dev/null || true
}

# sable_leaked_identity_vars — print "VAR=value" for each identity var
# currently set, space-joined; empty output means the environment is clean.
sable_leaked_identity_vars() {
  local v out=""
  for v in $_SABLE_IDENTITY_VARS; do
    if [ -n "${!v:-}" ]; then
      out="$out $v=${!v}"
    fi
  done
  printf '%s' "$out"
}

# sable_tmux_spawn <tmux args...> — guarded stand-in for a pane-spawning tmux
# call (new-session / new-window / split-window / respawn-pane). Refuses to
# exec tmux at all if an identity var is still present in the environment,
# so a suite that forgot (or mis-positioned) sable_scrub_identity_env fails
# at the exact point that would otherwise contaminate a pane.
sable_tmux_spawn() {
  local leaked
  leaked="$(sable_leaked_identity_vars)"
  if [ -n "$leaked" ]; then
    {
      echo "FATAL (SABLE-j3bi hermeticity guard): identity env leaking into a tmux pane-spawn call."
      echo "  Leaked:$leaked"
      echo "  Call sable_scrub_identity_env BEFORE the first pane-spawning tmux call in this"
      echo "  suite — scrubbing at a later tool-invocation call site cannot retroactively"
      echo "  clean an already-spawned pane's inherited environ (see the file header of"
      echo "  hooks/test/lib-identity-isolation.sh for the confirmed mechanism)."
      echo "  Refused call: tmux $*"
    } >&2
    return 90
  fi
  tmux "$@"
}
