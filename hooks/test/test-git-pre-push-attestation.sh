#!/usr/bin/env bash
# test-git-pre-push-attestation.sh — shape-independent push-gate integration.
# Uses only scratch repositories and a local bare remote; no network URL is
# configured or contacted.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ATTEST_LIB="$ROOT/hooks/multi-manager/lib-push-attestation.sh"
GIT_GATE="$ROOT/hooks/multi-manager/git-pre-push-attestation.sh"
PRETOOL_GATE="$ROOT/hooks/multi-manager/pre-push-rebase-test.sh"
TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/sable-git-pre-push.XXXXXX")"
trap 'rm -rf "$TMPROOT"' EXIT

PASS=0
FAIL=0
pass() { printf 'PASS: %s\n' "$1"; PASS=$((PASS + 1)); }
fail() { printf 'FAIL: %s — %s\n' "$1" "$2"; FAIL=$((FAIL + 1)); }

REMOTE="$TMPROOT/remote.git"
WORK="$TMPROOT/work"
git init --bare -q "$REMOTE"
git init -q -b main "$WORK"
git -C "$WORK" config user.name "SABLE fixture"
git -C "$WORK" config user.email "fixture@example.invalid"
git -C "$WORK" remote add origin "$REMOTE"
printf 'one\n' > "$WORK/value.txt"
git -C "$WORK" add value.txt
git -C "$WORK" commit -qm initial
git -C "$WORK" push -q -u origin main

REMOTE_URL="$(git -C "$WORK" remote get-url origin)"
case "$REMOTE_URL" in
  "$TMPROOT"/*) pass "fixture remote is local and isolated" ;;
  *) fail "fixture remote is local and isolated" "unexpected URL: $REMOTE_URL" ;;
esac

HOOKS="$WORK/.beads/hooks"
( cd "$WORK" && bd init --quiet >/dev/null 2>&1 )
mkdir -p "$HOOKS" "$WORK/hooks/multi-manager"
cp -f "$ROOT/.beads/hooks/pre-push" "$HOOKS/pre-push"
cp -f "$ATTEST_LIB" "$GIT_GATE" "$WORK/hooks/multi-manager/"
chmod +x "$HOOKS/pre-push" "$WORK/hooks/multi-manager/"*.sh
if ( cd "$WORK" && bd hooks install --beads >/dev/null 2>&1 ) &&
   grep -q "SABLE push-gate proof check" "$HOOKS/pre-push" &&
   [ "$(git -C "$WORK" config core.hooksPath)" = "$WORK/.beads/hooks" ]; then
  pass "installed beads hook preserves the tracked SABLE leg outside managed markers"
else
  fail "installed beads hook preserves the tracked SABLE leg outside managed markers" "hook regeneration removed the leg or configured the wrong path"
fi

# Foreground control: run the real PreToolUse gate, then execute the real push.
# The git hook must consume the exact-HEAD proof.
printf 'two\n' >> "$WORK/value.txt"
git -C "$WORK" commit -qam foreground
FG_SHA="$(git -C "$WORK" rev-parse HEAD)"
FG_SENTINEL="$TMPROOT/foreground-gate-ran"
if (
  cd "$WORK" || exit 1
  python3 -c 'import json,os; print(json.dumps({"cwd": os.getcwd(), "tool_input": {"command": "git push origin main"}}))' |
    env CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager \
      SABLE_PRE_PUSH_TEST_PHASE=skip "$PRETOOL_GATE" > "$TMPROOT/foreground-gate.out"
  grep -q 'additionalContext' "$TMPROOT/foreground-gate.out"
  : > "$FG_SENTINEL"
  git push origin main
) >"$TMPROOT/foreground.out" 2>&1 &&
   [ -f "$FG_SENTINEL" ] &&
   [ "$(git --git-dir="$REMOTE" rev-parse refs/heads/main)" = "$FG_SHA" ]; then
  pass "foreground push carries gate proof and moves the local fixture ref"
else
  fail "foreground push carries gate proof and moves the local fixture ref" "$(tail -20 "$TMPROOT/foreground.out")"
fi

# Background reproduction: the supported shell background shape executes the
# same git push without the PreToolUse producer. It must be denied before the
# bare remote ref moves, and the degraded verdict must be loud.
printf 'three\n' >> "$WORK/value.txt"
git -C "$WORK" commit -qam background
BG_SHA="$(git -C "$WORK" rev-parse HEAD)"
BEFORE_BG="$(git --git-dir="$REMOTE" rev-parse refs/heads/main)"
(
  cd "$WORK" || exit 1
  git push origin main >"$TMPROOT/background.out" 2>&1
) &
BG_PID=$!
if wait "$BG_PID"; then BG_RC=0; else BG_RC=$?; fi
AFTER_BG="$(git --git-dir="$REMOTE" rev-parse refs/heads/main)"
if [ "$BG_RC" -ne 0 ] && [ "$AFTER_BG" = "$BEFORE_BG" ] &&
   grep -q "verification did not run" "$TMPROOT/background.out"; then
  pass "background push is refused loudly before the local fixture ref moves"
else
  fail "background push is refused loudly before the local fixture ref moves" "rc=$BG_RC before=$BEFORE_BG after=$AFTER_BG wanted=$BG_SHA output=$(tail -20 "$TMPROOT/background.out")"
fi

# A non-git background command never reaches the push hook.
( printf 'allowed\n' > "$TMPROOT/unrelated-background" ) &
OTHER_PID=$!
if wait "$OTHER_PID" && [ "$(cat "$TMPROOT/unrelated-background")" = "allowed" ]; then
  pass "unrelated background command remains allowed"
else
  fail "unrelated background command remains allowed" "command failed or sentinel missing"
fi

# The emergency path remains explicit and loud; it is not reachable from an
# ordinary background push by accident.
if (
  cd "$WORK" || exit 1
  SABLE_ALLOW_UNVERIFIED_PUSH=1 git push origin main
) >"$TMPROOT/explicit-bypass.out" 2>&1 &&
   [ "$(git --git-dir="$REMOTE" rev-parse refs/heads/main)" = "$BG_SHA" ] &&
   grep -q "verification did not run" "$TMPROOT/explicit-bypass.out"; then
  pass "explicit emergency bypass is allowed but loudly names missing verification"
else
  fail "explicit emergency bypass is allowed but loudly names missing verification" "$(tail -20 "$TMPROOT/explicit-bypass.out")"
fi

# Plant-and-fail: remove only the new git-bound enforcement leg. The same
# background push must now move the fixture ref with no gate sentinel, while
# the foreground producer control still demonstrably fires.
printf 'four\n' >> "$WORK/value.txt"
git -C "$WORK" commit -qam plant-background
PLANT_SHA="$(git -C "$WORK" rev-parse HEAD)"
cat > "$HOOKS/pre-push" <<'EOF'
#!/usr/bin/env sh
exit 0
EOF
chmod +x "$HOOKS/pre-push"
PLANT_SENTINEL="$TMPROOT/plant-gate-ran"
rm -f "$PLANT_SENTINEL"
(
  cd "$WORK" || exit 1
  git push origin main >"$TMPROOT/plant.out" 2>&1
) &
PLANT_PID=$!
if wait "$PLANT_PID"; then PLANT_RC=0; else PLANT_RC=$?; fi
PLANT_REMOTE="$(git --git-dir="$REMOTE" rev-parse refs/heads/main)"
if [ "$PLANT_RC" -eq 0 ] && [ "$PLANT_REMOTE" = "$PLANT_SHA" ] && [ ! -e "$PLANT_SENTINEL" ]; then
  pass "PLANT: removing enforcement lets background push move ref without gate proof"
else
  fail "PLANT: removing enforcement lets background push move ref without gate proof" "rc=$PLANT_RC remote=$PLANT_REMOTE wanted=$PLANT_SHA"
fi
(
  cd "$WORK" || exit 1
  python3 -c 'import json,os; print(json.dumps({"cwd": os.getcwd(), "tool_input": {"command": "git push origin main"}}))' |
    env CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager \
      SABLE_PRE_PUSH_TEST_PHASE=skip "$PRETOOL_GATE" > "$TMPROOT/plant-gate.out"
  grep -q 'additionalContext' "$TMPROOT/plant-gate.out"
  : > "$PLANT_SENTINEL"
)
if [ -f "$PLANT_SENTINEL" ]; then
  pass "PLANT control: foreground gate producer still fires"
else
  fail "PLANT control: foreground gate producer still fires" "sentinel missing"
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
