#!/usr/bin/env bash
# test-sable-msg.sh — real-tmux coverage for sable-msg: verified delivery
# (SABLE-bq93) and bead-addressed worker delivery (SABLE-6izz).
#
# Composes:
#   - a manager-role pane (a stand-in "TUI" that shows nothing for a boot
#     delay, then a bare `❯ ` prompt) to prove --interrupt survives a
#     still-booting pane, VERIFIED by grepping capture-pane for the framed
#     header AND the recorded submitted line -- not just trusting send-keys'
#     zero exit code (the exact SABLE-bq93 false-positive).
#   - a worker-tagged pane (@sable_role=worker, @sable_bead=<id>, the same
#     tags sable-spawn-worker sets) to prove --bead delivery resolves it
#     (SABLE-6izz), that an unknown bead id fails cleanly, and that plain
#     manager-role resolution is unchanged.
#
# Run with: bash hooks/test/test-sable-msg.sh

set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
BIN="$REPO/bin"
command -v tmux >/dev/null 2>&1 || { echo "SKIP: tmux not installed"; exit 0; }

SOCK="sable-msg-e2e-$$"
REC="$(mktemp -d)"
SCRATCH_BEADS_DIR=""

PASS=0; FAIL=0; FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
tmux_() { tmux -L "$SOCK" "$@"; }
# Pane-spawning calls (new-session/new-window/split-window/respawn-pane) route
# through the guarded sable_tmux_spawn instead of plain tmux -- see below.
tmux_spawn_() { sable_tmux_spawn -L "$SOCK" "$@"; }
cleanup() { tmux_ kill-server >/dev/null 2>&1; rm -rf "$REC"; [ -n "$SCRATCH_BEADS_DIR" ] && rm -rf "$SCRATCH_BEADS_DIR" 2>/dev/null || true; }
trap cleanup EXIT

# This suite is itself commonly run FROM a real SABLE pane (a worker or
# manager dispatched to verify it, per the normal workflow) whose shell
# carries its own CLAUDE_AGENT_NAME / SABLE_WORKER_PANE / SABLE_BEAD. tmux
# new-session/respawn-pane/new-window inherit the launching shell's
# environment into the spawned pane's process, so a stand-in pane meant to
# simulate a bare manager/worker process would instead inherit e.g.
# CLAUDE_AGENT_NAME=tarzan -- and sable-msg's SABLE-to8m poisoned-identity
# check (pane_process_identity reads /proc/PID/environ) then correctly
# refuses delivery, since the "recipient" pane's real identity doesn't match
# the role under test. Scrub here (SABLE-4nr0q) so every pane this suite
# spawns starts from a clean identity regardless of the invoking shell.
# Central scrub lives in lib-identity-isolation.sh (SABLE-j3bi/SABLE-a9453)
# so every suite shares one definition of "identity vars" instead of
# drifting copies of an unset list; sable_tmux_spawn is the guard that fails
# loudly if a future spawn call site ever bypasses this scrub.
source "$REPO/hooks/test/lib-identity-isolation.sh"
sable_scrub_identity_env

export SABLE_TMUX_SOCKET="$SOCK"
export SABLE_MSG_POLL_INTERVAL="0.1"
export SABLE_MSG_SUBMIT_TRIES="30"
export SABLE_MSG_READY_TIMEOUT="5"
# Single-fleet cases (1-4, 6) target the isolated session "w"; pin it so
# per-repo resolution (SABLE-e1e3 / c066ded) does not derive sable-<repo> and
# miss the pane (SABLE-r0m9). The per-repo case (5) unsets this inline for its
# own CWD-derivation path.
export SABLE_TMUX_SESSION="w"

# A stand-in "TUI" pane: shows nothing (not ready) for BOOT_DELAY seconds, then
# a bare `❯ ` prompt line -- exactly the shape sable_pane_lib.pane_ready looks
# for. Each submitted line is appended to $REC_FILE so we can assert on what
# actually landed as a turn (not just that send-keys exited 0).
STAND_IN="$REC/stand-in.sh"
cat > "$STAND_IN" <<'SCRIPT'
#!/usr/bin/env bash
sleep "${BOOT_DELAY:-0}"
# Drain + scroll away anything sent while booting (e.g. --interrupt's Escape):
# a real TUI redraws its own UI once ready rather than leaving pre-ready
# keystrokes' raw tty-echo bleeding into the first prompt line, which would
# otherwise defeat dispatch_landed's "does the last ❯/> line hold the
# message" box-detection (a stray leading control char keeps that line from
# ever matching, causing a false-positive "landed").
while read -t 0.2 -r -N 1 _junk; do :; done
printf '%.0s\n' $(seq 1 60)
while true; do
  printf '\xe2\x9d\xaf '
  IFS= read -r line || break
  echo "$line" >> "$REC_FILE"
done
SCRIPT
chmod +x "$STAND_IN"

# --- 1) manager pane, booting for 2s: --interrupt must not drop the turn ----
tmux_spawn_ new-session -d -s w -x 200 -y 50
tmux_spawn_ respawn-pane -k -t w "REC_FILE=$REC/optimus.txt BOOT_DELAY=2 $STAND_IN"
tmux_ set-option -p -t w @sable_role optimus
sleep 0.2   # still booting at this point

if CLAUDE_AGENT_NAME=lincoln python3 "$BIN/sable-msg" optimus "cap in force" --interrupt >/dev/null 2>&1; then
  pass "sable-msg --interrupt into a booting pane returns ok"
else
  fail "sable-msg --interrupt into a booting pane returns ok"
fi
cap="$(tmux_ capture-pane -t w -p)"
if printf '%s' "$cap" | grep -q '⟦SABLE-MSG⟧ from=lincoln to=optimus :: cap in force'; then
  pass "interrupt message verified landed on a still-booting pane (SABLE-bq93)"
else
  fail "interrupt message verified landed on a still-booting pane (SABLE-bq93)" "$cap"
fi
if [ -f "$REC/optimus.txt" ] && grep -q 'cap in force' "$REC/optimus.txt"; then
  pass "message was actually SUBMITTED as a turn, not just typed"
else
  fail "message was actually SUBMITTED as a turn, not just typed"
fi

# --- 2) worker pane, tagged like sable-spawn-worker tags it -----------------
BEAD="market-brief-package-73t4"
tmux_spawn_ new-window -d -t w: -n worker -c /tmp "PS1='> ' bash --noprofile --norc"
wpane="$(tmux_ list-panes -a -F '#{pane_id} #{window_name}' | awk '$2=="worker"{print $1; exit}')"
tmux_ set-option -p -t "$wpane" @sable_role worker
tmux_ set-option -p -t "$wpane" @sable_bead "$BEAD"
tmux_ set-option -p -t "$wpane" @sable_status running
sleep 0.3

if CLAUDE_AGENT_NAME=optimus python3 "$BIN/sable-msg" "$BEAD" "hold the tree claim" --bead >/dev/null 2>&1; then
  pass "sable-msg --bead resolves a worker pane by @sable_bead (SABLE-6izz)"
else
  fail "sable-msg --bead resolves a worker pane by @sable_bead (SABLE-6izz)"
fi
wcap="$(tmux_ capture-pane -t "$wpane" -p)"
if printf '%s' "$wcap" | grep -q "⟦SABLE-MSG⟧ from=optimus to=$BEAD :: hold the tree claim"; then
  pass "bead-addressed message delivered to the correct worker pane"
else
  fail "bead-addressed message delivered to the correct worker pane" "$wcap"
fi

# --- 3) unknown bead id fails cleanly ----------------------------------------
ERRFILE="$REC/err.txt"
if python3 "$BIN/sable-msg" ghost-bead "hello" --bead --from optimus >/dev/null 2>"$ERRFILE"; then
  fail "unknown bead id errors cleanly (unexpectedly returned 0)"
else
  if grep -q "ghost-bead" "$ERRFILE"; then
    pass "unknown bead id errors cleanly"
  else
    fail "unknown bead id errors cleanly" "$(cat "$ERRFILE")"
  fi
fi

# --- 4) manager-role resolution is unchanged (regression) --------------------
if CLAUDE_AGENT_NAME=lincoln python3 "$BIN/sable-msg" optimus "still routes by role" >/dev/null 2>&1; then
  pass "manager-role resolution unchanged"
else
  fail "manager-role resolution unchanged"
fi

# --- 5) cross-repo CWD vs actual pane session (market-brief-package-ssd8) ---
# The live bug this bead fixes: a worker's shell CWD can be a DIFFERENT
# repo's worktree than the tmux session it actually lives in (e.g. dispatched
# by a manager tracking repo A but working in repo B's worktree as CWD).
# Plain sable-msg from that worker -- NO SABLE_TMUX_SESSION override -- must
# still reach the manager in its OWN actual session, not whatever session
# CWD-derivation would compute for repo B. Repo B is a REAL, concurrently
# running fleet (not a nonexistent guessed name): CWD-derivation confidently
# resolves a WRONG-but-real session and never falls through to any rescuing
# heuristic, matching the live failure exactly.
REPO_A="$REC/repo-alpha"; REPO_B="$REC/repo-beta"
mkdir -p "$REPO_A" "$REPO_B"
git init -q "$REPO_A"; git init -q "$REPO_B"
SESS_A="sable-$(basename "$REPO_A")"
SESS_B="sable-$(basename "$REPO_B")"

tmux_spawn_ new-session -d -s "$SESS_A" -x 200 -y 50 -c "$REPO_A" "PS1='> ' bash --noprofile --norc"
tmux_ set-option -t "$SESS_A" @sable_repo "$REPO_A"
tmux_ set-option -p -t "$SESS_A" @sable_role tarzan
tmux_spawn_ new-session -d -s "$SESS_B" -x 200 -y 50 -c "$REPO_B" "PS1='> ' bash --noprofile --norc"
tmux_ set-option -t "$SESS_B" @sable_repo "$REPO_B"
sleep 0.3

tarzan_pane="$(tmux_ list-panes -t "$SESS_A" -F '#{pane_id}')"
# a second pane in alpha's OWN session, shelled into beta's worktree — exactly
# the mismatched-CWD shape a cross-repo worker dispatch produces.
tmux_spawn_ split-window -t "$SESS_A" -d -c "$REPO_B" "PS1='> ' bash --noprofile --norc"
sleep 0.3
worker_pane="$(tmux_ list-panes -t "$SESS_A" -F '#{pane_id}' | grep -v "^$tarzan_pane$")"
tmux_ set-option -p -t "$worker_pane" @sable_role worker

# sent FROM the worker pane itself via send-keys, so $TMUX_PANE is real (set
# by tmux for that pane's own bash, not injected) even though CWD is beta.
tmux_ send-keys -t "$worker_pane" \
  "unset SABLE_TMUX_SESSION; SABLE_TMUX_SOCKET=$SOCK python3 $BIN/sable-msg tarzan 'cross-repo-ssd8-check' --from worker" Enter
sleep 1.5

acap="$(tmux_ capture-pane -t "$SESS_A" -p)"
bcap="$(tmux_ capture-pane -t "$SESS_B" -p)"
if printf '%s' "$acap" | grep -q '⟦SABLE-MSG⟧ from=worker to=tarzan :: cross-repo-ssd8-check'; then
  pass "sable-msg from a worker whose CWD is a different repo's worktree still reaches its own session's manager (market-brief-package-ssd8)"
else
  fail "sable-msg from a worker whose CWD is a different repo's worktree still reaches its own session's manager (market-brief-package-ssd8)" "$acap"
fi
if printf '%s' "$bcap" | grep -q 'cross-repo-ssd8-check'; then
  fail "message did NOT leak into the CWD-derived (wrong) repo's session" "$bcap"
else
  pass "message did NOT leak into the CWD-derived (wrong) repo's session"
fi

# --- 6) fresh-spawn window: --interrupt into a mid-turn pane (SABLE-m6is) -----
# The live failure: --interrupt into a manager pane actively mid-turn (seconds
# after sable-spawn-manager) dropped the message on all 8 submit attempts — the
# pane STILL shows the empty composer prompt during a turn, so pane_ready fired
# early and the message was typed into a pane still redrawing. This stand-in
# paints that exact shape (composer prompt + 'esc to interrupt'); a bare Escape
# settles it to an idle REPL that records submitted turns. The message is sent
# within ~2s of pane creation (the fresh-spawn window the failure hit).
BUSY_TUI="$REC/busy-tui.sh"
cat > "$BUSY_TUI" <<'SCRIPT'
#!/usr/bin/env bash
busy=1
END_AT=$((SECONDS + 60))   # only the interrupt can end the turn, never a timeout
while [ "$busy" = 1 ]; do
  printf '\033[H\033[2J  Running the turn (esc to interrupt)\n'
  printf '\xe2\x9d\xaf \n'
  if IFS= read -rsN1 -t 0.2 ch; then
    [ "$ch" = $'\x1b' ] && busy=0
  fi
  [ "$SECONDS" -ge "$END_AT" ] && busy=0
done
printf '\033[H\033[2J'
printf '%.0s\n' $(seq 1 60)
while true; do
  printf '\xe2\x9d\xaf '
  IFS= read -r line || break
  echo "$line" >> "$REC_FILE"
done
SCRIPT
chmod +x "$BUSY_TUI"

tmux_spawn_ new-window -d -t w: -n mgr2 "REC_FILE=$REC/tarzan.txt $BUSY_TUI"
mpane="$(tmux_ list-panes -a -F '#{pane_id} #{window_name}' | awk '$2=="mgr2"{print $1; exit}')"
tmux_ set-option -p -t "$mpane" @sable_role tarzan
sleep 0.3   # send within the fresh-spawn window, while the turn is busy

if CLAUDE_AGENT_NAME=lincoln python3 "$BIN/sable-msg" tarzan "fresh spawn wake" --interrupt >/dev/null 2>&1; then
  pass "sable-msg --interrupt lands on a freshly spawned mid-turn pane (SABLE-m6is)"
else
  fail "sable-msg --interrupt lands on a freshly spawned mid-turn pane (SABLE-m6is)" "$(tmux_ capture-pane -t "$mpane" -p)"
fi
if [ -f "$REC/tarzan.txt" ] && grep -q 'fresh spawn wake' "$REC/tarzan.txt"; then
  pass "fresh-spawn interrupt was SUBMITTED as a turn, not swallowed"
else
  fail "fresh-spawn interrupt was SUBMITTED as a turn, not swallowed" "$(tmux_ capture-pane -t "$mpane" -p)"
fi

# --- 7) unverifiable delivery files the SABLE-1umr fallback bead IN A SANDBOX,
#     never the live DB (SABLE-j0vr / SABLE-f3zp) -----------------------------
# A pane that never renders the composer prompt can never be confirmed IDLE,
# so deliver_text exhausts its retries and sable-msg falls back to
# file_fallback_bead. This is the exact shape test-tmux-e2e.sh's stand-in
# hits in isolated/CI runs (SABLE-gcmu) -- reproduced here directly against a
# pane that is provably never ready, so the fallback is guaranteed to fire
# rather than depending on timing. BEADS_DB scopes bd create to a throwaway
# sandbox DB; the assertions below prove the live repo DB never gained a bead.
if command -v bd >/dev/null 2>&1; then
  SCRATCH_BEADS_DIR="$(mktemp -d)"
  ( cd "$SCRATCH_BEADS_DIR" && BD_NON_INTERACTIVE=1 bd init --prefix=sbx \
      --non-interactive --skip-agents --skip-hooks --quiet >/dev/null 2>&1 )

  tmux_spawn_ new-window -d -t w: -n stuck 'sleep 60'
  stuckpane="$(tmux_ list-panes -a -F '#{pane_id} #{window_name}' | awk '$2=="stuck"{print $1; exit}')"
  tmux_ set-option -p -t "$stuckpane" @sable_role chuck
  sleep 0.2

  # Single-sourced so the fixture body the probe SENDS and the body the
  # attribution check LOOKS FOR can never drift apart.
  FIXTURE_BODY="sandbox fallback probe"

  ERRFILE2="$REC/fallback-err.txt"
  if CLAUDE_AGENT_NAME=lincoln SABLE_MSG_SUBMIT_TRIES=3 SABLE_MSG_POLL_INTERVAL=0.1 \
      BEADS_DB="$SCRATCH_BEADS_DIR/.beads" \
      python3 "$BIN/sable-msg" chuck "$FIXTURE_BODY" >/dev/null 2>"$ERRFILE2"; then
    fail "unverifiable delivery to a never-ready pane reports undelivered (unexpectedly returned 0)" "$(cat "$ERRFILE2")"
  else
    pass "unverifiable delivery to a never-ready pane reports undelivered"
  fi

  if grep -q "Filed durable inbox bead" "$ERRFILE2"; then
    pass "sable-msg's SABLE-1umr fallback fired"
  else
    fail "sable-msg's SABLE-1umr fallback fired" "$(cat "$ERRFILE2")"
  fi

  sandbox_count="$(BEADS_DB="$SCRATCH_BEADS_DIR/.beads" bd count 2>/dev/null)"
  if [ "$sandbox_count" = "1" ]; then
    pass "fallback bead was created IN THE SANDBOX"
  else
    fail "fallback bead was created IN THE SANDBOX" "sandbox bd count=$sandbox_count"
  fi
  sandbox_labeled="$(BEADS_DB="$SCRATCH_BEADS_DIR/.beads" bd count --by-label 2>/dev/null | grep -c 'for-chuck')"
  if [ "$sandbox_labeled" -ge 1 ]; then
    pass "sandboxed fallback bead carries the for-chuck inbox label"
  else
    fail "sandboxed fallback bead carries the for-chuck inbox label" "$(BEADS_DB="$SCRATCH_BEADS_DIR/.beads" bd list --all --json 2>/dev/null)"
  fi

  # --- ATTRIBUTION-SCOPED live-pollution check (SABLE-3mrv3) ----------------
  # This used to be `live_count_before=$(bd count)` / `live_count_after=$(bd
  # count)` / assert-equal. That is a GLOBAL COUNTER diff, and a global count
  # CANNOT ATTRIBUTE A DELTA: it answers "did the live DB change?" when the
  # question is "did MY TEST change the live DB?". On a working fleet those
  # differ constantly — ~1 bead filed per 5 min against a ~15 min impact tier
  # made tripping near-certain, and it RED'd branches that never touch
  # sable-msg. It also gets worse under exactly the behaviour we want:
  # managers filing findings promptly.
  #
  # The property from SABLE-j0vr is real and still checked — just scoped to
  # OUR fixture. The fallback bead's identity is deterministic: the title
  # sable-msg composes, plus the for-chuck inbox label. Another agent's
  # concurrent bead cannot match BOTH, so this is immune to fleet activity
  # rather than merely unlikely to collide.
  #
  # Derive the title from bin/sable-msg ITSELF rather than hand-typing it.
  # Hand-typing a format another file owns is what broke the first attempt at
  # this fix: the real title interpolates the FRAMED message (see
  # format_message), so the framing sits between the role and the body and a
  # guessed "...to chuck: sandbox fallback probe" matched nothing —
  # --title-contains then returned 0 unconditionally and the assertion could
  # not fail. Control (b) below exists to catch precisely that relapse.
  fallback_title="$(SABLE_FIXTURE_BODY="$FIXTURE_BODY" python3 - "$BIN" <<'PY'
import importlib.util, os, sys
from importlib.machinery import SourceFileLoader
loader = SourceFileLoader("sable_msg", os.path.join(sys.argv[1], "sable-msg"))
spec = importlib.util.spec_from_loader("sable_msg", loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
framed = mod.format_message("lincoln", "chuck", os.environ["SABLE_FIXTURE_BODY"])
print(mod.fallback_bead_title("chuck", framed), end="")
PY
)"
  if [ -n "$fallback_title" ] && [ "$fallback_title" != "${fallback_title#*$FIXTURE_BODY}" ]; then
    pass "fixture signature was derived from bin/sable-msg, not hand-typed"
  else
    fail "fixture signature was derived from bin/sable-msg, not hand-typed" "derived title=[$fallback_title]"
  fi

  # The predicate under test, parameterised by store so both directions can be
  # exercised against throwaway DBs instead of writing to the live one.
  # $1 = BEADS_DB dir, or "" for the live repo DB.
  fixture_bead_hits() {
    if [ -n "$1" ]; then
      BEADS_DB="$1" bd count --title-contains "$fallback_title" --label for-chuck 2>/dev/null
    else
      ( cd "$REPO" && bd count --title-contains "$fallback_title" --label for-chuck 2>/dev/null )
    fi
  }

  # The actual property: the fallback wrote to the sandbox, not to live.
  live_hits="$(fixture_bead_hits "")"
  if [ "$live_hits" = "0" ]; then
    pass "live bd DB never gained THIS fixture's fallback bead (attribution-scoped, not a global counter)"
  else
    fail "live bd DB never gained THIS fixture's fallback bead" "hits=$live_hits for title [$fallback_title]"
  fi

  # Sanity floor for the two controls: the predicate must SEE the bead the
  # fallback really filed. If the derived title did not match the sandbox
  # bead, the whole check is a constant and control (b) below is theatre.
  if [ "$(fixture_bead_hits "$SCRATCH_BEADS_DIR/.beads")" = "1" ]; then
    pass "the attribution predicate MATCHES the real fallback bead in the sandbox"
  else
    fail "the attribution predicate MATCHES the real fallback bead in the sandbox" \
         "derived title [$fallback_title] vs $(BEADS_DB="$SCRATCH_BEADS_DIR/.beads" bd list --all --json 2>/dev/null)"
  fi

  # --- NEGATIVE CONTROLS. Both directions are mandatory; the fix is not done
  # without them (skipping (b) is what let a non-functional assertion ship).
  # Run against a throwaway "pretend-live" DB — simulating fleet activity by
  # writing to the REAL live DB is the very pollution this leg forbids.
  PRETEND_LIVE_DIR="$(mktemp -d)"
  ( cd "$PRETEND_LIVE_DIR" && BD_NON_INTERACTIVE=1 bd init --prefix=plv \
      --non-interactive --skip-agents --skip-hooks --quiet >/dev/null 2>&1 )
  PRETEND_LIVE_DB="$PRETEND_LIVE_DIR/.beads"

  # (a) UNRELATED fleet activity must NOT trip the check. This is the bug:
  #     the old global-counter assertion RED'd here by construction.
  before_ct="$(BEADS_DB="$PRETEND_LIVE_DB" bd count 2>/dev/null)"
  BEADS_DB="$PRETEND_LIVE_DB" bd create --title="unrelated fleet bead filed mid-leg" \
      --description="simulated concurrent fleet activity (SABLE-3mrv3 control a)" \
      --type=task --priority=3 >/dev/null 2>&1
  after_ct="$(BEADS_DB="$PRETEND_LIVE_DB" bd count 2>/dev/null)"
  if [ "$(fixture_bead_hits "$PRETEND_LIVE_DB")" = "0" ]; then
    pass "CONTROL (a): unrelated concurrent bead leaves the check PASSING (old counter would have RED'd: before=$before_ct after=$after_ct)"
  else
    fail "CONTROL (a): unrelated concurrent bead leaves the check PASSING" \
         "predicate hit on a bead that is not ours"
  fi
  if [ "$before_ct" != "$after_ct" ]; then
    pass "CONTROL (a) is a REAL simulation: the global count did move ($before_ct -> $after_ct)"
  else
    fail "CONTROL (a) is a REAL simulation: the global count did move" "before=$before_ct after=$after_ct"
  fi

  # (b) A bead carrying the FIXTURE SIGNATURE must MAKE the check FAIL. This
  #     is the direction that proves the assertion still BITES and has not
  #     been weakened into always-passing. An assertion that cannot fail is
  #     worse than the global counter, because it looks like coverage.
  #
  #     Plant the title of the bead sable-msg REALLY filed, read back from the
  #     sandbox — NOT $fallback_title. Planting the derived string would make
  #     this control self-referential: the query would match its own input and
  #     pass even when the derivation is wrong, which is precisely the
  #     Defect-1 relapse this control exists to catch. The real bead is
  #     ground truth; the derivation is the thing under test.
  real_title="$(BEADS_DB="$SCRATCH_BEADS_DIR/.beads" bd list --all --json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["title"], end="")' 2>/dev/null)"
  BEADS_DB="$PRETEND_LIVE_DB" bd create --title="$real_title" \
      --description="planted fixture-signature bead (SABLE-3mrv3 control b)" \
      --type=task --priority=3 --labels=for-chuck,coord >/dev/null 2>&1
  planted_hits="$(fixture_bead_hits "$PRETEND_LIVE_DB")"
  if [ -n "$planted_hits" ] && [ "$planted_hits" -ge 1 ]; then
    pass "CONTROL (b): a planted fixture-signature bead MAKES the check FAIL (the assertion still bites)"
  else
    fail "CONTROL (b): a planted fixture-signature bead MAKES the check FAIL" \
         "predicate returned [$planted_hits] — the assertion is a CONSTANT and verifies nothing"
  fi

  rm -rf "$PRETEND_LIVE_DIR" 2>/dev/null || true
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
