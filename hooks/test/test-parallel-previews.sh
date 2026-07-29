#!/usr/bin/env bash
# test-parallel-previews.sh — parallel previews + promote-on-green (SABLE-jd5fj.3)
#
# WHAT IS UNDER TEST
# ------------------
# The three properties the merge-gate module split exists to deliver, against
# REAL git (a bare origin + working clones; only the Actions verdict is injected
# through the SABLE_MG_GH seam):
#
#   P1 PARALLEL PREVIEWS. Two workers' previews, kicked on distinct
#      ci-verify/<name>-<sha7> refs, are BOTH live at once — neither kick
#      disturbs the other's ref, and both remain verifiable independently. The
#      workflow-side half of the property is checked statically here too:
#      ci-verify.yml's concurrency group must be keyed on github.ref, because
#      cancel-in-progress: true is only safe when distinct refs are distinct
#      groups. A group keyed on anything coarser would make every new kick
#      cancel the run before it (SABLE-sc24's shape, fleet-wide). This is the
#      REGRESSION CASE for this bead, not a nice-to-have.
#
#   P2 PROMOTE-ON-GREEN CONSUMES A PRECOMPUTED VERDICT. With the run for the
#      preview SHA already complete, promote reads the verdict (one call, no
#      poll loop) and promotes the SAME OBJECT byte-identical: origin's base tip
#      after the promote must EQUAL the kicked preview SHA. This is the
#      load-bearing assertion of the whole gate, and it is asserted here on the
#      read-verdict path specifically, because that path is new.
#      Proven non-vacuous: the same harness with a RED verdict promotes nothing.
#
#   P3 THE EXIT-CODE TAXONOMY IS UNCHANGED BY THE SPLIT. 0/20/21/22/23/24/4,
#      one named case each, run through the post-split modules. (The pre-split
#      taxonomy suite in test-preview-kick.sh is retained unmodified as well —
#      that suite and this one are deliberately redundant on the taxonomy: it is
#      the property the split was least allowed to touch.)
#
# ACTIVATION NOTE (SABLE-jd5fj.3): bin/sable-merge-gate is a PINNED bin, so
# merging this changes nothing at runtime until an operator-brokered pin
# refresh. Everything asserted here runs against the WORKING TREE — it is
# evidence about the code, and deliberately not a claim about the live fleet.
#
# Run with:
#   bash hooks/test/test-parallel-previews.sh
#
# Clean-room safe (SABLE-59zu): needs only bash + git + python3. bd / sable-msg /
# gh are stubbed; nothing here touches a real remote.

set -uo pipefail

# Resolve absolute paths BEFORE the sandbox preamble cds away (SABLE-0ssz.2).
TESTDIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$TESTDIR/../.." && pwd)"
GATE="$REPO_ROOT/bin/sable-merge-gate"
CI_VERIFY="$REPO_ROOT/.github/workflows/ci-verify.yml"

# shellcheck source=lib-git-sandbox.sh
source "$TESTDIR/lib-git-sandbox.sh"

PASS=0
FAIL=0
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

for f in "$GATE" "$CI_VERIFY"; do
  [ -f "$f" ] || { echo "FATAL: missing $f"; exit 2; }
done

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"; sable_test_git_sandbox_cleanup' EXIT

BASE_BR="trunk"

# A fake `gh` answering `run list --branch <ref>` with a run whose headSha is the
# REAL tip of that ci-verify ref, so the SHA match is faithful for BOTH the
# non-blocking verdict read and the polling fall-through.
#   FAKE_GH_MODE=empty      models actions-down (no runs at all)
#   FAKE_GH_MODE=pending    a run exists but has NOT completed — the read must
#                           decline to call it a verdict
#   FAKE_GH_MODE=error      models gh itself failing (missing/erroring/hanging
#                           in prod) — a non-zero exit, so _gh_runs returns
#                           None: a NON-ANSWER, distinct from 'empty' (gh
#                           answered "no runs") and from 'pending' (gh
#                           answered "still running") — SABLE-fewih
#   FAKE_GH_ADVANCE=<sha>   moves origin's base at call time (the real
#                           base-moved-during-CI race, exit 23)
#   FAKE_GH_CALLS=<file>    appends one line per invocation, so "promote read the
#                           verdict instead of polling" is COUNTABLE
FAKE_GH="$TMPROOT/fake-gh"
cat > "$FAKE_GH" <<'EOF'
#!/usr/bin/env python3
import json, os, subprocess, sys
mode = os.environ.get("FAKE_GH_MODE", "success")
od = os.environ["FAKE_GH_ORIGIN"]
calls = os.environ.get("FAKE_GH_CALLS", "")
if calls:
    with open(calls, "a") as fh:
        fh.write(" ".join(sys.argv[1:]) + "\n")
advance = os.environ.get("FAKE_GH_ADVANCE", "")
if advance:
    subprocess.run(["git", "--git-dir=" + od, "update-ref",
                    "refs/heads/" + os.environ["FAKE_GH_BASE"], advance], check=False)
if mode == "error":
    print("gh: network error", file=sys.stderr); sys.exit(1)
if mode == "empty":
    print("[]"); sys.exit(0)
a = sys.argv[1:]
ref = a[a.index("--branch") + 1]
sha = subprocess.run(["git", "--git-dir=" + od, "rev-parse", "refs/heads/" + ref],
                     text=True, capture_output=True).stdout.strip()
if mode == "pending":
    print(json.dumps([{"databaseId": 1, "headSha": sha, "status": "in_progress",
                       "conclusion": None, "url": "http://fake/run/1"}]))
    sys.exit(0)
print(json.dumps([{"databaseId": 1, "headSha": sha, "status": "completed",
                   "conclusion": mode, "url": "http://fake/run/1"}]))
EOF
chmod +x "$FAKE_GH"

# setup_pair <name> [conflict] [worker-count] → B_ORIGIN / B_WORK for a fresh
# repo pair with N worker branches wk-1..wk-N off the base.
setup_pair() {
  local name="$1" conflict="${2:-}" workers="${3:-1}" i
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

  for i in $(seq 1 "$workers"); do
    git -C "$B_WORK" checkout -q -b "wk-$i" "$BASE_BR"
    if [ -n "$conflict" ]; then
      printf 'WORKER\nl2\nl3\n' > "$B_WORK/shared.txt"
    else
      echo "feature $i" > "$B_WORK/feature-$i.txt"
    fi
    git -C "$B_WORK" add -A
    git -C "$B_WORK" commit -q -m "worker $i change"
    git -C "$B_WORK" push -q origin "wk-$i"
  done

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
      FAKE_GH_CALLS="${FAKE_GH_CALLS:-}" \
      SABLE_MG_GH="$FAKE_GH" SABLE_MG_BD=true SABLE_MG_NOTIFY=true \
      SABLE_MG_POLL=0 SABLE_MG_GRACE=0 SABLE_MG_TIMEOUT=0 \
      PATH="$PATH" python3 "$GATE" "$@" 2>&1
}

ci_refs() { git --git-dir="$B_ORIGIN" for-each-ref --format='%(refname:short)' refs/heads/ci-verify/; }
ci_ref_count() { ci_refs | grep -c . ; }
origin_sha() { git --git-dir="$B_ORIGIN" rev-parse "refs/heads/$1"; }

# ==========================================================================
# P1 — parallel previews on distinct refs
# ==========================================================================

# (P1a) ci-verify.yml's concurrency group is keyed on the REF. The static half
# of the property: with cancel-in-progress: true, a coarser key would make each
# new preview cancel the previous one.
GROUP_LINE="$(awk '/^concurrency:/{f=1;next} f&&/group:/{print;exit}' "$CI_VERIFY")"
if printf '%s' "$GROUP_LINE" | grep -q 'github\.ref'; then
  pass "P1a: ci-verify concurrency group is per-ref (distinct previews never cancel each other)"
else
  fail "P1a: ci-verify concurrency group is per-ref" "group line: $GROUP_LINE"
fi
if grep -q "cancel-in-progress: true" "$CI_VERIFY"; then
  pass "P1a: cancel-in-progress stays on (a re-push to the SAME ref still supersedes its own stale run)"
else
  fail "P1a: cancel-in-progress stays on" "not found in $CI_VERIFY"
fi

# (P1b) Two workers kicked back to back: two distinct ci-verify refs, both
# present at once. This is what "N previews run concurrently" means on our side
# of the boundary — GitHub schedules them, we must not collapse them.
setup_pair parallel "" 2
gate preview --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin >/dev/null; RC1=$?
gate preview --branch wk-2 --base "$BASE_BR" --repo "$B_WORK" --remote origin >/dev/null; RC2=$?
REFS="$(ci_refs)"
N="$(ci_ref_count)"
if [ "$RC1" -eq 0 ] && [ "$RC2" -eq 0 ] && [ "$N" -eq 2 ]; then
  pass "P1b: two workers' previews live simultaneously on two distinct ci-verify refs"
else
  fail "P1b: two concurrent previews" "rc1=$RC1 rc2=$RC2 refs=[$REFS]"
fi
REF1="$(printf '%s\n' "$REFS" | grep 'wk-1' | head -1)"
REF2="$(printf '%s\n' "$REFS" | grep 'wk-2' | head -1)"
if [ -n "$REF1" ] && [ -n "$REF2" ] && [ "$REF1" != "$REF2" ]; then
  pass "P1b: each preview ref is named for its own branch + merge state"
else
  fail "P1b: distinct ref names" "ref1=$REF1 ref2=$REF2"
fi

# (P1c) The second kick did not disturb the first: wk-1's ref still points at
# the object CI is verifying for wk-1. (A shared or colliding ref name would
# show up here as a moved tip.)
SHA1_BEFORE="$(origin_sha "$REF1")"
gate preview --branch wk-2 --base "$BASE_BR" --repo "$B_WORK" --remote origin >/dev/null
if [ "$(origin_sha "$REF1")" = "$SHA1_BEFORE" ] && [ "$(ci_ref_count)" -eq 2 ]; then
  pass "P1c: re-kicking one worker leaves the other worker's in-flight preview untouched"
else
  fail "P1c: kicks are independent" "wk-1 ref moved or ref count changed ($(ci_ref_count))"
fi

# (P1d) Each preview is independently verifiable: reading wk-1's verdict says
# nothing about wk-2's and vice versa.
V1="$(gate verdict --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"
V2="$(gate verdict --branch wk-2 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"
if printf '%s' "$V1" | grep -q "$REF1" && printf '%s' "$V2" | grep -q "$REF2"; then
  pass "P1d: each branch's verdict resolves to its own preview ref"
else
  fail "P1d: per-branch verdict resolution" "v1=$V1 v2=$V2"
fi

# ==========================================================================
# P2 — promote-on-green consumes the precomputed verdict, byte-identically
# ==========================================================================

setup_pair promote-green "" 1
gate preview --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin >/dev/null
KICKED_REF="$(ci_refs | head -1)"
KICKED_SHA="$(origin_sha "$KICKED_REF")"

# (P2a) The verdict is readable BEFORE any promote runs — this is Chuck's
# read-verdict step, and it must not build, push, wait, or promote.
BASE_BEFORE="$(origin_sha "$BASE_BR")"
OUT="$(gate verdict --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin --json)"; RC=$?
if [ "$RC" -eq 0 ] && printf '%s' "$OUT" | grep -q '"state": "green"' \
   && printf '%s' "$OUT" | grep -q '"promotable": true'; then
  pass "P2a: verdict reads GREEN for a completed run, before any promote"
else
  fail "P2a: verdict reads GREEN" "rc=$RC out=$OUT"
fi
if [ "$(origin_sha "$BASE_BR")" = "$BASE_BEFORE" ] && [ "$(ci_ref_count)" -eq 1 ]; then
  pass "P2a: reading a verdict promotes nothing and creates no new refs (read-only)"
else
  fail "P2a: verdict read is read-only" "base moved or refs=[$(ci_refs)]"
fi

# (P2b) Non-vacuity of P2a: with the run still in flight, the SAME command
# reports pending, not green. Without this, 'state: green' could just be what
# the command always prints.
OUT="$(FAKE_GH_MODE=pending gate verdict --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin --json)"
if printf '%s' "$OUT" | grep -q '"state": "pending"' && printf '%s' "$OUT" | grep -q '"promotable": false'; then
  pass "P2b: an in-flight run reads as pending, not green (the read is a real observation)"
else
  fail "P2b: in-flight run reads pending" "out=$OUT"
fi

# (P2b') Non-vacuity in the other direction (SABLE-fewih): when gh itself
# cannot be asked at all (missing/erroring/hanging), the verdict CLI must NOT
# report the same 'pending' as a genuinely in-flight run — this read has no
# wait loop to converge through, unlike promote's wait_for_ci fallthrough.
OUT="$(FAKE_GH_MODE=error gate verdict --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin --json)"
if printf '%s' "$OUT" | grep -q '"state": "unknown"' && printf '%s' "$OUT" | grep -q '"promotable": false'; then
  pass "P2b': gh erroring reads as unknown, not pending (SABLE-fewih)"
else
  fail "P2b': non-answer verdict reads as unknown" "out=$OUT"
fi

# (P2c) promote CONSUMES that stored verdict: it says so, it does not poll, and
# the base tip afterwards is EXACTLY the kicked, CI-tested object. The gh call
# count is the non-narrative evidence that the poll loop was skipped.
CALLS="$TMPROOT/gh-calls.log"
: > "$CALLS"
OUT="$(FAKE_GH_CALLS="$CALLS" gate promote --bead TEST-P2 --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
NCALLS="$(grep -c . "$CALLS")"
if [ "$RC" -eq 0 ]; then
  pass "P2c: promote on a precomputed green verdict exits 0"
else
  fail "P2c: promote on a precomputed green verdict exits 0" "rc=$RC out=$OUT"
fi
if printf '%s' "$OUT" | grep -qi 'consuming precomputed verdict'; then
  pass "P2c: promote reports consuming the precomputed verdict (no CI wait)"
else
  fail "P2c: promote consumes the precomputed verdict" "out=$OUT"
fi
if [ "$NCALLS" -eq 1 ]; then
  pass "P2c: exactly ONE Actions API call on the promote path (a read, not a poll loop)"
else
  fail "P2c: exactly one Actions API call" "calls=$NCALLS: $(tr '\n' '|' < "$CALLS")"
fi
if [ "$(origin_sha "$BASE_BR")" = "$KICKED_SHA" ]; then
  pass "P2c: BYTE-IDENTICAL — the base tip IS the kicked object CI verified"
else
  fail "P2c: byte-identical promotion" "base=$(origin_sha "$BASE_BR") kicked=$KICKED_SHA"
fi
if [ -z "$(ci_refs)" ]; then
  pass "P2c: the ci-verify ref is cleaned up on green"
else
  fail "P2c: ci-verify ref cleaned up on green" "refs=[$(ci_refs)]"
fi

# (P2d) Non-vacuity of P2c: the same precomputed-verdict path with a RED verdict
# promotes NOTHING. If P2c passed because promote always fast-forwards, this fails.
setup_pair promote-red "" 1
FAKE_GH_MODE=failure gate preview --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin >/dev/null
BEFORE="$(origin_sha "$BASE_BR")"
OUT="$(FAKE_GH_MODE=failure gate promote --bead TEST-P2D --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
if [ "$RC" -eq 20 ] && [ "$(origin_sha "$BASE_BR")" = "$BEFORE" ]; then
  pass "P2d: a precomputed RED verdict promotes nothing (exit 20)"
else
  fail "P2d: precomputed RED promotes nothing" "rc=$RC base=$(origin_sha "$BASE_BR") out=$OUT"
fi

# (P2e) The fall-through is intact: with NO stored verdict (actions-down), the
# gate still reaches its blocked verdict rather than treating a non-answer as
# one. The optimization must not change the outcome when it misses.
setup_pair promote-fallthrough "" 1
OUT="$(FAKE_GH_MODE=empty gate promote --bead TEST-P2E --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
if [ "$RC" -eq 21 ]; then
  pass "P2e: no stored verdict falls through to the wait path and blocks (exit 21)"
else
  fail "P2e: fall-through to the wait path" "rc=$RC out=$OUT"
fi

# (P2f) verdict on a branch nobody kicked: 'none', exit 0, nothing built.
setup_pair verdict-none "" 1
OUT="$(gate verdict --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin --json)"; RC=$?
if [ "$RC" -eq 0 ] && printf '%s' "$OUT" | grep -q '"state": "none"' && [ -z "$(ci_refs)" ]; then
  pass "P2f: verdict for an unkicked branch is 'none' and builds nothing"
else
  fail "P2f: verdict for an unkicked branch" "rc=$RC out=$OUT refs=[$(ci_refs)]"
fi

# ==========================================================================
# P3 — IRON RULE: the exit-code taxonomy 0/20/21/22/23/24/4, post-split
# ==========================================================================

setup_pair tax-green
OUT="$(gate promote --bead T --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
[ "$RC" -eq 0 ] && pass "taxonomy 0: green promotes" || fail "taxonomy 0: green promotes" "rc=$RC out=$OUT"

setup_pair tax-red
BEFORE="$(origin_sha "$BASE_BR")"
OUT="$(FAKE_GH_MODE=failure gate promote --bead T --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
if [ "$RC" -eq 20 ] && [ "$(origin_sha "$BASE_BR")" = "$BEFORE" ]; then
  pass "taxonomy 20: red does not promote"
else
  fail "taxonomy 20: red does not promote" "rc=$RC out=$OUT"
fi

setup_pair tax-down
OUT="$(FAKE_GH_MODE=empty gate promote --bead T --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
[ "$RC" -eq 21 ] && pass "taxonomy 21: actions-down blocks" || fail "taxonomy 21: actions-down blocks" "rc=$RC out=$OUT"

setup_pair tax-conflict conflict
OUT="$(gate promote --bead T --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
if [ "$RC" -eq 22 ] && [ -z "$(ci_refs)" ]; then
  pass "taxonomy 22: merge-preview conflict delegates, no ref pushed"
else
  fail "taxonomy 22: merge-preview conflict delegates" "rc=$RC refs=[$(ci_refs)] out=$OUT"
fi

setup_pair tax-cancelled
OUT="$(FAKE_GH_MODE=cancelled gate promote --bead T --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
[ "$RC" -eq 24 ] && pass "taxonomy 24: cancelled run is retryable, not red" || fail "taxonomy 24: cancelled run is retryable" "rc=$RC out=$OUT"

# 23 — the base genuinely moves while the gate is mid-flight: the fake gh
# advances origin/trunk to a divergent commit at call time, so the promote push
# is a real non-fast-forward rejection.
setup_pair tax-moved
git -C "$B_WORK" checkout -q -b stray "$BASE_BR"
echo stray > "$B_WORK/stray.txt"
git -C "$B_WORK" add -A
git -C "$B_WORK" commit -q -m stray
git -C "$B_WORK" push -q origin stray
STRAY="$(origin_sha stray)"
BEFORE="$STRAY"
OUT="$(FAKE_GH_ADVANCE="$STRAY" gate promote --bead T --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
if [ "$RC" -eq 23 ]; then
  pass "taxonomy 23: base moved mid-gate — non-ff promote is retryable"
else
  fail "taxonomy 23: base moved mid-gate" "rc=$RC out=$OUT"
fi
if [ "$(origin_sha "$BASE_BR")" = "$BEFORE" ]; then
  pass "taxonomy 23: nothing was promoted onto the moved base"
else
  fail "taxonomy 23: nothing was promoted onto the moved base" "base=$(origin_sha "$BASE_BR")"
fi

# 4 — integrity abort: the promote push lands, but the base tip is then NOT the
# tested object. Provoked for real with a post-receive hook in the bare origin
# that rewrites trunk after the promotion push (a stand-in for the serialization
# violation the guard exists to catch loudly). THIS ASSERTION IS THE ONE THE
# SPLIT WAS FORBIDDEN TO TOUCH — it is preserved verbatim in the promote module.
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
OUT="$(gate promote --bead T --branch wk-1 --base "$BASE_BR" --repo "$B_WORK" --remote origin)"; RC=$?
if [ "$RC" -eq 4 ]; then
  pass "taxonomy 4: integrity abort when the base tip is not the tested object"
else
  fail "taxonomy 4: integrity abort when the base tip is not the tested object" "rc=$RC out=$OUT"
fi

echo "----------------------------------------------------------------------"
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
[ "$FAIL" -eq 0 ]
