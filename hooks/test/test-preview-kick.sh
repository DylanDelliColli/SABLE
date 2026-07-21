#!/usr/bin/env bash
# test-preview-kick.sh — push-time preview kick, end to end (SABLE-jd5fj.1)
#
# WHAT IS UNDER TEST
# ------------------
# Two legs of one mechanism:
#
#   PART A (hook leg) — post-push-merge-notify.sh fires `sable-merge-gate
#   preview` in the background on a CONFIRMED worker push, exactly once, and
#   never on a push that would not become a Chuck merge. Every pre-existing
#   no-kick guard is re-asserted here as a kick guard: integration-branch
#   self-push, 'Everything up-to-date' no-op, empty diff vs the base, and a push
#   that never landed on origin. The hook must also NOT wait for the kick — it
#   is warm-up, and the merge handoff below it must not queue behind CI.
#
#   PART B (gate leg) — the real bin/sable-merge-gate against REAL git (a bare
#   origin + a working clone; only the Actions verdict is injected through the
#   SABLE_MG_GH seam). Covers the kick's own contract (returns at the ref push,
#   idempotent per merge state, adopted by a later promote) and the REGRESSION
#   the split had to preserve: the promote exit-code taxonomy 0/20/21/22/23/24/4,
#   unchanged. 23 (base moved during the CI wait) and 4 (integrity abort) are
#   provoked for real here — a fake `gh` that advances origin's base mid-wait,
#   and a bare-repo post-receive hook that rewrites the base after the promote
#   push — rather than mocked.
#
# Run with:
#   bash hooks/test/test-preview-kick.sh
#
# Clean-room safe (SABLE-59zu): needs only bash + git + python3. bd / sable-msg /
# gh / tmux are stubbed; nothing here touches a real remote.

set -uo pipefail

# Resolve absolute paths BEFORE the sandbox preamble cds away (SABLE-0ssz.2).
TESTDIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$TESTDIR/../.." && pwd)"
HOOK="$REPO_ROOT/hooks/multi-manager/post-push-merge-notify.sh"
GATE="$REPO_ROOT/bin/sable-merge-gate"

# shellcheck source=lib-git-sandbox.sh
source "$TESTDIR/lib-git-sandbox.sh"

PASS=0
FAIL=0
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

for f in "$HOOK" "$GATE"; do
  [ -f "$f" ] || { echo "FATAL: missing $f"; exit 2; }
done

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"; sable_test_git_sandbox_cleanup' EXIT

# ==========================================================================
# PART A — hook leg
# ==========================================================================

STUB_DIR="$TMPROOT/stubs"
mkdir -p "$STUB_DIR"
KICK_LOG="$STUB_DIR/kick-calls.log"
export KICK_LOG

# sable-merge-gate recorder. Logs its argv (so 'exactly once' and the argument
# shape are both assertable) and can be made slow, to prove the hook does not
# block on it.
cat > "$STUB_DIR/sable-merge-gate" <<'EOF'
#!/usr/bin/env bash
echo "$*" >> "${KICK_LOG:-/dev/null}"
[ -n "${KICK_STUB_SLEEP:-}" ] && sleep "$KICK_STUB_SLEEP"
exit 0
EOF
chmod +x "$STUB_DIR/sable-merge-gate"

cat > "$STUB_DIR/bd" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat > "$STUB_DIR/gh" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
cat > "$STUB_DIR/sable-msg" <<'EOF'
#!/usr/bin/env bash
exit "${SABLE_MSG_STUB_RC:-1}"
EOF
cat > "$STUB_DIR/tmux" <<'EOF'
#!/usr/bin/env bash
for a in "$@"; do
  [ "$a" = "list-panes" ] && { echo "chuck"; exit 0; }
  [ "$a" = "display-message" ] && { echo "${SABLE_STUB_PANE_ROLE:-}"; exit 0; }
done
exit 0
EOF
chmod +x "$STUB_DIR/bd" "$STUB_DIR/gh" "$STUB_DIR/sable-msg" "$STUB_DIR/tmux"

# --- Fixture repo: integration branch tmux-only + a worker branch off it -----
HOOK_ORIGIN="$TMPROOT/hook-origin.git"
HOOK_REPO="$TMPROOT/hook-repo"
git init -q --bare "$HOOK_ORIGIN"
git clone -q "$HOOK_ORIGIN" "$HOOK_REPO" 2>/dev/null
git -C "$HOOK_REPO" config user.email "t@sable.invalid"
git -C "$HOOK_REPO" config user.name "SABLE Test"
git -C "$HOOK_REPO" config sable.integrationBranch tmux-only
echo base > "$HOOK_REPO/base.txt"
git -C "$HOOK_REPO" add base.txt
git -C "$HOOK_REPO" commit -q -m base
git -C "$HOOK_REPO" branch -M tmux-only
git -C "$HOOK_REPO" push -q origin tmux-only
git -C "$HOOK_REPO" update-ref refs/remotes/origin/tmux-only HEAD

git -C "$HOOK_REPO" checkout -q -b wk-kick
echo work > "$HOOK_REPO/work.txt"
git -C "$HOOK_REPO" add work.txt
git -C "$HOOK_REPO" commit -q -m "worker change"
git -C "$HOOK_REPO" push -q origin wk-kick
git -C "$HOOK_REPO" update-ref refs/remotes/origin/wk-kick HEAD

# A branch with no diff vs the integration branch (empty-diff guard).
git -C "$HOOK_REPO" checkout -q -b wk-empty tmux-only
git -C "$HOOK_REPO" push -q origin wk-empty
git -C "$HOOK_REPO" update-ref refs/remotes/origin/wk-empty HEAD

# A branch whose commit never reached origin (unconfirmed-push guard).
git -C "$HOOK_REPO" checkout -q -b wk-unpushed tmux-only
echo nope > "$HOOK_REPO/nope.txt"
git -C "$HOOK_REPO" add nope.txt
git -C "$HOOK_REPO" commit -q -m "never pushed"

make_post_input() {
  python3 -c "
import json, sys
cmd, cwd, stdout, stderr = sys.argv[1:5]
print(json.dumps({'tool_input': {'command': cmd}, 'cwd': cwd,
                  'tool_response': {'stdout': stdout, 'stderr': stderr}}))
" "$1" "$2" "${3:-}" "${4:-}"
}

# run_hook_on <branch> [env-prefix] [stderr-text] → hook stdout; leaves the
# fixture repo checked out on <branch> so the hook resolves it from CWD.
run_hook_on() {
  local branch="$1" env_prefix="${2:-}" stderr_text="${3:-}"
  git -C "$HOOK_REPO" checkout -q "$branch"
  : > "$KICK_LOG"
  local json
  json="$(make_post_input "git push" "$HOOK_REPO" "" "$stderr_text")"
  # shellcheck disable=SC2086 — env_prefix is a deliberate word-split list
  env -i PATH="${HOOK_PATH:-$STUB_DIR:$PATH}" HOME="$HOME" KICK_LOG="$KICK_LOG" \
      GIT_CONFIG_GLOBAL="${GIT_CONFIG_GLOBAL:-}" GIT_CONFIG_SYSTEM="${GIT_CONFIG_SYSTEM:-/dev/null}" \
      SABLE_HOOK_TRACE_LOG="$STUB_DIR/hook-trace.log" \
      CLAUDE_AGENT_NAME=optimus CLAUDE_AGENT_ROLE=manager \
      $env_prefix timeout 30 bash "$HOOK" <<< "$json" 2>&1
}

# The kick is detached, so its log line can land after the hook returns. Poll a
# bounded window for the expected count instead of reading once (an immediate
# read would make every "fires none" assertion race-flaky in the other direction).
# grep -c always PRINTS the count (0 when none) but exits 1 on zero matches, so
# capture it rather than chaining `|| echo 0` (which would print 0 twice).
kick_count() {
  local n
  n="$(grep -c . "$KICK_LOG" 2>/dev/null)"
  echo "${n:-0}"
}
await_kicks() {
  local want="$1" i
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    [ "$(kick_count)" -ge "$want" ] && break
    sleep 0.2
  done
  # Settle a beat past the target so an unexpected SECOND kick is still caught.
  sleep 0.4
  kick_count
}

# (A1) confirmed worker push → exactly one kick, naming the branch and repo
run_hook_on wk-kick >/dev/null
N="$(await_kicks 1)"
if [ "$N" -eq 1 ]; then
  pass "A1: confirmed worker push fires exactly one preview kick"
else
  fail "A1: confirmed worker push fires exactly one preview kick" "got $N kick(s): $(cat "$KICK_LOG" 2>/dev/null)"
fi
if grep -q -- "preview --branch wk-kick --repo $HOOK_REPO" "$KICK_LOG" 2>/dev/null; then
  pass "A1: kick invokes 'preview --branch <branch> --repo <resolved repo>'"
else
  fail "A1: kick invokes 'preview --branch <branch> --repo <resolved repo>'" "KICK_LOG: $(cat "$KICK_LOG" 2>/dev/null)"
fi

# (A2) integration-branch self-push → no kick
run_hook_on tmux-only >/dev/null
N="$(await_kicks 0)"
if [ "$N" -eq 0 ]; then
  pass "A2: integration-branch self-push fires NO kick"
else
  fail "A2: integration-branch self-push fires NO kick" "got $N: $(cat "$KICK_LOG")"
fi

# (A3) no-op push ('Everything up-to-date') → no kick
run_hook_on wk-kick "" "Everything up-to-date" >/dev/null
N="$(await_kicks 0)"
if [ "$N" -eq 0 ]; then
  pass "A3: no-op push ('Everything up-to-date') fires NO kick"
else
  fail "A3: no-op push fires NO kick" "got $N: $(cat "$KICK_LOG")"
fi

# (A4) empty diff vs the base → no kick
run_hook_on wk-empty >/dev/null
N="$(await_kicks 0)"
if [ "$N" -eq 0 ]; then
  pass "A4: empty-diff push fires NO kick"
else
  fail "A4: empty-diff push fires NO kick" "got $N: $(cat "$KICK_LOG")"
fi

# (A5) push that never landed on origin → no kick
run_hook_on wk-unpushed "SABLE_PUSH_CONFIRM_RETRIES=0" >/dev/null
N="$(await_kicks 0)"
if [ "$N" -eq 0 ]; then
  pass "A5: unconfirmed push (branch tip not on origin) fires NO kick"
else
  fail "A5: unconfirmed push fires NO kick" "got $N: $(cat "$KICK_LOG")"
fi

# (A6) explicit opt-out
run_hook_on wk-kick "SABLE_PREVIEW_KICK=0" >/dev/null
N="$(await_kicks 0)"
if [ "$N" -eq 0 ]; then
  pass "A6: SABLE_PREVIEW_KICK=0 fires NO kick"
else
  fail "A6: SABLE_PREVIEW_KICK=0 fires NO kick" "got $N: $(cat "$KICK_LOG")"
fi

# (A7) the hook does not BLOCK on the kick: a 6s kick must not add 6s to a hook
# whose own budget is 10s. Also still exactly one kick.
START=$SECONDS
run_hook_on wk-kick "KICK_STUB_SLEEP=6" >/dev/null
ELAPSED=$((SECONDS - START))
if [ "$ELAPSED" -lt 5 ]; then
  pass "A7: hook returns without waiting on the kick (${ELAPSED}s < 6s kick)"
else
  fail "A7: hook returns without waiting on the kick" "hook took ${ELAPSED}s with a 6s kick"
fi
N="$(await_kicks 1)"
if [ "$N" -eq 1 ]; then
  pass "A7: slow kick is still fired exactly once"
else
  fail "A7: slow kick is still fired exactly once" "got $N"
fi

# (A8) sable-merge-gate absent → loud skip, hook still exits 0 and still hands
# off. The PATH is narrowed to the stubs + the system dirs so an INSTALLED real
# sable-merge-gate on the developer's PATH cannot make this case vacuous (or,
# worse, run the real gate against the fixture).
mv "$STUB_DIR/sable-merge-gate" "$TMPROOT/sable-merge-gate.bak"
OUT="$(HOOK_PATH="$STUB_DIR:/usr/bin:/bin" run_hook_on wk-kick)"
RC=$?
mv "$TMPROOT/sable-merge-gate.bak" "$STUB_DIR/sable-merge-gate"
if [ "$RC" -eq 0 ] && printf '%s' "$OUT" | grep -qi 'not kicking the merge preview'; then
  pass "A8: missing sable-merge-gate is a LOUD skip, not a silent one (hook still exits 0)"
else
  fail "A8: missing sable-merge-gate is a LOUD skip" "rc=$RC out=$OUT"
fi

# ==========================================================================
# PART B — gate leg, real git
# ==========================================================================

BASE_BR="trunk"
WK="wk-b"

# A fake `gh` answering `run list --branch <ref>` with a run whose headSha is the
# REAL tip of that ci-verify ref, so wait_for_ci's SHA match is faithful.
# FAKE_GH_MODE=empty models actions-down. FAKE_GH_ADVANCE, when set, moves
# origin's base branch at poll time — the real base-moved-during-CI race (23).
FAKE_GH="$TMPROOT/fake-gh"
cat > "$FAKE_GH" <<'EOF'
#!/usr/bin/env python3
import json, os, subprocess, sys
mode = os.environ.get("FAKE_GH_MODE", "success")
od = os.environ["FAKE_GH_ORIGIN"]
advance = os.environ.get("FAKE_GH_ADVANCE", "")
if advance:
    subprocess.run(["git", "--git-dir=" + od, "update-ref",
                    "refs/heads/" + os.environ["FAKE_GH_BASE"], advance], check=False)
if mode == "empty":
    print("[]"); sys.exit(0)
a = sys.argv[1:]
ref = a[a.index("--branch") + 1]
sha = subprocess.run(["git", "--git-dir=" + od, "rev-parse", "refs/heads/" + ref],
                     text=True, capture_output=True).stdout.strip()
print(json.dumps([{"databaseId": 1, "headSha": sha, "status": "completed",
                   "conclusion": mode, "url": "http://fake/run/1"}]))
EOF
chmod +x "$FAKE_GH"

# setup_pair <name> [conflict] → sets B_ORIGIN / B_WORK for a fresh repo pair
setup_pair() {
  local name="$1" conflict="${2:-}"
  B_ORIGIN="$TMPROOT/$name-origin.git"
  B_WORK="$TMPROOT/$name-work"
  git init -q --bare -b "$BASE_BR" "$B_ORIGIN"
  git clone -q "$B_ORIGIN" "$B_WORK" 2>/dev/null
  git -C "$B_WORK" config user.email "t@sable.invalid"
  git -C "$B_WORK" config user.name "SABLE Test"
  printf 'l1\nl2\nl3\n' > "$B_WORK/shared.txt"
  git -C "$B_WORK" add -A
  git -C "$B_WORK" commit -q -m init
  git -C "$B_WORK" push -q origin "$BASE_BR"

  git -C "$B_WORK" checkout -q -b "$WK"
  if [ -n "$conflict" ]; then
    printf 'WORKER\nl2\nl3\n' > "$B_WORK/shared.txt"
  else
    echo feature > "$B_WORK/feature.txt"
  fi
  git -C "$B_WORK" add -A
  git -C "$B_WORK" commit -q -m "worker change"
  git -C "$B_WORK" push -q origin "$WK"

  git -C "$B_WORK" checkout -q "$BASE_BR"
  if [ -n "$conflict" ]; then
    printf 'BASE\nl2\nl3\n' > "$B_WORK/shared.txt"
    git -C "$B_WORK" add -A
    git -C "$B_WORK" commit -q -m "base change"
    git -C "$B_WORK" push -q origin "$BASE_BR"
  fi
}

# gate <subcommand-args...> → runs the REAL gate; echoes output, returns its code
gate() {
  env FAKE_GH_ORIGIN="$B_ORIGIN" FAKE_GH_BASE="$BASE_BR" \
      FAKE_GH_MODE="${FAKE_GH_MODE:-success}" FAKE_GH_ADVANCE="${FAKE_GH_ADVANCE:-}" \
      SABLE_MG_GH="$FAKE_GH" SABLE_MG_BD=true SABLE_MG_NOTIFY=true \
      SABLE_MG_POLL=0 SABLE_MG_GRACE=0 SABLE_MG_TIMEOUT=0 \
      PATH="$PATH" python3 "$GATE" "$@" 2>&1
}

ci_refs() { git --git-dir="$B_ORIGIN" for-each-ref --format='%(refname:short)' refs/heads/ci-verify/; }
origin_sha() { git --git-dir="$B_ORIGIN" rev-parse "refs/heads/$1"; }

# (B1) kick: exits 0 and leaves exactly one ci-verify ref
setup_pair kick
OUT="$(gate preview --branch "$WK" --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
REFS="$(ci_refs)"
if [ "$RC" -eq 0 ] && [ "$(printf '%s\n' "$REFS" | grep -c .)" -eq 1 ]; then
  pass "B1: preview kick exits 0 and pushes exactly one ci-verify ref"
else
  fail "B1: preview kick exits 0 and pushes exactly one ci-verify ref" "rc=$RC refs=[$REFS] out=$OUT"
fi
if printf '%s' "$OUT" | grep -qi 'NOT waiting for CI'; then
  pass "B1: kick reports that it is not waiting for CI"
else
  fail "B1: kick reports that it is not waiting for CI" "out=$OUT"
fi
KICKED_REF="$(printf '%s\n' "$REFS" | head -1)"
KICKED_SHA="$(origin_sha "$KICKED_REF")"

# (B2) idempotent: a second kick for the same merge state pushes nothing new
OUT="$(gate preview --branch "$WK" --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
REFS2="$(ci_refs)"
if [ "$RC" -eq 0 ] && [ "$REFS2" = "$REFS" ] && [ "$(origin_sha "$KICKED_REF")" = "$KICKED_SHA" ]; then
  pass "B2: re-kicking the same (base, branch) state is a no-op (shared idempotency key)"
else
  fail "B2: re-kicking the same state is a no-op" "rc=$RC refs=[$REFS2] out=$OUT"
fi
if printf '%s' "$OUT" | grep -qi 'already exists'; then
  pass "B2: the second kick says so rather than re-pushing"
else
  fail "B2: the second kick says so rather than re-pushing" "out=$OUT"
fi

# (B3) promote ADOPTS the kicked preview: the promoted object IS the kicked
# commit (byte-identical to what CI tested), and no second ci-verify ref appears.
OUT="$(gate promote --bead TEST-1 --branch "$WK" --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
if [ "$RC" -eq 0 ]; then
  pass "B3: promote after a kick exits 0 (green)"
else
  fail "B3: promote after a kick exits 0 (green)" "rc=$RC out=$OUT"
fi
if printf '%s' "$OUT" | grep -qi 'adopting push-time preview'; then
  pass "B3: promote adopts the kicked preview instead of building a second one"
else
  fail "B3: promote adopts the kicked preview" "out=$OUT"
fi
if [ "$(origin_sha "$BASE_BR")" = "$KICKED_SHA" ]; then
  pass "B3: the promoted base tip IS the kicked, CI-tested object (byte-identical)"
else
  fail "B3: the promoted base tip IS the kicked object" "base=$(origin_sha "$BASE_BR") kicked=$KICKED_SHA"
fi
if [ -z "$(ci_refs)" ]; then
  pass "B3: the adopted ci-verify ref is cleaned up on green"
else
  fail "B3: the adopted ci-verify ref is cleaned up on green" "refs=[$(ci_refs)]"
fi

# --- Exit-code taxonomy regression: 0/20/21/22/23/24/4 unchanged -------------
# Each runs promote with NO prior kick, so this is the pre-split flow verbatim.

setup_pair tax-green
OUT="$(gate promote --bead T --branch "$WK" --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
[ "$RC" -eq 0 ] && pass "taxonomy 0: green promotes" || fail "taxonomy 0: green promotes" "rc=$RC out=$OUT"

setup_pair tax-red
BEFORE="$(origin_sha "$BASE_BR")"
OUT="$(FAKE_GH_MODE=failure gate promote --bead T --branch "$WK" --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
if [ "$RC" -eq 20 ] && [ "$(origin_sha "$BASE_BR")" = "$BEFORE" ]; then
  pass "taxonomy 20: red does not promote"
else
  fail "taxonomy 20: red does not promote" "rc=$RC out=$OUT"
fi

setup_pair tax-down
OUT="$(FAKE_GH_MODE=empty gate promote --bead T --branch "$WK" --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
[ "$RC" -eq 21 ] && pass "taxonomy 21: actions-down blocks" || fail "taxonomy 21: actions-down blocks" "rc=$RC out=$OUT"

setup_pair tax-conflict conflict
OUT="$(gate promote --bead T --branch "$WK" --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
if [ "$RC" -eq 22 ] && [ -z "$(ci_refs)" ]; then
  pass "taxonomy 22: merge-preview conflict delegates, no ref pushed"
else
  fail "taxonomy 22: merge-preview conflict delegates" "rc=$RC refs=[$(ci_refs)] out=$OUT"
fi

setup_pair tax-cancelled
OUT="$(FAKE_GH_MODE=cancelled gate promote --bead T --branch "$WK" --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
[ "$RC" -eq 24 ] && pass "taxonomy 24: cancelled run is retryable, not red" || fail "taxonomy 24: cancelled run is retryable" "rc=$RC out=$OUT"

# 23 — the base genuinely moves DURING the CI wait: the fake gh advances
# origin/trunk to a divergent commit at poll time, so the promote push is a real
# non-fast-forward rejection.
setup_pair tax-moved
git -C "$B_WORK" checkout -q -b stray "$BASE_BR"
echo stray > "$B_WORK/stray.txt"
git -C "$B_WORK" add -A
git -C "$B_WORK" commit -q -m stray
git -C "$B_WORK" push -q origin stray
STRAY="$(origin_sha stray)"
BEFORE="$STRAY"
OUT="$(FAKE_GH_ADVANCE="$STRAY" gate promote --bead T --branch "$WK" --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
if [ "$RC" -eq 23 ]; then
  pass "taxonomy 23: base moved during the CI wait — non-ff promote is retryable"
else
  fail "taxonomy 23: base moved during the CI wait" "rc=$RC out=$OUT"
fi
if [ "$(origin_sha "$BASE_BR")" = "$BEFORE" ]; then
  pass "taxonomy 23: nothing was promoted onto the moved base"
else
  fail "taxonomy 23: nothing was promoted onto the moved base" "base=$(origin_sha "$BASE_BR")"
fi

# 4 — integrity abort: the promote push lands, but the base tip is then NOT the
# tested object. Provoked for real with a post-receive hook in the bare origin
# that rewrites trunk after the promotion push (a stand-in for the serialization
# violation the guard exists to catch loudly).
setup_pair tax-integrity
git -C "$B_WORK" checkout -q -b stray "$BASE_BR"
echo stray > "$B_WORK/stray.txt"
git -C "$B_WORK" add -A
git -C "$B_WORK" commit -q -m stray
git -C "$B_WORK" push -q origin stray
cat > "$B_ORIGIN/hooks/post-receive" <<'EOF'
#!/bin/sh
while read -r old new ref; do
  if [ "$ref" = "refs/heads/trunk" ]; then
    git update-ref refs/heads/trunk "$(git rev-parse refs/heads/stray)"
  fi
done
EOF
chmod +x "$B_ORIGIN/hooks/post-receive"
OUT="$(gate promote --bead T --branch "$WK" --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
if [ "$RC" -eq 4 ]; then
  pass "taxonomy 4: integrity abort when the base tip is not the tested object"
else
  fail "taxonomy 4: integrity abort when the base tip is not the tested object" "rc=$RC out=$OUT"
fi

echo "----------------------------------------------------------------------"
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
[ "$FAIL" -eq 0 ]
