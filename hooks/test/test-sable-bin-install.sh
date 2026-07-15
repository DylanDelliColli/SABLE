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
#
# CLEAN-ROOM NOTE (SABLE-59zu / SABLE-59t6.6): the S2 hybrid-contract block at
# the bottom drives the REAL install.sh, whose Step 1/8 hard-requires `bd` on
# PATH (exits 1 otherwise, even under --dry-run). The CI clean-room has no bd, so
# those blocks prepend a NO-OP bd stub to PATH for the install.sh runs — see the
# S2_STUB comment below. This whole suite must exit 0 with real bd ABSENT and
# only the stub present; reproduce that before pushing (README: run it with bd
# scrubbed from PATH).

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

# ============================================================================
# S2 hybrid-contract tests (SABLE-59t6.6) — bin-symlink parity + idempotency
# under install.sh --project vs global.
#
# These exercise the REAL install.sh entrypoint (NOT sable-bin-install directly):
# the hybrid contract (SABLE-59t6) is that a project-scoped install still links
# the SAME ~/.local/bin sable-* set as a global install, both targeting the
# canonical checkout's bin/, that the .claude layer alone moves into the project,
# and that the SABLE-s6qk linked-worktree guard still refuses. install.sh here
# lives in a linked worktree (and may run from the main checkout post-merge), so
# the parity/targeting runs pass --from-here to clear the guard; the guard test
# builds its OWN linked worktree so its refusal assertion holds no matter where
# the suite runs from. HOME is redirected to a temp dir per run so the global
# ~/.local/bin (and ~/.claude) are never touched.
# ============================================================================

INSTALL_SH="$REPO/install.sh"

# Clean-room bd stub (SABLE-59zu / SABLE-59t6.6). install.sh Step 1/8 hard-checks
# `command -v bd` and exits 1 when bd is absent — BEFORE any install step,
# including under --dry-run. The CI clean-room (ci-verify.yml) deliberately ships
# NO bd, so a bare install.sh run would exit 1 at Step 1 and false-FAIL every S2
# assertion below. Prepending this NO-OP bd to PATH for each install.sh run that
# reaches Step 1 lets install.sh complete, so the parity/targeting/hybrid/
# idempotency assertions actually RUN (and stay GATED) in the clean-room. The
# pure s6qk-REFUSAL run below is intentionally left WITHOUT the stub: its guard
# fires and exits before Step 1, so it needs no bd — keeping it a pure guard
# assertion. With real bd present, this stub is simply shadowed and never used.
S2_STUB="$(mktemp -d)"
printf '#!/bin/sh\nexit 0\n' > "$S2_STUB/bd"
chmod +x "$S2_STUB/bd"

# link_set DIR — print "name -> target" for every sable* symlink in DIR, sorted.
# Portable: no GNU find -printf; readlink prints the immediate target of a
# symlink identically on Linux and macOS.
link_set() {
  local d="$1" f
  for f in "$d"/sable "$d"/sable-*; do
    [ -L "$f" ] || continue
    printf '%s -> %s\n' "$(basename "$f")" "$(readlink "$f")"
  done | sort
}

if [ ! -f "$INSTALL_SH" ]; then
  fail "install.sh present for S2 hybrid-contract tests" "missing: $INSTALL_SH"
else

# ---- S2.1: project-scope and global installs create the SAME ~/.local/bin set ----
# Run install.sh --project=<gitproj> under HOME=A and global install.sh under
# HOME=B (separate HOMEs so the two ~/.local/bin dirs diff cleanly), then assert
# the sable-* symlink SET (basenames AND readlink targets) is byte-identical.
# This is the real contract — it catches install.sh --project skipping or
# mis-invoking Step 3 (the false-green first pass only ran sable-bin-install
# twice and compared run-to-run; it never went through install.sh at all).
S2_HA="$(mktemp -d)"; S2_HB="$(mktemp -d)"; S2_PROJ="$(mktemp -d)"
git -C "$S2_PROJ" init -q >/dev/null 2>&1
PATH="$S2_STUB:$PATH" HOME="$S2_HA" bash "$INSTALL_SH" --from-here --project="$S2_PROJ" >/dev/null 2>&1
PATH="$S2_STUB:$PATH" HOME="$S2_HB" bash "$INSTALL_SH" --from-here                      >/dev/null 2>&1
PROJ_SET="$(link_set "$S2_HA/.local/bin")"
GLOB_SET="$(link_set "$S2_HB/.local/bin")"
if [ -n "$PROJ_SET" ] && [ "$PROJ_SET" = "$GLOB_SET" ]; then
  pass "install.sh --project links the SAME ~/.local/bin sable-* set as global (names+targets)"
else
  fail "project vs global ~/.local/bin symlink parity" "diff (global<->project):
$(diff <(printf '%s\n' "$GLOB_SET") <(printf '%s\n' "$PROJ_SET"))"
fi
# ...and every target points into the canonical checkout's bin/ (this REPO).
off_target=0; counted=0
for f in "$S2_HA/.local/bin"/sable "$S2_HA/.local/bin"/sable-*; do
  [ -L "$f" ] || continue
  counted=$((counted+1))
  case "$(readlink "$f")" in "$REPO_BIN"/*) ;; *) off_target=1 ;; esac
done
if [ "$counted" -gt 0 ] && [ "$off_target" = "0" ]; then
  pass "project-scope ~/.local/bin symlinks all target the canonical checkout bin ($REPO_BIN)"
else
  fail "project-scope symlinks target canonical bin" "counted=$counted off_target=$off_target"
fi
# Hybrid contract: --project moves the .claude layer INTO the project, but the
# CLI links stay GLOBAL under HOME — so HOME=A/.claude must NOT be written.
if [ -d "$S2_PROJ/.claude" ] && [ ! -d "$S2_HA/.claude" ]; then
  pass "--project writes the .claude layer to the project, not HOME (hybrid contract)"
else
  fail "hybrid contract: project .claude, not HOME .claude" "proj/.claude=$([ -d "$S2_PROJ/.claude" ] && echo yes || echo no) home/.claude=$([ -d "$S2_HA/.claude" ] && echo yes || echo no)"
fi
rm -rf "$S2_HA" "$S2_HB" "$S2_PROJ"

# ---- S2.2: installed symlinks resolve to the canonical checkout; the
# SABLE-s6qk linked-worktree guard refuses (unless --from-here) ----
# (a) SABLE-ofl targeting: the installed sable-note, invoked through its
#     ~/.local/bin symlink, resolves its feedback dir back to the REPO — proof
#     the link points at the canonical checkout, not a stray copy.
S2_HC="$(mktemp -d)"
PATH="$S2_STUB:$PATH" HOME="$S2_HC" bash "$INSTALL_SH" --from-here >/dev/null 2>&1
GOT_TARGET="$(env -u SABLE_FEEDBACK_DIR "$S2_HC/.local/bin/sable-note" --dir 2>/dev/null || true)"
if [ "$GOT_TARGET" = "$REPO/feedback" ]; then
  pass "install.sh-installed sable-note resolves to the canonical checkout feedback dir (SABLE-ofl)"
else
  fail "installed symlink resolves to canonical checkout" "expected [$REPO/feedback] got [$GOT_TARGET]"
fi
rm -rf "$S2_HC"
# (b) guard: build a REAL linked worktree (git worktree add) and assert
#     install.sh run FROM it refuses by default and proceeds only with
#     --from-here. Self-contained so this holds whether the suite runs from a
#     linked worktree or the main checkout (post-merge). dry-run: no writes.
#     The refusal run carries NO bd stub on PATH — the s6qk guard exits before
#     Step 1, so its refusal is proven independent of bd (stays green in the
#     clean-room unconditionally). The --from-here override run clears the guard
#     and DOES reach Step 1, so it gets the stub.
S2_WTP="$(mktemp -d)"; S2_WT="$S2_WTP/linked"; S2_WT_HOME="$(mktemp -d)"
if git -C "$REPO" worktree add --detach "$S2_WT" HEAD >/dev/null 2>&1; then
  G_OUT="$(HOME="$S2_WT_HOME" bash "$S2_WT/install.sh" --dry-run 2>&1)"; G_RC=$?
  if [ "$G_RC" -ne 0 ] && printf '%s' "$G_OUT" | grep -q "refusing to run from a linked git worktree"; then
    pass "install.sh refuses to run from a linked worktree (SABLE-s6qk guard, non-zero + named message)"
  else
    fail "guard refuses from linked worktree" "rc=$G_RC out=[$G_OUT]"
  fi
  F_OUT="$(PATH="$S2_STUB:$PATH" HOME="$S2_WT_HOME" bash "$S2_WT/install.sh" --from-here --dry-run 2>&1)"; F_RC=$?
  if [ "$F_RC" -eq 0 ] && ! printf '%s' "$F_OUT" | grep -q "refusing to run from a linked git worktree"; then
    pass "--from-here overrides the linked-worktree guard"
  else
    fail "--from-here overrides linked-worktree guard" "rc=$F_RC out=[$F_OUT]"
  fi
  git -C "$REPO" worktree remove --force "$S2_WT" >/dev/null 2>&1 || true
  git -C "$REPO" worktree prune >/dev/null 2>&1 || true
else
  fail "git worktree add for guard test" "could not create a linked worktree at $S2_WT"
fi
rm -rf "$S2_WTP" "$S2_WT_HOME"

# ---- S2.3: install.sh --project Step 3 is idempotent ----
# A second identical --project run must change NO ~/.local/bin sable-* link:
# same names+targets, same count (no duplicates), and none dangling.
S2_IH="$(mktemp -d)"; S2_IPROJ="$(mktemp -d)"
git -C "$S2_IPROJ" init -q >/dev/null 2>&1
PATH="$S2_STUB:$PATH" HOME="$S2_IH" bash "$INSTALL_SH" --from-here --project="$S2_IPROJ" >/dev/null 2>&1
BEFORE="$(link_set "$S2_IH/.local/bin")"
BEFORE_N="$(printf '%s\n' "$BEFORE" | grep -c . || true)"
PATH="$S2_STUB:$PATH" HOME="$S2_IH" bash "$INSTALL_SH" --from-here --project="$S2_IPROJ" >/dev/null 2>&1
AFTER="$(link_set "$S2_IH/.local/bin")"
AFTER_N="$(printf '%s\n' "$AFTER" | grep -c . || true)"
dangling=0
for f in "$S2_IH/.local/bin"/sable "$S2_IH/.local/bin"/sable-*; do
  [ -L "$f" ] || continue
  [ -e "$f" ] || dangling=$((dangling+1))
done
if [ -n "$BEFORE" ] && [ "$BEFORE" = "$AFTER" ] && [ "$BEFORE_N" = "$AFTER_N" ] && [ "$dangling" = "0" ]; then
  pass "install.sh --project Step 3 idempotent — re-run leaves the link set unchanged, none dangling"
else
  fail "project-install symlink idempotency" "dangling=$dangling count($BEFORE_N->$AFTER_N) diff:
$(diff <(printf '%s\n' "$BEFORE") <(printf '%s\n' "$AFTER"))"
fi
rm -rf "$S2_IH" "$S2_IPROJ"

fi  # end: install.sh present

# Cleanup
rm -rf "$D1" "$D_ONPATH" "$D_COPY" "$D_DRY" "$D_INT" "$D_INT_COPY" "$S2_STUB"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
