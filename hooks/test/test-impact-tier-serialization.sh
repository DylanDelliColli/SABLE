#!/usr/bin/env bash
# test-impact-tier-serialization.sh — the merge seat runs ONE local impact tier
# at a time, mechanically (SABLE-jd5fj.13).
#
# WHAT WENT WRONG, AND WHY A TEST CAN SEE IT AT ALL
# ------------------------------------------------
# Chuck hit a 6+ promote pile-up at the merge seat. Each promote that took the
# optimistic-disjoint path ran the LOCAL combined-tree impact tier, and those
# tiers ran CONCURRENTLY. The iron-rule suites the tier selects are deliberately
# NON-HERMETIC — real bd (with sandbox-scoped beads carrying serialize_with
# metadata, and a DB lock), the real sable-spawn-worker binary, the live
# ~/.claude/settings.json — so they read AND WRITE state they share with every
# other tier running beside them. test-dep-merge-state.sh's WIRING subtest and
# test-overlap-dispatch-e2e.sh's serialize_grant subtest went red repeatedly
# under the pile-up; standalone on the SAME clean HEAD they were 18/18 and 5/5.
# Six branches were ejected with nothing wrong with them (wk-accept-protocol
# touches only templates/roles + sable_accept_bar.py; wk-preflight-config touches
# only CLAUDE.md).
#
# The amplifier is structural: hermetic CI ships no bd, and both suites SELF-SKIP
# their real-bd subtests when bd is absent, so those legs never run in GitHub
# Actions. The merge seat's local tier is their sole executor — the only place
# that coverage runs is exactly the place concurrency corrupts it, with no CI
# signal to arbitrate.
#
# WHAT IS UNDER TEST
# ------------------
# Not the suites, and not their flakiness: the CONTROL. run_impact_tier now takes
# an exclusive flock in the per-repo merge-gate state dir for the duration of the
# tier, so two promotes on one seat QUEUE instead of racing. Chuck's interim
# "one at a time, by hand" rule was the guidance-as-control shape SABLE-rkc3o
# refuted for the pinning suites; this replaces it with a code path.
#
# THE INSTRUMENT IS THE HARD PART. Overlap is invisible from suite results —
# that is exactly why the pile-up read as six broken branches instead of one
# broken control. So the tier writes start/end stamps to a window log, and this
# suite reads those windows:
#
#   S1 two REAL, backgrounded run_impact_tier invocations against ONE fixture
#      repo (one seat) -> their tier windows do not overlap, AND both report
#      GREEN. Serialization queues work; it must never drop or fail it.
#   S2 NEGATIVE CONTROL: the same two invocations with SABLE_MG_IMPACT_SERIALIZE=0
#      -> overlap IS observed. Without this, S1 proves only that two processes
#      happened not to collide, and would keep passing if the stamps were bogus.
#   S3 the lock is one file per repo, in the merge-gate state dir, so every
#      worktree of a seat contends on it (that IS the collision being prevented).
#   S4 a tier that could not START (lock timeout) reports ERROR, not GREEN and
#      not RED — an unstartable tier taught us nothing, so it must degrade to a
#      full re-preview rather than promote or blame an author.
#   S5 flock, not a pidfile: killing a holder mid-tier releases the seat. A ^C at
#      the merge seat must not wedge every later promote.
#
# The tier body is driven through the SABLE_MG_IMPACT override — a real
# subprocess in a real checked-out combined-tree worktree that sleeps a known
# duration — because what has to be measured is WHEN the tier occupies the seat,
# not what the iron-rule suites conclude. Running the real iron-rule suites here
# would make this suite itself one of the non-hermetic things it exists to
# protect.
#
# Run with:
#   bash hooks/test/test-impact-tier-serialization.sh
#
# Clean-room safe (SABLE-59zu): needs only bash + git + python3. No bd, no gh,
# no remote, no real merge-gate state — SABLE_MERGE_GATE_STATE is redirected
# into the fixture for the whole run.

set -uo pipefail

# Resolve absolute paths BEFORE the sandbox preamble cds away (SABLE-0ssz.2).
TESTDIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$TESTDIR/../.." && pwd)"
LIB="$REPO_ROOT/bin/sable_gate_promote_lib.py"

# shellcheck source=lib-git-sandbox.sh
source "$TESTDIR/lib-git-sandbox.sh"

PASS=0
FAIL=0
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

[ -f "$LIB" ] || { echo "FATAL: missing $LIB"; exit 2; }

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"; sable_test_git_sandbox_cleanup' EXIT

FIXTURE="$TMPROOT/seat"
STATE="$TMPROOT/state"
DRIVER="$TMPROOT/run-tier.py"
TIER="$TMPROOT/tier.sh"

mkdir -p "$STATE"

# --- the fixture repo: one seat, one repo, real commits --------------------
mkdir -p "$FIXTURE/bin"
git -C "$FIXTURE" init -q -b trunk
git -C "$FIXTURE" config user.email "t@sable.invalid"
git -C "$FIXTURE" config user.name "SABLE Test"
printf 'x = 1\n' > "$FIXTURE/bin/thing.py"
git -C "$FIXTURE" add -A
git -C "$FIXTURE" commit -q -m "init"
COMBINED_SHA="$(git -C "$FIXTURE" rev-parse HEAD)"

# --- the tier body: a real subprocess that occupies the seat ---------------
# Sleeps long enough that two unserialized runs overlap with margin, short
# enough that the suite stays fast.
cat > "$TIER" <<'EOF'
#!/usr/bin/env bash
sleep 0.6
exit 0
EOF
chmod +x "$TIER"

# --- the driver: the REAL bin/sable_gate_promote_lib.py entry --------------
# Not a reimplementation of the lock — it imports the production module and
# calls run_impact_tier, which is the function the promote flow calls.
cat > "$DRIVER" <<'EOF'
import sys
sys.path.insert(0, sys.argv[1])
import sable_gate_promote_lib as promote_lib
outcome, detail = promote_lib.run_impact_tier(sys.argv[2], sys.argv[3], ["bin/thing.py"])
print(f"{outcome}\t{detail}")
sys.exit(0 if outcome == promote_lib.IMPACT_GREEN else 1)
EOF

# run_pair <window-log> [extra env assignments...] — two backgrounded tier runs
# against the one fixture repo. Echoes nothing; sets RC1/RC2/OUT1/OUT2.
run_pair() {
  local log="$1"; shift
  rm -f "$log"
  ( SABLE_MG_IMPACT_WINDOW_LOG="$log" "$@" python3 "$DRIVER" "$REPO_ROOT/bin" "$FIXTURE" "$COMBINED_SHA" \
      > "$TMPROOT/out1" 2>&1 ) &
  local p1=$!
  ( SABLE_MG_IMPACT_WINDOW_LOG="$log" "$@" python3 "$DRIVER" "$REPO_ROOT/bin" "$FIXTURE" "$COMBINED_SHA" \
      > "$TMPROOT/out2" 2>&1 ) &
  local p2=$!
  wait $p1; RC1=$?
  wait $p2; RC2=$?
  OUT1="$(cat "$TMPROOT/out1")"
  OUT2="$(cat "$TMPROOT/out2")"
}

# overlap_seconds <window-log> — prints the number of seconds by which the two
# recorded tier windows overlap (0 or negative means they did not), or "ERR:..."
# if the log does not carry exactly two complete windows.
overlap_seconds() {
  python3 - "$1" <<'EOF'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
spans = {}
for r in rows:
    spans.setdefault(r["pid"], {})[r["event"]] = r["at"]
done = [(v["start"], v["end"]) for v in spans.values() if "start" in v and "end" in v]
if len(done) != 2:
    print(f"ERR:expected 2 complete windows, got {len(done)} from {len(rows)} stamps")
    sys.exit(0)
a, b = sorted(done)
print(f"{min(a[1], b[1]) - b[0]:.3f}")
EOF
}

export SABLE_MERGE_GATE_STATE="$STATE"
export SABLE_MG_IMPACT="bash $TIER"
unset SABLE_MG_IMPACT_LOCK SABLE_MG_IMPACT_SERIALIZE SABLE_MG_IMPACT_LOCK_TIMEOUT

# ==========================================================================
# S1 — serialized: the windows do not overlap, and both promotes get answers
# ==========================================================================
run_pair "$TMPROOT/serialized.jsonl"
OV="$(overlap_seconds "$TMPROOT/serialized.jsonl")"
if [ "$RC1" -eq 0 ] && [ "$RC2" -eq 0 ]; then
  pass "S1: both concurrent impact tiers completed GREEN"
else
  fail "S1: both concurrent impact tiers complete green" "rc1=$RC1 out1=$OUT1 rc2=$RC2 out2=$OUT2"
fi
case "$OV" in
  ERR:*) fail "S1: the window log records two complete tier windows" "$OV" ;;
  *) if python3 -c "import sys; sys.exit(0 if float(sys.argv[1]) <= 0 else 1)" "$OV"; then
       pass "S1: the two tier windows do NOT overlap (overlap=${OV}s, i.e. a real gap)"
     else
       fail "S1: concurrent tier windows must not overlap" \
            "they overlapped by ${OV}s — the seat ran two non-hermetic tiers at once"
     fi ;;
esac

# ==========================================================================
# S2 — NEGATIVE CONTROL: with the lock off, the same harness SEES the overlap
# ==========================================================================
run_pair "$TMPROOT/unserialized.jsonl" env SABLE_MG_IMPACT_SERIALIZE=0
OV0="$(overlap_seconds "$TMPROOT/unserialized.jsonl")"
case "$OV0" in
  ERR:*) fail "S2: the window log records two complete tier windows" "$OV0" ;;
  *) if python3 -c "import sys; sys.exit(0 if float(sys.argv[1]) > 0 else 1)" "$OV0"; then
       pass "S2: negative control — unserialized tiers DO overlap (${OV0}s), so S1 is not vacuous"
     else
       fail "S2: the instrument can detect overlap at all" \
            "with SABLE_MG_IMPACT_SERIALIZE=0 the windows still did not overlap (overlap=${OV0}s); \
S1's non-overlap therefore proves nothing"
     fi ;;
esac

# ==========================================================================
# S3 — one lock file per repo, in the merge-gate state dir
# ==========================================================================
LOCKPATH="$(python3 -c "
import sys
sys.path.insert(0, sys.argv[1])
import sable_gate_promote_lib as p
print(p.impact_lock_path(sys.argv[2]))" "$REPO_ROOT/bin" "$FIXTURE")"
if [ "$LOCKPATH" = "$STATE/impact-tier.lock" ] && [ -f "$LOCKPATH" ]; then
  pass "S3: the lock is one file in the repo's merge-gate state dir ($LOCKPATH)"
else
  fail "S3: the lock lives in the merge-gate state dir" "got=$LOCKPATH state=$STATE"
fi

# ==========================================================================
# S4 — a tier that could not START is an ERROR, never a green or a red
# ==========================================================================
HOLDER="$TMPROOT/holder.py"
cat > "$HOLDER" <<'EOF'
import fcntl, sys, time
fh = open(sys.argv[1], "a+")
fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
print("held", flush=True)
time.sleep(60)
EOF
python3 "$HOLDER" "$STATE/impact-tier.lock" > "$TMPROOT/holder.out" &
HOLDER_PID=$!
for _ in $(seq 1 100); do
  grep -q held "$TMPROOT/holder.out" 2>/dev/null && break
  sleep 0.05
done
if grep -q held "$TMPROOT/holder.out" 2>/dev/null; then
  OUT="$(SABLE_MG_IMPACT_LOCK_TIMEOUT=0.5 SABLE_MG_IMPACT_WINDOW_LOG="$TMPROOT/s4.jsonl" \
         python3 "$DRIVER" "$REPO_ROOT/bin" "$FIXTURE" "$COMBINED_SHA" 2>&1)"; RC=$?
  if [ "$RC" -ne 0 ] && printf '%s' "$OUT" | grep -q '^error'; then
    pass "S4: a promote that never got the seat reports ERROR (-> full re-preview), not green/red"
  else
    fail "S4: an unstartable tier is an ERROR" "rc=$RC out=$OUT"
  fi
  if [ ! -s "$TMPROOT/s4.jsonl" ]; then
    pass "S4: and it opened no tier window at all — it never ran"
  else
    fail "S4: a tier that never started must stamp no window" "$(cat "$TMPROOT/s4.jsonl")"
  fi
else
  fail "S4: the fixture holder acquired the lock" "$(cat "$TMPROOT/holder.out" 2>/dev/null)"
fi

# ==========================================================================
# S5 — flock, not a pidfile: killing the holder frees the seat
# ==========================================================================
kill -9 "$HOLDER_PID" 2>/dev/null
wait "$HOLDER_PID" 2>/dev/null
OUT="$(SABLE_MG_IMPACT_LOCK_TIMEOUT=10 SABLE_MG_IMPACT_WINDOW_LOG="$TMPROOT/s5.jsonl" \
       python3 "$DRIVER" "$REPO_ROOT/bin" "$FIXTURE" "$COMBINED_SHA" 2>&1)"; RC=$?
if [ "$RC" -eq 0 ]; then
  pass "S5: a holder killed mid-tier releases the seat — the next promote is not wedged"
else
  fail "S5: the kernel releases the lock when a holder dies" "rc=$RC out=$OUT"
fi

# --- no worktree left behind by any of the above ---------------------------
LEFT="$(git -C "$FIXTURE" worktree list | tail -n +2)"
if [ -z "$LEFT" ]; then
  pass "S6: no combined-tree worktree survived any tier run"
else
  fail "S6: every tier run cleans up its throwaway worktree" "$LEFT"
fi

echo "----------------------------------------------------------------------"
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
[ "$FAIL" -eq 0 ]
