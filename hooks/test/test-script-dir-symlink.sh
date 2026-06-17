#!/usr/bin/env bash
# test-script-dir-symlink.sh — SABLE-5p2 regression. bin/sable-note must resolve
# its own physical path (follow symlinks) before deriving the default feedback
# dir, so a symlinked install (~/.local/bin/sable-note -> repo bin/sable-note)
# still writes to the repo feedback/ dir rather than the symlink's parent.
#
# Run with:
#   bash hooks/test/test-script-dir-symlink.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
NOTE="$REPO/bin/sable-note"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

# --- Unit: `--dir` via a symlink resolves to the repo feedback dir ---
# (no SABLE_FEEDBACK_DIR override — we are testing the DEFAULT resolution).
mkdir -p "$SCRATCH/linkdir"
ln -s "$NOTE" "$SCRATCH/linkdir/sable-note"
GOT_DIR="$(unset SABLE_FEEDBACK_DIR; bash "$SCRATCH/linkdir/sable-note" --dir 2>/dev/null)"
if [ "$GOT_DIR" = "$REPO/feedback" ]; then
  pass "sable-note --dir via symlink resolves to repo feedback (SABLE-5p2)"
else
  fail "sable-note --dir via symlink resolves to repo feedback (SABLE-5p2)" \
       "expected '$REPO/feedback', got '${GOT_DIR:-<empty>}'"
fi

# --- Integration: an append via a symlink lands in the real script's repo, not
# the symlink's parent. Use a scratch copy so the real repo feedback/ is never
# touched. ---
mkdir -p "$SCRATCH/repo/bin" "$SCRATCH/links"
cp "$NOTE" "$SCRATCH/repo/bin/sable-note"
chmod +x "$SCRATCH/repo/bin/sable-note"
ln -s "$SCRATCH/repo/bin/sable-note" "$SCRATCH/links/sable-note"

( unset SABLE_FEEDBACK_DIR; bash "$SCRATCH/links/sable-note" "symlink integration note" >/dev/null 2>&1 )

TODAY="$(date +%Y-%m-%d)"
REPO_FILE="$SCRATCH/repo/feedback/${TODAY}.md"
STRAY_DIR="$SCRATCH/feedback"   # where the pre-fix bug (symlink's ../feedback) would write

if [ -f "$REPO_FILE" ] && grep -q "symlink integration note" "$REPO_FILE"; then
  pass "append via symlink lands in the script's own repo feedback dir"
else
  fail "append via symlink lands in the script's own repo feedback dir" \
       "expected note in $REPO_FILE"
fi

if [ ! -d "$STRAY_DIR" ]; then
  pass "append via symlink does NOT write to the symlink's parent dir"
else
  fail "append via symlink does NOT write to the symlink's parent dir" \
       "stray feedback dir created at $STRAY_DIR"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
