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
#   S7 SABLE-jd5fj.15 (the follow-up this bead's own docstring calls for):
#      hermeticizing beats queueing. With the lock OFF (SABLE_MG_IMPACT_SERIALIZE=0)
#      and the tier body replaced by a probe that actually touches bd + a
#      HOME/settings-shaped path, two REAL concurrent tiers must land in
#      DIFFERENT isolated HOME/BEADS_DB scratch dirs and neither may observe the
#      other's write — the overlap S2 proves is dangerous here becomes provably
#      harmless, which is the whole point of hermeticizing instead of queueing.
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

run_pair "$TMPROOT/hermetic.jsonl" env SABLE_MG_IMPACT_SERIALIZE=0 \
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

echo "----------------------------------------------------------------------"
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL | Skipped: $SKIP"
[ "$FAIL" -eq 0 ]
