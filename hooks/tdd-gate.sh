#!/usr/bin/env bash
# tdd-gate.sh — Block bd close without test evidence
# Checks the evidence file written by tdd-evidence.sh.
# Escape hatch: add [no-test] to bead notes OR description (single-close only).
#
# SABLE-h853 (2026-07-13): a worker's pre-push test run is a SCOPED run — the
# bead's own test files plus tests importing the modules the diff touched,
# coverage off, fail-fast on — not the full suite. This gate accepts that
# evidence as-is: the check below is scope-agnostic by design (it verifies a
# test run happened this session, never what ran), so it does not require or
# check for full-suite execution. The full suite runs exactly once, PRE-merge,
# as a merge-preview ci-verify GitHub Actions run (the worker branch
# pre-merged onto the integration-branch tip on a throwaway ci-verify branch,
# gated before the fast-forward) — that is the SOLE full-suite authority,
# owned by chuck's merge gate, never a worker or this hook.

set -euo pipefail

# SABLE-jfg6.1 (contract D1): durable entry trace at TRUE line 1, before any
# stdin read, so absence-of-line == hook-never-fired (separable from
# fired-with-empty-stdin, which additionally logs STDIN_BYTES=0). Additive
# instrumentation only — the bd-close gate logic below is unchanged.
# shellcheck source=multi-manager/lib-hook-trace.sh
. "$(dirname "${BASH_SOURCE[0]}")/multi-manager/lib-hook-trace.sh"
sable_trace_entry tdd-gate

# SABLE-jfg6.4 (contract D4): derive the evidence-file path via the shared
# lib-evidence-key.sh so this READER can never drift from the tdd-evidence.sh
# WRITER (the tfkv mismatch class), and absent-session envs get a deterministic
# non-empty key instead of the empty-session garbage path.
# shellcheck source=multi-manager/lib-evidence-key.sh
. "$(dirname "${BASH_SOURCE[0]}")/multi-manager/lib-evidence-key.sh"

HOOK_INPUT=$(sable_trace_read_stdin) || exit 0

# Parse the hook identity, bd-close matcher, and positional bead IDs at one
# Python boundary (jq is unavailable). NUL-delimited fields let Bash consume
# the result without sed/grep/wc subprocesses. Invalid JSON or an unexpected
# payload shape emits no complete field set and preserves the silent fail-open.
TDD_GATE_FIELDS=()
mapfile -d '' -t TDD_GATE_FIELDS < <(printf '%s' "$HOOK_INPUT" | python3 -c "
import json, re, shlex, sys

d = json.load(sys.stdin)
cmd = d.get('tool_input', {}).get('command', '')
sid = d.get('session_id', '')
aid = d.get('agent_id', '') or ''

# Preserve the former print + sed -n '1p/2p/3p' field behavior exactly,
# including its first-line command boundary.
legacy_lines = f'{sid}\n{aid}\n{cmd}'.split('\\n')
session_id = legacy_lines[0] if legacy_lines else ''
agent_id = legacy_lines[1] if len(legacy_lines) > 1 else ''
command = legacy_lines[2] if len(legacy_lines) > 2 else ''

is_bd_close = bool(command) and command.startswith('bd close')
ids = []
if is_bd_close:
    close_args = re.sub(r'^bd close\s+', '', command)
    try:
        tokens = shlex.split(close_args)
    except ValueError:
        tokens = []
    # Only consider tokens before the first flag. Flag values such as
    # 'docs-only' can look like bead IDs but must not inflate the count.
    positional = []
    for token in tokens:
        if token.startswith('-'):
            break
        positional.append(token)
    id_pattern = re.compile(r'^[A-Za-z][A-Za-z0-9-]*-[a-z0-9]+(\\.[0-9]+)?\$')
    ids = [token for token in positional if id_pattern.match(token)]

fields = [session_id, agent_id, '1' if is_bd_close else '0',
          ' '.join(ids), str(len(ids))]
sys.stdout.write('\\0'.join(fields) + '\\0')
" 2>/dev/null)

[ "${#TDD_GATE_FIELDS[@]}" -eq 5 ] || exit 0
SESSION_ID="${TDD_GATE_FIELDS[0]}"
AGENT_ID="${TDD_GATE_FIELDS[1]}"
IS_BD_CLOSE="${TDD_GATE_FIELDS[2]}"
BEAD_ARGS="${TDD_GATE_FIELDS[3]}"
ID_COUNT="${TDD_GATE_FIELDS[4]}"

[ "$IS_BD_CLOSE" = "1" ] || exit 0

deny_with_reason() {
  TDD_GATE_DENY_REASON="$1" python3 -c "
import json, os
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': os.environ['TDD_GATE_DENY_REASON'],
    }
}))
"
}

# Bead IDs from the close command are shlex-tokenized above. Strategy: keep
# only tokens that match the bead-ID shape
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

# Single-bead close: check [no-test] escape hatch
MARKER_LOOKUP_FAILURE=""
MARKER_SCAN_DETAIL=""
if [ "$ID_COUNT" -eq 1 ]; then
  BEAD_ID="$BEAD_ARGS"
  # Check for the [no-test] marker in BOTH the notes AND the description field
  # via bd show --json. SABLE-p84b: the marker is a natural fit for the
  # description (where sable-spawn-worker's auto-prompt surfaces bead text), so
  # a notes-only scan stranded docs/config beads whose worker put [no-test] in
  # the description — the close was denied, then mis-reported as success
  # (SABLE-u0c6), leaving the bead in_progress with a pushed branch. Scanning
  # both fields is the cheapest, most forgiving fix.
  #
  # SABLE-wkxtl: a failed bd lookup is NOT equivalent to a successful lookup
  # with no marker. Preserve the lookup/parser status and stderr so, if no test
  # evidence can independently authorize this close, the final denial names
  # the real failure instead of telling the author to add a marker they may
  # already have added.
  if MARKER_ERR_FILE=$(mktemp "${TMPDIR:-/tmp}/sable-tdd-gate-marker.XXXXXX"); then
    if MARKER_JSON=$(bd show "$BEAD_ID" --json 2>"$MARKER_ERR_FILE"); then
      if MARKER_FIELDS=$(printf '%s' "$MARKER_JSON" | python3 -c "
import json, sys
data = json.load(sys.stdin)
if not isinstance(data, list) or not data or not isinstance(data[0], dict):
    raise ValueError('expected a non-empty JSON list containing one bead object')
print(data[0].get('notes', '') or '')
print(data[0].get('description', '') or '')
" 2>"$MARKER_ERR_FILE"); then
        MARKER_BYTES=$(printf '%s' "$MARKER_FIELDS" | wc -c | tr -d '[:space:]')
        MARKER_SCAN_DETAIL=" Marker lookup succeeded and scanned ${MARKER_BYTES:-0} bytes across notes and description; [no-test] was absent."
        if printf '%s\n' "$MARKER_FIELDS" | grep -q '\[no-test\]'; then
          rm -f -- "$MARKER_ERR_FILE"
          exit 0  # Escape hatch: allow close without test evidence
        fi
      else
        MARKER_PARSE_RC=$?
        MARKER_PARSE_ERROR=$(tr '\n' ' ' < "$MARKER_ERR_FILE")
        [ -n "$MARKER_PARSE_ERROR" ] || MARKER_PARSE_ERROR="no parser stderr"
        MARKER_LOOKUP_FAILURE="TDD gate: bd show returned unreadable JSON while checking the no-test escape hatch for $BEAD_ID (parser exit $MARKER_PARSE_RC): $MARKER_PARSE_ERROR. Close remains blocked because no test evidence is available; retry after bd is healthy."
      fi
    else
      MARKER_LOOKUP_RC=$?
      MARKER_LOOKUP_ERROR=$(tr '\n' ' ' < "$MARKER_ERR_FILE")
      [ -n "$MARKER_LOOKUP_ERROR" ] || MARKER_LOOKUP_ERROR="no stderr"
      MARKER_LOOKUP_FAILURE="TDD gate: bd show failed while checking the no-test escape hatch for $BEAD_ID (exit $MARKER_LOOKUP_RC): $MARKER_LOOKUP_ERROR. Close remains blocked because no test evidence is available; retry after bd is healthy."
    fi
    rm -f -- "$MARKER_ERR_FILE"
  else
    MARKER_LOOKUP_FAILURE="TDD gate: could not create a temporary diagnostic file while checking the no-test escape hatch for $BEAD_ID. Close remains blocked because no test evidence is available; retry after the temporary directory is writable."
  fi
fi

# Check for test evidence. Per-agent keying (SABLE-d72): read the SAME key
# tdd-evidence.sh writes — session_id + agent_id for subagents (so worker A's
# test run can't satisfy worker B's close in a shared session), session-global
# for main sessions. Companion convention: workers close their OWN beads after
# green, so the test run and the bd close share one agent context.
EVIDENCE_FILE=$(sable_evidence_key "$SESSION_ID" "$AGENT_ID")
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
  # SABLE-yh1o: derive the glob base via the SAME lib-evidence-key.sh helper
  # the exact-key check above uses, instead of interpolating $SESSION_ID
  # directly. An empty SESSION_ID (the absent-session case jfg6.4 hardened
  # the exact-key path against) previously expanded the raw glob to
  # /tmp/tdd-evidence-* — every session's evidence on the box — so a
  # companion-declared bead in an absent-session close could be satisfied
  # by an unrelated session's REPO= line. Routing through sable_evidence_key
  # gives an absent session its own deterministic ppid-scoped base, matching
  # only this session's (and its agent variants') evidence files.
  #
  # SABLE-bo10: the base alone is still prefix-collidable — a bare
  # "${base}"* glob matches any OTHER session whose key happens to start
  # with this base (ppid-123* matches ppid-1234's evidence file, a
  # different, concurrently-live process), narrowing but not eliminating
  # the yh1o failure shape. Agent variants are always base + "-" + agent_id
  # (see lib-evidence-key.sh), so the only legitimate matches are the exact
  # base itself or base followed by a literal "-" separator. Checking those
  # two forms explicitly — instead of one open-ended glob — closes the
  # prefix collision without narrowing the legitimate agent-variant match.
  _companion_evidence_base=$(sable_evidence_key "$SESSION_ID" "")
  # All positional IDs are already validated by the parser above. Resolve
  # their records in one bd read and extract every declaration in one Python
  # parse; the acceptance rule is additive (ANY bead + ANY same-session
  # evidence), so batching changes no ordering or verdict semantics while
  # removing a Dolt/Python startup per additional bead (SABLE-y4nom.7.6).
  read -r -a _bead_ids <<< "$BEAD_ARGS"
  _companion_json=$(bd show "${_bead_ids[@]}" --json 2>/dev/null) || true
  while IFS= read -r _companion; do
    [ -n "$_companion" ] || continue
    for _f in "${_companion_evidence_base}" "${_companion_evidence_base}"-*; do
      if [ -f "$_f" ] && grep -qF "REPO=${_companion}" "$_f" 2>/dev/null; then
        exit 0
      fi
    done
  done < <(printf '%s' "${_companion_json:-}" | python3 -c "
import json, re, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if isinstance(data, dict):
    data = [data]
if not isinstance(data, list):
    sys.exit(0)
seen = set()
for record in data:
    if not isinstance(record, dict):
        continue
    notes = record.get('notes', '') or ''
    match = re.search(r'Companion repo:\s*(\S+)', str(notes))
    if match and match.group(1) not in seen:
        seen.add(match.group(1))
        print(match.group(1))
" 2>/dev/null)
fi

# No evidence found — block the close
if [ -n "$MARKER_LOOKUP_FAILURE" ]; then
  deny_with_reason "$MARKER_LOOKUP_FAILURE"
else
  deny_with_reason "TDD gate: No tests were run this session. Run your test suite first (npm test, pytest, etc.). For non-code beads: add [no-test] to bead notes and close individually.$MARKER_SCAN_DETAIL"
fi
