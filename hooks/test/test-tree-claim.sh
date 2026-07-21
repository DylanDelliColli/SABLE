#!/usr/bin/env bash
# test-tree-claim.sh — Unit + integration tests for hooks/multi-manager/tree-claim.sh
#
# The hook is exercised for real via stdin (no mocking).
# Unit tests use a scratch git repo in a mktemp directory.
# Integration tests use a real `git worktree add` to prove per-checkout
# claim independence (each worktree gets its own independent claim file).
#
# Run with:
#   bash hooks/test/test-tree-claim.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO/hooks/multi-manager/tree-claim.sh"

if [ ! -f "$HOOK" ]; then
  echo "FAIL: hook not found at $HOOK"
  exit 2
fi
chmod +x "$HOOK"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

# Scratch repo (unit tests live here)
SCRATCH_ROOT="$(mktemp -d)"
SCRATCH="$SCRATCH_ROOT/repo"
git init "$SCRATCH" -q
git -C "$SCRATCH" commit --allow-empty -m "init" -q
trap 'rm -rf "$SCRATCH_ROOT"' EXIT

# Build PreToolUse:Bash JSON for the hook
# make_json <command> <session_id> <cwd>
make_json() {
  python3 -c "
import json, sys
cmd, sid, cwd = sys.argv[1], sys.argv[2], sys.argv[3]
d = {'tool_name': 'Bash', 'tool_input': {'command': cmd}, 'cwd': cwd}
if sid:
    d['session_id'] = sid
print(json.dumps(d))
" "$1" "$2" "$3"
}

# run_hook <json_string>
# Optional env vars set by the caller (e.g. SABLE_TREE_CLAIM_OVERRIDE=1 run_hook ...)
run_hook() {
  printf '%s' "$1" | bash "$HOOK" 2>/dev/null
}

is_deny() {
  printf '%s' "$1" | grep -q '"permissionDecision": *"deny"'
}

is_allow() {
  ! is_deny "$1"
}

has_additional_context() {
  printf '%s' "$1" | grep -q '"additionalContext"'
}

claim_file() {
  # $1=repo_path — returns the absolute path to the claim file
  local gdir
  gdir=$(git -C "$1" rev-parse --git-dir 2>/dev/null) || { echo ""; return 1; }
  case "$gdir" in
    /*) ;;
    *)  gdir="$1/$gdir" ;;
  esac
  echo "$gdir/sable-tree-claim"
}

claim_session() {
  # $1=repo_path
  local cf
  cf="$(claim_file "$1")"
  awk '{print $1}' "$cf" 2>/dev/null
}

# Clean claim state between tests
clear_claim() {
  local cf
  cf="$(claim_file "$1")" 2>/dev/null || return 0
  rm -f "$cf"
}

# ============================================================
# UNIT TESTS
# ============================================================

echo "--- Unit: non-mutating commands ---"

for CMD in "ls" "git status" "git diff" "git log --oneline" "git show HEAD" "git fetch origin"; do
  clear_claim "$SCRATCH"
  JSON=$(make_json "$CMD" "sess-A" "$SCRATCH")
  OUT=$(run_hook "$JSON")
  if is_allow "$OUT"; then
    pass "non-mutating command ignored: $CMD"
  else
    fail "non-mutating command ignored: $CMD" "got deny"
  fi
  CF="$(claim_file "$SCRATCH")"
  if [ ! -f "$CF" ]; then
    pass "non-mutating does not write claim: $CMD"
  else
    fail "non-mutating does not write claim: $CMD" "claim file written unexpectedly"
    rm -f "$CF"
  fi
done

echo "--- Unit: first claim (git add) ---"

clear_claim "$SCRATCH"
JSON=$(make_json "git add ." "sess-A" "$SCRATCH")
OUT=$(run_hook "$JSON")
if is_allow "$OUT"; then pass "first git add: allow"; else fail "first git add: allow" "got deny"; fi
CF="$(claim_file "$SCRATCH")"
if [ -f "$CF" ]; then pass "first git add: claim file created"; else fail "first git add: claim file created" "path: $CF"; fi
if [ "$(claim_session "$SCRATCH")" = "sess-A" ]; then pass "first git add: claim session is sess-A"; else fail "first git add: claim session is sess-A" "got: $(claim_session "$SCRATCH")"; fi

echo "--- Unit: market-brief-package-q6yu — taking a claim names the path + release hint ---"

# First claim (previously silent exit 0) must now emit additionalContext
# naming the claim file, so the holder can find and release it later without
# reading hook source (the LIVE EVIDENCE incident this bead was filed for).
if has_additional_context "$OUT"; then pass "first git add: additionalContext present (claim-taken message)"; else fail "first git add: additionalContext present (claim-taken message)" "output: $OUT"; fi
if printf '%s' "$OUT" | grep -qF "$CF"; then pass "first git add: additionalContext names the claim file path"; else fail "first git add: additionalContext names the claim file path" "output: $OUT"; fi
if printf '%s' "$OUT" | grep -q "sable-claim release"; then pass "first git add: additionalContext mentions the release command"; else fail "first git add: additionalContext mentions the release command" "output: $OUT"; fi

echo "--- Unit: market-brief-package-q6yu — claim record carries an attributable agent name ---"

clear_claim "$SCRATCH"
JSON=$(python3 -c "
import json
print(json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'git add .'}, 'cwd': '$SCRATCH', 'session_id': 'sess-named', 'agent_type': 'tarzan'}))
")
OUT=$(run_hook "$JSON")
if is_allow "$OUT"; then pass "named claim: allow"; else fail "named claim: allow" "got deny"; fi
CF="$(claim_file "$SCRATCH")"
CLAIM_AGENT="$(awk '{print $3}' "$CF" 2>/dev/null)"
if [ "$CLAIM_AGENT" = "tarzan" ]; then pass "named claim: third field is the agent name (tarzan)"; else fail "named claim: third field is the agent name (tarzan)" "got: '$CLAIM_AGENT' (file: $(cat "$CF" 2>/dev/null))"; fi
if printf '%s' "$OUT" | grep -qF "$CF"; then pass "named claim: additionalContext names the claim file path"; else fail "named claim: additionalContext names the claim file path" "output: $OUT"; fi

# Unnamed session: third field falls back to '-' (unresolved), never blank.
# Explicitly unset CLAUDE_AGENT_NAME (this test suite may itself run inside a
# named agent's session, e.g. CLAUDE_AGENT_NAME=optimus — the exact env-leak
# shape this bead's own AGENT_NAME fallback is designed to resolve, so the
# "unnamed" case must force it absent to test the true no-signal path).
clear_claim "$SCRATCH"
JSON=$(make_json "git add ." "sess-anon" "$SCRATCH")
OUT=$(
  unset CLAUDE_AGENT_NAME
  printf '%s' "$JSON" | bash "$HOOK" 2>/dev/null
)
CF="$(claim_file "$SCRATCH")"
CLAIM_AGENT="$(awk '{print $3}' "$CF" 2>/dev/null)"
if [ "$CLAIM_AGENT" = "-" ]; then pass "unnamed claim: third field falls back to '-'"; else fail "unnamed claim: third field falls back to '-'" "got: '$CLAIM_AGENT' (file: $(cat "$CF" 2>/dev/null))"; fi

echo "--- Unit: SABLE-hccq — session_id falls back to CLAUDE_CODE_SESSION_ID when JSON omits it ---"

# The hook-input JSON almost always carries session_id, but the fallback
# order matters: CLAUDE_CODE_SESSION_ID is the env var Claude Code actually
# exports (CLAUDE_SESSION_ID is checked first for any environment that does
# set it, but is unset in practice). If a hook invocation ever lacks
# session_id in its JSON, it must still resolve the SAME identity a later
# 'sable-claim release' call (env-only, no JSON) would see.
clear_claim "$SCRATCH"
JSON_NO_SID=$(python3 -c "
import json
print(json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'git add .'}, 'cwd': '$SCRATCH'}))
")
OUT=$(
  unset CLAUDE_SESSION_ID
  CLAUDE_CODE_SESSION_ID="sess-from-code-env" bash "$HOOK" <<< "$JSON_NO_SID" 2>/dev/null
)
if [ "$(claim_session "$SCRATCH")" = "sess-from-code-env" ]; then
  pass "session_id falls back to CLAUDE_CODE_SESSION_ID when JSON omits it"
else
  fail "session_id falls back to CLAUDE_CODE_SESSION_ID when JSON omits it" "got: $(claim_session "$SCRATCH")"
fi

echo "--- Unit: same session refresh ---"

CF="$(claim_file "$SCRATCH")"
PAST=$(( $(date +%s) - 300 ))
printf 'sess-A %s\n' "$PAST" > "$CF"
JSON=$(make_json "git commit -m 'x'" "sess-A" "$SCRATCH")
OUT=$(run_hook "$JSON")
if is_allow "$OUT"; then pass "same-session refresh: allow"; else fail "same-session refresh: allow" "got deny"; fi
NEW_TS=$(awk '{print $2}' "$CF" 2>/dev/null)
NOW=$(date +%s)
AGE=$(( NOW - NEW_TS ))
if [ "$AGE" -lt 5 ]; then pass "same-session refresh: timestamp updated"; else fail "same-session refresh: timestamp updated" "age=${AGE}s (new_ts=$NEW_TS now=$NOW)"; fi

echo "--- Unit: foreign fresh claim -> deny ---"

CF="$(claim_file "$SCRATCH")"
printf 'sess-A %s\n' "$(date +%s)" > "$CF"
JSON=$(make_json "git add -u" "sess-B" "$SCRATCH")
OUT=$(run_hook "$JSON")
if is_deny "$OUT"; then pass "foreign fresh: deny"; else fail "foreign fresh: deny" "got allow: ${OUT:-<empty>}"; fi
if printf '%s' "$OUT" | grep -q "sess-A"; then pass "foreign fresh: names holder"; else fail "foreign fresh: names holder" "output: $OUT"; fi
if printf '%s' "$OUT" | grep -qE "SABLE_TREE_CLAIM_OVERRIDE|delete"; then pass "foreign fresh: escape hatch mentioned"; else fail "foreign fresh: escape hatch mentioned" "output: $OUT"; fi
if printf '%s' "$OUT" | grep -q "sable-claim release"; then pass "foreign fresh: deny names the sable-claim release escape hatch (market-brief-package-q6yu)"; else fail "foreign fresh: deny names the sable-claim release escape hatch" "output: $OUT"; fi

echo "--- Unit: stale claim -> takeover + allow ---"

CF="$(claim_file "$SCRATCH")"
STALE=$(( $(date +%s) - 7200 ))
printf 'sess-A %s\n' "$STALE" > "$CF"
JSON=$(make_json "git rm file.txt" "sess-B" "$SCRATCH")
OUT=$(SABLE_TREE_CLAIM_TTL=3600 run_hook "$JSON")
if is_allow "$OUT"; then pass "stale takeover: allow"; else fail "stale takeover: allow" "got deny"; fi
if [ "$(claim_session "$SCRATCH")" = "sess-B" ]; then pass "stale takeover: claim now sess-B"; else fail "stale takeover: claim now sess-B" "got: $(claim_session "$SCRATCH")"; fi
if has_additional_context "$OUT"; then pass "stale takeover: additionalContext present"; else fail "stale takeover: additionalContext present" "output: $OUT"; fi
if printf '%s' "$OUT" | grep -qF "$CF"; then pass "stale takeover: additionalContext names the claim file path (market-brief-package-q6yu)"; else fail "stale takeover: additionalContext names the claim file path" "output: $OUT"; fi

echo "--- Unit: SABLE_TREE_CLAIM_OVERRIDE ---"

CF="$(claim_file "$SCRATCH")"
printf 'sess-A %s\n' "$(date +%s)" > "$CF"
JSON=$(make_json "git mv a b" "sess-B" "$SCRATCH")
OUT=$(SABLE_TREE_CLAIM_OVERRIDE=1 run_hook "$JSON")
if is_allow "$OUT"; then pass "override: allow"; else fail "override: allow" "got deny"; fi
if [ "$(claim_session "$SCRATCH")" = "sess-B" ]; then pass "override: claim taken by sess-B"; else fail "override: claim taken by sess-B" "got: $(claim_session "$SCRATCH")"; fi
if has_additional_context "$OUT"; then pass "override: additionalContext present"; else fail "override: additionalContext present" "output: $OUT"; fi

echo "--- Unit: command outside a git repo ---"

NONGIT="$(mktemp -d)"
JSON=$(make_json "git add ." "sess-A" "$NONGIT")
OUT=$(run_hook "$JSON")
if is_allow "$OUT"; then pass "non-repo cwd: allow"; else fail "non-repo cwd: allow" "got deny"; fi
rm -rf "$NONGIT"

echo "--- Unit: missing session identity ---"

clear_claim "$SCRATCH"
# JSON with no session_id; unset CLAUDE_SESSION_ID/CLAUDE_CODE_SESSION_ID so
# identity is truly unknown (SABLE-hccq: this harness's own real session sets
# CLAUDE_CODE_SESSION_ID ambiently, which the hook now also consults as a
# fallback — it must be scrubbed too or this "truly unknown" scenario
# silently resolves to a KNOWN identity).
JSON=$(python3 -c "
import json
print(json.dumps({'tool_name':'Bash','tool_input':{'command':'git add .'},'cwd':'$SCRATCH'}))
")
# env -u cannot call shell functions; use a subshell to unset the variable
OUT=$(
  unset CLAUDE_SESSION_ID CLAUDE_CODE_SESSION_ID
  printf '%s' "$JSON" | bash "$HOOK" 2>/dev/null
)
if is_allow "$OUT"; then pass "missing identity: allow"; else fail "missing identity: allow" "got deny"; fi
if has_additional_context "$OUT"; then pass "missing identity: additionalContext present"; else fail "missing identity: additionalContext present" "output: $OUT"; fi

echo "--- Unit: git restore --staged (index-mutating) ---"

clear_claim "$SCRATCH"
JSON=$(make_json "git restore --staged ." "sess-A" "$SCRATCH")
OUT=$(run_hook "$JSON")
if is_allow "$OUT"; then pass "git restore --staged: allow (first claim)"; else fail "git restore --staged: allow (first claim)" "got deny"; fi
CF="$(claim_file "$SCRATCH")"
if [ -f "$CF" ]; then pass "git restore --staged: claim written"; else fail "git restore --staged: claim written" "path: $CF"; fi

echo "--- Unit: git restore without --staged (non-mutating) ---"

clear_claim "$SCRATCH"
JSON=$(make_json "git restore ." "sess-A" "$SCRATCH")
OUT=$(run_hook "$JSON")
if is_allow "$OUT"; then pass "git restore (no --staged): allow"; else fail "git restore (no --staged): allow" "got deny"; fi
CF="$(claim_file "$SCRATCH")"
if [ ! -f "$CF" ]; then pass "git restore (no --staged): no claim written"; else fail "git restore (no --staged): no claim written"; rm -f "$CF"; fi

echo "--- Unit: git reset (index-mutating) ---"

clear_claim "$SCRATCH"
JSON=$(make_json "git reset HEAD~1" "sess-A" "$SCRATCH")
OUT=$(run_hook "$JSON")
if is_allow "$OUT"; then pass "git reset: allow (first claim)"; else fail "git reset: allow (first claim)" "got deny"; fi

echo "--- Unit: git global flags tolerated ---"

clear_claim "$SCRATCH"
JSON=$(make_json "git -C $SCRATCH add ." "sess-A" "$SCRATCH")
OUT=$(run_hook "$JSON")
if is_allow "$OUT"; then pass "git -C flag: allow"; else fail "git -C flag: allow" "got deny"; fi

clear_claim "$SCRATCH"
JSON=$(make_json "git -c user.email=x@y.com commit -m msg" "sess-A" "$SCRATCH")
OUT=$(run_hook "$JSON")
if is_allow "$OUT"; then pass "git -c flag: allow"; else fail "git -c flag: allow" "got deny"; fi

echo "--- Unit: manual delete of claim file allows the other session ---"

CF="$(claim_file "$SCRATCH")"
printf 'sess-A %s\n' "$(date +%s)" > "$CF"
rm -f "$CF"
JSON=$(make_json "git add ." "sess-B" "$SCRATCH")
OUT=$(run_hook "$JSON")
if is_allow "$OUT"; then pass "manual delete: sess-B can take claim"; else fail "manual delete: sess-B can take claim" "got deny"; fi

# ============================================================
# INTEGRATION TESTS — real git worktree
# ============================================================

echo "--- Integration: worktree claim independence ---"

WT="$SCRATCH_ROOT/wt-branch"
if git -C "$SCRATCH" worktree add "$WT" -b wt-branch -q 2>/dev/null; then
  clear_claim "$SCRATCH"
  clear_claim "$WT"

  # Write a fresh claim in the main checkout
  CF_MAIN="$(claim_file "$SCRATCH")"
  printf 'sess-main %s\n' "$(date +%s)" > "$CF_MAIN"

  # sess-wt should be allowed in the worktree (independent claim file)
  JSON=$(make_json "git add ." "sess-wt" "$WT")
  OUT=$(run_hook "$JSON")
  if is_allow "$OUT"; then pass "integration: worktree allows separate session independently"; else fail "integration: worktree allows separate session independently" "got deny: ${OUT:-<empty>}"; fi

  # Claim files must be in different locations
  CF_WT="$(claim_file "$WT")"
  if [ "$CF_MAIN" != "$CF_WT" ]; then
    pass "integration: main and worktree have different claim file paths"
  else
    fail "integration: main and worktree have different claim file paths" "both: $CF_MAIN"
  fi

  # Worktree claim should now be sess-wt
  if [ "$(claim_session "$WT")" = "sess-wt" ]; then
    pass "integration: worktree claim is sess-wt"
  else
    fail "integration: worktree claim is sess-wt" "got: $(claim_session "$WT")"
  fi

  # Main claim is still sess-main (independent)
  if [ "$(claim_session "$SCRATCH")" = "sess-main" ]; then
    pass "integration: main claim still sess-main after worktree claim"
  else
    fail "integration: main claim still sess-main after worktree claim" "got: $(claim_session "$SCRATCH")"
  fi

  # sess-main blocked in worktree when sess-wt holds it fresh
  CF_WT_FILE="$(claim_file "$WT")"
  printf 'sess-wt %s\n' "$(date +%s)" > "$CF_WT_FILE"
  JSON=$(make_json "git add ." "sess-main" "$WT")
  OUT=$(run_hook "$JSON")
  if is_deny "$OUT"; then pass "integration: foreign session blocked in worktree"; else fail "integration: foreign session blocked in worktree" "got allow: ${OUT:-<empty>}"; fi

  git -C "$SCRATCH" worktree remove "$WT" --force 2>/dev/null || true
else
  fail "integration setup: git worktree add failed"
fi

echo "--- Integration: SABLE-hccq — sable-claim release succeeds via CLAUDE_CODE_SESSION_ID after the real hook records that same session_id ---"

# Reproduces the live bug end-to-end: the hook receives a real harness-issued
# session_id via hook-input JSON (as it always does for a genuine
# PreToolUse:Bash call) and writes it to the claim file. In production the
# SAME session_id is also what the harness exports as CLAUDE_CODE_SESSION_ID
# into a later plain Bash tool call's env — but sable-claim previously
# checked only CLAUDE_SESSION_ID (which the harness never sets), so a
# self-release always fell through to needing --force. Setting
# CLAUDE_CODE_SESSION_ID here to the SAME id the hook wrote reproduces that
# production pairing exactly.
clear_claim "$SCRATCH"
SABLE_CLAIM_BIN="$REPO/bin/sable-claim"
REAL_SID="ea22e248-simulated-harness-session-uuid"
JSON_REAL=$(python3 -c "
import json
print(json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 'git add .'}, 'cwd': '$SCRATCH', 'session_id': '$REAL_SID'}))
")
printf '%s' "$JSON_REAL" | bash "$HOOK" >/dev/null 2>&1
OUT=$(env -u CLAUDE_SESSION_ID -u CLAUDE_AGENT_NAME CLAUDE_CODE_SESSION_ID="$REAL_SID" "$SABLE_CLAIM_BIN" release "$SCRATCH" 2>&1)
RC=$?
CF="$(claim_file "$SCRATCH")"
if [ "$RC" -eq 0 ] && [ ! -f "$CF" ]; then
  pass "integration: sable-claim release succeeds without --force via CLAUDE_CODE_SESSION_ID (repro fix verification)"
else
  fail "integration: sable-claim release succeeds without --force via CLAUDE_CODE_SESSION_ID (repro fix verification)" "rc=$RC file_exists=$([ -f "$CF" ] && echo yes || echo no) out=$OUT"
fi

echo "--- Integration: SABLE-hccq — a genuinely foreign holder is still refused without --force ---"

# Same shape (CLAUDE_CODE_SESSION_ID set, CLAUDE_SESSION_ID/CLAUDE_AGENT_NAME
# not), but it belongs to a DIFFERENT session than the one the hook
# recorded — the deny leg must survive.
clear_claim "$SCRATCH"
CF="$(claim_file "$SCRATCH")"
printf 'ea22e248-foreign-session %s -\n' "$(date +%s)" > "$CF"
OUT=$(env -u CLAUDE_SESSION_ID -u CLAUDE_AGENT_NAME CLAUDE_CODE_SESSION_ID="ea22e248-different-session" "$SABLE_CLAIM_BIN" release "$SCRATCH" 2>&1)
RC=$?
if [ "$RC" -ne 0 ] && [ -f "$CF" ]; then
  pass "integration: foreign holder (non-matching CLAUDE_CODE_SESSION_ID) still refused without --force"
else
  fail "integration: foreign holder (non-matching CLAUDE_CODE_SESSION_ID) still refused without --force" "rc=$RC file_exists=$([ -f "$CF" ] && echo yes || echo no) out=$OUT"
fi
rm -f "$CF"

echo "--- Integration: SABLE-5pci — 'git -C <wt>' and 'cd <wt> && git' resolve the SAME claim file ---"

if git -C "$SCRATCH" worktree add "$WT" -b wt-branch-5pci -q 2>/dev/null; then
  clear_claim "$SCRATCH"
  clear_claim "$WT"

  # Invocation form 1: 'git -C <wt> add .', hook cwd is the MAIN checkout.
  JSON=$(make_json "git -C $WT add ." "sess-form1" "$SCRATCH")
  OUT=$(run_hook "$JSON")
  if is_allow "$OUT"; then pass "5pci: 'git -C <wt>' from main cwd: allow"; else fail "5pci: 'git -C <wt>' from main cwd: allow" "got deny: $OUT"; fi
  CF_FORM1="$(claim_file "$WT")"
  if [ "$(claim_session "$WT")" = "sess-form1" ]; then
    pass "5pci: 'git -C <wt>' claims the worktree's own claim file"
  else
    fail "5pci: 'git -C <wt>' claims the worktree's own claim file" "got: $(claim_session "$WT")"
  fi

  # Invocation form 2: 'cd <wt> && git add .', hook cwd is STILL the MAIN
  # checkout (this is the exact defect: cwd != cd target). A different
  # session must land in the SAME claim file as form 1, and — since form 1's
  # claim (sess-form1) is fresh — must be DENIED, not silently allowed
  # against a different (main-checkout) claim file.
  clear_claim_leave=""  # no-op marker; keep worktree claim from form 1 intact
  JSON=$(make_json "cd $WT && git add ." "sess-form2" "$SCRATCH")
  OUT=$(run_hook "$JSON")
  if is_deny "$OUT"; then
    pass "5pci: 'cd <wt> && git add' from main cwd resolves to the SAME (worktree) claim file and is denied by sess-form1's fresh claim"
  else
    fail "5pci: 'cd <wt> && git add' from main cwd resolves to the SAME (worktree) claim file and is denied by sess-form1's fresh claim" "got allow: ${OUT:-<empty>}"
  fi
  if printf '%s' "$OUT" | grep -q "sess-form1"; then
    pass "5pci: denial names sess-form1 (proves same claim file as -C form)"
  else
    fail "5pci: denial names sess-form1 (proves same claim file as -C form)" "output: $OUT"
  fi

  # Main checkout's own claim file must be untouched by either worktree
  # invocation form (proves the main checkout and worktree stay in
  # independent namespaces — the reverse-hole half of the bug).
  CF_MAIN="$(claim_file "$SCRATCH")"
  if [ ! -f "$CF_MAIN" ]; then
    pass "5pci: main-checkout claim file untouched by worktree invocations (independent namespace)"
  else
    fail "5pci: main-checkout claim file untouched by worktree invocations (independent namespace)" "unexpected claim at $CF_MAIN: $(cat "$CF_MAIN" 2>/dev/null)"
  fi

  # And the converse: a fresh MAIN-checkout claim must not block a
  # 'cd <wt> && git add' mutation in the worktree.
  printf 'sess-main-holder %s\n' "$(date +%s)" > "$CF_MAIN"
  clear_claim "$WT"
  JSON=$(make_json "cd $WT && git add ." "sess-form3" "$SCRATCH")
  OUT=$(run_hook "$JSON")
  if is_allow "$OUT"; then
    pass "5pci: fresh main-checkout claim does not block 'cd <wt> && git add' in the worktree"
  else
    fail "5pci: fresh main-checkout claim does not block 'cd <wt> && git add' in the worktree" "got deny: $OUT"
  fi
  if [ "$(claim_session "$WT")" = "sess-form3" ]; then
    pass "5pci: worktree claim taken by sess-form3, independent of main-checkout holder"
  else
    fail "5pci: worktree claim taken by sess-form3, independent of main-checkout holder" "got: $(claim_session "$WT")"
  fi

  git -C "$SCRATCH" worktree remove "$WT" --force 2>/dev/null || true
else
  fail "5pci integration setup: git worktree add failed"
fi

# ============================================================
# Defect-regression tests (SABLE-ct8 verdict)
# ============================================================

echo "--- Defect-regression (a): git -C <other-repo> from cwd with foreign fresh claim ---"

# Two independent repos; repoA holds a fresh foreign claim for sess-OTHER.
# sess-ME runs 'git -C repoB add .' from repoA's cwd.
# Expected: allow (target is repoB), repoB gets sess-ME's claim, repoA untouched.
REG_ROOT="$(mktemp -d)"
trap 'rm -rf "$REG_ROOT"' EXIT
REG_A="$REG_ROOT/repoA"
REG_B="$REG_ROOT/repoB"
git init "$REG_A" -q
git -C "$REG_A" commit --allow-empty -m "init" -q
git init "$REG_B" -q
git -C "$REG_B" commit --allow-empty -m "init" -q

CF_A="$(claim_file "$REG_A")"
CF_B="$(claim_file "$REG_B")"
# Write a fresh foreign claim on repoA
printf 'sess-OTHER %s\n' "$(date +%s)" > "$CF_A"
# No claim on repoB

JSON=$(make_json "git -C $REG_B add ." "sess-ME" "$REG_A")
OUT=$(run_hook "$JSON")
if is_allow "$OUT"; then
  pass "defect-a: -C target allowed (repoA has foreign claim, target=repoB)"
else
  fail "defect-a: -C target allowed (repoA has foreign claim, target=repoB)" "got deny: $OUT"
fi
if [ -f "$CF_B" ]; then
  pass "defect-a: claim written in TARGET repo (repoB)"
else
  fail "defect-a: claim written in TARGET repo (repoB)" "no claim at $CF_B"
fi
if [ "$(awk '{print $1}' "$CF_B" 2>/dev/null)" = "sess-ME" ]; then
  pass "defect-a: TARGET repo claim owner is sess-ME"
else
  fail "defect-a: TARGET repo claim owner is sess-ME" "got: $(awk '{print $1}' "$CF_B" 2>/dev/null)"
fi
# repoA claim must be untouched (still sess-OTHER)
if [ "$(awk '{print $1}' "$CF_A" 2>/dev/null)" = "sess-OTHER" ]; then
  pass "defect-a: repoA (cwd) claim unchanged"
else
  fail "defect-a: repoA (cwd) claim unchanged" "got: $(awk '{print $1}' "$CF_A" 2>/dev/null)"
fi
rm -rf "$REG_ROOT"

echo "--- Defect-regression (b): identity-unknown does not overwrite existing fresh claim ---"

# sess-HOLDER owns a fresh claim; an identity-unknown invocation must not
# overwrite it and must not cause sess-HOLDER to be denied on its next command.
clear_claim "$SCRATCH"
CF="$(claim_file "$SCRATCH")"
printf 'sess-HOLDER %s\n' "$(date +%s)" > "$CF"
ORIGINAL_CONTENT="$(cat "$CF")"

# identity-unknown invocation (no session_id, CLAUDE_SESSION_ID and
# CLAUDE_CODE_SESSION_ID both unset — SABLE-hccq: this harness's own real
# session sets CLAUDE_CODE_SESSION_ID ambiently, which the hook now also
# consults as a fallback, so it must be scrubbed too for this to be truly
# identity-unknown rather than silently resolving to a KNOWN identity)
JSON_UNK=$(python3 -c "
import json
print(json.dumps({'tool_name':'Bash','tool_input':{'command':'git add .'},'cwd':'$SCRATCH'}))
")
OUT_UNK=$(
  unset CLAUDE_SESSION_ID CLAUDE_CODE_SESSION_ID
  printf '%s' "$JSON_UNK" | bash "$HOOK" 2>/dev/null
)
if is_allow "$OUT_UNK"; then
  pass "defect-b: identity-unknown allows"
else
  fail "defect-b: identity-unknown allows" "got deny: $OUT_UNK"
fi
if has_additional_context "$OUT_UNK"; then
  pass "defect-b: identity-unknown has additionalContext"
else
  fail "defect-b: identity-unknown has additionalContext" "output: $OUT_UNK"
fi
AFTER_CONTENT="$(cat "$CF" 2>/dev/null)"
if [ "$AFTER_CONTENT" = "$ORIGINAL_CONTENT" ]; then
  pass "defect-b: claim file content unchanged after identity-unknown"
else
  fail "defect-b: claim file content unchanged after identity-unknown" "before='$ORIGINAL_CONTENT' after='$AFTER_CONTENT'"
fi

# sess-HOLDER must still be allowed on the next mutating command
JSON_H=$(make_json "git commit -m x" "sess-HOLDER" "$SCRATCH")
OUT_H=$(run_hook "$JSON_H")
if is_allow "$OUT_H"; then
  pass "defect-b: original holder still allowed after identity-unknown pass"
else
  fail "defect-b: original holder still allowed after identity-unknown pass" "got deny: $OUT_H"
fi

echo "--- Defect-regression (c): chained 'cd /x && git add .' is protected ---"

clear_claim "$SCRATCH"
JSON=$(make_json "cd $SCRATCH && git add ." "sess-A" "$SCRATCH")
OUT=$(run_hook "$JSON")
# After fix: the 'git add .' segment is at a command position after '&&'
# and must be detected as mutating.
if is_allow "$OUT"; then
  pass "defect-c: chained cd && git add . allowed (first claim written)"
else
  fail "defect-c: chained cd && git add . allowed (first claim written)" "got deny: $OUT"
fi
CF="$(claim_file "$SCRATCH")"
if [ -f "$CF" ]; then
  pass "defect-c: claim written for chained command"
else
  fail "defect-c: claim written for chained command" "no claim at $CF"
fi

# Now sess-B must be denied (foreign fresh claim held by sess-A)
clear_claim "$SCRATCH"
printf 'sess-A %s\n' "$(date +%s)" > "$CF"
JSON=$(make_json "cd $SCRATCH && git add ." "sess-B" "$SCRATCH")
OUT=$(run_hook "$JSON")
if is_deny "$OUT"; then
  pass "defect-c: chained cd && git add . denied for foreign session"
else
  fail "defect-c: chained cd && git add . denied for foreign session" "got allow: ${OUT:-<empty>}"
fi

echo "--- Defect-regression (d): 'FOO=1 git add .' is protected ---"

clear_claim "$SCRATCH"
JSON=$(make_json "FOO=1 git add ." "sess-A" "$SCRATCH")
OUT=$(run_hook "$JSON")
if is_allow "$OUT"; then
  pass "defect-d: FOO=1 git add . allowed (first claim written)"
else
  fail "defect-d: FOO=1 git add . allowed (first claim written)" "got deny: $OUT"
fi
CF="$(claim_file "$SCRATCH")"
if [ -f "$CF" ]; then
  pass "defect-d: claim written for env-prefixed command"
else
  fail "defect-d: claim written for env-prefixed command" "no claim at $CF"
fi

# Now sess-B must be denied
clear_claim "$SCRATCH"
printf 'sess-A %s\n' "$(date +%s)" > "$CF"
JSON=$(make_json "FOO=1 git add ." "sess-B" "$SCRATCH")
OUT=$(run_hook "$JSON")
if is_deny "$OUT"; then
  pass "defect-d: FOO=1 git add . denied for foreign session"
else
  fail "defect-d: FOO=1 git add . denied for foreign session" "got allow: ${OUT:-<empty>}"
fi

echo "--- Defect-regression (e): 'git stash && git add .' is protected (mutating segment not first) ---"

clear_claim "$SCRATCH"
JSON=$(make_json "git stash && git add ." "sess-A" "$SCRATCH")
OUT=$(run_hook "$JSON")
if is_allow "$OUT"; then
  pass "defect-e: git stash && git add . allowed (first claim written)"
else
  fail "defect-e: git stash && git add . allowed (first claim written)" "got deny: $OUT"
fi
CF="$(claim_file "$SCRATCH")"
if [ -f "$CF" ]; then
  pass "defect-e: claim written for compound command with non-first mutating segment"
else
  fail "defect-e: claim written for compound command with non-first mutating segment" "no claim at $CF"
fi

# Now sess-B must be denied
clear_claim "$SCRATCH"
printf 'sess-A %s\n' "$(date +%s)" > "$CF"
JSON=$(make_json "git stash && git add ." "sess-B" "$SCRATCH")
OUT=$(run_hook "$JSON")
if is_deny "$OUT"; then
  pass "defect-e: git stash && git add . denied for foreign session"
else
  fail "defect-e: git stash && git add . denied for foreign session" "got allow: ${OUT:-<empty>}"
fi

# ============================================================
# SABLE-vx4aj — the claim must gate the command's ACTUAL TARGET repo,
# never the ambient session cwd
# ============================================================
#
# Observed (tarzan, 2026-07-21): a git write in an unrelated throwaway repo
# was refused citing the SABLE checkout's claim. The hypothesis was
# "attribution is session-cwd based". Investigation narrowed it: cd tracking
# (SABLE-5pci) only fires when 'cd' sits at a command position the tokenizer
# RECOGNISES. Any shell construct it does not model — a newline separator,
# a subshell '(...)', a brace group, a background '&' — knocks the walk out
# of command position, and then BOTH failure directions appear:
#   FALSE POSITIVE: the 'cd <elsewhere>' prefix is invisible, so the git
#     write is attributed to the ambient session cwd and gated by a claim on
#     a repo it never touches (the observed symptom).
#   FALSE NEGATIVE (the incident class, SABLE-041/936y/nsmc): the git write
#     itself is invisible, so a write TARGETING the claimed repo sails past
#     the claim entirely. A two-line Bash command is enough.
#
# Both directions are tested here. Ambiguous targets (an unexpandable 'cd
# "$VAR"', an unparseable command) must GATE against the session cwd rather
# than allow — a false positive is recoverable, a missed claim is not.

echo "--- SABLE-vx4aj: target-repo attribution (two real repos) ---"

VX_ROOT="$(mktemp -d)"
VX_CLAIMED="$VX_ROOT/claimed"
VX_UNRELATED="$VX_ROOT/unrelated"
git init "$VX_CLAIMED" -q
git -C "$VX_CLAIMED" commit --allow-empty -m init -q
git init "$VX_UNRELATED" -q
git -C "$VX_UNRELATED" commit --allow-empty -m init -q

CF_CLAIMED="$(claim_file "$VX_CLAIMED")"
CF_UNRELATED="$(claim_file "$VX_UNRELATED")"

# Re-arm the foreign fresh claim on the claimed repo, clear the other side.
vx_arm() {
  printf 'sess-HOLDER %s chuck\n' "$(date +%s)" > "$CF_CLAIMED"
  rm -f "$CF_UNRELATED"
}

# vx_check <label> <cwd> <command> <expect: deny|allow>
vx_check() {
  local label="$1" cwd="$2" cmd="$3" expect="$4"
  vx_arm
  local json out
  json=$(make_json "$cmd" "sess-ME" "$cwd")
  out=$(run_hook "$json")
  if [ "$expect" = "deny" ]; then
    if is_deny "$out"; then pass "vx4aj: $label"; else fail "vx4aj: $label" "expected deny, got allow: ${out:-<empty>}"; fi
  else
    if is_allow "$out"; then pass "vx4aj: $label"; else fail "vx4aj: $label" "expected allow, got deny: $out"; fi
  fi
}

# --- Regression: the three forms the bead's test spec names explicitly ---
vx_check "cd <unrelated> && git commit (cwd=claimed) is NOT gated by the claimed repo" \
  "$VX_CLAIMED" "cd $VX_UNRELATED && git commit -m x" allow
vx_check "git -C <claimed> commit (cwd=unrelated) IS gated" \
  "$VX_UNRELATED" "git -C $VX_CLAIMED commit -m x" deny
vx_check "plain git commit (cwd=claimed) IS gated" \
  "$VX_CLAIMED" "git commit -m x" deny

# --- FALSE NEGATIVE class: newline as a command separator ---
# A multi-line Bash command is the single most common shape an agent sends.
# Every one of these targets the claimed repo and must be refused.
vx_check "newline separator: 'cd <claimed>' NEWLINE 'git commit' (cwd=unrelated) IS gated" \
  "$VX_UNRELATED" "cd $VX_CLAIMED
git commit -m x" deny
vx_check "newline separator: non-git first line then 'git commit' (cwd=claimed) IS gated" \
  "$VX_CLAIMED" "echo hi
git commit -m x" deny
vx_check "background '&' separator: 'sleep 1 & git commit' (cwd=claimed) IS gated" \
  "$VX_CLAIMED" "sleep 1 & git commit -m x" deny
vx_check "subshell: '( cd <claimed> && git commit )' (cwd=unrelated) IS gated" \
  "$VX_UNRELATED" "( cd $VX_CLAIMED && git commit -m x )" deny
vx_check "brace group: '{ cd <claimed>; git commit; }' (cwd=unrelated) IS gated" \
  "$VX_UNRELATED" "{ cd $VX_CLAIMED; git commit -m x; }" deny

# --- FALSE POSITIVE class: unrelated targets must proceed ---
vx_check "newline separator: 'cd <unrelated>' NEWLINE 'git commit' (cwd=claimed) is NOT gated" \
  "$VX_CLAIMED" "cd $VX_UNRELATED
git commit -m x" allow
vx_check "subshell: '( cd <unrelated> && git commit )' (cwd=claimed) is NOT gated" \
  "$VX_CLAIMED" "( cd $VX_UNRELATED && git commit -m x )" allow
vx_check "brace group: '{ cd <unrelated>; git commit; }' (cwd=claimed) is NOT gated" \
  "$VX_CLAIMED" "{ cd $VX_UNRELATED; git commit -m x; }" allow
vx_check "observed repro: 'cd <scratch> && git init && git add && git commit' (cwd=claimed) is NOT gated" \
  "$VX_CLAIMED" "cd $VX_UNRELATED && git init -q . && git config user.email a@b && git add -A && git commit -m init" allow

# The allowed unrelated write must claim the UNRELATED repo, and must leave
# the claimed repo's foreign holder untouched.
vx_arm
JSON=$(make_json "cd $VX_UNRELATED
git commit -m x" "sess-ME" "$VX_CLAIMED")
OUT=$(run_hook "$JSON")
if [ "$(awk '{print $1}' "$CF_UNRELATED" 2>/dev/null)" = "sess-ME" ]; then
  pass "vx4aj: allowed unrelated write claims the UNRELATED repo"
else
  fail "vx4aj: allowed unrelated write claims the UNRELATED repo" "got: $(awk '{print $1}' "$CF_UNRELATED" 2>/dev/null)"
fi
if [ "$(awk '{print $1}' "$CF_CLAIMED" 2>/dev/null)" = "sess-HOLDER" ]; then
  pass "vx4aj: claimed repo's foreign holder untouched by the unrelated write"
else
  fail "vx4aj: claimed repo's foreign holder untouched by the unrelated write" "got: $(awk '{print $1}' "$CF_CLAIMED" 2>/dev/null)"
fi

# --- Quoted text is NOT a command: no spurious gate ---
vx_check "quoted 'cd /x && git commit' inside an echo argument is not read as a command" \
  "$VX_UNRELATED" "echo \"cd $VX_CLAIMED && git commit -m x\"" allow

# --- Fail-safe on ambiguity: gate rather than allow ---
vx_check "unexpandable cd target ('cd \$VAR && git commit') falls back to the session cwd and IS gated" \
  "$VX_CLAIMED" "cd \$SOMEWHERE && git commit -m x" deny
vx_check "unparseable command (unbalanced quote) containing a git write IS gated" \
  "$VX_CLAIMED" "echo \"unterminated && git commit -m x" deny

vx_arm
JSON=$(make_json "cd \$SOMEWHERE && git commit -m x" "sess-ME" "$VX_CLAIMED")
OUT=$(run_hook "$JSON")
if printf '%s' "$OUT" | grep -q "could not be resolved"; then
  pass "vx4aj: ambiguous-target deny explains that the target was unresolvable"
else
  fail "vx4aj: ambiguous-target deny explains that the target was unresolvable" "output: $OUT"
fi

# ============================================================
# SABLE-vx4aj INTEGRATION — real git writes, real claim files, no mocks
# ============================================================
#
# The unit block above asserts the hook's decision JSON. This block closes
# the loop: when the hook ALLOWS, the command is actually executed against
# real repos and the resulting git state is asserted; when it DENIES, the
# command is not executed and the target repo is proven unchanged. That is
# the property that matters operationally — "the write landed / did not
# land" — not just the shape of the decision.

echo "--- SABLE-vx4aj Integration: gate decision drives real git state ---"

# gate_and_run <cwd> <command> — run the hook; execute for real iff allowed.
# Echoes "allow" or "deny".
gate_and_run() {
  local cwd="$1" cmd="$2" json out
  json=$(make_json "$cmd" "sess-ME" "$cwd")
  out=$(run_hook "$json")
  if is_deny "$out"; then
    echo "deny"
  else
    ( cd "$cwd" && eval "$cmd" ) >/dev/null 2>&1
    echo "allow"
  fi
}

count_commits() { git -C "$1" rev-list --count HEAD 2>/dev/null || echo 0; }

# Real content to commit in each repo.
echo unrelated-work > "$VX_UNRELATED/u.txt"
echo claimed-work   > "$VX_CLAIMED/c.txt"

# (1) Real write in the UNRELATED repo, issued from the CLAIMED repo's cwd
#     while a foreign session holds the CLAIMED repo's claim: must proceed
#     and must actually produce a commit in the unrelated repo.
vx_arm
BEFORE_U=$(count_commits "$VX_UNRELATED")
DECISION=$(gate_and_run "$VX_CLAIMED" "cd $VX_UNRELATED && git add -A && git commit -q -m 'unrelated real write'")
AFTER_U=$(count_commits "$VX_UNRELATED")
if [ "$DECISION" = "allow" ] && [ "$AFTER_U" -eq $((BEFORE_U + 1)) ]; then
  pass "vx4aj integration: real write in unrelated repo proceeds under a foreign claim on the SABLE-like checkout"
else
  fail "vx4aj integration: real write in unrelated repo proceeds under a foreign claim on the SABLE-like checkout" "decision=$DECISION commits $BEFORE_U -> $AFTER_U"
fi
# ...and the claimed repo must have gained nothing.
if [ -z "$(git -C "$VX_CLAIMED" log --oneline --all --grep='unrelated real write' 2>/dev/null)" ]; then
  pass "vx4aj integration: claimed repo received no commit from the unrelated write"
else
  fail "vx4aj integration: claimed repo received no commit from the unrelated write"
fi

# (2) Real 'git -C <claimed>' write issued from the UNRELATED repo's cwd:
#     must be refused, and the claimed repo must be provably unchanged.
vx_arm
BEFORE_C=$(count_commits "$VX_CLAIMED")
DECISION=$(gate_and_run "$VX_UNRELATED" "git -C $VX_CLAIMED add -A && git -C $VX_CLAIMED commit -q -m 'cross-repo write'")
AFTER_C=$(count_commits "$VX_CLAIMED")
if [ "$DECISION" = "deny" ] && [ "$AFTER_C" -eq "$BEFORE_C" ]; then
  pass "vx4aj integration: real 'git -C <claimed>' write from another repo's cwd is refused and lands nothing"
else
  fail "vx4aj integration: real 'git -C <claimed>' write from another repo's cwd is refused and lands nothing" "decision=$DECISION commits $BEFORE_C -> $AFTER_C"
fi

# (3) The false-negative class, end to end: a newline-separated
#     'cd <claimed>' + 'git commit' issued from the unrelated repo must be
#     refused and must leave the claimed repo untouched.
vx_arm
BEFORE_C=$(count_commits "$VX_CLAIMED")
DECISION=$(gate_and_run "$VX_UNRELATED" "cd $VX_CLAIMED
git add -A
git commit -q -m 'newline evasion'")
AFTER_C=$(count_commits "$VX_CLAIMED")
if [ "$DECISION" = "deny" ] && [ "$AFTER_C" -eq "$BEFORE_C" ]; then
  pass "vx4aj integration: newline-separated write into the claimed repo is refused and lands nothing"
else
  fail "vx4aj integration: newline-separated write into the claimed repo is refused and lands nothing" "decision=$DECISION commits $BEFORE_C -> $AFTER_C"
fi

rm -rf "$VX_ROOT"

# ============================================================
# SABLE-vx4aj / SABLE-hfkdd — REAL-BASH ORACLE
# ============================================================
#
# Everything above asserts hand-written expected values. That is exactly how
# the previous 102-assertion revision of this suite passed while shipping a
# live evasion: it enumerated the constructs the fix had ADDED handling for,
# and asserted the case where the 'cd' and the write are BOTH inside a
# subshell — the complement of the failing case. Coverage read complete while
# the load-bearing invariant went unasserted.
#
# THE INVARIANT, asserted directly and in both directions:
#   the claim gates a command IF AND ONLY IF that command's write really
#   lands in the claimed repo.
#
# Expected values below are NOT hand-written. For each probe, bash itself
# executes the command against a fresh pair of real repos and we observe which
# repo actually gained a commit; the hook's decision must agree with what bash
# really did. Bash's own scoping is the ground truth.
#
# Every probe additionally carries a POSITIVE CONTROL in its own fixture (see
# oracle_check): an allow is an absence-assertion, and a rig that has stopped
# gating anything returns allow for everything and looks green. The control
# proves the fixture denies when it must before any allow from it is believed.
#
# Cases are derived from what the tokenizer's normalisation DISCARDS. It
# rewrites shell operators into separators, which resets command position but
# throws away SCOPE — and scope is load-bearing for a gate that attributes
# writes to directories. Hence the central pair:
#   '( cd X ) ; git commit'    -> bash unwinds the cd at ')': writes HERE
#   '{ cd X ; } ; git commit'  -> brace group runs in THIS shell: writes in X
# Subshell and brace group MUST differ; any implementation treating them alike
# is wrong by construction in one direction or the other.

echo "--- SABLE-vx4aj/hfkdd: real-bash oracle (hook decision vs. where bash actually writes) ---"

ORC_ROOT=""
ORC_CLAIMED=""
ORC_UNRELATED=""

# Fresh pair of real repos, both dirty so 'git commit -am' can succeed in
# EITHER one — otherwise a probe could "land nowhere" for an uninteresting
# reason and vacuously agree with an allow.
orc_setup() {
  ORC_ROOT="$(mktemp -d)"
  ORC_CLAIMED="$ORC_ROOT/claimed"
  ORC_UNRELATED="$ORC_ROOT/unrelated"
  local r
  for r in "$ORC_CLAIMED" "$ORC_UNRELATED"; do
    git init -q "$r"
    git -C "$r" config user.email oracle@test
    git -C "$r" config user.name oracle
    echo base > "$r/f.txt"
    git -C "$r" add f.txt
    git -C "$r" commit -qm init
    echo change >> "$r/f.txt"
  done
}
orc_teardown() { [ -n "$ORC_ROOT" ] && rm -rf "$ORC_ROOT"; ORC_ROOT=""; }

orc_expand() {
  # $1 = template using @CLAIMED@ / @UNRELATED@ placeholders
  local t="$1"
  t="${t//@CLAIMED@/$ORC_CLAIMED}"
  t="${t//@UNRELATED@/$ORC_UNRELATED}"
  printf '%s' "$t"
}

# oracle_check <label> <cwd: claimed|unrelated> <command template>
oracle_check() {
  local label="$1" cwdsel="$2" tmpl="$3"
  local cmd cwd truth c_before c_after u_before u_after

  # --- Phase 1: ORACLE. Real bash, real repos, hook not involved at all.
  orc_setup
  cmd="$(orc_expand "$tmpl")"
  if [ "$cwdsel" = "claimed" ]; then cwd="$ORC_CLAIMED"; else cwd="$ORC_UNRELATED"; fi
  c_before=$(git -C "$ORC_CLAIMED" rev-list --count HEAD)
  u_before=$(git -C "$ORC_UNRELATED" rev-list --count HEAD)
  ( cd "$cwd" && bash -c "$cmd" ) >/dev/null 2>&1
  c_after=$(git -C "$ORC_CLAIMED" rev-list --count HEAD)
  u_after=$(git -C "$ORC_UNRELATED" rev-list --count HEAD)
  orc_teardown

  if [ "$c_after" -gt "$c_before" ]; then
    truth="claimed"
  elif [ "$u_after" -gt "$u_before" ]; then
    truth="unrelated"
  else
    # The probe committed nowhere — a broken probe command, not a result.
    # Fail loudly rather than let it agree with 'allow' by accident.
    fail "oracle: $label" "PROBE BROKEN — bash committed to neither repo; cmd: $cmd"
    return
  fi

  # --- Phase 2: the hook, identical fresh sandbox, foreign fresh claim on
  #     the claimed repo.
  orc_setup
  cmd="$(orc_expand "$tmpl")"
  if [ "$cwdsel" = "claimed" ]; then cwd="$ORC_CLAIMED"; else cwd="$ORC_UNRELATED"; fi
  printf 'sess-HOLDER %s chuck\n' "$(date +%s)" > "$ORC_CLAIMED/.git/sable-tree-claim"
  local out decision want
  out=$(run_hook "$(make_json "$cmd" "sess-ME" "$cwd")")
  if is_deny "$out"; then decision="deny"; else decision="allow"; fi

  # --- POSITIVE CONTROL, in this same fixture, before teardown.
  # An ALLOW is an ABSENCE-assertion: a rig that has silently stopped gating
  # anything at all returns ALLOW for every probe and reads as a clean pass.
  # So prove the fixture still DENIES when it must, every time — a plain
  # 'git commit' into the claimed repo from its own cwd, which is
  # unconditionally gated. Without this pairing an allow-shaped assertion is
  # worthless: it cannot distinguish working from broken.
  local ctl ctl_ok
  printf 'sess-HOLDER %s chuck\n' "$(date +%s)" > "$ORC_CLAIMED/.git/sable-tree-claim"
  ctl=$(run_hook "$(make_json "git commit -qam control" "sess-ME" "$ORC_CLAIMED")")
  if is_deny "$ctl"; then ctl_ok="yes"; else ctl_ok="no"; fi
  orc_teardown

  if [ "$truth" = "claimed" ]; then want="deny"; else want="allow"; fi
  if [ "$ctl_ok" != "yes" ]; then
    fail "oracle: $label" "POSITIVE CONTROL FAILED — a plain 'git commit' into the claimed repo from its own cwd was NOT denied in this fixture, so the rig is not gating and no allow/deny result from it can be believed. control output: ${ctl:-<empty>}"
  elif [ "$decision" = "$want" ]; then
    pass "oracle: $label [bash wrote to $truth -> hook $decision; control denies]"
  else
    fail "oracle: $label" "bash wrote to $truth, so the hook must $want, but it returned $decision. cmd: $cmd"
  fi
}

# --- The scoping invariant: subshell unwinds, brace group persists ---------
# These four are the whole bead. The first is the regression that shipped:
# the write really lands in the claimed repo and the hook allowed it.
oracle_check "subshell cd is unwound at ')': '( cd <unrel> ) ; git commit' from claimed cwd" \
  claimed '( cd @UNRELATED@ && true ) ; git commit -qam probe'
oracle_check "brace-group cd PERSISTS past '}': '{ cd <unrel>; } ; git commit' from claimed cwd" \
  claimed '{ cd @UNRELATED@ ; true ; } ; git commit -qam probe'
oracle_check "subshell cd is unwound (mirror): '( cd <claimed> ) ; git commit' from unrelated cwd" \
  unrelated '( cd @CLAIMED@ && true ) ; git commit -qam probe'
oracle_check "brace-group cd PERSISTS (mirror): '{ cd <claimed>; } ; git commit' from unrelated cwd" \
  unrelated '{ cd @CLAIMED@ ; true ; } ; git commit -qam probe'

# --- The complement the old suite covered: cd and write both inside --------
oracle_check "both inside subshell: '( cd <unrel> && git commit )' from claimed cwd" \
  claimed '( cd @UNRELATED@ && git commit -qam probe )'
oracle_check "both inside subshell: '( cd <claimed> && git commit )' from unrelated cwd" \
  unrelated '( cd @CLAIMED@ && git commit -qam probe )'

# --- Nesting and ordering: scope must stack, not merely toggle -------------
oracle_check "nested subshell: '( ( cd <unrel> ) ; git commit )' from claimed cwd" \
  claimed '( ( cd @UNRELATED@ ) ; git commit -qam probe )'
oracle_check "persistent cd then subshell cd: only the persistent one survives" \
  claimed 'cd @UNRELATED@ ; ( cd @CLAIMED@ ) ; git commit -qam probe'
oracle_check "subshell cd then persistent cd: the persistent one wins" \
  unrelated '( cd @UNRELATED@ ) ; cd @CLAIMED@ ; git commit -qam probe'
oracle_check "backgrounded subshell: '( cd <unrel> ) & wait ; git commit' from claimed cwd" \
  claimed '( cd @UNRELATED@ ) & wait ; git commit -qam probe'

# --- Unmatched ')' — a case-pattern terminator, not a subshell close -------
# These belong to the paren work: ')' resets command position, and popping an
# empty scope stack must NOT relocate the shell cwd, because a case body runs
# in the CURRENT shell. Getting this wrong reopens the false negative.
#
# (Reserved words — 'if true; then git commit; fi' and friends — are a
# SEPARATE live false negative tracked as SABLE-hfkdd. Pre-existing on the
# base branch, deliberately not addressed or asserted here.)
oracle_check "case body: 'case x in x) git commit ;; esac' from claimed cwd" \
  claimed 'case x in x) git commit -qam probe ;; esac'
oracle_check "cd then case body: 'cd <claimed>; case x in x) git commit;; esac' from unrelated cwd" \
  unrelated 'cd @CLAIMED@ ; case x in x) git commit -qam probe ;; esac'

# --- Baselines: the shapes that already worked must still agree -----------
oracle_check "plain 'git commit' from claimed cwd" \
  claimed 'git commit -qam probe'
oracle_check "'cd <unrel> && git commit' from claimed cwd" \
  claimed 'cd @UNRELATED@ && git commit -qam probe'
oracle_check "'git -C <claimed> commit' from unrelated cwd" \
  unrelated 'git -C @CLAIMED@ commit -qam probe'
oracle_check "newline separator: 'cd <claimed>' NEWLINE 'git commit' from unrelated cwd" \
  unrelated 'cd @CLAIMED@
git commit -qam probe'

# ============================================================
# settings-snippet registration
# ============================================================

echo "--- Registration check ---"

SNIPPET="$REPO/templates/multi-manager/settings-snippet.json"
if python3 -c "import json; json.load(open('$SNIPPET'))" 2>/dev/null; then
  pass "settings-snippet.json is valid JSON"
else
  fail "settings-snippet.json is valid JSON"
fi

if grep -q 'tree-claim.sh' "$SNIPPET"; then
  pass "tree-claim.sh registered in settings-snippet.json"
else
  fail "tree-claim.sh registered in settings-snippet.json"
fi

# ============================================================
# Summary
# ============================================================

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then
  printf "Failed tests:%s\n" "$FAIL_NAMES"
  exit 1
fi
exit 0
