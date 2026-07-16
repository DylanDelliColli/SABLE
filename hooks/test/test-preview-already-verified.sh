#!/usr/bin/env bash
# test-preview-already-verified.sh — SABLE-r3i6: unit-tests the ci-verify
# tmux-only dedup guard (.github/ci/preview-already-verified.sh) by stubbing
# 'gh' on PATH so no real GitHub API call is made. Covers the four cases from
# the bead's test spec: verified-success, no-runs, only-failed-preview-run,
# and gh-error (fail-open).
#
# Run with:
#   bash hooks/test/test-preview-already-verified.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
GUARD="$REPO/.github/ci/preview-already-verified.sh"
STUBDIR=""

cleanup() {
  [ -n "$STUBDIR" ] && rm -rf "$STUBDIR" 2>/dev/null || true
}
trap cleanup EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

SHA="deadbeef1234567890deadbeef1234567890dead"

make_stub_gh() {
  # $1 = body written to the stub gh script (defines what `gh api ...` does)
  STUBDIR="$(mktemp -d)"
  cat > "$STUBDIR/gh" <<EOF
#!/usr/bin/env bash
$1
EOF
  chmod +x "$STUBDIR/gh"
}

run_guard() {
  PATH="$STUBDIR:$PATH" "$GUARD" "$SHA"
}

# --- (a) a successful ci-verify/** run for the SHA -> 'verified' exit 0 ---
make_stub_gh 'cat <<JSON
{
  "workflow_runs": [
    {
      "head_sha": "'"$SHA"'",
      "name": "ci-verify",
      "head_branch": "ci-verify/SABLE-r3i6-abc1234",
      "status": "completed",
      "conclusion": "success"
    }
  ]
}
JSON'
OUT="$(run_guard)"; RC=$?
if [ $RC -eq 0 ] && [ "$OUT" = "verified ci-verify/SABLE-r3i6-abc1234" ]; then
  pass "(a) successful preview run -> verified, exit 0"
else
  fail "(a) successful preview run -> verified, exit 0" "got rc=$RC out='$OUT'"
fi
rm -rf "$STUBDIR"

# --- (b) no runs -> 'unverified' exit 1 ---
make_stub_gh 'echo "{\"workflow_runs\": []}"'
OUT="$(run_guard)"; RC=$?
if [ $RC -eq 1 ] && [ "$OUT" = "unverified" ]; then
  pass "(b) no runs -> unverified, exit 1"
else
  fail "(b) no runs -> unverified, exit 1" "got rc=$RC out='$OUT'"
fi
rm -rf "$STUBDIR"

# --- (c) only a FAILED preview run -> 'unverified' exit 1 ---
make_stub_gh 'cat <<JSON
{
  "workflow_runs": [
    {
      "head_sha": "'"$SHA"'",
      "name": "ci-verify",
      "head_branch": "ci-verify/SABLE-r3i6-abc1234",
      "status": "completed",
      "conclusion": "failure"
    }
  ]
}
JSON'
OUT="$(run_guard)"; RC=$?
if [ $RC -eq 1 ] && [ "$OUT" = "unverified" ]; then
  pass "(c) only a failed preview run -> unverified, exit 1"
else
  fail "(c) only a failed preview run -> unverified, exit 1" "got rc=$RC out='$OUT'"
fi
rm -rf "$STUBDIR"

# --- (d) gh error/exit nonzero -> 'unverified' exit 1 (fail-open) ---
make_stub_gh 'echo "gh: some API error" >&2; exit 1'
OUT="$(run_guard)"; RC=$?
if [ $RC -eq 1 ] && [ "$OUT" = "unverified" ]; then
  pass "(d) gh error -> unverified, exit 1 (fail-open)"
else
  fail "(d) gh error -> unverified, exit 1 (fail-open)" "got rc=$RC out='$OUT'"
fi
rm -rf "$STUBDIR"

# --- bonus: a successful run on a NON-preview branch (e.g. tmux-only itself)
#     must NOT count as verification — only ci-verify/** previews count ---
make_stub_gh 'cat <<JSON
{
  "workflow_runs": [
    {
      "head_sha": "'"$SHA"'",
      "name": "ci-verify",
      "head_branch": "tmux-only",
      "status": "completed",
      "conclusion": "success"
    }
  ]
}
JSON'
OUT="$(run_guard)"; RC=$?
if [ $RC -eq 1 ] && [ "$OUT" = "unverified" ]; then
  pass "(e) successful run on tmux-only itself does not self-verify"
else
  fail "(e) successful run on tmux-only itself does not self-verify" "got rc=$RC out='$OUT'"
fi
rm -rf "$STUBDIR"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then exit 1; fi
exit 0
