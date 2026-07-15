#!/usr/bin/env bash
# test-quickstart-project.sh — SABLE-59t6.7 (S5). QUICKSTART.md must document the
# project-scoped install (install.sh --project, the hybrid ~/.local/bin PATH-tools
# note, the teammate-clone bootstrap, and the v1 fleet boundary) AND the documented
# bootstrap must EXECUTE green end-to-end from a fresh clone fixture — the framing
# S5 acceptance ("the documented steps must be EXECUTED by a test, not just written").
#
# Two layers in one file. The E2E execution harness matches test-install.sh (runs
# install.sh against scratch HOMEs with real bd/git/python on PATH), NOT the pure
# doc-grep harness of test-quickstart-orchestration.sh — hence a new file rather
# than extending that suite (bead: name the choice on close).
#   1. doc-content: the project section exists and names the exact shipped flags.
#   2. one E2E (docs bead carries one E2E only, per test-strategy): parse the
#      section's teammate-bootstrap fence, run it against a fresh tmp project +
#      cloned copy, assert each command exits 0 and the final `sable-doctor
#      --project` reports clean (doctor green).
#
# Run with: bash hooks/test/test-quickstart-project.sh
set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
DOC="$REPO/QUICKSTART.md"
INSTALL="$REPO/install.sh"

PASS=0; FAIL=0; NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); NAMES="$NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
has(){ if grep -qiF -- "$2" "$DOC" 2>/dev/null; then pass "$1"; else fail "$1" "missing: $2"; fi; }

[ -f "$DOC" ] || { echo "FAIL: $DOC missing"; exit 2; }

# ---------- doc-content: project section exists + exact shipped flag names ----------
has "documents install.sh --project"                 "install.sh --project"
has "documents the --force double-fire escape"       "--force"
has "documents sable-doctor --project verify"        "sable-doctor --project"
has "documents bd init in the teammate bootstrap"    "bd init"
has "hybrid PATH-tools note names ~/.local/bin"      "~/.local/bin"
has "names the v1 fleet boundary refusal verbatim"   "fleet requires the global install in v1"
has "names the fleet env-var escape hatch"           "SABLE_AGENTS_YAML"

# ---------- E2E: the documented teammate-bootstrap executes green end-to-end ----------
# Extractor: the fenced block immediately after the machine anchor comment. Keyed
# on the anchor so the owner-install fence (earlier in the section) is never grabbed.
extract_block() {
  awk -v anchor="$1" '
    index($0, anchor) { f=1; next }
    f && /^```/       { if (inb) exit; inb=1; next }
    f && inb          { print }
  ' "$DOC"
}

BOOTSTRAP="$(extract_block "sable-e2e: teammate-bootstrap")"
if [ -z "$BOOTSTRAP" ]; then
  fail "E2E: teammate-bootstrap fence present" "anchor '<!-- sable-e2e: teammate-bootstrap -->' + its fenced block not found in $DOC"
else
  pass "E2E: teammate-bootstrap fence present"

  # Isolate HOME + a scratch ~/.local/bin so the real ~/.claude and PATH tools are
  # never touched. Neutralize any ambient SABLE/CLAUDE env that would skew resolution.
  ISOHOME="$(mktemp -d)"; mkdir -p "$ISOHOME/.claude"
  PROJ="$(mktemp -d)"; git init -q "$PROJ"
  ( cd "$PROJ" \
      && git config user.email sable@test && git config user.name sable \
      && printf '# Fixture project\n' > README.md \
      && git add -A && git commit -qm init ) >/dev/null 2>&1

  # OWNER side: run the documented project install as fixture setup. --from-here
  # bypasses the SABLE-s6qk linked-worktree guard (this suite runs from a fleet
  # worktree); it is a harness concern, not part of the documented command form.
  env -u SABLE_AGENTS_YAML -u SABLE_DISPATCH_DIR -u CLAUDE_USER_DIR -u CLAUDE_PROJECT_DIR \
    HOME="$ISOHOME" bash "$INSTALL" --project="$PROJ" --from-here >"$ISOHOME/install.log" 2>&1
  rc=$?
  [ "$rc" = "0" ] && pass "E2E: install.sh --project into fresh repo (rc=0)" \
    || fail "E2E: install.sh --project into fresh repo" "rc=$rc (see $ISOHOME/install.log)"
  ( cd "$PROJ" && git add -A && git commit -qm "SABLE project install" ) >/dev/null 2>&1

  # TEAMMATE side: clone, then run the documented bootstrap verbatim from the clone CWD.
  CLONE_PARENT="$(mktemp -d)"; CLONE="$CLONE_PARENT/clone"
  git clone -q "$PROJ" "$CLONE"

  export PATH="$ISOHOME/.local/bin:$PATH"
  last_out=""; n=0
  while IFS= read -r line; do
    case "$line" in ""|"#"*) continue ;; esac   # skip blank / full-line-comment lines
    n=$((n+1))
    last_out="$(cd "$CLONE" && env -u SABLE_AGENTS_YAML -u SABLE_DISPATCH_DIR -u CLAUDE_USER_DIR -u CLAUDE_PROJECT_DIR HOME="$ISOHOME" bash -c "$line" 2>&1)"
    lrc=$?
    if [ "$lrc" = "0" ]; then
      pass "E2E: bootstrap cmd exits 0 -> $line"
    else
      fail "E2E: bootstrap cmd exits 0 -> $line" "rc=$lrc; out: $last_out"
    fi
  done < <(printf '%s\n' "$BOOTSTRAP")

  [ "$n" -ge 3 ] && pass "E2E: bootstrap ran the 3 documented commands (n=$n)" \
    || fail "E2E: bootstrap command count" "n=$n (expected >=3: verify PATH, bd init, sable-doctor --project)"
  printf '%s' "$last_out" | grep -qi 'clean' && pass "E2E: ends with sable-doctor --project green (clean)" \
    || fail "E2E: ends with doctor green" "last command output: $last_out"

  rm -rf "$ISOHOME" "$PROJ" "$CLONE_PARENT"
fi

echo
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
if [ "$FAIL" -gt 0 ]; then echo -e "Failed:$NAMES"; exit 1; fi
exit 0
