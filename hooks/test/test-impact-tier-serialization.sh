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
#      S1/S2 both classify overlap from the window log's own APPEND ORDER, not
#      wall-clock timestamps (SABLE-awmj4) — immune to clock skew and to a
#      near-zero-margin threshold flake. A driver killed by a contended CI
#      host (its own rc is 128+signal) makes its window legitimately
#      incomplete; that reports SKIP (inconclusive), never a FAIL, because
#      host contention killing a process is not evidence the lock is broken.
#   S3 the lock is one file per repo, in the merge-gate state dir, so every
#      worktree of a seat contends on it (that IS the collision being prevented).
#   S4 a tier that could not START (lock timeout) reports ERROR, not GREEN and
#      not RED — an unstartable tier taught us nothing, so it must degrade to a
#      full re-preview rather than promote or blame an author.
#   S5 flock, not a pidfile: killing a holder mid-tier releases the seat. A ^C at
#      the merge seat must not wedge every later promote.
#   S7 SABLE-jd5fj.15 (the follow-up this bead's own docstring calls for):
#      hermeticizing beats queueing. With the lock OFF (SABLE_MG_IMPACT_SERIALIZE=0)
#      and the tier body replaced by a probe that actually touches bd + a
#      HOME/settings-shaped path, two REAL concurrent tiers must land in
#      DIFFERENT isolated HOME/BEADS_DB scratch dirs and neither may observe the
#      other's write — the overlap S2 proves is dangerous here becomes provably
#      harmless, which is the whole point of hermeticizing instead of queueing.
#   S8 tier_window_verdict's SKIP path, forced rather than awaited: a driver is
#      genuinely SIGKILLed mid-tier, and the verdict on its (real, incomplete)
#      window must be SKIP, never FAIL/ERR. A green S1/S2 alone never proves
#      this path executes — it only fires under real CI contention.
#   S9 the negative control for S8: an incomplete window whose driver exited
#      with a plain non-signal rc must still classify ERR, not SKIP — rules
#      out an implementation that fails open by SKIPping every incomplete
#      window regardless of cause.
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
SKIP=0
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
skip() { SKIP=$((SKIP+1)); echo "SKIP: $1"; }

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
import os, sys
sys.path.insert(0, sys.argv[1])
import sable_gate_promote_lib as promote_lib
# SABLE-awmj4: flushed BEFORE run_impact_tier does any real work, so this
# line survives even a driver that a contended CI host kills mid-tier — it
# is what lets the test correlate ITS OWN rc/output with a specific pid in
# the window log, instead of guessing from bash's internal subshell pid.
print(f"PID={os.getpid()}", flush=True)
outcome, detail = promote_lib.run_impact_tier(sys.argv[2], sys.argv[3], ["bin/thing.py"])
print(f"{outcome}\t{detail}")
sys.exit(0 if outcome == promote_lib.IMPACT_GREEN else 1)
EOF

# run_pair <window-log> [extra env assignments...] — two backgrounded tier runs
# against the one fixture repo. Echoes nothing; sets RC1/RC2/OUT1/OUT2/PID1/PID2.
# PID1/PID2 come from the DRIVER's own flushed "PID=" line (SABLE-awmj4), not
# bash's $!, so they name the SAME process identity the window log itself
# records via os.getpid() even when a driver dies before writing anything else.
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
  PID1="$(printf '%s\n' "$OUT1" | sed -n 's/^PID=//p' | head -n1)"
  PID2="$(printf '%s\n' "$OUT2" | sed -n 's/^PID=//p' | head -n1)"
}

# tier_window_verdict <window-log> <pid1> <rc1> <pid2> <rc2> — classifies the
# two recorded tier windows as OVERLAP or SEPARATE using the window log's own
# APPEND ORDER (SABLE-awmj4), never wall-clock `at` deltas: O_APPEND writes to
# one file are kernel-serialized, so a line's POSITION is a true total order
# of when each stamp actually landed, with no clock-skew or near-zero-margin
# flakiness a wall-clock subtraction invites. Prints exactly one of:
#   SEPARATE:<info>  the two windows did not interleave in log order
#   OVERLAP:<info>   the two windows DID interleave in log order
#   SKIP:<info>      a pid's driver was killed by an external signal (its own
#                     rc is 128+N) so its window is legitimately incomplete —
#                     this run is INCONCLUSIVE about serialization, not a
#                     failure of it; host contention is not a lock defect
#   ERR:<info>       a stamp is missing for a reason other than a signalled
#                     kill — names exactly which pid/event, a real gap to
#                     investigate rather than a bare stamp-count mismatch
tier_window_verdict() {
  local log="$1" pid1="$2" rc1="$3" pid2="$4" rc2="$5"
  if [ -z "$pid1" ] || [ -z "$pid2" ]; then
    printf 'ERR:%s\n' "a driver's own PID line was never seen (pid1='$pid1' rc1=$rc1, pid2='$pid2' rc2=$rc2) — cannot correlate window-log rows to a driver"
    return
  fi
  python3 - "$log" "$pid1" "$rc1" "$pid2" "$rc2" <<'EOF'
import json, sys

log, pid1, rc1, pid2, rc2 = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
rcs = {pid1: rc1, pid2: rc2}

try:
    rows = [json.loads(l) for l in open(log) if l.strip()]
except FileNotFoundError:
    rows = []

spans = {}
for idx, r in enumerate(rows):
    spans.setdefault(r.get("pid"), {})[r.get("event")] = idx

expected = [pid1, pid2]
incomplete = [p for p in expected if not ({"start", "end"} <= spans.get(p, {}).keys())]

if incomplete:
    if all(rcs.get(p, 0) >= 128 for p in incomplete):
        detail = "; ".join(
            f"pid {p} driver terminated by signal {rcs[p] - 128} (rc={rcs[p]})" for p in incomplete
        )
        print(f"SKIP:{detail}")
        sys.exit(0)
    detail = "; ".join(
        f"pid {p}: recorded {sorted(spans.get(p, {}).keys()) or ['nothing']} (rc={rcs.get(p)})"
        for p in incomplete
    )
    print(f"ERR:missing stamp(s) — {detail}")
    sys.exit(0)

a = (spans[pid1]["start"], spans[pid1]["end"], pid1)
b = (spans[pid2]["start"], spans[pid2]["end"], pid2)
first, second = sorted([a, b])
gap = second[0] - first[1]
verdict = "SEPARATE" if gap > 0 else "OVERLAP"
print(f"{verdict}:log-order gap {gap} line(s) between pid {first[2]}'s end stamp and pid {second[2]}'s start stamp")
EOF
}

export SABLE_MERGE_GATE_STATE="$STATE"
export SABLE_MG_IMPACT="bash $TIER"
# S1-S6/S8-S9 exercise locking and window semantics with an injected tier body;
# their bead store is deliberately irrelevant. Respect the gate's documented
# subprocess seam so those runs do not cold-initialize a real bd database.
# S7 explicitly unsets this override below: it is the one authority that
# creates real beads and proves the per-run BEADS_DB isolation boundary.
export SABLE_MG_BD=true
unset SABLE_MG_IMPACT_LOCK SABLE_MG_IMPACT_SERIALIZE SABLE_MG_IMPACT_LOCK_TIMEOUT

# ==========================================================================
# S1 — serialized: the windows do not overlap, and both promotes get answers
# ==========================================================================
run_pair "$TMPROOT/serialized.jsonl"
if [ "$RC1" -eq 0 ] && [ "$RC2" -eq 0 ]; then
  pass "S1: both concurrent impact tiers completed GREEN"
else
  fail "S1: both concurrent impact tiers complete green" "rc1=$RC1 out1=$OUT1 rc2=$RC2 out2=$OUT2"
fi
VERDICT="$(tier_window_verdict "$TMPROOT/serialized.jsonl" "$PID1" "$RC1" "$PID2" "$RC2")"
case "$VERDICT" in
  SKIP:*) skip "S1: the window log records two complete tier windows — ${VERDICT#SKIP:}" ;;
  ERR:*) fail "S1: the window log records two complete tier windows" "${VERDICT#ERR:}" ;;
  SEPARATE:*) pass "S1: the two tier windows do NOT overlap (${VERDICT#SEPARATE:})" ;;
  OVERLAP:*) fail "S1: concurrent tier windows must not overlap" \
                   "they overlapped (${VERDICT#OVERLAP:}) — the seat ran two non-hermetic tiers at once" ;;
esac

# ==========================================================================
# S2 — NEGATIVE CONTROL: with the lock off, the same harness SEES the overlap
# ==========================================================================
run_pair "$TMPROOT/unserialized.jsonl" env SABLE_MG_IMPACT_SERIALIZE=0
VERDICT0="$(tier_window_verdict "$TMPROOT/unserialized.jsonl" "$PID1" "$RC1" "$PID2" "$RC2")"
case "$VERDICT0" in
  SKIP:*) skip "S2: negative control — inconclusive, a driver was killed by host contention (${VERDICT0#SKIP:})" ;;
  ERR:*) fail "S2: the window log records two complete tier windows" "${VERDICT0#ERR:}" ;;
  OVERLAP:*) pass "S2: negative control — unserialized tiers DO overlap (${VERDICT0#OVERLAP:}), so S1 is not vacuous" ;;
  SEPARATE:*) fail "S2: the instrument can detect overlap at all" \
                    "with SABLE_MG_IMPACT_SERIALIZE=0 the windows still did not overlap (${VERDICT0#SEPARATE:}); \
S1's non-overlap therefore proves nothing" ;;
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

# ==========================================================================
# S7 — HERMETIC (SABLE-jd5fj.15): with serialization OFF, two REAL concurrent
# tiers whose suite bodies actually touch bd + a HOME/settings-shaped path
# must not observe each other's writes, and both must still go GREEN. This is
# the claim jd5fj.13's own docstring makes: hermeticizing the shared state
# kills the false-RED class outright, where the flock only ever queued
# around it. Clean-room safe: the bd-touching assertions below SKIP loudly
# (never a silent pass) when bd is not on PATH; the HOME/TMPDIR isolation
# assertions need only bash + python3 and always run.
# ==========================================================================
MARKERS="$TMPROOT/markers"
mkdir -p "$MARKERS"

BDPROBE="$TMPROOT/bd-settings-probe.sh"
cat > "$BDPROBE" <<PROBE
#!/usr/bin/env bash
set -uo pipefail
TAG="run-\$\$"
echo "\$HOME" > "$MARKERS/\$TAG.home"
echo "\${BEADS_DB:-}" > "$MARKERS/\$TAG.beads_db"
echo "\${TMPDIR:-}" > "$MARKERS/\$TAG.tmpdir"
SETTINGS_PATH="\$HOME/.claude/settings.json"
if [ -f "\$SETTINGS_PATH" ]; then
  MODE="\$(python3 -c 'import os,stat,sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode))[-3:])' "\$SETTINGS_PATH" 2>/dev/null)"
  echo "present:\$SETTINGS_PATH:\$MODE" > "$MARKERS/\$TAG.settings"
else
  echo "absent::" > "$MARKERS/\$TAG.settings"
fi
if command -v bd >/dev/null 2>&1 && [ -n "\${BEADS_DB:-}" ]; then
  # Create OWN bead, then wait — so a run whose write leaked into (or was
  # visible from) the OTHER run's DB would already show it by the time each
  # side snapshots its own listing below. The snapshot is taken and written
  # to the marker HERE, before run_impact_tier's own cleanup rmtree's this
  # DB out from under a post-hoc query.
  bd create --sandbox -q --title="jd5fj15-hermetic-probe-\$TAG" >/dev/null 2>&1 || true
  sleep 1.0
  bd list --all --json > "$MARKERS/\$TAG.titles.json" 2>/dev/null || echo '[]' > "$MARKERS/\$TAG.titles.json"
else
  sleep 0.5
fi
exit 0
PROBE
chmod +x "$BDPROBE"

run_pair "$TMPROOT/hermetic.jsonl" env -u SABLE_MG_BD SABLE_MG_IMPACT_SERIALIZE=0 \
  SABLE_MG_IMPACT="bash $BDPROBE"

if [ "$RC1" -eq 0 ] && [ "$RC2" -eq 0 ]; then
  pass "S7: two REAL concurrent hermetic tiers (serialization OFF) both report GREEN"
else
  fail "S7: two REAL concurrent hermetic tiers (serialization OFF) both report GREEN" \
       "rc1=$RC1 out1=$OUT1 rc2=$RC2 out2=$OUT2"
fi

mapfile -t HOME_MARKERS < <(find "$MARKERS" -maxdepth 1 -name '*.home' | sort)
if [ "${#HOME_MARKERS[@]}" -eq 2 ]; then
  pass "S7: exactly two hermetic probe runs recorded a HOME marker"
  HOME_A="$(cat "${HOME_MARKERS[0]}")"; HOME_B="$(cat "${HOME_MARKERS[1]}")"
  TAG_A="$(basename "${HOME_MARKERS[0]}" .home)"; TAG_B="$(basename "${HOME_MARKERS[1]}" .home)"

  if [ -n "$HOME_A" ] && [ -n "$HOME_B" ] && [ "$HOME_A" != "$HOME_B" ]; then
    pass "S7: the two concurrent tiers got DIFFERENT isolated HOME dirs"
  else
    fail "S7: the two concurrent tiers got DIFFERENT isolated HOME dirs" "A=$HOME_A B=$HOME_B"
  fi
  if [ "$HOME_A" != "$HOME" ] && [ "$HOME_B" != "$HOME" ]; then
    pass "S7: neither tier ran under the REAL \$HOME"
  else
    fail "S7: neither tier ran under the REAL \$HOME" "real=$HOME A=$HOME_A B=$HOME_B"
  fi

  TMPDIR_A="$(cat "$MARKERS/$TAG_A.tmpdir" 2>/dev/null || echo "")"
  TMPDIR_B="$(cat "$MARKERS/$TAG_B.tmpdir" 2>/dev/null || echo "")"
  if [ -n "$TMPDIR_A" ] && [ -n "$TMPDIR_B" ] && [ "$TMPDIR_A" != "$TMPDIR_B" ]; then
    pass "S7: the two concurrent tiers got DIFFERENT isolated TMPDIRs"
  else
    fail "S7: the two concurrent tiers got DIFFERENT isolated TMPDIRs" "A=$TMPDIR_A B=$TMPDIR_B"
  fi

  if [ -f "$HOME/.claude/settings.json" ]; then
    SETTINGS_A="$(cat "$MARKERS/$TAG_A.settings" 2>/dev/null || echo "")"
    SETTINGS_B="$(cat "$MARKERS/$TAG_B.settings" 2>/dev/null || echo "")"
    IFS=: read -r STATUS_A SPATH_A SMODE_A <<< "$SETTINGS_A"
    IFS=: read -r STATUS_B SPATH_B SMODE_B <<< "$SETTINGS_B"
    # ATTRIBUTABLE, not just "a file exists somewhere": the path must be
    # inside THIS run's own isolated HOME (A's prefix != B's prefix, and
    # neither is the real live settings.json every run could otherwise see
    # ambiently) — proven with the mkdtemp'd HOME_A/HOME_B captured above.
    if [ "$STATUS_A" = "present" ] && [ "$STATUS_B" = "present" ] \
       && [ "${SPATH_A#"$HOME_A"}" != "$SPATH_A" ] \
       && [ "${SPATH_B#"$HOME_B"}" != "$SPATH_B" ] \
       && [ "$SPATH_A" != "$SPATH_B" ]; then
      pass "S7: each tier's settings.json VIEW lives INSIDE its own isolated HOME (A=$SPATH_A B=$SPATH_B)"
    else
      fail "S7: each tier's settings.json VIEW lives INSIDE its own isolated HOME" \
           "status_A=$STATUS_A path_A=$SPATH_A home_A=$HOME_A; status_B=$STATUS_B path_B=$SPATH_B home_B=$HOME_B"
    fi
    # A copy, not the live file: mode 0444 additionally proves the isolated
    # env made its OWN read-only copy rather than inheriting the real
    # (differently-moded) ~/.claude/settings.json.
    if [ "$SMODE_A" = "444" ] && [ "$SMODE_B" = "444" ]; then
      pass "S7: both settings.json VIEWs are read-only copies (mode 444), not the live file"
    else
      fail "S7: both settings.json VIEWs are read-only copies (mode 444)" \
           "mode_A=$SMODE_A mode_B=$SMODE_B"
    fi
  else
    skip "S7: settings.json VIEW check — this host has no real ~/.claude/settings.json"
  fi

  if command -v bd >/dev/null 2>&1; then
    BEADS_DB_A="$(cat "$MARKERS/$TAG_A.beads_db" 2>/dev/null || echo "")"
    BEADS_DB_B="$(cat "$MARKERS/$TAG_B.beads_db" 2>/dev/null || echo "")"
    if [ -n "$BEADS_DB_A" ] && [ -n "$BEADS_DB_B" ] && [ "$BEADS_DB_A" != "$BEADS_DB_B" ]; then
      pass "S7: the two concurrent tiers got DIFFERENT isolated BEADS_DBs"
    else
      fail "S7: the two concurrent tiers got DIFFERENT isolated BEADS_DBs" \
           "A=$BEADS_DB_A B=$BEADS_DB_B"
    fi

    TITLES_A="$(python3 -c 'import json,sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    data = []
print(" ".join(b.get("title","") for b in data))' "$MARKERS/$TAG_A.titles.json" 2>/dev/null)"
    TITLES_B="$(python3 -c 'import json,sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    data = []
print(" ".join(b.get("title","") for b in data))' "$MARKERS/$TAG_B.titles.json" 2>/dev/null)"
    if printf '%s' "$TITLES_A" | grep -q "$TAG_A" && ! printf '%s' "$TITLES_A" | grep -q "$TAG_B"; then
      pass "S7: run A's isolated bd DB has its OWN probe bead and NOT run B's"
    else
      fail "S7: run A's isolated bd DB has its OWN probe bead and NOT run B's" \
           "titles_A='$TITLES_A' (want $TAG_A, not $TAG_B)"
    fi
    if printf '%s' "$TITLES_B" | grep -q "$TAG_B" && ! printf '%s' "$TITLES_B" | grep -q "$TAG_A"; then
      pass "S7: run B's isolated bd DB has its OWN probe bead and NOT run A's"
    else
      fail "S7: run B's isolated bd DB has its OWN probe bead and NOT run A's" \
           "titles_B='$TITLES_B' (want $TAG_B, not $TAG_A)"
    fi
  else
    skip "S7: bd-isolation assertions — bd not on PATH (clean-room) — HOME/TMPDIR isolation above still ran for real"
  fi
else
  fail "S7: exactly two hermetic probe runs recorded a HOME marker" \
       "found ${#HOME_MARKERS[@]}: ${HOME_MARKERS[*]:-<none>}"
fi

# ==========================================================================
# S8 — tier_window_verdict's SKIP path, forced deterministically (SABLE-awmj4
# review, optimus): a driver genuinely SIGKILLed mid-tier must classify SKIP,
# never FAIL/ERR. This is the exact mechanism the bead exists to add, and a
# green run of S1/S2 alone never proves it executes — it only fires when CI
# contention supplies a real kill, which no ordinary run does. Force it.
# ==========================================================================
KILLLOG="$TMPROOT/skip.jsonl"
rm -f "$KILLLOG"

SLOWTIER="$TMPROOT/slow-tier.sh"
cat > "$SLOWTIER" <<'EOF'
#!/usr/bin/env bash
sleep 5
exit 0
EOF
chmod +x "$SLOWTIER"

( SABLE_MG_IMPACT_WINDOW_LOG="$KILLLOG" SABLE_MG_IMPACT="bash $SLOWTIER" \
    python3 "$DRIVER" "$REPO_ROOT/bin" "$FIXTURE" "$COMBINED_SHA" \
    > "$TMPROOT/kill_out" 2>&1 ) &
KILL_SUBPID=$!
KILL_PID=""
for _ in $(seq 1 100); do
  KILL_PID="$(sed -n 's/^PID=//p' "$TMPROOT/kill_out" 2>/dev/null | head -n1)"
  [ -n "$KILL_PID" ] && break
  sleep 0.05
done
# Killed the instant its own PID is visible — the tier body sleeps 5s, so
# this lands well before any "end" stamp could exist; no timing margin to
# tune, no chance of the tier finishing first.
if [ -n "$KILL_PID" ]; then
  kill -9 "$KILL_PID" 2>/dev/null
fi
wait "$KILL_SUBPID"; KILL_RC=$?

( SABLE_MG_IMPACT_WINDOW_LOG="$KILLLOG" python3 "$DRIVER" "$REPO_ROOT/bin" "$FIXTURE" "$COMBINED_SHA" \
    > "$TMPROOT/kill_ok_out" 2>&1 ) &
OK_SUBPID=$!
wait "$OK_SUBPID"; OK_RC=$?
OK_PID="$(sed -n 's/^PID=//p' "$TMPROOT/kill_ok_out" 2>/dev/null | head -n1)"

if [ -n "$KILL_PID" ] && [ "$KILL_RC" -ge 128 ]; then
  pass "S8: the SIGKILLed driver's own rc is signal-shaped (rc=$KILL_RC)"
else
  fail "S8: the SIGKILLed driver's own rc is signal-shaped" \
       "pid='$KILL_PID' rc=$KILL_RC — the kill may not have landed before the tier finished"
fi
if [ -n "$OK_PID" ] && [ "$OK_RC" -eq 0 ]; then
  VERDICT8="$(tier_window_verdict "$KILLLOG" "$KILL_PID" "$KILL_RC" "$OK_PID" "$OK_RC")"
  case "$VERDICT8" in
    SKIP:*) pass "S8: a genuinely SIGKILLed driver classifies SKIP, not FAIL/ERR (${VERDICT8#SKIP:})" ;;
    *) fail "S8: a genuinely SIGKILLed driver classifies SKIP, not FAIL/ERR" "got: $VERDICT8" ;;
  esac
else
  fail "S8: the counterpart driver completed normally" \
       "pid='$OK_PID' rc=$OK_RC out=$(cat "$TMPROOT/kill_ok_out" 2>/dev/null)"
fi

# ==========================================================================
# S9 — tier_window_verdict's ERR path, forced deterministically: the negative
# control for S8. An incomplete window whose driver did NOT die by signal (a
# plain non-zero exit) must still classify ERR, never SKIP. Without this, an
# implementation that returns SKIP for EVERY incomplete window — the fail-
# open bug S8 alone cannot catch — would pass S8 undetected.
# ==========================================================================
INCLOG="$TMPROOT/err.jsonl"
rm -f "$INCLOG"

INCDRIVER="$TMPROOT/incomplete-driver.py"
cat > "$INCDRIVER" <<'EOF'
import json, os, sys
print(f"PID={os.getpid()}", flush=True)
with open(os.environ["SABLE_MG_IMPACT_WINDOW_LOG"], "a") as fh:
    fh.write(json.dumps({"event": "start", "pid": os.getpid(), "at": 0,
                          "tree": "deadbeef", "waited": 0}) + "\n")
# A real "end" stamp is deliberately never written: this simulates a driver
# that recorded start and then exited abnormally WITHOUT a signal — must
# read as ERR, never as the SKIP path S8 forces.
sys.exit(1)
EOF

INC_OUT="$(SABLE_MG_IMPACT_WINDOW_LOG="$INCLOG" python3 "$INCDRIVER" 2>&1)"; INC_RC=$?
INC_PID="$(printf '%s\n' "$INC_OUT" | sed -n 's/^PID=//p' | head -n1)"

( SABLE_MG_IMPACT_WINDOW_LOG="$INCLOG" python3 "$DRIVER" "$REPO_ROOT/bin" "$FIXTURE" "$COMBINED_SHA" \
    > "$TMPROOT/err_ok_out" 2>&1 ) &
ERR_OK_SUBPID=$!
wait "$ERR_OK_SUBPID"; ERR_OK_RC=$?
ERR_OK_PID="$(sed -n 's/^PID=//p' "$TMPROOT/err_ok_out" 2>/dev/null | head -n1)"

if [ -n "$INC_PID" ] && [ "$INC_RC" -eq 1 ]; then
  pass "S9: the incomplete driver's own rc is a plain, non-signal exit (rc=$INC_RC)"
else
  fail "S9: the incomplete driver's own rc is a plain, non-signal exit" \
       "pid='$INC_PID' rc=$INC_RC out=$INC_OUT"
fi
if [ -n "$ERR_OK_PID" ] && [ "$ERR_OK_RC" -eq 0 ]; then
  VERDICT9="$(tier_window_verdict "$INCLOG" "$INC_PID" "$INC_RC" "$ERR_OK_PID" "$ERR_OK_RC")"
  case "$VERDICT9" in
    ERR:*) pass "S9: an incomplete window with a non-signal rc classifies ERR, not SKIP (${VERDICT9#ERR:})" ;;
    *) fail "S9: an incomplete window with a non-signal rc classifies ERR, not SKIP" \
            "got: $VERDICT9 — a SKIP here would be the fail-open bug S8 cannot catch alone" ;;
  esac
else
  fail "S9: the counterpart driver completed normally" \
       "pid='$ERR_OK_PID' rc=$ERR_OK_RC out=$(cat "$TMPROOT/err_ok_out" 2>/dev/null)"
fi

echo "----------------------------------------------------------------------"
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL | Skipped: $SKIP"
[ "$FAIL" -eq 0 ]
