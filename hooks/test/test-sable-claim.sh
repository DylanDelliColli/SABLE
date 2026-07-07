#!/usr/bin/env bash
# test-sable-claim.sh — Unit tests for bin/sable-claim (market-brief-package-q6yu)
#
# Contract under test:
#   sable-claim status [<repo>]            prints holder + agent + age + TTL
#   sable-claim release [<repo>] [--force] releases; refuses non-holders
#
# The claim file this CLI reads/writes is the SAME one tree-claim.sh manages
# (<git-dir>/sable-tree-claim, "session_id epoch agent_name"). These tests
# write/inspect it directly rather than driving the hook — the hook's own
# behavior is covered by test-tree-claim.sh.
#
# Run with:
#   bash hooks/test/test-sable-claim.sh

set -uo pipefail

CLAIM_BIN="$(cd "$(dirname "$0")/../.." && pwd)/bin/sable-claim"

if [ ! -x "$CLAIM_BIN" ]; then
  echo "FAIL: sable-claim not executable at $CLAIM_BIN"
  exit 2
fi

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

SCRATCH_ROOT="$(mktemp -d)"
SCRATCH="$SCRATCH_ROOT/repo"
git init "$SCRATCH" -q
git -C "$SCRATCH" commit --allow-empty -m "init" -q
trap 'rm -rf "$SCRATCH_ROOT"' EXIT

claim_file() {
  local gdir
  gdir=$(git -C "$SCRATCH" rev-parse --git-dir 2>/dev/null)
  case "$gdir" in
    /*) ;;
    *)  gdir="$SCRATCH/$gdir" ;;
  esac
  echo "$gdir/sable-tree-claim"
}
CF="$(claim_file)"

write_claim() {
  # $1=session $2=age_seconds_ago $3=agent (optional)
  local session="$1" age="$2" agent="${3:-}"
  local ts=$(( $(date +%s) - age ))
  if [ -n "$agent" ]; then
    printf '%s %s %s\n' "$session" "$ts" "$agent" > "$CF"
  else
    printf '%s %s\n' "$session" "$ts" > "$CF"
  fi
}

# run_status / run_release with a clean env (no ambient CLAUDE_SESSION_ID /
# CLAUDE_AGENT_NAME leaking in from this very test's own session).
run_status() {
  env -u CLAUDE_SESSION_ID -u CLAUDE_AGENT_NAME "$CLAIM_BIN" status "$SCRATCH" 2>&1
}
run_release() {
  # extra args after $SCRATCH (e.g. --force) passed through
  env -u CLAUDE_SESSION_ID -u CLAUDE_AGENT_NAME "$CLAIM_BIN" release "$SCRATCH" "$@" 2>&1
}
run_release_as() {
  # $1=CLAUDE_SESSION_ID $2=CLAUDE_AGENT_NAME (either may be empty), rest passed through
  local sid="$1" aname="$2"; shift 2
  env CLAUDE_SESSION_ID="$sid" CLAUDE_AGENT_NAME="$aname" "$CLAIM_BIN" release "$SCRATCH" "$@" 2>&1
}

# ---------- status ----------

echo "--- status: no claim ---"
rm -f "$CF"
OUT=$(run_status)
if echo "$OUT" | grep -q "no claim"; then pass "status: reports 'no claim' when unclaimed"; else fail "status: reports 'no claim' when unclaimed" "$OUT"; fi

echo "--- status: fresh named claim ---"
write_claim "sess-123" 30 "lincoln"
OUT=$(run_status)
if echo "$OUT" | grep -qF "$CF"; then pass "status: shows the claim file path"; else fail "status: shows the claim file path" "$OUT"; fi
if echo "$OUT" | grep -q "sess-123"; then pass "status: shows the holder session id"; else fail "status: shows the holder session id" "$OUT"; fi
if echo "$OUT" | grep -q "lincoln"; then pass "status: shows the attributable agent name"; else fail "status: shows the attributable agent name" "$OUT"; fi
if echo "$OUT" | grep -qE "age:.*30s"; then pass "status: shows age"; else fail "status: shows age" "$OUT"; fi
if echo "$OUT" | grep -qE "TTL:.*remaining"; then pass "status: shows TTL remaining for a fresh claim"; else fail "status: shows TTL remaining for a fresh claim" "$OUT"; fi

echo "--- status: expired claim ---"
write_claim "sess-old" 7200 "tarzan"
OUT=$(SABLE_TREE_CLAIM_TTL=3600 env -u CLAUDE_SESSION_ID -u CLAUDE_AGENT_NAME "$CLAIM_BIN" status "$SCRATCH" 2>&1)
if echo "$OUT" | grep -qi "expired"; then pass "status: reports expired for a stale claim"; else fail "status: reports expired for a stale claim" "$OUT"; fi

echo "--- status: legacy two-field claim (no agent name) ---"
printf 'sess-legacy %s\n' "$(date +%s)" > "$CF"
OUT=$(run_status)
if echo "$OUT" | grep -q "agent: -"; then pass "status: legacy two-field claim shows agent '-'"; else fail "status: legacy two-field claim shows agent '-'" "$OUT"; fi

echo "--- status: non-repo path ---"
NONGIT="$(mktemp -d)"
if env -u CLAUDE_SESSION_ID -u CLAUDE_AGENT_NAME "$CLAIM_BIN" status "$NONGIT" >/dev/null 2>&1; then
  fail "status: non-repo path errors (nonzero exit)"
else
  pass "status: non-repo path errors (nonzero exit)"
fi
rm -rf "$NONGIT"

# ---------- release ----------

echo "--- release: no claim held ---"
rm -f "$CF"
OUT=$(run_release)
RC=$?
if [ "$RC" -eq 0 ] && echo "$OUT" | grep -qi "nothing to release"; then
  pass "release: no-op with a clear message when nothing is claimed"
else
  fail "release: no-op with a clear message when nothing is claimed" "rc=$RC out=$OUT"
fi

echo "--- release: by holder (matched via CLAUDE_SESSION_ID) ---"
write_claim "sess-abc" 10 "lincoln"
OUT=$(run_release_as "sess-abc" "")
RC=$?
if [ "$RC" -eq 0 ] && [ ! -f "$CF" ]; then
  pass "release: holder (session id match) clears the claim file"
else
  fail "release: holder (session id match) clears the claim file" "rc=$RC file_exists=$([ -f "$CF" ] && echo yes || echo no) out=$OUT"
fi

echo "--- release: by holder (matched via CLAUDE_AGENT_NAME) ---"
write_claim "sess-def" 10 "lincoln"
OUT=$(run_release_as "" "lincoln")
RC=$?
if [ "$RC" -eq 0 ] && [ ! -f "$CF" ]; then
  pass "release: holder (agent name match) clears the claim file"
else
  fail "release: holder (agent name match) clears the claim file" "rc=$RC file_exists=$([ -f "$CF" ] && echo yes || echo no) out=$OUT"
fi

echo "--- release: non-holder refused without --force ---"
write_claim "sess-ghi" 10 "tarzan"
OUT=$(run_release_as "sess-other" "optimus")
RC=$?
if [ "$RC" -ne 0 ] && [ -f "$CF" ]; then
  pass "release: non-holder refused, claim file untouched"
else
  fail "release: non-holder refused, claim file untouched" "rc=$RC file_exists=$([ -f "$CF" ] && echo yes || echo no) out=$OUT"
fi
if echo "$OUT" | grep -qi "refused"; then pass "release: non-holder gets a clear refusal message"; else fail "release: non-holder gets a clear refusal message" "$OUT"; fi
if echo "$OUT" | grep -q -- "--force"; then pass "release: refusal message names the --force override"; else fail "release: refusal message names the --force override" "$OUT"; fi

echo "--- release: non-holder with --force succeeds ---"
write_claim "sess-jkl" 10 "tarzan"
OUT=$(run_release_as "sess-other" "optimus" --force)
RC=$?
if [ "$RC" -eq 0 ] && [ ! -f "$CF" ]; then
  pass "release: non-holder with --force clears the claim (operator override)"
else
  fail "release: non-holder with --force clears the claim (operator override)" "rc=$RC file_exists=$([ -f "$CF" ] && echo yes || echo no) out=$OUT"
fi

echo "--- release: no identity signals at all (no CLAUDE_SESSION_ID / CLAUDE_AGENT_NAME) refused without --force ---"
write_claim "sess-mno" 10 "chuck"
OUT=$(run_release)
RC=$?
if [ "$RC" -ne 0 ] && [ -f "$CF" ]; then
  pass "release: no identity signals, no --force → refused, claim untouched"
else
  fail "release: no identity signals, no --force → refused, claim untouched" "rc=$RC file_exists=$([ -f "$CF" ] && echo yes || echo no) out=$OUT"
fi
rm -f "$CF"

echo "--- release: non-repo path ---"
NONGIT2="$(mktemp -d)"
if env -u CLAUDE_SESSION_ID -u CLAUDE_AGENT_NAME "$CLAIM_BIN" release "$NONGIT2" >/dev/null 2>&1; then
  fail "release: non-repo path errors (nonzero exit)"
else
  pass "release: non-repo path errors (nonzero exit)"
fi
rm -rf "$NONGIT2"

# ---------- --help ----------

echo "--- --help ---"
if "$CLAIM_BIN" --help 2>&1 | grep -q "sable-claim status"; then
  pass "--help: prints usage"
else
  fail "--help: prints usage"
fi

# ---------- Summary ----------

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then
  printf "Failed tests:%b\n" "$FAIL_NAMES"
  exit 1
fi
exit 0
