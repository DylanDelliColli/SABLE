#!/usr/bin/env bash
# test-concurrent-sessions.sh — INTEGRATION test for SABLE-5hck: two repos hold
# independent SABLE modes at the same time.
#
# This is the keystone composition test (unit coverage lives in
# test-lib-mode-path.sh / test-sable-mode.sh / test-mode-interlock.sh /
# test-lib-identity.sh). It uses REAL git repos, the REAL bin/sable-mode binary,
# and the REAL mode-interlock.sh hook end-to-end — no mocking beyond the
# per-repo path resolution the resolver itself performs. Crucially it does NOT
# set SABLE_MODE_STATE, so the genuine cwd-based resolution is exercised.
#
# Proves:
#   - sable-mode set/get keep independent modes in two repos concurrently;
#     flipping one never disturbs the other.
#   - the state file lives inside each repo.
#   - mode-interlock enforces the boundary of the repo the tool call's cwd
#     belongs to (same command, opposite verdicts per repo).
#   - a linked git worktree shares its main checkout's mode.
#
# Run with:
#   bash hooks/test/test-concurrent-sessions.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
MODE_BIN="$REPO/bin/sable-mode"
HOOK="$REPO/hooks/multi-manager/mode-interlock.sh"

if [ ! -x "$MODE_BIN" ] || [ ! -x "$HOOK" ]; then
  echo "FAIL: sable-mode or mode-interlock not executable"
  exit 2
fi

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
assert_eq() { if [ "$2" = "$3" ]; then pass "$1"; else fail "$1" "expected '$2', got '$3'"; fi; }

# CRITICAL: no SABLE_MODE_STATE override — exercise real per-repo resolution.
unset SABLE_MODE_STATE 2>/dev/null || true

# Hermetic registry so the interlock classifies spawn targets (columbo=producer,
# optimus=manager) without reading the installed ~/.claude registry.
SABLE_AGENTS_YAML="$(mktemp)"
export SABLE_AGENTS_YAML
cat > "$SABLE_AGENTS_YAML" <<'YAML'
agents:
  optimus:
    type: epic_manager
  tarzan:
    type: one_off_manager
  columbo:
    type: test_planner
  sherlock:
    type: auditor
YAML

# The interlock Bash leg only engages for the lead session identity.
export CLAUDE_AGENT_NAME=lincoln
unset SABLE_ORCHESTRATION_FORCE 2>/dev/null || true

# Canonicalized temp git repo with one commit (worktree add needs a HEAD).
make_repo() {
  local d; d="$(cd "$(mktemp -d)" && pwd)"
  git -C "$d" init -q
  git -C "$d" -c user.email=t@t -c user.name=t commit --allow-empty -m init -q
  printf '%s\n' "$d"
}

REPO_A="$(make_repo)"   # → execution
REPO_B="$(make_repo)"   # → planning
trap 'rm -f "$SABLE_AGENTS_YAML"; rm -rf "$REPO_A" "$REPO_B"' EXIT

# ---------- two sessions flip independent modes concurrently ----------
( cd "$REPO_A" && "$MODE_BIN" set execution >/dev/null 2>&1 )
( cd "$REPO_B" && "$MODE_BIN" set planning  >/dev/null 2>&1 )

assert_eq "repo A reads its own execution mode" "execution" \
  "$(cd "$REPO_A" && "$MODE_BIN" get 2>/dev/null)"
assert_eq "repo B reads its own planning mode" "planning" \
  "$(cd "$REPO_B" && "$MODE_BIN" get 2>/dev/null)"

# the core concurrency property: flipping B does not disturb A
( cd "$REPO_B" && "$MODE_BIN" set planning >/dev/null 2>&1 )
assert_eq "repo A mode unaffected by repo B flip" "execution" \
  "$(cd "$REPO_A" && "$MODE_BIN" get 2>/dev/null)"

# state lives inside each repo
assert_eq "repo A state path is in-repo" \
  "$REPO_A/.claude/sable/state/mode-state.json" "$(cd "$REPO_A" && "$MODE_BIN" path 2>/dev/null)"
assert_eq "repo B state path is in-repo" \
  "$REPO_B/.claude/sable/state/mode-state.json" "$(cd "$REPO_B" && "$MODE_BIN" path 2>/dev/null)"

# ---------- interlock enforces each repo's mode by the tool call's cwd ----------
run_hook_cwd() {
  python3 -c "
import json, sys
print(json.dumps({'tool_input': {'command': sys.argv[1]}, 'cwd': sys.argv[2]}))
" "$1" "$2" | bash "$HOOK" 2>/dev/null
}
is_deny() { printf '%s' "$1" | grep -q '"permissionDecision": *"deny"'; }
assert_deny()  { local o; o="$(run_hook_cwd "$2" "$3")"; if is_deny "$o"; then pass "$1"; else fail "$1" "expected deny, got: ${o:-<empty>}"; fi; }
assert_allow() { local o; o="$(run_hook_cwd "$2" "$3")"; if is_deny "$o"; then fail "$1" "expected allow, got deny: $o"; else pass "$1"; fi; }

assert_deny  "interlock: producer spawn blocked in execution repo (by cwd)" 'columbo' "$REPO_A"
assert_allow "interlock: producer spawn allowed in planning repo (by cwd)"  'columbo' "$REPO_B"
assert_deny  "interlock: manager spawn blocked in planning repo (by cwd)"   'optimus' "$REPO_B"
assert_allow "interlock: manager spawn allowed in execution repo (by cwd)"  'optimus' "$REPO_A"

# ---------- a linked worktree shares its main checkout's mode ----------
WT="$(mktemp -u)"
git -C "$REPO_A" worktree add -q "$WT" -b concurrent-wt
WT_C="$(cd "$WT" && pwd)"
assert_eq "worktree resolves to main repo state path" \
  "$REPO_A/.claude/sable/state/mode-state.json" "$(cd "$WT_C" && "$MODE_BIN" path 2>/dev/null)"
assert_eq "worktree reads the main repo's execution mode" "execution" \
  "$(cd "$WT_C" && "$MODE_BIN" get 2>/dev/null)"
assert_deny "interlock: worktree cwd inherits main repo's execution boundary" \
  'columbo' "$WT_C"
git -C "$REPO_A" worktree remove --force "$WT" 2>/dev/null || rm -rf "$WT"

# ---------- Summary ----------
echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
