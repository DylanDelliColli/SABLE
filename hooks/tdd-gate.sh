#!/usr/bin/env bash
# tdd-gate.sh — Block bd close without test evidence
# Checks the evidence file written by tdd-evidence.sh.
# Escape hatch: add [no-test] to bead notes (single-close only).

set -euo pipefail

# Read stdin and parse with python3 (jq not available)
PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
cmd = d.get('tool_input', {}).get('command', '')
sid = d.get('session_id', '')
aid = d.get('agent_id', '') or ''
print(f'{sid}\n{aid}\n{cmd}')
" 2>/dev/null) || exit 0

SESSION_ID=$(echo "$PARSED" | sed -n '1p')
AGENT_ID=$(echo "$PARSED" | sed -n '2p')
COMMAND=$(echo "$PARSED" | sed -n '3p')

[ -z "$COMMAND" ] && exit 0

# Only act on bd close commands
echo "$COMMAND" | grep -q '^bd close' || exit 0

# Extract bead IDs from the close command. Strategy: shlex-tokenize the
# string, then keep only tokens that match the bead-ID shape
# (PREFIX-suffix or PREFIX-suffix.N, with any-case prefix + lowercase
# alphanumeric suffix). This naturally excludes flags (--reason, --json),
# flag values (text after a flag), pipes (|), redirects (2>&1, > file),
# and command chains (&&, ||, ;) — none of those tokens look like a
# bead ID, so they don't inflate ID_COUNT.
#
# Replaces the previous sed pipeline (SABLE-1n2: missed --flag value
# forms) and the shlex+flag-walker variant (SABLE-sqz: missed pipe /
# redirect / chain tokens since they aren't flags but aren't IDs either).
# Updated to accept lowercase prefixes (SABLE-i2m) for rigs using twine-*,
# chess-*, or other any-case prefix schemes. Prefix class now allows hyphens
# (market-brief-package-2e4o) so monorepo rigs with multi-hyphen prefixes
# (market-brief-package-*) bind the suffix to the LAST hyphen segment; without
# it those IDs matched zero tokens and the [no-test] hatch was silently skipped.
BEAD_ARGS=$(BEAD_CMD="$COMMAND" python3 -c "
import os, re, shlex
cmd = re.sub(r'^bd close\s+', '', os.environ.get('BEAD_CMD', ''))
try:
    tokens = shlex.split(cmd)
except ValueError:
    tokens = []
# Only consider tokens before the first flag (first token starting with '-').
# Flag values after a flag token (e.g. 'docs-only' after '--reason') can
# match the bead-ID shape and would inflate ID_COUNT — SABLE-3uw / SABLE-9we.
positional = []
for t in tokens:
    if t.startswith('-'):
        break
    positional.append(t)
ID_PATTERN = re.compile(r'^[A-Za-z][A-Za-z0-9-]*-[a-z0-9]+(\.[0-9]+)?\$')
ids = [t for t in positional if ID_PATTERN.match(t)]
print(' '.join(ids))
")
ID_COUNT=$(echo "$BEAD_ARGS" | wc -w)

# Single-bead close: check [no-test] escape hatch
if [ "$ID_COUNT" -eq 1 ]; then
  BEAD_ID="$BEAD_ARGS"
  # Check notes field for [no-test] marker via bd show --json
  NOTES=$(bd show "$BEAD_ID" --json 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
if isinstance(data, list) and len(data) > 0:
    print(data[0].get('notes', '') or '')
" 2>/dev/null || echo "")
  if echo "$NOTES" | grep -q '\[no-test\]'; then
    exit 0  # Escape hatch: allow close without test evidence
  fi
fi

# Check for test evidence. Per-agent keying (SABLE-d72): read the SAME key
# tdd-evidence.sh writes — session_id + agent_id for subagents (so worker A's
# test run can't satisfy worker B's close in a shared session), session-global
# for main sessions. Companion convention: workers close their OWN beads after
# green, so the test run and the bd close share one agent context.
if [ -n "$AGENT_ID" ]; then
  EVIDENCE_FILE="/tmp/tdd-evidence-${SESSION_ID}-${AGENT_ID}"
else
  EVIDENCE_FILE="/tmp/tdd-evidence-${SESSION_ID}"
fi
if [ -s "$EVIDENCE_FILE" ]; then
  exit 0  # Tests were run by this agent this session — allow close
fi

# market-brief-package-sqcr: companion-repo acceptance. A cross-repo bead (a
# fix tracked in one bd tracker whose acceptance evidence is a test suite in a
# DIFFERENT repo — the 73t4 pattern: a SABLE-hooks fix tracked as a
# market-brief-package bead) declares that repo in its notes as a line
# "Companion repo: <path>". If any bead in this close command carries that
# declaration, and ANY evidence file for this session (any agent — a nested
# sub-call may have run the companion suite under a different agent_id) has a
# REPO=<path>-tagged line from tdd-evidence.sh, accept. This is purely
# additive: it only fires when the exact-key evidence file above was empty,
# so it cannot weaken the existing per-agent gate.
if [ -n "$BEAD_ARGS" ]; then
  for _bid in $BEAD_ARGS; do
    _companion=$(bd show "$_bid" --json 2>/dev/null | python3 -c "
import json, re, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if not isinstance(data, list) or not data:
    sys.exit(0)
notes = data[0].get('notes', '') or ''
m = re.search(r'Companion repo:\s*(\S+)', notes)
print(m.group(1) if m else '')
" 2>/dev/null) || _companion=""
    if [ -n "$_companion" ] && grep -qF "REPO=${_companion}" /tmp/tdd-evidence-"${SESSION_ID}"* 2>/dev/null; then
      exit 0
    fi
  done
fi

# No evidence found — block the close
python3 -c "
import json
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': 'TDD gate: No tests were run this session. Run your test suite first (npm test, pytest, etc.). For non-code beads: add [no-test] to bead notes and close individually.'
    }
}))
"
