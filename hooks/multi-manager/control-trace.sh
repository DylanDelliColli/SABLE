#!/usr/bin/env bash
# control-trace.sh — neutral-observer control hook (SABLE-jfg6.2 / contract D2)
#
# DELIBERATELY SELF-CONTAINED: does NOT source lib-hook-trace.sh (SABLE-jfg6.1
# / D1). If the shared trace lib ever has a bug that silently swallows its own
# entry-log write, this hook still corroborates independently — two
# independent observers beat one. It carries zero logic in common with D1 on
# purpose; a shared bug in a shared helper would blind both at once.
#
# Contract (all three load-bearing for the D6 repro matrix, which diffs this
# log 1:1 against the session transcript jsonl to separate genuine
# non-dispatch, #6305/#15441, from dispatched-with-empty-stdin, #16047):
#   1. Its LITERAL FIRST EXECUTABLE LINE appends 'ts pid tool-hint' to its own
#      log. No setup, no matcher logic, nothing gets to run before the write.
#   2. It NEVER reads stdin (no `cat`, no `read`) — its presence in the log
#      can never be confused with, or interfere with, the D1 STDIN_BYTES
#      read. Tool-hint comes from argv/env only.
#   3. It NEVER blocks and ALWAYS exits 0 — it must be structurally incapable
#      of interfering with tool dispatch, even if logging fails outright.
#
# Log path honors SABLE_CONTROL_TRACE_LOG (SABLE-6cf9 fixture-isolation
# lesson: always redirectable for hermetic tests), defaulting to
# ~/.claude/sable/logs/control-trace.log.

mkdir -p "$(dirname "${SABLE_CONTROL_TRACE_LOG:-${HOME:-/tmp}/.claude/sable/logs/control-trace.log}")" 2>/dev/null; printf '%s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo '?')" "$$" "${1:-${CLAUDE_TOOL_NAME:-unknown}}" >> "${SABLE_CONTROL_TRACE_LOG:-${HOME:-/tmp}/.claude/sable/logs/control-trace.log}" 2>/dev/null

exit 0
