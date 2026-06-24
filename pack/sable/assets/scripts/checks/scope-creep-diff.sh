#!/usr/bin/env bash
set -euo pipefail

# SABLE scope-creep gate (SABLE-vj4x.3 / implements SABLE-bijh).
#
# Asserts that every file a worker actually changed (the implementation-summary
# "Changed Files" section) falls within the bead's DECLARED scope. Out-of-scope
# edits fail the gate. This is the pre-merge "worker touched only what the bead
# said it would" check that SABLE documented but never built.
#
# Escapes:
#   [scope-override] <reason>  - in the summary, waives the gate intentionally
#   empty/absent declared scope - FAILS OPEN (warn + pass). Phase 1: the base
#                                 decomposition does not record per-bead scope;
#                                 sable-decomposition (Phase 3) will, at which
#                                 point this gate becomes fully enforcing.
#
# Declared scope entries are path prefixes: "src/foo" matches "src/foo/bar.py"
# and exactly "src/foo"; "src/foo.py" matches only that file.
#
# Resolution: production reads $GC_BEAD_ID -> implementation-summary artifact +
# the bead's gc.scope.allowed_paths metadata. Tests pass $1=summary_path and
# $2=comma-separated scope to bypass bd. This gate never prompts.

fail() {
  echo "scope-creep-check: $*" >&2
  exit 1
}

declared=()

in_scope() {
  local c="$1" d
  for d in "${declared[@]}"; do
    [ "$c" = "$d" ] && return 0
    case "$c" in
      "$d"/*) return 0 ;;
    esac
  done
  return 1
}

run_check() {
  local path="$1" scope="$2"
  [ -f "$path" ] || fail "implementation-summary artifact not found: $path"

  if grep -qiE '\[scope-override\]' "$path"; then
    echo "scope-creep: [scope-override] honored ($path)"
    return 0
  fi

  # Fail open when no real scope is declared.
  if [ -z "$(printf '%s' "$scope" | tr -d '[:space:],')" ]; then
    echo "scope-creep: no declared scope on the bead; failing open (Phase 1 — sable-decomposition will populate per-bead scope and make this enforcing)"
    return 0
  fi

  local d
  declared=()
  IFS=',' read -r -a _raw <<<"$scope"
  for d in "${_raw[@]}"; do
    d="$(printf '%s' "$d" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's:/*$::')"
    [ -n "$d" ] && declared+=("$d")
  done
  if [ "${#declared[@]}" -eq 0 ]; then
    echo "scope-creep: declared scope resolved empty; failing open"
    return 0
  fi

  # Extract changed-file paths from the "## Changed Files" section.
  local changed
  changed="$(awk '
    /^##[[:space:]]+Changed Files[[:space:]]*$/ { inblock=1; next }
    /^##[[:space:]]/ { inblock=0 }
    inblock {
      line=$0
      sub(/^[[:space:]]*[-*][[:space:]]*/, "", line)
      gsub(/`/, "", line)
      sub(/^[[:space:]]+/, "", line)
      if (line == "") next
      n=split(line, a, /[[:space:]]+/)
      print a[1]
    }
  ' "$path")"

  local offenders="" c
  while IFS= read -r c; do
    [ -n "$c" ] || continue
    in_scope "$c" || offenders="${offenders:+$offenders }$c"
  done <<<"$changed"

  if [ -n "$offenders" ]; then
    fail "out-of-scope files changed: ${offenders}. Declared scope: ${declared[*]}. A worker may touch only files within the bead's declared scope; widen the bead's scope, split the work, or record '[scope-override] <reason>' in the summary."
  fi
  echo "scope-creep: all changed files within declared scope (${declared[*]})"
  return 0
}

# --- Path / scope resolution ----------------------------------------------

if [ "$#" -ge 1 ]; then
  run_check "$1" "${2:-}"
  exit 0
fi

BEAD_ID="${GC_BEAD_ID:-}"
[ -n "$BEAD_ID" ] || fail "GC_BEAD_ID is required (or pass summary path + scope arguments)"
command -v bd >/dev/null 2>&1 || fail "bd is required on PATH"
command -v python3 >/dev/null 2>&1 || fail "python3 is required on PATH"

metadata_value() {
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

SUMMARY_PATH=""
for key in gc.implementation.summary_path gc.build.implementation_summary_path gc.var.summary_path; do
  value="$(metadata_value "$ROOT_JSON" "$key")"
  [ -n "$value" ] && { SUMMARY_PATH="$value"; break; }
done
[ -n "$SUMMARY_PATH" ] || fail "no implementation-summary path recorded on workflow root ${ROOT_ID:-$BEAD_ID}"
case "$SUMMARY_PATH" in
  /*) ;;
  *)
    [ -n "${GC_WORK_DIR:-}" ] || fail "summary path $SUMMARY_PATH is relative and GC_WORK_DIR is unset"
    SUMMARY_PATH="$GC_WORK_DIR/$SUMMARY_PATH"
    ;;
esac

# Declared scope: prefer the work-item bead, then the workflow root.
SCOPE="$(metadata_value "$SHOW_JSON" "gc.scope.allowed_paths")"
[ -n "$SCOPE" ] || SCOPE="$(metadata_value "$ROOT_JSON" "gc.scope.allowed_paths")"

run_check "$SUMMARY_PATH" "$SCOPE"
