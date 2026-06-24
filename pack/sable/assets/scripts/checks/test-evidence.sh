#!/usr/bin/env bash
set -euo pipefail

# SABLE test-evidence gate (SABLE-vj4x.2).
#
# Mechanically asserts that a work item's implementation-summary records BOTH a
# unit-test proof AND an integration-test proof in its Verification section (the
# SABLE Prime Directive). This is the self-report-INDEPENDENT complement to the
# base 3-verdict review gate (gascity .../checks/implementation-review-approved.sh),
# which carries an *agent* code_review.test_evidence_verdict; SABLE does not
# trust the agent's verdict alone and verifies the artifact mechanically.
#
# Convention (enforced by the SABLE implementation-worker prompt): the
# Verification section labels its proofs, e.g.
#   - Unit tests: <command + result>
#   - Integration tests: <command + result>
# Escapes (for genuinely non-applicable work):
#   [no-integration] <reason>  - waives the integration proof, unit still required
#   [no-test] <reason>         - waives all test evidence (docs-only / non-code)
#
# Resolution: production reads $GC_BEAD_ID -> workflow root -> the
# implementation-summary artifact path (same mechanism as build-artifact-valid.sh).
# Tests pass a summary path as $1 to bypass bd. This gate never prompts.

fail() {
  echo "test-evidence-check: $*" >&2
  exit 1
}

validate_summary() {
  local path="$1"
  [ -f "$path" ] || fail "implementation-summary artifact not found: $path"

  # [no-test] escape: non-code work, no test evidence required.
  if grep -qiE '\[no-test\]' "$path"; then
    echo "test-evidence: [no-test] escape honored ($path)"
    return 0
  fi

  local unit_ok=1 integ_ok=1
  grep -qiE 'unit[ -]?tests?' "$path" || unit_ok=0
  if ! grep -qiE 'integration[ -]?tests?' "$path"; then
    # integration proof absent; allow only an explicit [no-integration] reason.
    grep -qiE '\[no-integration\]' "$path" || integ_ok=0
  fi

  if [ "$unit_ok" -eq 1 ] && [ "$integ_ok" -eq 1 ]; then
    echo "test-evidence: unit + integration proof present ($path)"
    return 0
  fi

  local missing=""
  [ "$unit_ok" -eq 0 ] && missing="unit"
  [ "$integ_ok" -eq 0 ] && missing="${missing:+$missing, }integration"
  fail "implementation-summary $path is missing required test evidence: ${missing}. SABLE requires BOTH a unit-test and an integration-test proof in the Verification section (Prime Directive). Record the proof, or use '[no-integration] <reason>' / '[no-test] <reason>' for genuinely non-applicable work."
}

# --- Path resolution -------------------------------------------------------

# Test/override path: a positional summary path bypasses bd resolution.
if [ "$#" -ge 1 ] && [ -n "${1:-}" ]; then
  validate_summary "$1"
  exit 0
fi

BEAD_ID="${GC_BEAD_ID:-}"
[ -n "$BEAD_ID" ] || fail "GC_BEAD_ID is required (or pass a summary path argument)"
command -v bd >/dev/null 2>&1 || fail "bd is required on PATH"
command -v python3 >/dev/null 2>&1 || fail "python3 is required on PATH"

metadata_value() {
  # metadata_value <json> <key> -> prints metadata[key] or empty
  printf '%s' "$1" | python3 -c '
import json
import sys

key = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    print("")
    raise SystemExit(0)
if isinstance(data, list):
    data = data[0] if data else {}
if not isinstance(data, dict):
    print("")
    raise SystemExit(0)
metadata = data.get("metadata") or {}
value = metadata.get(key, "") if isinstance(metadata, dict) else ""
print(value if isinstance(value, str) else "")
' "$2"
}

SHOW_JSON="$(bd show "$BEAD_ID" --json 2>/dev/null)" || fail "bd show $BEAD_ID failed"

ROOT_ID="$(metadata_value "$SHOW_JSON" "gc.root_bead_id")"
ROOT_JSON="$SHOW_JSON"
if [ -n "$ROOT_ID" ] && [ "$ROOT_ID" != "$BEAD_ID" ]; then
  ROOT_JSON="$(bd show "$ROOT_ID" --json 2>/dev/null)" || fail "bd show $ROOT_ID failed"
fi

# Same artifact-path-key precedence as the base ITEM_SUMMARY_GATE.
PATH_KEYS="gc.implementation.summary_path,gc.build.implementation_summary_path,gc.var.summary_path"
SUMMARY_PATH=""
RESOLVED_KEY=""
IFS=',' read -r -a KEYS <<<"$PATH_KEYS"
for key in "${KEYS[@]}"; do
  key="$(printf '%s' "$key" | tr -d '[:space:]')"
  [ -n "$key" ] || continue
  value="$(metadata_value "$ROOT_JSON" "$key")"
  if [ -n "$value" ]; then
    SUMMARY_PATH="$value"
    RESOLVED_KEY="$key"
    break
  fi
done
[ -n "$SUMMARY_PATH" ] || fail "no implementation-summary path recorded on workflow root ${ROOT_ID:-$BEAD_ID}; tried metadata keys: $PATH_KEYS"

case "$SUMMARY_PATH" in
  /*) ;;
  *)
    [ -n "${GC_WORK_DIR:-}" ] || fail "summary path $SUMMARY_PATH from $RESOLVED_KEY is relative and GC_WORK_DIR is unset"
    SUMMARY_PATH="$GC_WORK_DIR/$SUMMARY_PATH"
    ;;
esac

validate_summary "$SUMMARY_PATH"
