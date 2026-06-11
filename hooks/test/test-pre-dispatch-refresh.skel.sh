#!/usr/bin/env bash
# Columbo skeleton — see SABLE-sp2 (v3 test-spec: pre-dispatch-refresh
# Worktree prompt-line resolution).
#
# pre-dispatch-refresh.sh currently has NO test file. The locked worktree
# lifecycle contract (Gaudi --epic SABLE-d50): rebase target comes from the
# structured 'Worktree: /abs/path' prompt line FIRST; hook-input cwd is the
# fallback only when the line is absent. Spike SABLE-d50.1 proved depth-1
# manager Agent-call hook input carries the MAIN checkout cwd — cwd inference
# alone rebases the wrong checkout in v3.
#
# Worker: implement each todo case in house style — crafted PreToolUse:Agent
# hook-input JSON (agent_id + agent_type + tool_input.prompt + cwd), real
# scratch git repos with `git worktree add` under mktemp -d, no filesystem
# mocking. Then rename test-pre-dispatch-refresh.skel.sh →
# test-pre-dispatch-refresh.sh.
#
# Run with:
#   bash hooks/test/test-pre-dispatch-refresh.skel.sh   (always exits 1 — skeleton)

set -uo pipefail

TODO=0
todo() { TODO=$((TODO+1)); echo "TODO (unimplemented case): $1"; }

# Why: manager-created worktree is where the refresh must land, not the
# manager's own cwd (which the hook input carries — the spike-verified mismatch).
todo "case A: prompt Worktree line targets that checkout"

# Why: preserves Chuck-legacy env-terminal behavior where cwd is correct.
todo "case B: no Worktree line falls back to hook-input cwd"

# Why: a relative path resolved against the wrong cwd would rebase an
# arbitrary directory; contract says ignore + cwd fallback + advisory.
todo "case C: malformed Worktree line (relative path) is ignored with advisory"

# Why: pin the ambiguity before a prompt accidentally carries two Worktree
# lines (e.g. a quoted bead description) and the hook picks the wrong one.
todo "duplicate Worktree lines: first match wins"

# Why: a manager typo must not crash the dispatch or silently rebase the
# wrong checkout.
todo "Worktree path that does not exist on disk fails open with advisory"

# Why: activation is gated by keystone lane resolution; a depth-2 worker
# prompt containing a Worktree line must not trigger refresh actions.
todo "non-manager dispatches stand down regardless of Worktree line"

# Why: template-assembled prompts carry trailing whitespace; a brittle regex
# silently drops to cwd fallback and the failure is invisible until a worker
# edits stale files.
todo "Worktree line with trailing whitespace/CR parses cleanly"

echo
echo "=========================================="
echo "SKELETON: $TODO cases unimplemented — this is a contract, not a passing suite."
echo "See SABLE-sp2 for inputs/expected per case."
echo "=========================================="
exit 1
