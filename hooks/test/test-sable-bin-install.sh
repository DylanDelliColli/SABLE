#!/usr/bin/env bash
# test-sable-bin-install.sh — SABLE-cmql
# Unit + integration tests for bin/sable-bin-install: symlinks the repo's
# sable-* CLI tools onto PATH (default ~/.local/bin), idempotent, --copy /
# --uninstall / --dry-run variants, PATH warning, and the integration property
# that a symlinked sable-note resolves back to the REPO feedback dir (the
# SABLE-ofl fix that makes symlink-not-copy load-bearing).
#
# Run with:
#   bash hooks/test/test-sable-bin-install.sh

set -uo pipefail
unset SABLE_FEEDBACK_DIR 2>/dev/null || true

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL="$REPO/bin/sable-bin-install"
REPO_BIN="$REPO/bin"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

[ -x "$INSTALL" ] || { fail "sable-bin-install is executable"; echo "Tests: 1 | Passed: 0 | Failed: 1"; exit 1; }
pass "sable-bin-install is executable"

CORE_TOOLS="sable sable-launch sable-note sable-mode sable-tmux sable-msg sable-view"

# ---- UNIT: symlink install into a dir NOT on PATH ----
D1=$(mktemp -d)
ERR1=$(bash "$INSTALL" --dir "$D1" 2>&1 1>/dev/null) || true

ok=1
for t in $CORE_TOOLS; do
  [ -L "$D1/$t" ] || { ok=0; break; }
  [ "$(readlink "$D1/$t")" = "$REPO_BIN/$t" ] || { ok=0; break; }
done
if [ "$ok" -eq 1 ]; then
  pass "symlinks core tools -> repo bin/ ($CORE_TOOLS)"
else
  fail "symlinks core tools -> repo bin/" "links in $D1: $(ls -l "$D1" 2>/dev/null)"
fi

# *.py prefilters and test files must NOT be linked
if [ ! -e "$D1/columbo-prefilter.py" ] && [ ! -e "$D1/test_columbo_prefilter.py" ] && [ ! -e "$D1/tripwire-watcher.py" ]; then
  pass "does not link *.py prefilters or test files"
else
  fail "excludes *.py and tests" "unexpected: $(ls "$D1" | grep -E '\.py$' || true)"
fi

# Warns (with the export hint) when target dir is not on PATH
if printf '%s' "$ERR1" | grep -qi "PATH"; then
  pass "warns when target dir is not on PATH"
else
  fail "PATH warning when not on PATH" "stderr=[$ERR1]"
fi

# Idempotent: re-run must succeed and keep correct links
if bash "$INSTALL" --dir "$D1" >/dev/null 2>&1 && [ "$(readlink "$D1/sable-launch")" = "$REPO_BIN/sable-launch" ]; then
  pass "idempotent on re-run"
else
  fail "idempotent on re-run"
fi

# ---- UNIT: target dir already on PATH -> no PATH warning ----
D_ONPATH=$(mktemp -d)
ERR_ON=$(PATH="$D_ONPATH:$PATH" bash "$INSTALL" --dir "$D_ONPATH" 2>&1 1>/dev/null) || true
if ! printf '%s' "$ERR_ON" | grep -qi "not on .*PATH\|add .* to .*PATH"; then
  pass "no PATH warning when target dir is already on PATH"
else
  fail "no warning when on PATH" "stderr=[$ERR_ON]"
fi

# ---- UNIT: --copy yields regular files, not symlinks ----
D_COPY=$(mktemp -d)
bash "$INSTALL" --dir "$D_COPY" --copy >/dev/null 2>&1 || true
if [ -f "$D_COPY/sable-note" ] && [ ! -L "$D_COPY/sable-note" ] && [ -x "$D_COPY/sable-note" ]; then
  pass "--copy installs regular executable files (not symlinks)"
else
  fail "--copy mode" "$(ls -l "$D_COPY/sable-note" 2>/dev/null)"
fi

# ---- UNIT: --uninstall removes the installed tools ----
bash "$INSTALL" --dir "$D1" --uninstall >/dev/null 2>&1 || true
if [ ! -e "$D1/sable-launch" ] && [ ! -e "$D1/sable-note" ]; then
  pass "--uninstall removes the installed tools"
else
  fail "--uninstall" "still present: $(ls "$D1" 2>/dev/null)"
fi

# ---- UNIT: --dry-run writes nothing ----
D_DRY=$(mktemp -d)
bash "$INSTALL" --dir "$D_DRY" --dry-run >/dev/null 2>&1 || true
if [ -z "$(ls -A "$D_DRY" 2>/dev/null)" ]; then
  pass "--dry-run writes nothing"
else
  fail "--dry-run is non-writing" "created: $(ls -A "$D_DRY")"
fi

# ---- INTEGRATION: symlinked sable-note resolves to the REPO feedback dir ----
# (proves symlink-not-copy is correct and fixes the SABLE-ofl fragmentation)
D_INT=$(mktemp -d)
bash "$INSTALL" --dir "$D_INT" >/dev/null 2>&1 || true
GOT_DIR=$(env -u SABLE_FEEDBACK_DIR "$D_INT/sable-note" --dir 2>/dev/null || true)
if [ "$GOT_DIR" = "$REPO/feedback" ]; then
  pass "symlinked sable-note --dir resolves to the repo feedback dir (SABLE-ofl fix)"
else
  fail "symlinked sable-note resolves to repo feedback" "expected [$REPO/feedback] got [$GOT_DIR]"
fi

# Contrast: a COPIED sable-note resolves to the copy's own ../feedback, NOT the repo
# (this is exactly the SABLE-ofl bug — documents why we default to symlink).
D_INT_COPY=$(mktemp -d)
bash "$INSTALL" --dir "$D_INT_COPY" --copy >/dev/null 2>&1 || true
GOT_COPY=$(env -u SABLE_FEEDBACK_DIR "$D_INT_COPY/sable-note" --dir 2>/dev/null || true)
if [ "$GOT_COPY" != "$REPO/feedback" ]; then
  pass "copied sable-note does NOT resolve to repo feedback (confirms symlink is the right default)"
else
  fail "copy diverges from repo feedback" "copy unexpectedly resolved to repo: [$GOT_COPY]"
fi

# Cleanup
rm -rf "$D1" "$D_ONPATH" "$D_COPY" "$D_DRY" "$D_INT" "$D_INT_COPY"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
