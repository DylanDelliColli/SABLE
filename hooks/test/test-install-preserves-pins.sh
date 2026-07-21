#!/usr/bin/env bash
# test-install-preserves-pins.sh — SABLE-mkj6k
#
# 'bash install.sh' silently reverted an operator-authorized de-hazard window:
# (1) the pre-dispatch-refresh bypass got re-armed via the settings snippet
#     template, and (2) the three deliberately pinned spine bins
#     (sable-merge-gate, sable-reconcile-handoffs, sable-dolt-push) got
#     silently re-symlinked, restoring the y6ik3 hot-swap hazard the pinning
#     was authorized to remove. Nothing warned either time.
#
# UNIT: the settings-snippet template no longer wires pre-dispatch-refresh
# (the durable de-wire), and sable-bin-install refuses to silently re-symlink
# a pinned (regular-file) bin, with a loud notice, unless --repin is passed.
#
# INTEGRATION: real fs, real install.sh, no mocks. HOME is redirected to a
# throwaway sandbox scope for every install.sh invocation below — this suite
# NEVER touches the real ~/.claude or ~/.local/bin (do not remove that
# redirection; running install.sh against the live system is exactly the
# defect this bead fixes — see SABLE-mkj6k / SABLE-o3xju).
#
# Run with:
#   bash hooks/test/test-install-preserves-pins.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL="$REPO/bin/sable-bin-install"
INSTALL_SH="$REPO/install.sh"
SNIPPET="$REPO/templates/multi-manager/settings-snippet.json"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

[ -x "$INSTALL" ] || { fail "sable-bin-install is executable"; echo "Tests: 1 | Passed: 0 | Failed: 1"; exit 1; }
pass "sable-bin-install is executable"

# ============================================================================
# UNIT
# ============================================================================

# ---- DEFECT 1: settings-snippet.json no longer WIRES pre-dispatch-refresh
# (the durable de-wire — o3xju's window only edited the INSTALLED artifact,
# this template is the actual source every install merges from). Matches the
# hook command path specifically, not just the bare string, so an explanatory
# comment recording WHY it was removed doesn't self-trip this assertion. ----
COUNT="$(grep -c "multi-manager/pre-dispatch-refresh\.sh" "$SNIPPET" 2>/dev/null || true)"
if [ "${COUNT:-0}" = "0" ]; then
  pass "settings-snippet.json no longer wires pre-dispatch-refresh.sh (durable de-wire)"
else
  fail "settings-snippet.json must not re-arm pre-dispatch-refresh" "found $COUNT occurrence(s) in $SNIPPET"
fi

# ---- DEFECT 2: a pinned regular-file bin must survive a plain install ----
PINNED_NAME="sable-merge-gate"
PINNED_CONTENT="#!/bin/sh
echo PINNED-FIXTURE-CONTENT
"

D1=$(mktemp -d)
printf '%s' "$PINNED_CONTENT" > "$D1/$PINNED_NAME"
chmod +x "$D1/$PINNED_NAME"
OUT="$(bash "$INSTALL" --dir "$D1" 2>&1)"

if [ -f "$D1/$PINNED_NAME" ] && [ ! -L "$D1/$PINNED_NAME" ]; then
  pass "pinned bin stays a regular file (not re-symlinked) after a plain install"
else
  fail "pinned bin must stay a regular file" "$(ls -l "$D1/$PINNED_NAME" 2>/dev/null)"
fi

if [ "$(cat "$D1/$PINNED_NAME" 2>/dev/null)" = "$(printf '%s' "$PINNED_CONTENT")" ]; then
  pass "pinned bin content is unchanged after a plain install"
else
  fail "pinned bin content must be unchanged" "got: $(cat "$D1/$PINNED_NAME" 2>/dev/null)"
fi

if printf '%s' "$OUT" | grep -qi "pinned"; then
  pass "a loud notice is emitted when a pinned bin is skipped"
else
  fail "loud notice on skipped pin" "output=[$OUT]"
fi

# unrelated tools still install normally alongside the pinned one
if [ -L "$D1/sable-launch" ] && [ "$(readlink "$D1/sable-launch")" = "$REPO/bin/sable-launch" ]; then
  pass "unrelated tools still install normally alongside a pinned bin"
else
  fail "unrelated tools still install" "$(ls -l "$D1/sable-launch" 2>/dev/null)"
fi

# pin marker records the pinned name (sable-doctor's policy source)
if [ -f "$D1/.sable-pinned" ] && grep -qx "$PINNED_NAME" "$D1/.sable-pinned"; then
  pass "pin marker (.sable-pinned) records the skipped tool name"
else
  fail "pin marker records skipped tool" "$(cat "$D1/.sable-pinned" 2>/dev/null)"
fi

# ---- --repin explicitly overrides the pin ----
D2=$(mktemp -d)
printf '%s' "$PINNED_CONTENT" > "$D2/$PINNED_NAME"
chmod +x "$D2/$PINNED_NAME"
bash "$INSTALL" --dir "$D2" --repin >/dev/null 2>&1 || true
if [ -L "$D2/$PINNED_NAME" ] && [ "$(readlink "$D2/$PINNED_NAME")" = "$REPO/bin/$PINNED_NAME" ]; then
  pass "--repin explicitly overrides the pin and re-symlinks"
else
  fail "--repin overrides pin" "$(ls -l "$D2/$PINNED_NAME" 2>/dev/null)"
fi
if [ ! -f "$D2/.sable-pinned" ] || ! grep -qx "$PINNED_NAME" "$D2/.sable-pinned" 2>/dev/null; then
  pass "--repin drops the name from the pin marker (self-healing)"
else
  fail "--repin must clear the pin marker entry" "$(cat "$D2/.sable-pinned" 2>/dev/null)"
fi

# ---- --copy mode is unaffected by pin protection (explicit copy intent) ----
D3=$(mktemp -d)
printf '%s' "$PINNED_CONTENT" > "$D3/$PINNED_NAME"
chmod +x "$D3/$PINNED_NAME"
bash "$INSTALL" --dir "$D3" --copy >/dev/null 2>&1 || true
if [ -f "$D3/$PINNED_NAME" ] && [ ! -L "$D3/$PINNED_NAME" ]; then
  pass "--copy mode still installs a regular file over an existing pinned bin"
else
  fail "--copy unaffected by pin guard" "$(ls -l "$D3/$PINNED_NAME" 2>/dev/null)"
fi

# ---- a bin with no prior state still installs as a symlink by default ----
D4=$(mktemp -d)
bash "$INSTALL" --dir "$D4" >/dev/null 2>&1 || true
if [ -L "$D4/$PINNED_NAME" ] && [ "$(readlink "$D4/$PINNED_NAME")" = "$REPO/bin/$PINNED_NAME" ]; then
  pass "a bin with no prior pin installs as a symlink on first install (default behavior preserved)"
else
  fail "first install still symlinks" "$(ls -l "$D4/$PINNED_NAME" 2>/dev/null)"
fi
if [ ! -e "$D4/.sable-pinned" ]; then
  pass "no pin marker is written when nothing is pinned"
else
  fail "pin marker must not appear with nothing pinned" "$(cat "$D4/.sable-pinned" 2>/dev/null)"
fi

rm -rf "$D1" "$D2" "$D3" "$D4"

# ============================================================================
# INTEGRATION: real fs, real install.sh, no mocks (SABLE-mkj6k acceptance
# criterion — assert SURVIVAL, not absence). Sandbox scope only.
# ============================================================================

if [ ! -f "$INSTALL_SH" ]; then
  fail "install.sh present for the integration acceptance run" "missing: $INSTALL_SH"
else

# Clean-room bd stub (mirrors hooks/test/test-sable-bin-install.sh's S2 block):
# install.sh Step 1/8 hard-requires bd on PATH even under normal (non-dry-run)
# operation. Shadowed harmlessly when real bd is already present.
I_STUB="$(mktemp -d)"
printf '#!/bin/sh\nexit 0\n' > "$I_STUB/bd"
chmod +x "$I_STUB/bd"

I_HOME="$(mktemp -d)"
mkdir -p "$I_HOME/.claude" "$I_HOME/.local/bin"

# Seed the scope exactly as the window's hand-refresh left it: settings.json
# with ZERO pre-dispatch-refresh entries, and the three spine bins pinned as
# regular files whose content deliberately differs from the repo source (so a
# post-install match would prove nothing was silently overwritten, not just
# that it happened to already match).
cat > "$I_HOME/.claude/settings.json" <<'JSON'
{
  "hooks": {
    "SessionStart": [
      {"matcher": "", "hooks": [{"type": "command", "command": "bd prime"}]}
    ]
  }
}
JSON

PINNED_BINS="sable-merge-gate sable-reconcile-handoffs sable-dolt-push"
for pb in $PINNED_BINS; do
  printf '#!/bin/sh\necho PINNED-FIXTURE-%s\n' "$pb" > "$I_HOME/.local/bin/$pb"
  chmod +x "$I_HOME/.local/bin/$pb"
done
PRE_MD5="$(cd "$I_HOME/.local/bin" && md5sum $PINNED_BINS 2>/dev/null | sort)"

# --from-here: this suite commonly runs from a linked SABLE worker worktree,
# which install.sh otherwise refuses to install from (SABLE-s6qk guard); the
# HOME being installed into is a throwaway tmp dir either way.
I_LOG="$I_HOME/install-stdout.log"
PATH="$I_STUB:$PATH" HOME="$I_HOME" bash "$INSTALL_SH" --from-here >"$I_LOG" 2>&1
I_RC=$?

if [ "$I_RC" -eq 0 ]; then
  pass "install.sh --from-here completes end-to-end against the sandbox scope"
else
  fail "install.sh completes" "rc=$I_RC log: $(cat "$I_LOG" 2>/dev/null)"
fi

# (i) settings.json still has zero pre-dispatch-refresh entries
POST_COUNT="$(grep -c "pre-dispatch-refresh" "$I_HOME/.claude/settings.json" 2>/dev/null || true)"
if [ "${POST_COUNT:-0}" = "0" ]; then
  pass "install.sh does not re-arm pre-dispatch-refresh in the scope's settings.json"
else
  fail "install.sh must not re-arm pre-dispatch-refresh" "found $POST_COUNT occurrence(s) after install"
fi

# (ii) all three spine bins are still regular files with unchanged content
POST_MD5="$(cd "$I_HOME/.local/bin" && md5sum $PINNED_BINS 2>/dev/null | sort)"
all_regular=1
for pb in $PINNED_BINS; do
  { [ -f "$I_HOME/.local/bin/$pb" ] && [ ! -L "$I_HOME/.local/bin/$pb" ]; } || all_regular=0
done
if [ "$all_regular" = "1" ] && [ "$POST_MD5" = "$PRE_MD5" ] && [ -n "$PRE_MD5" ]; then
  pass "install.sh --from-here leaves all three pinned spine bins as unchanged regular files (acceptance criterion)"
else
  fail "pinned spine bins must survive install.sh unchanged" "regular=$all_regular
pre:  $PRE_MD5
post: $POST_MD5"
fi

rm -rf "$I_STUB" "$I_HOME"

fi  # end: install.sh present

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
