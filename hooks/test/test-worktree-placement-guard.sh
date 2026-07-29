#!/usr/bin/env bash
# test-worktree-placement-guard.sh — Unit + integration tests for
# hooks/multi-manager/worktree-placement-guard.sh (SABLE-djgy.2 / SABLE-z56d).
#
# Run:  bash hooks/test/test-worktree-placement-guard.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO/hooks/multi-manager/worktree-placement-guard.sh"

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

# make_json <command> <cwd>
make_json() {
  python3 -c "
import json, sys
cmd, cwd = sys.argv[1], sys.argv[2]
print(json.dumps({'tool_name': 'Bash', 'tool_input': {'command': cmd}, 'cwd': cwd}))
" "$1" "$2"
}

run_hook() {
  printf '%s' "$1" | bash "$HOOK" 2>/dev/null
}

is_deny() { printf '%s' "$1" | grep -q '"permissionDecision": *"deny"'; }
is_allow() { ! is_deny "$1"; }

SCRATCH_ROOT="$(mktemp -d)"
trap 'rm -rf "$SCRATCH_ROOT"' EXIT

# ==========================================================================
# Fixture: a plain git repo (no beads db needed for the classifier itself —
# the guard only shells out to `git rev-parse --show-toplevel`).
# ==========================================================================
REPO_A="$SCRATCH_ROOT/repo-a"
git init -q "$REPO_A"
git -C "$REPO_A" config user.email a@b.com
git -C "$REPO_A" config user.name a
git -C "$REPO_A" commit -q --allow-empty -m init

REPO_B="$SCRATCH_ROOT/repo-b"
git init -q "$REPO_B"
git -C "$REPO_B" config user.email a@b.com
git -C "$REPO_B" config user.name a
git -C "$REPO_B" commit -q --allow-empty -m init

NOT_A_REPO="$SCRATCH_ROOT/not-a-repo"
mkdir -p "$NOT_A_REPO"

# ==========================================================================
# UNIT: hazardous (repo-root-relative bare) forms DENY.
# ==========================================================================
echo "--- Unit: hazardous bare/relative forms DENY ---"

for CMD in "bd worktree create wk-foo" "bd worktree create wk-foo --branch bar" \
           "bd worktree create ./wk-foo" "bd worktree create sub/wk-foo"; do
  JSON=$(make_json "$CMD" "$REPO_A")
  OUT=$(run_hook "$JSON")
  if is_deny "$OUT"; then
    pass "denied: $CMD"
  else
    fail "denied: $CMD" "expected deny, got: $OUT"
  fi
done

# Deny reason names the hazard and the safe alternative — a bare "no" teaches
# nothing (same doctrine as stash-worktree-guard.sh's deny messages).
JSON=$(make_json "bd worktree create wk-foo" "$REPO_A")
OUT=$(run_hook "$JSON")
if printf '%s' "$OUT" | grep -q 'z56d' && printf '%s' "$OUT" | grep -q 'gitignore' \
   && printf '%s' "$OUT" | grep -q 'sable-spawn-worker'; then
  pass "deny reason names the z56d hazard, the .gitignore write, and the safe-form pointer"
else
  fail "deny reason names the z56d hazard, the .gitignore write, and the safe-form pointer" "got: $OUT"
fi

# ==========================================================================
# UNIT: safe (already-outside-the-repo) forms ALLOW silently.
# ==========================================================================
echo "--- Unit: safe absolute-sibling / outside-repo forms ALLOW ---"

for CMD in "bd worktree create $SCRATCH_ROOT/wk-sibling" \
           "bd worktree create ../wk-sibling-rel" \
           "bd worktree create ../../elsewhere/wk-far"; do
  JSON=$(make_json "$CMD" "$REPO_A")
  OUT=$(run_hook "$JSON")
  if is_allow "$OUT"; then
    pass "allowed: $CMD"
  else
    fail "allowed: $CMD" "expected allow, got: $OUT"
  fi
done

# ==========================================================================
# UNIT: unrelated bd subcommands and non-bd commands ALLOW silently — this
# guard's scope is `bd worktree create` ONLY.
# ==========================================================================
echo "--- Unit: unrelated commands ALLOW silently ---"

for CMD in "bd worktree remove wk-foo" "bd worktree list" "bd ready" "bd show SABLE-1" \
           "git status" "ls -la" "bd worktree create"; do
  JSON=$(make_json "$CMD" "$REPO_A")
  OUT=$(run_hook "$JSON")
  if is_allow "$OUT"; then
    pass "allowed (out of scope): $CMD"
  else
    fail "allowed (out of scope): $CMD" "expected allow, got: $OUT"
  fi
done

# ==========================================================================
# UNIT: base-directory resolution — `cd <dir> &&` and `bd -C <dir>` shift the
# effective base BEFORE the repo-root check, so the guard gates the command's
# ACTUAL target, not the ambient hook cwd (same discipline as tree-claim.sh).
# ==========================================================================
echo "--- Unit: cd / -C base-directory resolution ---"

# cd into repo B (a DIFFERENT repo than the hook's own cwd) then issue a bare
# create — must DENY against repo B's root, proving the cd shift is tracked.
JSON=$(make_json "cd $REPO_B && bd worktree create wk-foo" "$REPO_A")
OUT=$(run_hook "$JSON")
if is_deny "$OUT" && printf '%s' "$OUT" | grep -q "$REPO_B"; then
  pass "'cd $REPO_B && bd worktree create wk-foo' denies against repo B, not the hook's cwd (repo A)"
else
  fail "'cd $REPO_B && bd worktree create wk-foo' denies against repo B, not the hook's cwd (repo A)" "got: $OUT"
fi

# `bd -C <dir>` flag does the same shift without a shell `cd`.
JSON=$(make_json "bd -C $REPO_B worktree create wk-foo" "$REPO_A")
OUT=$(run_hook "$JSON")
if is_deny "$OUT" && printf '%s' "$OUT" | grep -q "$REPO_B"; then
  pass "'bd -C $REPO_B worktree create wk-foo' denies against repo B via the -C flag"
else
  fail "'bd -C $REPO_B worktree create wk-foo' denies against repo B via the -C flag" "got: $OUT"
fi

# A base directory that is not inside ANY git repo can't be gated (no repo
# root to compare against) — fail open, matching every other guard in this
# hooks/multi-manager/ layer.
JSON=$(make_json "bd worktree create wk-foo" "$NOT_A_REPO")
OUT=$(run_hook "$JSON")
if is_allow "$OUT"; then
  pass "bare create from a non-repo directory allows (nothing to gate against)"
else
  fail "bare create from a non-repo directory allows (nothing to gate against)" "got: $OUT"
fi

# ==========================================================================
# UNIT: unparseable / no-target input fails open (exit 0, no output) rather
# than crashing or wrongly denying.
# ==========================================================================
echo "--- Unit: fail-open on unparseable input ---"

OUT=$(printf 'not json at all' | bash "$HOOK" 2>/dev/null)
if is_allow "$OUT"; then
  pass "non-JSON stdin fails open"
else
  fail "non-JSON stdin fails open" "got: $OUT"
fi

OUT=$(printf '' | bash "$HOOK" 2>/dev/null)
if is_allow "$OUT"; then
  pass "empty stdin fails open"
else
  fail "empty stdin fails open" "got: $OUT"
fi

# ==========================================================================
# INTEGRATION (real repo, real bd, real git worktrees) — gated on `bd` being
# on PATH, per test-worker-flag-done.sh's --skip-agents/--skip-hooks
# convention (keeps the fixture repo free of CLAUDE.md/AGENTS.md scaffolding
# noise so the git-status comparison below is clean).
# ==========================================================================
if ! command -v bd >/dev/null 2>&1; then
  echo "SKIP: integration section (bd not on PATH)"
else
  echo "--- Integration: real repo, real bd, real git ---"

  MAIN="$SCRATCH_ROOT/main"
  mkdir -p "$MAIN"
  ( cd "$MAIN" && BD_NON_INTERACTIVE=1 bd init --prefix=wpg \
      --non-interactive --skip-agents --skip-hooks --quiet >/dev/null 2>&1 )
  git -C "$MAIN" config user.email a@b.com
  git -C "$MAIN" config user.name a
  git -C "$MAIN" add -A >/dev/null 2>&1
  git -C "$MAIN" commit -q -m "init: bd-initialized fixture repo" >/dev/null 2>&1 || true

  BASELINE="$(git -C "$MAIN" status --porcelain)"
  if [ -z "$BASELINE" ]; then
    pass "fixture sanity: MAIN's git status --porcelain is clean before any worktree-create attempt"
  else
    fail "fixture sanity: MAIN's git status --porcelain is clean before any worktree-create attempt" "got: $BASELINE"
  fi

  # --- Guarded bare form: the hook denies BEFORE the real command ever runs.
  # An agent obeying the hook's PreToolUse deny never executes `bd`, so we
  # assert the deny fires and (since we correctly never ran the real command)
  # the checkout is untouched.
  JSON=$(make_json "bd worktree create wk-integration" "$MAIN")
  OUT=$(run_hook "$JSON")
  if is_deny "$OUT"; then
    pass "integration: guard denies the bare form in a real bd-initialized repo"
  else
    fail "integration: guard denies the bare form in a real bd-initialized repo" "got: $OUT"
  fi

  AFTER_DENY="$(git -C "$MAIN" status --porcelain)"
  if [ "$AFTER_DENY" = "$BASELINE" ]; then
    pass "integration: MAIN's git status --porcelain is byte-identical after the denied attempt (nothing ran)"
  else
    fail "integration: MAIN's git status --porcelain is byte-identical after the denied attempt" "before=[$BASELINE] after=[$AFTER_DENY]"
  fi

  # --- Safe absolute-sibling form: the hook allows, and running it FOR REAL
  # creates a working worktree without touching MAIN at all.
  SIBLING="$SCRATCH_ROOT/wk-integration-sibling"
  JSON=$(make_json "bd worktree create $SIBLING --branch wk-integration-sibling-branch" "$MAIN")
  OUT=$(run_hook "$JSON")
  if is_allow "$OUT"; then
    pass "integration: guard allows the absolute-sibling form"
  else
    fail "integration: guard allows the absolute-sibling form" "got: $OUT"
  fi

  ( cd "$MAIN" && bd worktree create "$SIBLING" --branch wk-integration-sibling-branch >/dev/null 2>&1 )
  if [ -d "$SIBLING" ]; then
    pass "integration: the allowed absolute-sibling form actually creates the worktree"
  else
    fail "integration: the allowed absolute-sibling form actually creates the worktree" "$SIBLING does not exist"
  fi

  AFTER_SAFE="$(git -C "$MAIN" status --porcelain)"
  if [ "$AFTER_SAFE" = "$BASELINE" ]; then
    pass "integration: MAIN's git status --porcelain stays byte-identical after the real safe-form create"
  else
    fail "integration: MAIN's git status --porcelain stays byte-identical after the real safe-form create" "before=[$BASELINE] after=[$AFTER_SAFE]"
  fi

  git -C "$MAIN" worktree remove --force "$SIBLING" >/dev/null 2>&1 || true

  AFTER_REMOVE="$(git -C "$MAIN" status --porcelain)"
  if [ "$AFTER_REMOVE" = "$BASELINE" ]; then
    pass "integration: MAIN's git status --porcelain is byte-identical across the full create+remove cycle"
  else
    fail "integration: MAIN's git status --porcelain is byte-identical across the full create+remove cycle" "before=[$BASELINE] after=[$AFTER_REMOVE]"
  fi

  # --- NEGATIVE CONTROL: prove the guard is discriminating a REAL hazard,
  # not a strawman — an UNROUTED bare create (i.e. run directly, bypassing
  # the guard, as if no PreToolUse hook were wired) really does nest inside
  # MAIN and dirty its TRACKED .gitignore.
  echo "--- Integration: negative control (unrouted bare create really nests+dirties) ---"

  ( cd "$MAIN" && bd worktree create wk-negative-control --branch wk-negative-control-branch >/dev/null 2>&1 )

  if [ -d "$MAIN/wk-negative-control" ]; then
    pass "negative control: the unrouted bare form nests the worktree INSIDE the checkout"
  else
    fail "negative control: the unrouted bare form nests the worktree INSIDE the checkout" "$MAIN/wk-negative-control does not exist"
  fi

  AFTER_UNGUARDED="$(git -C "$MAIN" status --porcelain)"
  if [ "$AFTER_UNGUARDED" != "$BASELINE" ] && printf '%s' "$AFTER_UNGUARDED" | grep -q '\.gitignore'; then
    pass "negative control: the unrouted bare form dirties MAIN's tracked .gitignore (git status diverges from baseline)"
  else
    fail "negative control: the unrouted bare form dirties MAIN's tracked .gitignore" "before=[$BASELINE] after=[$AFTER_UNGUARDED]"
  fi

  # This is exactly what the guard's DENY on the same command (above) exists
  # to prevent — restore MAIN to baseline.
  git -C "$MAIN" worktree remove --force "$MAIN/wk-negative-control" >/dev/null 2>&1 || true
  git -C "$MAIN" checkout -q -- .gitignore 2>/dev/null || true

  RESTORED="$(git -C "$MAIN" status --porcelain)"
  if [ "$RESTORED" = "$BASELINE" ]; then
    pass "fixture cleanup: MAIN restored to baseline after the negative-control run"
  else
    fail "fixture cleanup: MAIN restored to baseline after the negative-control run" "got: $RESTORED"
  fi
fi

# ==========================================================================
# Summary
# ==========================================================================
echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="

if [ "$FAIL" -gt 0 ]; then
  printf "Failed tests:%b\n" "$FAIL_NAMES"
  exit 1
fi
exit 0
