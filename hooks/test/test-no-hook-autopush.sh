#!/usr/bin/env bash
# test-no-hook-autopush.sh — regression guard for SABLE-81dr
#
# INVARIANT UNDER TEST
# --------------------
# No Bash-matched Claude Code hook (PreToolUse or PostToolUse) may execute a
# `git push` as a side effect. Hooks run on EVERY Bash tool call, so a stray
# push in any of them fires on every command — the exact class chuck reported as
# SABLE-81dr ("auto-push path fires 'git push wk-merge-gate' against the WRONG
# repo on non-push Bash commands") and the git twin of SABLE-rq9k (post-push-
# merge-notify's unguarded `bd create` auto-pushing the Dolt remote).
#
# The SABLE-81dr forensic conclusion was that NO standing auto-push path existed:
# the observed event was a transient async output-bleed from chuck's persistent
# mbp shell (all of his real pushes were hook-DENIED and never ran). This test is
# the durable FORWARD guard the bead's TEST SPEC asked for: it fails the build
# the moment ANY Bash-matched hook reintroduces a `git push` side effect —
# whether on a non-push command (the reproducer shape) or during a real push.
#
# MECHANISM
# ---------
# A `git` shim on PATH forwards every call to real git but RECORDS each
# invocation, tagging the ones whose git subcommand (after skipping -C/-c/global
# flags) is `push`. Each hook is driven with the two exact SABLE-81dr reproducer
# commands (a `bd ... update ... --append-notes` and a `sable-msg ...`, both
# NON-push) plus, for the push-capable hooks, a real `git push` command — and the
# push-tag count in the shim log must stay ZERO after the hook runs. A positive
# control proves the shim actually detects a push (so the ZERO assertions are not
# vacuous).
#
# Run with:
#   bash hooks/test/test-no-hook-autopush.sh
#
# Clean-room safe (SABLE-59zu): stubs bd/sable-msg/gh/tmux; needs only
# python3 + bash + git. Sandboxed via lib-git-sandbox.sh (SABLE-0ssz.2).

set -uo pipefail

# Resolve all absolute paths BEFORE the sandbox preamble cds away (SABLE-0ssz.2).
TESTDIR="$(cd "$(dirname "$0")" && pwd)"
HOOKS_DIR="$(cd "$TESTDIR/.." && pwd)"
MM_DIR="$HOOKS_DIR/multi-manager"
REAL_GIT="$(command -v git)"

# Env-neutralize real-repo git escapes for the suite duration.
# shellcheck source=lib-git-sandbox.sh
source "$TESTDIR/lib-git-sandbox.sh"

PASS=0
FAIL=0
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

# --------------------------------------------------------------------------
# Fixtures: stub dir + git recorder shim + a real scratch repo
# --------------------------------------------------------------------------
STUB_DIR="$(mktemp -d)"
GIT_LOG="$STUB_DIR/git-calls.log"
FIXTURE_REPO="$(mktemp -d)"
FIXTURE_ORIGIN="$(mktemp -d)"
cleanup_local() { rm -rf "$STUB_DIR" "$FIXTURE_REPO" "$FIXTURE_ORIGIN"; sable_test_git_sandbox_cleanup; }
trap cleanup_local EXIT

# git recorder shim: logs every invocation, tags real `push` subcommands, then
# forwards to real git (a push forwarded here reaches only the sandbox/fixture
# origins the preamble redirects to — never a real remote). The push detector
# skips a leading `-C <path>`, `-c <cfg>`, and `--global`-style flags so the
# FIRST bareword is the true subcommand (so a path containing "push" is not a
# false positive, and `git -C x push` IS a true positive).
cat > "$STUB_DIR/git" <<EOF
#!/usr/bin/env bash
echo "PUSHPROBE|\$*" >> "$GIT_LOG"
sub=""
skip=0
for a in "\$@"; do
  if [ "\$skip" = "1" ]; then skip=0; continue; fi
  case "\$a" in
    -C|-c) skip=1; continue ;;
    -*) continue ;;
    *) sub="\$a"; break ;;
  esac
done
[ "\$sub" = "push" ] && echo "GITPUSH|\$*" >> "$GIT_LOG"
exec "$REAL_GIT" "\$@"
EOF
chmod +x "$STUB_DIR/git"

# Inert stubs for every external the hooks may shell out to (clean-room has none
# of these). None must ever cause a git push.
for tool in bd sable-msg gh; do
  cat > "$STUB_DIR/$tool" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "$STUB_DIR/$tool"
done
# tmux stub: emit a chuck/optimus role line for the presence probes; never push.
cat > "$STUB_DIR/tmux" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do
  [ "$a" = "list-panes" ] && { echo "chuck"; echo "optimus"; exit 0; }
  [ "$a" = "display-message" ] && { echo ""; exit 0; }
done
exit 0
EOF
chmod +x "$STUB_DIR/tmux"

# Real scratch repo with an origin (file remote) so push-capable hooks' fetch/
# rebase/ls-remote reads resolve locally and fast.
git init -q --bare "$FIXTURE_ORIGIN"
git clone -q "$FIXTURE_ORIGIN" "$FIXTURE_REPO" 2>/dev/null
git -C "$FIXTURE_REPO" config user.email "test@sable.invalid"
git -C "$FIXTURE_REPO" config user.name "SABLE Test"
echo x > "$FIXTURE_REPO/f.txt"
git -C "$FIXTURE_REPO" add f.txt
git -C "$FIXTURE_REPO" commit -q -m init
git -C "$FIXTURE_REPO" push -q origin HEAD:refs/heads/main 2>/dev/null
git -C "$FIXTURE_REPO" update-ref refs/remotes/origin/main HEAD

# Manager identity so the push-capable hooks reach their git-op paths rather than
# standing down at the identity gate (same shape as test-post-push-merge-notify).
MGR_ENV=(CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager)

# The two EXACT SABLE-81dr reproducer commands (both NON-push) + a real push.
CMD_BD='bd -C /home/ddc/dev-environment/SABLE update SABLE-1238 --append-notes "triage note"'
CMD_MSG='sable-msg lincoln "override attempt OUTCOME (documented), git push still blocked"'
CMD_PUSH="git -C $FIXTURE_REPO push origin main"

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
# make_input <event> <command> <cwd>  → JSON on stdout
#   event = pre → PreToolUse shape ; post → PostToolUse shape (with tool_response)
make_input() {
  python3 -c "
import json, sys
event, cmd, cwd = sys.argv[1], sys.argv[2], sys.argv[3]
d = {'tool_input': {'command': cmd}, 'cwd': cwd}
if event == 'post':
    d['tool_response'] = {'stdout': '', 'stderr': ''}
print(json.dumps(d))
" "$1" "$2" "$3"
}

# run_hook_and_count_pushes <hook-abs> <event> <command>  → echoes push count
run_hook_and_count_pushes() {
  local hook="$1" event="$2" cmd="$3" json
  json="$(make_input "$event" "$cmd" "$FIXTURE_REPO")"
  : > "$GIT_LOG"
  # Prepend STUB_DIR so the git shim + external stubs win; keep the sandbox git
  # config so forwarded git ops stay hermetic. Timeout-bound so a hung hook
  # (e.g. a network fetch) cannot wedge the suite.
  env -i PATH="$STUB_DIR:$PATH" HOME="$HOME" \
      GIT_CONFIG_GLOBAL="${GIT_CONFIG_GLOBAL:-}" GIT_CONFIG_SYSTEM="${GIT_CONFIG_SYSTEM:-/dev/null}" \
      SABLE_SKIP_PRE_PUSH=1 SABLE_PRE_PUSH_TEST_PHASE=skip \
      "${MGR_ENV[@]}" \
      timeout 60 bash "$hook" <<< "$json" >/dev/null 2>&1 || true
  # grep -c always prints the count (0 when none); its exit-1-on-zero is why we
  # capture rather than rely on the exit code (a `|| echo 0` would double-print).
  local c
  c="$(grep -c '^GITPUSH|' "$GIT_LOG" 2>/dev/null)"
  echo "${c:-0}"
}

# assert_no_push <name> <hook-abs> <event> <command>
assert_no_push() {
  local name="$1" hook="$2" event="$3" cmd="$4" n
  n="$(run_hook_and_count_pushes "$hook" "$event" "$cmd")"
  if [ "$n" -eq 0 ]; then
    pass "$name"
  else
    fail "$name" "hook executed $n git push(es): $(grep '^GITPUSH|' "$GIT_LOG" | head -3)"
  fi
}

# --------------------------------------------------------------------------
# Positive control — the shim MUST detect a real push (non-vacuity proof).
# --------------------------------------------------------------------------
: > "$GIT_LOG"
PATH="$STUB_DIR:$PATH" git -C "$FIXTURE_REPO" push origin main >/dev/null 2>&1 || true
if [ "$(grep -c '^GITPUSH|' "$GIT_LOG")" -ge 1 ]; then
  pass "positive control: git shim records a real 'git -C <path> push'"
else
  fail "positive control: git shim did NOT record a real push — every ZERO assertion below is vacuous"
fi
# And a non-push git call must NOT be tagged as a push.
: > "$GIT_LOG"
PATH="$STUB_DIR:$PATH" git -C "$FIXTURE_REPO" status >/dev/null 2>&1 || true
if [ "$(grep -c '^GITPUSH|' "$GIT_LOG")" -eq 0 ]; then
  pass "positive control: 'git status' is NOT mis-tagged as a push"
else
  fail "positive control: 'git status' was mis-tagged as a push (detector too greedy)"
fi

# --------------------------------------------------------------------------
# The guard: every Bash-matched hook, on the reproducer commands, pushes NOTHING.
# --------------------------------------------------------------------------
# Bash-matched hooks as wired in settings.json (PreToolUse then PostToolUse).
# Format: <event>:<abs-path>
BASH_HOOKS=(
  "pre:$HOOKS_DIR/tdd-evidence.sh"
  "pre:$HOOKS_DIR/tdd-gate.sh"
  "pre:$HOOKS_DIR/bead-description-gate.sh"
  "pre:$MM_DIR/tree-claim.sh"
  "pre:$MM_DIR/read-guard.sh"
  "pre:$MM_DIR/pre-push-rebase-test.sh"
  "pre:$MM_DIR/mode-interlock.sh"
  "post:$HOOKS_DIR/bead-quality.sh"
  "post:$MM_DIR/post-push-merge-notify.sh"
)

for entry in "${BASH_HOOKS[@]}"; do
  event="${entry%%:*}"
  hook="${entry#*:}"
  base="$(basename "$hook")"
  if [ ! -f "$hook" ]; then
    fail "hook present: $base" "not found at $hook"
    continue
  fi
  # Non-push reproducer commands: NO hook may push.
  assert_no_push "no-push on 'bd update' — $base"  "$hook" "$event" "$CMD_BD"
  assert_no_push "no-push on 'sable-msg' — $base"  "$hook" "$event" "$CMD_MSG"
done

# The push-capable hooks (they legitimately fetch/rebase/ls-remote on a REAL
# push) must ALSO never issue a push of their own during that push.
assert_no_push "no self-push on real push — pre-push-rebase-test.sh" \
  "$MM_DIR/pre-push-rebase-test.sh" "pre" "$CMD_PUSH"
assert_no_push "no self-push on real push — post-push-merge-notify.sh" \
  "$MM_DIR/post-push-merge-notify.sh" "post" "$CMD_PUSH"

# --------------------------------------------------------------------------
echo "----------------------------------------------------------------------"
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
[ "$FAIL" -eq 0 ]
