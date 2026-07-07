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
# JSON with no session_id; unset CLAUDE_SESSION_ID so identity is truly unknown
JSON=$(python3 -c "
import json
print(json.dumps({'tool_name':'Bash','tool_input':{'command':'git add .'},'cwd':'$SCRATCH'}))
")
# env -u cannot call shell functions; use a subshell to unset the variable
OUT=$(
  unset CLAUDE_SESSION_ID
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

# identity-unknown invocation (no session_id, CLAUDE_SESSION_ID unset)
JSON_UNK=$(python3 -c "
import json
print(json.dumps({'tool_name':'Bash','tool_input':{'command':'git add .'},'cwd':'$SCRATCH'}))
")
OUT_UNK=$(
  unset CLAUDE_SESSION_ID
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
