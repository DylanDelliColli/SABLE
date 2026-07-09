#!/usr/bin/env bash
# test-sable-worker-status.sh — market-brief-package-c0k5: `--reap` crashes
# when a grouped-session topology causes `tmux list-panes -a` to enumerate the
# SAME physical pane once per session alias (reproduced live below via
# `tmux new-session -t <sess> -s <alias>`, which shares one window list under
# two session names). The reaper then calls kill-pane on the same pane twice;
# the second call throws CalledProcessError and aborts main(), leaving later
# done panes unreaped.
#
# Runs against a REAL tmux server on an isolated socket (-L); never touches
# the operator's session.
#
# Run with:
#   bash hooks/test/test-sable-worker-status.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
BIN="$REPO/bin/sable-worker-status"
SOCK="sws-test-$$"

cleanup() { tmux -L "$SOCK" kill-server >/dev/null 2>&1 || true; }
trap cleanup EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

tag() { # tag <pane> <role> <bead> <status>
  tmux -L "$SOCK" set-option -p -t "$1" @sable_role "$2"
  tmux -L "$SOCK" set-option -p -t "$1" @sable_bead "$3"
  tmux -L "$SOCK" set-option -p -t "$1" @sable_status "$4"
}

tag_full() { # tag_full <pane> <role> <bead> <status> <class> <deliverable>
  tmux -L "$SOCK" set-option -p -t "$1" @sable_role "$2"
  tmux -L "$SOCK" set-option -p -t "$1" @sable_bead "$3"
  tmux -L "$SOCK" set-option -p -t "$1" @sable_status "$4"
  tmux -L "$SOCK" set-option -p -t "$1" @sable_class "$5"
  tmux -L "$SOCK" set-option -p -t "$1" @sable_deliverable "$6"
}

# SABLE-v4e5: resolve_session() (bin/sable_pane_lib.py) now scopes list-panes
# to a derived-per-repo or calling-pane session (SABLE-e1e3) rather than -a
# across the whole socket, so the fixture's literal 'w' session must be named
# explicitly or every listing/reap call here silently sees zero panes.
# SABLE-1kbo: windowed sampling adds a real sleep between two internal
# listing reads (default 1.5s); shrink it here since none of this suite's
# cases are timing-sensitive -- they just need the fix to not regress them.
run_status() { SABLE_TMUX_SOCKET="$SOCK" SABLE_TMUX_SESSION="w" \
  SABLE_STATUS_SAMPLE_INTERVAL="0.1" python3 "$BIN" "$@"; }

# --- fixture: two worker panes, plus a SECOND session name grouped with the
#     first — the exact mechanism that duplicates list-panes -a rows ---
tmux -L "$SOCK" new-session -d -s w -x 200 -y 50 'bash --noprofile --norc'
sleep 0.3
tmux -L "$SOCK" split-window -t w 'bash --noprofile --norc'
sleep 0.3
tmux -L "$SOCK" new-session -d -t w -s w2   # grouped alias — duplicates every pane row
sleep 0.2

PANE1="$(tmux -L "$SOCK" list-panes -t w -F '#{pane_id}' | sed -n 1p)"
PANE2="$(tmux -L "$SOCK" list-panes -t w -F '#{pane_id}' | sed -n 2p)"
tag "$PANE1" worker bead-one done
tag "$PANE2" worker bead-two done

# sanity: the group DOES duplicate raw listing rows (pins the root cause)
raw_count="$(tmux -L "$SOCK" list-panes -a -F '#{pane_id}' | grep -xc "$PANE1")"
if [ "$raw_count" -ge 2 ]; then
  pass "fixture reproduces grouped-session row duplication"
else
  fail "fixture reproduces grouped-session row duplication" "got $raw_count row(s) for $PANE1"
fi

# --- duplicated-rows listing dedupes to unique pane ids ---
listing="$(run_status)"
count1="$(printf '%s\n' "$listing" | grep -c "bead-one")"
count2="$(printf '%s\n' "$listing" | grep -c "bead-two")"
if [ "$count1" -eq 1 ] && [ "$count2" -eq 1 ]; then
  pass "listing dedupes duplicated rows to one line per pane"
else
  fail "listing dedupes duplicated rows to one line per pane" "bead-one x$count1, bead-two x$count2"
fi

# --- reap with two (duplicate-listed) done panes kills both, exits 0, no traceback ---
out="$(run_status --reap 2>&1)"
rc=$?
if [ "$rc" -eq 0 ]; then
  pass "--reap exits 0 with two done panes under grouped-session duplication"
else
  fail "--reap exits 0 with two done panes under grouped-session duplication" "exit $rc: $out"
fi
if ! printf '%s' "$out" | grep -qi "traceback"; then
  pass "--reap prints no traceback"
else
  fail "--reap prints no traceback" "$out"
fi
sleep 0.3
remaining="$(tmux -L "$SOCK" list-panes -a -F '#{pane_id}' 2>/dev/null | sort -u | wc -l)"
if [ "$remaining" -eq 0 ]; then
  pass "--reap killed both done panes"
else
  fail "--reap killed both done panes" "$remaining pane(s) left"
fi

# --- market-brief-package-0h8k: a done pane holding unsubmitted composer
#     input (a misrouted/queued instruction) must be flagged, not silently
#     killed over — and no pane may survive reap still holding it ---
tmux -L "$SOCK" new-session -d -s w -x 200 -y 50 'bash --noprofile --norc'
sleep 0.3
tmux -L "$SOCK" split-window -t w 'bash --noprofile --norc'
sleep 0.3
PANE5="$(tmux -L "$SOCK" list-panes -t w -F '#{pane_id}' | sed -n 1p)"   # pending-input pane
PANE6="$(tmux -L "$SOCK" list-panes -t w -F '#{pane_id}' | sed -n 2p)"   # clean done pane
tag "$PANE5" worker bead-five done
tag "$PANE6" worker bead-six done
# emulate a claude composer's '> ' prompt glyph, then type an unsubmitted,
# queued line into it with NO Enter — mirrors a misrouted/queued instruction
# sitting in the composer un-submitted
tmux -L "$SOCK" send-keys -t "$PANE5" "PS1='> '" Enter
sleep 0.3
tmux -L "$SOCK" send-keys -t "$PANE5" -l "check the pool for next work"
sleep 0.2

out3="$(run_status --reap 2>&1)"
rc3=$?
if [ "$rc3" -eq 0 ]; then
  pass "--reap exits 0 when a done pane holds pending input"
else
  fail "--reap exits 0 when a done pane holds pending input" "exit $rc3: $out3"
fi
if printf '%s' "$out3" | grep -q "market-brief-package-0h8k"; then
  pass "--reap flags the pending-input pane instead of staying silent"
else
  fail "--reap flags the pending-input pane instead of staying silent" "$out3"
fi
# market-brief-package-b5ow: the flag message must carry the actual pending
# text, not just the pane id — the text IS the evidence, and it is destroyed
# by the C-u clear immediately after this line is written
if printf '%s' "$out3" | grep -q "check the pool for next work"; then
  pass "--reap flag message includes the literal pending-input text"
else
  fail "--reap flag message includes the literal pending-input text" "$out3"
fi
sleep 0.3
survivors5="$(tmux -L "$SOCK" list-panes -a -F '#{pane_id}' 2>/dev/null | grep -xc "$PANE5" || true)"
survivors6="$(tmux -L "$SOCK" list-panes -a -F '#{pane_id}' 2>/dev/null | grep -xc "$PANE6" || true)"
if [ "$survivors5" -eq 0 ] && [ "$survivors6" -eq 0 ]; then
  pass "no pane is left behind holding pending input after reap"
else
  fail "no pane is left behind holding pending input after reap" "PANE5 alive=$survivors5 PANE6 alive=$survivors6"
fi


# --- SABLE-tz7h.2: @sable_class filter — a done producer pane (e.g. victor)
#     with a valid deliverable is reaped; a manager pane's warm loop is never
#     touched, even if (erroneously) flagged done ---
DELIVERABLE="$(mktemp)"
echo '{"ok": true}' > "$DELIVERABLE"

tmux -L "$SOCK" new-session -d -s w -x 200 -y 50 'bash --noprofile --norc'
sleep 0.3
tmux -L "$SOCK" split-window -t w 'bash --noprofile --norc'
sleep 0.3
PANE7="$(tmux -L "$SOCK" list-panes -t w -F '#{pane_id}' | sed -n 1p)"   # producer, done, valid deliverable
PANE8="$(tmux -L "$SOCK" list-panes -t w -F '#{pane_id}' | sed -n 2p)"   # manager warm loop
tag_full "$PANE7" victor bead-victor done producer "$DELIVERABLE"
tag_full "$PANE8" optimus "" done manager ""

out4="$(run_status --reap 2>&1)"
rc4=$?
if [ "$rc4" -eq 0 ]; then
  pass "--reap exits 0 with a done producer + a manager pane present"
else
  fail "--reap exits 0 with a done producer + a manager pane present" "exit $rc4: $out4"
fi
sleep 0.3
p7_alive="$(tmux -L "$SOCK" list-panes -a -F '#{pane_id}' 2>/dev/null | grep -xc "$PANE7" || true)"
p8_alive="$(tmux -L "$SOCK" list-panes -a -F '#{pane_id}' 2>/dev/null | grep -xc "$PANE8" || true)"
if [ "$p7_alive" -eq 0 ]; then
  pass "--reap kills a done producer pane with a valid deliverable"
else
  fail "--reap kills a done producer pane with a valid deliverable" "producer pane still alive"
fi
if [ "$p8_alive" -eq 1 ]; then
  pass "--reap never touches a manager-class pane, even flagged done"
else
  fail "--reap never touches a manager-class pane, even flagged done" "manager pane alive=$p8_alive"
fi
rm -f "$DELIVERABLE"
# the manager pane survives by design -- tear its session down explicitly so
# the next fixture block gets a clean 'w' session, not a collision
tmux -L "$SOCK" kill-session -t w >/dev/null 2>&1 || true
sleep 0.2

# --- composer-safety regression (SABLE-1umr) must also fire for producer
#     panes: unsubmitted input is cleared + flagged before the kill, not
#     silently destroyed, and the pane is still reaped afterward ---
DELIVERABLE2="$(mktemp)"
echo '{"ok": true}' > "$DELIVERABLE2"
tmux -L "$SOCK" new-session -d -s w -x 200 -y 50 'bash --noprofile --norc'
sleep 0.3
PANE9="$(tmux -L "$SOCK" list-panes -t w -F '#{pane_id}' | sed -n 1p)"
tag_full "$PANE9" victor bead-victor2 done producer "$DELIVERABLE2"
tmux -L "$SOCK" send-keys -t "$PANE9" "PS1='> '" Enter
sleep 0.3
tmux -L "$SOCK" send-keys -t "$PANE9" -l "check the pool for next work"
sleep 0.2

out5="$(run_status --reap 2>&1)"
if printf '%s' "$out5" | grep -q "market-brief-package-0h8k"; then
  pass "--reap flags a producer pane's pending composer input instead of staying silent"
else
  fail "--reap flags a producer pane's pending composer input instead of staying silent" "$out5"
fi
sleep 0.3
p9_alive="$(tmux -L "$SOCK" list-panes -a -F '#{pane_id}' 2>/dev/null | grep -xc "$PANE9" || true)"
if [ "$p9_alive" -eq 0 ]; then
  pass "producer pane is still reaped after its pending input is cleared+flagged"
else
  fail "producer pane is still reaped after its pending input is cleared+flagged" "pane alive=$p9_alive"
fi
rm -f "$DELIVERABLE2"

# --- SABLE-exab: a producer pane spawned WITHOUT a bead tag (e.g. victor in
#     the tz7h.5 acceptance run, which passes no bead) renders an EMPTY
#     @sable_bead placeholder; the parser must not let that shift every
#     later column left and starve the kill decision ---
DELIVERABLE3="$(mktemp)"
echo '{"ok": true}' > "$DELIVERABLE3"
tmux -L "$SOCK" new-session -d -s w -x 200 -y 50 'bash --noprofile --norc'
sleep 0.3
PANE10="$(tmux -L "$SOCK" list-panes -t w -F '#{pane_id}' | sed -n 1p)"
tag_full "$PANE10" victor "" done producer "$DELIVERABLE3"

out6="$(run_status --reap 2>&1)"
rc6=$?
if [ "$rc6" -eq 0 ]; then
  pass "--reap exits 0 with a beadless done producer present"
else
  fail "--reap exits 0 with a beadless done producer present" "exit $rc6: $out6"
fi
sleep 0.3
p10_alive="$(tmux -L "$SOCK" list-panes -a -F '#{pane_id}' 2>/dev/null | grep -xc "$PANE10" || true)"
if [ "$p10_alive" -eq 0 ]; then
  pass "--reap kills a beadless done producer pane with a valid deliverable"
else
  fail "--reap kills a beadless done producer pane with a valid deliverable" "producer pane still alive"
fi
rm -f "$DELIVERABLE3"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then exit 1; fi
exit 0
