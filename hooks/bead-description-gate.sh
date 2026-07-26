#!/usr/bin/env bash
# bead-description-gate.sh — Validate bead descriptions on creation
# Trigger: PreToolUse on Bash (matching bd create) | Timeout: 3000ms
#
# Two-mode operation:
#   - Manager mode (CLAUDE_AGENT_ROLE=manager OR CLAUDE_AGENT_NAME set):
#       Hard-block (deny) on missing required content. Multi-manager pattern
#       depends on bead descriptions reliably naming files; nudge isn't enough.
#   - Default mode (no agent identity): nudge via additionalContext.
#
# Label-aware: when --labels includes sherlock-finding, additional sections
# from templates/sherlock-bead.md are required (Rationale, Evidence with
# Fingerprint, Proposed approach, Scope estimate, Risk if not addressed).
# When --labels includes columbo-test-spec, sections from
# templates/columbo-bead.md are required (Feature under test, Test file,
# Cases with Why:, Categories, Fixtures / setup, Out of scope). When
# --labels includes columbo-test-gap, the audit-mode required sections
# are enforced (Symptom, Cited test file, Cited source file, Fingerprint,
# Cases to add, Categories, Risk if not addressed). These are the contracts
# each agent commits to in its role file.

set -euo pipefail

# origin: taxonomy read path (SABLE-8b41.1 foundation; consumed by the
# soft-nudge check landing in SABLE-8b41.7). Single source of truth is
# bin/sable_telemetry_lib.py's ORIGIN_LABELS constant — this hook never
# hardcodes a second copy (the Shotgun Surgery risk flagged in
# .claude/sable/state/planning/SABLE-8b41/architecture.json); it shells out
# to the CLI's --print-origin-labels accessor instead. Checked first, before
# anything below reads stdin, so it never blocks waiting on a pipe. Prefers
# the in-repo bin/sable-telemetry (dev checkout / this repo's own worktrees)
# and falls back to the installed CLI on PATH (~/.local/bin, post
# sable-bin-install) since an installed hook lives under ~/.claude/hooks,
# separate from the installed bin/ directory.
if [ "${1:-}" = "--print-origin-labels" ]; then
  REPO_BIN="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." 2>/dev/null && pwd)/bin/sable-telemetry"
  if [ -x "$REPO_BIN" ]; then
    exec "$REPO_BIN" --print-origin-labels
  else
    exec sable-telemetry --print-origin-labels
  fi
fi

PARSED=$(python3 -c "
import json, sys
d = json.load(sys.stdin)
cmd = d.get('tool_input', {}).get('command', '')
print(cmd)
" 2>/dev/null) || exit 0

COMMAND="$PARSED"
[ -z "$COMMAND" ] && exit 0

# Only act on bd create
echo "$COMMAND" | grep -q '^bd create' || exit 0

# Skip epics — they don't need test specs / file paths
echo "$COMMAND" | grep -qiE -- '--type[= ]?epic' && exit 0

# Determine enforcement mode
if [ -n "${CLAUDE_AGENT_NAME:-}" ] || [ "${CLAUDE_AGENT_ROLE:-}" = "manager" ]; then
  MODE="block"
else
  MODE="nudge"
fi

# Extract --labels or --label value (comma-separated list)
LABELS=$(echo "$COMMAND" | python3 -c "
import sys, re
cmd = sys.stdin.read()
m = (
    re.search(r'--labels?[= ]\"([^\"]+)\"', cmd)
    or re.search(r\"--labels?[= ]'([^']+)'\", cmd)
    or re.search(r'--labels?[= ]([^\s\"\']+)', cmd)
)
print(m.group(1) if m else '')
" 2>/dev/null || echo "")

# origin: intake-attribution soft-nudge (SABLE-8b41.7). WARN only — never
# deny, in ANY mode (including manager/block) — architecture decision:
# preserves the instant `bd q` quick-capture flow the Prime Directive
# depends on. Checked independently of MISSING_LIST/append_missing() so it
# can never join the block-mode deny path below.
ORIGIN_PRESENT=0
if echo ",$LABELS," | grep -qE ',origin:[^,]+,'; then
  ORIGIN_PRESENT=1
fi

SHERLOCK_FINDING=0
if echo ",$LABELS," | grep -q ',sherlock-finding,'; then
  SHERLOCK_FINDING=1
fi

COLUMBO_SPEC=0
if echo ",$LABELS," | grep -q ',columbo-test-spec,'; then
  COLUMBO_SPEC=1
fi

COLUMBO_GAP=0
if echo ",$LABELS," | grep -q ',columbo-test-gap,'; then
  COLUMBO_GAP=1
fi

# Detect batch/file description modes early — content lives in a file or
# structured format, not inline in the --description flag.
#
# --body-file <path>  (not "-"): read the file and apply the same quality
#                     checks against its content.
# --body-file -       : content comes from stdin at runtime — unavailable
#                     here; exempt from hard-deny, emit nudge.
# --graph <file>      : batch import — structured JSON; exempt/nudge.
# --file <file>       : batch create from markdown file; exempt/nudge.
# --stdin             : content piped in at runtime; exempt/nudge.

BODY_FILE=$(echo "$COMMAND" | python3 -c "
import sys, re
cmd = sys.stdin.read()
m = re.search(r'--body-file[= ]\"([^\"]+)\"', cmd) \
    or re.search(r\"--body-file[= ]'([^']+)'\", cmd) \
    or re.search(r'--body-file[= ](\S+)', cmd)
print(m.group(1) if m else '')
" 2>/dev/null || echo "")

HAS_GRAPH=0
echo "$COMMAND" | grep -qE -- '--graph(\s|=)' && HAS_GRAPH=1

HAS_FILE_FLAG=0
echo "$COMMAND" | grep -qE -- '(--file(\s|=)|(^|\s)-f(\s|=))' && HAS_FILE_FLAG=1

HAS_STDIN=0
echo "$COMMAND" | grep -q -- '--stdin' && HAS_STDIN=1

# --body-file <real path>: read and quality-check the file content
if [ -n "$BODY_FILE" ] && [ "$BODY_FILE" != "-" ]; then
  if [ ! -f "$BODY_FILE" ]; then
    # File doesn't exist yet — can't check; let it through with a nudge
    python3 -c "
import json
print(json.dumps({
    'additionalContext': 'SABLE bead quality: --body-file path does not exist yet; quality check skipped. Ensure the file passes the Fresh Agent Test (file paths, test spec, acceptance criteria) before creating.'
}))
"
    exit 0
  fi
  # Read the file content and use it as DESC for quality checks below
  DESC=$(python3 -c "
import sys
try:
    with open(sys.argv[1]) as f:
        print(f.read())
except Exception as e:
    print('')
" "$BODY_FILE" 2>/dev/null || echo "")
  # Fall through to quality checks with DESC set from file
elif [ -n "$BODY_FILE" ] || [ "$HAS_GRAPH" = "1" ] || [ "$HAS_FILE_FLAG" = "1" ] || [ "$HAS_STDIN" = "1" ]; then
  # Batch/stdin modes — content unavailable at hook time; nudge only, never deny
  python3 -c "
import json
print(json.dumps({
    'additionalContext': 'SABLE bead quality: bd create uses a batch/stdin mode (--graph/--file/--stdin/--body-file -). Quality check skipped at hook time. Ensure each bead in the batch passes the Fresh Agent Test: file paths, function names, what to change, test file reference, acceptance criteria.'
}))
"
  exit 0
else
  # No batch mode — require --description inline

  # Extract description content (between quotes after --description or -d)
  # Anchor on word boundary: (?:--description|-d(?=[ =])) to prevent
  # a literal " -d " inside a quoted description string from confusing
  # extraction — the outer flag must appear as a standalone token.
  DESC=$(echo "$COMMAND" | python3 -c "
import sys, re
cmd = sys.stdin.read()
# Match --description or -d (short alias), both space and equals forms.
# The negative-lookbehind on -d ensures we only match it as a standalone
# flag (preceded by start-of-string or whitespace), not inside a value.
flag_pat = r'(?:--description|(?<![^\s])-d)(?:\s|=)'
m = re.search(flag_pat + r'\"((?:[^\"\\\\]|\\\\.)*)\"', cmd, re.DOTALL) \
    or re.search(flag_pat + r\"'((?:[^'\\\\]|\\\\.)*)'\" , cmd, re.DOTALL)
print(m.group(1) if m else '')
" 2>/dev/null || echo "")

  # No --description / -d at all
  if ! echo "$COMMAND" | grep -qE -- '(--description|(^|\s)-d(\s|=))'; then
    if [ "$MODE" = "block" ]; then
      python3 -c "
import json
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': 'SABLE bead quality: bd create has no --description flag. Manager context requires a description that passes the Fresh Agent Test (file paths, test spec, acceptance criteria). Add --description and retry.'
    }
}))
"
      exit 0
    else
      python3 -c "
import json
print(json.dumps({
    'additionalContext': 'SABLE bead quality: This bd create has no --description flag. Every bead needs a description that passes the Fresh Agent Test: file paths, function names, what to change, test file path, and acceptance criteria.'
}))
"
      exit 0
    fi
  fi

  # Description present but empty
  if [ -z "$DESC" ]; then
    exit 0
  fi
fi

# Guard: if DESC is still empty after all paths, exit clean
if [ -z "$DESC" ]; then
  exit 0
fi

# Build the missing-sections list
MISSING_LIST=""

append_missing() {
  if [ -z "$MISSING_LIST" ]; then
    MISSING_LIST="$1"
  else
    MISSING_LIST="$MISSING_LIST; $1"
  fi
}

# get_origin_labels_hint — reads the origin: taxonomy from the single source
# (bin/sable_telemetry_lib.py's ORIGIN_LABELS, via sable-telemetry
# --print-origin-labels) so the nudge text never hardcodes a second copy
# (Shotgun Surgery risk, architecture.json). Same repo-then-PATH resolution
# as the --print-origin-labels bypass above. Never fails the hook: any
# missing/broken CLI just yields an empty hint under set -e.
get_origin_labels_hint() {
  local repo_bin hint
  repo_bin="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." 2>/dev/null && pwd)/bin/sable-telemetry"
  if [ -x "$repo_bin" ]; then
    hint=$("$repo_bin" --print-origin-labels 2>/dev/null | tr '\n' ',' | sed 's/,$//') || hint=""
  else
    hint=$(sable-telemetry --print-origin-labels 2>/dev/null | tr '\n' ',' | sed 's/,$//') || hint=""
  fi
  echo "$hint"
}

# Sherlock-finding additional checks (only if labeled)
if [ "$SHERLOCK_FINDING" = "1" ]; then
  echo "$DESC" | grep -qE '^## Rationale' \
    || append_missing "## Rationale section"
  echo "$DESC" | grep -qE 'Fingerprint:' \
    || append_missing "Evidence with at least one Fingerprint: line"
  echo "$DESC" | grep -qE '^## Proposed approach' \
    || append_missing "## Proposed approach section"
  echo "$DESC" | grep -qE '^## Scope estimate' \
    || append_missing "## Scope estimate section"
  echo "$DESC" | grep -qE '^## Risk if not addressed' \
    || append_missing "## Risk if not addressed section"
fi

# Columbo-test-spec additional checks (forward-mode output, only if labeled)
if [ "$COLUMBO_SPEC" = "1" ]; then
  echo "$DESC" | grep -qE '^## Feature under test' \
    || append_missing "## Feature under test section"
  echo "$DESC" | grep -qE '^## Test file' \
    || append_missing "## Test file section"
  echo "$DESC" | grep -qE '^## Test layer' \
    || append_missing "## Test layer section (UNIT | E2E | EVAL)"
  echo "$DESC" | grep -qE '^## Cases' \
    || append_missing "## Cases section"
  # Cases must contain at least one bullet with a Why: sub-line
  echo "$DESC" | grep -qE '(Why|why):' \
    || append_missing "## Cases must include at least one Why: sub-line per case"
  echo "$DESC" | grep -qE '^## Categories' \
    || append_missing "## Categories section"
  echo "$DESC" | grep -qE '^## Fixtures' \
    || append_missing "## Fixtures / setup section (use 'Fixtures: none.' if no setup)"
  echo "$DESC" | grep -qE '^## Out of scope' \
    || append_missing "## Out of scope section"
fi

# Columbo-test-gap additional checks (audit-mode output, only if labeled)
if [ "$COLUMBO_GAP" = "1" ]; then
  echo "$DESC" | grep -qE '^## Symptom' \
    || append_missing "## Symptom section"
  echo "$DESC" | grep -qE '^## Cited test file' \
    || append_missing "## Cited test file section"
  echo "$DESC" | grep -qE '^## Cited source file' \
    || append_missing "## Cited source file section"
  echo "$DESC" | grep -qE '^## Existing test quality' \
    || append_missing "## Existing test quality section (★/★★/★★★ grade or 'none — net-new test required')"
  echo "$DESC" | grep -qE '^## Fingerprint' \
    || append_missing "## Fingerprint section (literal substring grep-able from cited file)"
  echo "$DESC" | grep -qE '^## Cases to add' \
    || append_missing "## Cases to add section"
  echo "$DESC" | grep -qE '^## Categories' \
    || append_missing "## Categories section"
  echo "$DESC" | grep -qE '^## Risk if not addressed' \
    || append_missing "## Risk if not addressed section"
fi

# Standard checks (apply to all non-epic beads)
if ! echo "$DESC" | grep -qiE '(test|\.test\.|\.spec\.|__tests__|pytest|vitest|TDD|red.green|\[no-test\])'; then
  append_missing "test spec (which test file, what assertions)"
fi

# Extensionless build files (Makefile/Dockerfile/Justfile/Rakefile) are matched by name since
# they have no extension to catch on. Bare extensionless BIN NAMES (e.g. sable-msg) are
# deliberately NOT matched here (SABLE-i0db) — cite them via their bin/ prefix (bin/sable-msg),
# which already passes on the bin/ alternation above. Widening this to bare bin names would trade
# away the gate's real quality property (paths, not bare names) for convenience; do not "fix" this.
if ! echo "$DESC" | grep -qiE '(\.(ts|tsx|py|js|jsx|sh|go|rs|rb|md|json|yaml|yml|toml|kdl|cfg|ini|txt)|frontend/|src/|lib/|components/|hooks/|templates/|docs/|feedback/|bin/|location-briefing/|\.[a-zA-Z][a-zA-Z0-9_-]*/|\b(Makefile|Dockerfile|Justfile|Rakefile)\b)'; then
  append_missing "file paths (exact files to create/modify)"
fi

# Pass on the standard/label checks — still apply the origin: soft nudge.
# This fires in EVERY mode, including manager/block, because origin is a
# warn-only concern (never a deny reason): a bead with everything else in
# order but no origin: label must still be created without friction.
if [ -z "$MISSING_LIST" ]; then
  if [ "$ORIGIN_PRESENT" = "0" ]; then
    ORIGIN_HINT="$(get_origin_labels_hint)"
    ORIGIN_HINT="$ORIGIN_HINT" python3 -c "
import json, os
hint = os.environ.get('ORIGIN_HINT', '')
values = f' Valid values: {hint}.' if hint else ''
print(json.dumps({
    'additionalContext': 'SABLE bead quality: no origin: label found (e.g. origin:planned).' + values + ' Soft nudge only — creation is never blocked; add one when convenient to improve intake-attribution telemetry.'
}))
"
  fi
  exit 0
fi

# Emit verdict based on mode
if [ "$MODE" = "block" ]; then
  if [ "$SHERLOCK_FINDING" = "1" ]; then
    REASON="SABLE bead quality (sherlock-finding): Description missing required sections per templates/sherlock-bead.md — $MISSING_LIST. Fix the description and retry. Sherlock findings have a higher quality bar than the default Fresh Agent Test."
  elif [ "$COLUMBO_SPEC" = "1" ]; then
    REASON="SABLE bead quality (columbo-test-spec): Description missing required sections per templates/columbo-bead.md — $MISSING_LIST. Fix the description and retry. Columbo test-spec beads form the worker's contract — the skeleton file plus the bead's Cases section together specify what must be tested."
  elif [ "$COLUMBO_GAP" = "1" ]; then
    REASON="SABLE bead quality (columbo-test-gap): Description missing required sections per templates/columbo-bead.md — $MISSING_LIST. Fix the description and retry. Columbo gap beads must include a Fingerprint to survive line drift between audit and execution."
  else
    REASON="SABLE bead quality: Description missing — $MISSING_LIST. Manager context requires beads pass the Fresh Agent Test before creation. Add the missing sections and retry."
  fi
  MISSING_LIST="$MISSING_LIST" REASON="$REASON" python3 -c "
import json, os
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'permissionDecision': 'deny',
        'permissionDecisionReason': os.environ.get('REASON', '')
    }
}))
"
else
  MISSING_LIST="$MISSING_LIST" python3 -c "
import json, os
m = os.environ.get('MISSING_LIST', '')
print(json.dumps({
    'additionalContext': f'SABLE bead quality: Description is missing: {m}. Good beads include file paths, function names, test file references, and acceptance criteria so agents can act immediately without re-exploring.'
}))
"
fi
