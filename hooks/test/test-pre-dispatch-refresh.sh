#!/usr/bin/env bash
# test-pre-dispatch-refresh.sh — worktree-targeting + rebase for
# pre-dispatch-refresh.sh (SABLE-uz9.15; realizes the orphaned SABLE-sp2
# skeleton contract). Native dispatch: a manager-subagent's Agent-call hook
# input carries the MANAGER's cwd (main checkout), so the rebase target must
# come from a structured `Worktree: /abs/path` line in the prompt FIRST; cwd is
# the fallback only when the line is absent. Uses real scratch git repos with
# `git worktree add` (no filesystem mocking).
#
# Run with:
#   bash hooks/test/test-pre-dispatch-refresh.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$REPO/hooks/multi-manager/pre-dispatch-refresh.sh"

PASS=0; FAIL=0; FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

if [ ! -x "$HOOK" ]; then echo "FAIL: hook not executable at $HOOK"; exit 2; fi

FIX="$(mktemp -d)"; trap 'rm -rf "$FIX"' EXIT
AGENTS_YAML="$FIX/agents.yaml"
cat > "$AGENTS_YAML" <<'YAML'
agents:
  optimus:
    type: epic_manager
YAML
NONEXISTENT_MODE="$FIX/no-mode.json"   # hermetic — never read live cockpit (SABLE-wtv)

# setup_repo <root> — sets BARE/MAIN/WT. origin/main advanced to commit "Y";
# the worktree branch "feat" sits at X->W (no Y) so a correct rebase pulls Y in.
setup_repo() {
  local root="$1"
  BARE="$root/bare.git"; MAIN="$root/main"; WT="$root/wt"
  git init -q --bare "$BARE"
  git clone -q "$BARE" "$MAIN" 2>/dev/null
  git -C "$MAIN" config user.email t@t; git -C "$MAIN" config user.name t
  echo base > "$MAIN/f.txt"; git -C "$MAIN" add f.txt; git -C "$MAIN" commit -qm X
  git -C "$MAIN" push -q origin HEAD:refs/heads/main 2>/dev/null
  git -C "$MAIN" worktree add -q -b feat "$WT" 2>/dev/null
  git -C "$WT" config user.email t@t; git -C "$WT" config user.name t
  echo work > "$WT/w.txt"; git -C "$WT" add w.txt; git -C "$WT" commit -qm W
  echo more >> "$MAIN/f.txt"; git -C "$MAIN" commit -qam Y
  git -C "$MAIN" push -q origin HEAD:refs/heads/main 2>/dev/null
}

# hook_input <agent_type> <cwd> <prompt>
hook_input() {
  python3 -c "
import json,sys
at,cwd,prompt=sys.argv[1],sys.argv[2],sys.argv[3]
print(json.dumps({'tool_name':'Agent','agent_id':'a1','agent_type':at,'cwd':cwd,
  'tool_input':{'subagent_type':'general-purpose','prompt':prompt}}))
" "$1" "$2" "$3"
}

run() { # <json> ; echoes hook stdout
  printf '%s' "$1" | env -u CLAUDE_AGENT_NAME -u CLAUDE_AGENT_ROLE \
    SABLE_AGENTS_YAML="$AGENTS_YAML" SABLE_MODE_FILE="$NONEXISTENT_MODE" \
    SABLE_BASE_BRANCH=origin/main bash "$HOOK" 2>/dev/null
}

has_Y() { git -C "$1" log --format=%s 2>/dev/null | grep -qx Y; }

# --- Case A: structured Worktree line targets that checkout ---
R="$FIX/A"; mkdir -p "$R"; setup_repo "$R"
run "$(hook_input optimus "$MAIN" "Work SABLE-x.
Worktree: $WT
Run the tests.")" >/dev/null
if has_Y "$WT"; then pass "case A: Worktree line targets that checkout (worktree rebased onto origin/main)"
else fail "case A: Worktree line targets that checkout" "worktree 'feat' lacks Y after refresh"; fi

# --- Case B: no Worktree line falls back to hook-input cwd ---
R="$FIX/B"; mkdir -p "$R"; setup_repo "$R"
run "$(hook_input optimus "$WT" "Work SABLE-x in this checkout. Run the tests.")" >/dev/null
if has_Y "$WT"; then pass "case B: no Worktree line falls back to cwd (cwd rebased)"
else fail "case B: no Worktree line falls back to cwd" "cwd worktree lacks Y after refresh"; fi

# --- Case C: malformed (relative) Worktree line ignored w/ advisory + cwd fallback ---
R="$FIX/C"; mkdir -p "$R"; setup_repo "$R"
OUT=$(run "$(hook_input optimus "$WT" "Work SABLE-x.
Worktree: ../some/relative/path
Run tests.")")
if echo "$OUT" | grep -qi "absolute"; then pass "case C: relative Worktree line emits an advisory"
else fail "case C: relative Worktree line emits an advisory" "got: ${OUT:-<empty>}"; fi
if has_Y "$WT"; then pass "case C: relative Worktree line falls back to cwd (cwd rebased)"
else fail "case C: relative Worktree line falls back to cwd" "cwd worktree lacks Y"; fi

# --- Nonexistent absolute Worktree path: advisory + fail open (no rebase) ---
R="$FIX/N"; mkdir -p "$R"; setup_repo "$R"
OUT=$(run "$(hook_input optimus "$MAIN" "Work SABLE-x.
Worktree: $R/does-not-exist
Run tests.")"); RC=$?
if echo "$OUT" | grep -qi "not found\|does not exist"; then pass "nonexistent Worktree path emits an advisory"
else fail "nonexistent Worktree path emits an advisory" "got: ${OUT:-<empty>}"; fi
# A manager typo in the Worktree path must never crash the dispatch (SABLE-sp2 case 5).
if [ "$RC" -eq 0 ]; then pass "nonexistent Worktree path fails open (exit 0, no crash)"
else fail "nonexistent Worktree path fails open (exit 0, no crash)" "hook exited $RC"; fi

# --- Duplicate Worktree lines: first match wins ---
R="$FIX/D"; mkdir -p "$R"; setup_repo "$R"
# second worktree that must NOT be the target — branch it from X (origin/main~1)
# so it does NOT already contain Y; a correct hook leaves it untouched.
git -C "$MAIN" worktree add -q -b feat2 "$R/wt2" origin/main~1 2>/dev/null
run "$(hook_input optimus "$MAIN" "Work SABLE-x.
Worktree: $WT
Worktree: $R/wt2
go.")" >/dev/null
if has_Y "$WT" && ! has_Y "$R/wt2"; then pass "duplicate Worktree lines: first match wins"
else fail "duplicate Worktree lines: first match wins" "wt hasY=$(has_Y "$WT" && echo y || echo n) wt2 hasY=$(has_Y "$R/wt2" && echo y || echo n)"; fi

# --- Trailing whitespace / CR on the Worktree line parses cleanly ---
R="$FIX/W"; mkdir -p "$R"; setup_repo "$R"
run "$(hook_input optimus "$MAIN" "$(printf 'Work SABLE-x.\nWorktree: %s  \t\r\ngo.' "$WT")")" >/dev/null
if has_Y "$WT"; then pass "Worktree line with trailing whitespace/CR parses cleanly"
else fail "Worktree line with trailing whitespace/CR parses cleanly" "worktree lacks Y"; fi

# --- Non-manager dispatch stands down regardless of Worktree line ---
R="$FIX/S"; mkdir -p "$R"; setup_repo "$R"
run "$(hook_input general-purpose "$MAIN" "Work SABLE-x.
Worktree: $WT
go.")" >/dev/null
if has_Y "$WT"; then fail "non-manager dispatch stands down (no rebase)" "worktree was rebased despite non-manager dispatcher"
else pass "non-manager dispatch stands down regardless of Worktree line"; fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
