#!/usr/bin/env bash
# test-stash-worktree-guard.sh — Unit + integration tests for
# hooks/multi-manager/stash-worktree-guard.sh (SABLE-5dmh).
#
# SCOPE NOTE (binding ruling, lincoln via tarzan, SABLE-8hvqt): the ORIGINAL
# SABLE-5dmh bead spec said "allow any stash run from the PRIMARY checkout
# (git-dir == common-dir)". That case is VOID — superseded by lincoln's live
# ruling that the ban applies in EVERY checkout, primary included, because
# refs/stash is one shared stack regardless of which checkout pushes/pops.
# This suite tests the INVERSE of the original case (see
# "primary checkout is NOT exempt" below) instead of the voided one.
#
# Run:  bash hooks/test/test-stash-worktree-guard.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO/hooks/multi-manager/stash-worktree-guard.sh"

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
has_warning_text() {
  # asserts the shared-stack warning text is actually present in the
  # additionalContext, not just that the decision is "allow" (tarzan/SABLE-8hvqt:
  # "assert the warning text ... or the warn path is untested").
  printf '%s' "$1" | grep -q 'SINGLE stack shared across every worktree'
}
has_denial_context_labels() {
  printf '%s' "$1" | grep -Eq 'hook-input cwd:|matched command:'
}
has_denial_context() {
  local out="$1" expected_cwd="$2" expected_command="$3"
  printf '%s' "$out" | grep -Fq -- "hook-input cwd: $expected_cwd" &&
    printf '%s' "$out" | grep -Fq -- "matched command: $expected_command"
}

SCRATCH_ROOT="$(mktemp -d)"
trap 'rm -rf "$SCRATCH_ROOT"' EXIT

# ==========================================================================
# UNIT: deny cases
# ==========================================================================
echo "--- Unit: deny cases ---"

for CMD in "git stash" "git stash pop" "git stash drop" "git stash apply" "git stash push -m plain" "git stash save plain" "git stash clear"; do
  JSON=$(make_json "$CMD" "$SCRATCH_ROOT")
  OUT=$(run_hook "$JSON")
  if is_deny "$OUT"; then
    pass "denied: $CMD"
  else
    fail "denied: $CMD" "expected deny, got: $OUT"
  fi
done

# 'git stash clear' has no break-glass form at all — assert the reason says so.
JSON=$(make_json "git stash clear" "$SCRATCH_ROOT")
OUT=$(run_hook "$JSON")
if printf '%s' "$OUT" | grep -q 'no break-glass form'; then
  pass "'git stash clear' deny reason states no break-glass form exists"
else
  fail "'git stash clear' deny reason states no break-glass form exists" "got: $OUT"
fi

# Deny reason hands over BOTH worker-facing alternatives (tarzan/SABLE-8hvqt
# doctrine: a guard that only says no teaches nothing).
JSON=$(make_json "git stash" "$SCRATCH_ROOT")
OUT=$(run_hook "$JSON")
if printf '%s' "$OUT" | grep -q 'diff-to-file' && printf '%s' "$OUT" | grep -q 'git show origin'; then
  pass "deny reason names both alternatives (diff-to-file + git show origin/<base>:<path>)"
else
  fail "deny reason names both alternatives" "got: $OUT"
fi

# ==========================================================================
# UNIT: break-glass forms ALLOW but with the shared-stack WARNING present
# (binding ruling: break-glass is warn, never a silent allow).
# ==========================================================================
echo "--- Unit: break-glass allow-with-warning cases ---"

for CMD in "git stash pop stash@{2}" "git stash apply stash@{0}" "git stash drop stash@{1}" "git stash push -m 'scope: doing thing'"; do
  JSON=$(make_json "$CMD" "$SCRATCH_ROOT")
  OUT=$(run_hook "$JSON")
  if is_allow "$OUT"; then
    pass "break-glass allowed: $CMD"
  else
    fail "break-glass allowed: $CMD" "expected allow, got: $OUT"
  fi
  if has_warning_text "$OUT"; then
    pass "break-glass carries shared-stack warning: $CMD"
  else
    fail "break-glass carries shared-stack warning: $CMD" "got: $OUT"
  fi
  if ! has_denial_context_labels "$OUT"; then
    pass "break-glass carries no misleading denial context: $CMD"
  else
    fail "break-glass carries no misleading denial context: $CMD" "got: $OUT"
  fi
done

# ==========================================================================
# UNIT: non-gated stash subcommands + non-stash commands ALLOW silently
# (no warning text — these are not part of the banned set).
# ==========================================================================
echo "--- Unit: silent-allow cases ---"

for CMD in "git stash list" "git stash show" "git status" "git diff" "git log --oneline" "ls" "git fetch origin"; do
  JSON=$(make_json "$CMD" "$SCRATCH_ROOT")
  OUT=$(run_hook "$JSON")
  if is_allow "$OUT" && ! has_warning_text "$OUT" && ! has_denial_context_labels "$OUT"; then
    pass "silent allow: $CMD"
  else
    fail "silent allow: $CMD" "got: $OUT"
  fi
done

# ==========================================================================
# UNIT: primary checkout is NOT exempt (binding ruling — supersedes the
# original bead's "allow from primary checkout" case, which is now VOID).
# A real, non-worktree repo (git-dir == common-dir) must still DENY a bare
# 'git stash'.
# ==========================================================================
echo "--- Unit: primary checkout is not exempt ---"

PRIMARY="$SCRATCH_ROOT/primary-repo"
git init -q "$PRIMARY"
git -C "$PRIMARY" commit -q --allow-empty -m init
GITDIR=$(git -C "$PRIMARY" rev-parse --absolute-git-dir)
COMMONDIR=$(git -C "$PRIMARY" rev-parse --git-common-dir)
case "$COMMONDIR" in /*) ;; *) COMMONDIR="$PRIMARY/$COMMONDIR" ;; esac
if [ "$GITDIR" = "$COMMONDIR" ]; then
  pass "fixture sanity: PRIMARY is a real primary checkout (git-dir == common-dir)"
else
  fail "fixture sanity: PRIMARY is a real primary checkout" "git-dir=$GITDIR common-dir=$COMMONDIR"
fi

JSON=$(make_json "git stash" "$PRIMARY")
OUT=$(run_hook "$JSON")
if is_deny "$OUT"; then
  pass "bare 'git stash' from the PRIMARY checkout DENIES (worktree location is not part of the decision)"
else
  fail "bare 'git stash' from the PRIMARY checkout DENIES" "got: $OUT"
fi

# SABLE-fye8l RED: preserve the payload cwd and only the matched stash command
# segment. The unrelated segments are distinctive negative controls: logging
# the whole Bash line would preserve more transcript than the denial needs.
PRIMARY_MATCHED="git stash push -m primary-unscoped-marker"
PRIMARY_BEFORE="primary-unrelated-before-marker"
PRIMARY_AFTER="primary-unrelated-after-marker"
PRIMARY_COMMAND="printf $PRIMARY_BEFORE ; $PRIMARY_MATCHED ; printf $PRIMARY_AFTER"
JSON=$(make_json "$PRIMARY_COMMAND" "$PRIMARY")
OUT=$(run_hook "$JSON")
if is_deny "$OUT" &&
   has_denial_context "$OUT" "$PRIMARY" "$PRIMARY_MATCHED" &&
   ! printf '%s' "$OUT" | grep -Fq -- "$PRIMARY_BEFORE" &&
   ! printf '%s' "$OUT" | grep -Fq -- "$PRIMARY_AFTER"; then
  pass "PRIMARY denial records hook-input cwd and only the exact matched stash command segment"
else
  fail "PRIMARY denial records hook-input cwd and only the exact matched stash command segment" "got: $OUT"
fi

# Environment assignments are stripped before classification and may carry
# credentials. They are not part of the matched git invocation and must never
# be copied into denial output.
ENV_SECRET="fye8l-env-secret-do-not-log"
ENV_MATCHED="git stash push -m plain"
ENV_COMMAND="API_TOKEN=$ENV_SECRET $ENV_MATCHED"
JSON=$(make_json "$ENV_COMMAND" "$PRIMARY")
OUT=$(run_hook "$JSON")
if is_deny "$OUT" &&
   has_denial_context "$OUT" "$PRIMARY" "$ENV_MATCHED" &&
   ! printf '%s' "$OUT" | grep -Fq -- "$ENV_SECRET" &&
   ! printf '%s' "$OUT" | grep -Fq -- "API_TOKEN="; then
  pass "denial context omits stripped environment assignments and secrets"
else
  fail "denial context omits stripped environment assignments and secrets" "got: $OUT"
fi

# Diagnostic context is flattened before it reaches a terminal or transcript.
# Exercise both the raw payload cwd and the shlex-rendered matched command.
CONTROL_CWD=$'/tmp/fye8l-control\nnext\tfield\e[31m'
CONTROL_COMMAND=$'git stash push -m plain\nnext\tfield\e[31m'
JSON=$(make_json "$CONTROL_COMMAND" "$CONTROL_CWD")
OUT=$(run_hook "$JSON")
if is_deny "$OUT" && printf '%s' "$OUT" | python3 -c '
import json, sys
payload = json.load(sys.stdin)
reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
context = reason.split("Denial context -- ", 1)[1]
bad = "\r\n\t\x1b"
raise SystemExit(0 if not any(ch in context for ch in bad) and "?" in context else 1)
'; then
  pass "denial context flattens line controls and replaces terminal controls"
else
  fail "denial context flattens line controls and replaces terminal controls" "got: $OUT"
fi

# Both user-derived fields are independently capped at 512 characters.
printf -v LONG_FILL '%600s' ''
LONG_FILL=${LONG_FILL// /x}
LONG_CWD="/tmp/$LONG_FILL"
LONG_COMMAND="git stash push -m $LONG_FILL"
JSON=$(make_json "$LONG_COMMAND" "$LONG_CWD")
OUT=$(run_hook "$JSON")
if is_deny "$OUT" && printf '%s' "$OUT" | python3 -c '
import json, sys
payload = json.load(sys.stdin)
reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
context = reason.split("Denial context -- hook-input cwd: ", 1)[1]
cwd, matched = context.split("; matched command: ", 1)
matched = matched[:-1] if matched.endswith(".") else matched
bounded = len(cwd) <= 512 and len(matched) <= 512
truncated = cwd.endswith("...") and matched.endswith("...")
raise SystemExit(0 if bounded and truncated else 1)
'; then
  pass "denial context independently bounds cwd and matched command"
else
  fail "denial context independently bounds cwd and matched command" "got: $OUT"
fi

# ==========================================================================
# INTEGRATION: the guard closes the exact SABLE-nhrb crossing that
# test-worktree-isolation.sh Test 3 reproduces as UNGUARDED. Real git, real
# worktrees, real shared stash stack — no mocks.
#
# NOTE: per tarzan/SABLE-8hvqt point 3 ("do NOT use git stash while BUILDING
# the stash guard"), we still use real 'git stash' calls here on the WRITE
# side to set up the fixture — that instruction targets the guard's own
# development workflow (don't reach for stash as a shortcut while iterating
# on this file), not this harness's job of reproducing the exact mechanism
# the guard defends against. test-worktree-isolation.sh's own Test 3 does the
# same for the same reason.
# ==========================================================================
echo "--- Integration: guard closes the nhrb crossing (real git, real worktrees) ---"

MAIN="$SCRATCH_ROOT/main"
git init -q "$MAIN"
mkdir -p "$MAIN/hooks"
printf 'ORIGINAL tdd-gate\n' > "$MAIN/hooks/tdd-gate.sh"
git -C "$MAIN" add -A
git -C "$MAIN" commit -qm "init: shared-tracked hooks/tdd-gate.sh"

WK_A="$SCRATCH_ROOT/wk-A"
WK_B="$SCRATCH_ROOT/wk-B"
git -C "$MAIN" worktree add -q "$WK_A" -b wk-A
git -C "$MAIN" worktree add -q "$WK_B" -b wk-B

# Worktree A stashes a real edit onto the SHARED stack.
printf 'A-STASH-EDIT tdd-gate\n' > "$WK_A/hooks/tdd-gate.sh"
git -C "$WK_A" stash -q

shared_entries="$(git -C "$WK_B" stash list 2>/dev/null | grep -c . || true)"
if [ "$shared_entries" -ge 1 ]; then
  pass "fixture sanity: A's stash is visible from sibling B (shared stack confirmed)"
else
  fail "fixture sanity: A's stash is visible from sibling B" "shared_entries=$shared_entries"
fi

# Worker B attempts ordinary rebase hygiene: a bare 'git stash pop', run from
# ITS OWN worktree (a linked worktree, NOT the primary checkout) — the guard
# must deny this BEFORE the pop ever executes, closing the exact crossing
# test-worktree-isolation.sh Test 3 shows landing unguarded.
JSON=$(make_json "git stash pop" "$WK_B")
OUT=$(run_hook "$JSON")
if is_deny "$OUT"; then
  pass "guard DENIES a bare 'git stash pop' issued from worktree B while A's stash sits on the shared stack"
else
  fail "guard DENIES a bare 'git stash pop' issued from worktree B" "got: $OUT"
fi

# SABLE-fye8l RED at the linked-worktree seam. `--quiet` makes the exact
# matched form distinguishable from the generic prose already in the reason.
LINKED_MATCHED="git stash pop --quiet"
LINKED_BEFORE="linked-unrelated-before-marker"
LINKED_AFTER="linked-unrelated-after-marker"
LINKED_COMMAND="printf $LINKED_BEFORE ; $LINKED_MATCHED ; printf $LINKED_AFTER"
JSON=$(make_json "$LINKED_COMMAND" "$WK_B")
OUT=$(run_hook "$JSON")
if is_deny "$OUT" &&
   has_denial_context "$OUT" "$WK_B" "$LINKED_MATCHED" &&
   ! printf '%s' "$OUT" | grep -Fq -- "$LINKED_BEFORE" &&
   ! printf '%s' "$OUT" | grep -Fq -- "$LINKED_AFTER"; then
  pass "linked-worktree denial records hook-input cwd and only the exact matched stash command segment"
else
  fail "linked-worktree denial records hook-input cwd and only the exact matched stash command segment" "got: $OUT"
fi

# Confirm B's checkout is untouched (the guard fired before any real pop ran
# — the hook only classifies/denies, it never executes the command itself,
# but this pins down that B's tree still shows the ORIGINAL content, i.e. the
# contamination never happened in this run).
b_content="$(cat "$WK_B/hooks/tdd-gate.sh" 2>/dev/null || true)"
if [ "$b_content" = "ORIGINAL tdd-gate" ]; then
  pass "worktree B's file is untouched (guard fired before any pop reached B's tree)"
else
  fail "worktree B's file is untouched" "got: $b_content"
fi

# And the break-glass form — explicit index, from the same worktree B — is
# still ALLOWED (with warning), so legitimate scoped recovery is not blocked.
JSON=$(make_json "git stash pop stash@{0}" "$WK_B")
OUT=$(run_hook "$JSON")
if is_allow "$OUT" && has_warning_text "$OUT"; then
  pass "break-glass 'git stash pop stash@{0}' from worktree B still allowed, with warning"
else
  fail "break-glass 'git stash pop stash@{0}' from worktree B still allowed, with warning" "got: $OUT"
fi

# Clean up the real shared stash entry we pushed for this fixture — restore
# via the explicit-index form we just proved the guard allows, rather than
# leaving a stray entry on the shared stack for whatever runs next.
git -C "$WK_B" stash drop -q stash@{0} 2>/dev/null || true

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
