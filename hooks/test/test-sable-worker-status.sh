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

run_status() { SABLE_TMUX_SOCKET="$SOCK" python3 "$BIN" "$@"; }

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
sleep 0.3
survivors5="$(tmux -L "$SOCK" list-panes -a -F '#{pane_id}' 2>/dev/null | grep -xc "$PANE5" || true)"
survivors6="$(tmux -L "$SOCK" list-panes -a -F '#{pane_id}' 2>/dev/null | grep -xc "$PANE6" || true)"
if [ "$survivors5" -eq 0 ] && [ "$survivors6" -eq 0 ]; then
  pass "no pane is left behind holding pending input after reap"
else
  fail "no pane is left behind holding pending input after reap" "PANE5 alive=$survivors5 PANE6 alive=$survivors6"
fi

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then exit 1; fi
exit 0
