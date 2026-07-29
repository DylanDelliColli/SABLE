#!/usr/bin/env bash
# test-install.sh — front-door integration for install.sh (SABLE-ppy / iw0; tmux-only SABLE-qa4d).
# Verifies install.sh delegates the Orchestration tier to sable-orchestration-install
# (no topology fork — the tmux warm-pane layer is the only one), applies the
# settings snippet only with explicit consent, rejects the retired topology
# flags, skips the layer for Foundation, and leaves runnable installed hook copies.
# Runs install.sh against scratch HOMEs (real bd/dolt/python on PATH).
set -uo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALL="$REPO/install.sh"
PASS=0; FAIL=0; NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); NAMES="$NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
present(){ [ -e "$1" ] && pass "$2" || fail "$2" "missing: $1"; }
absent(){ [ ! -e "$1" ] && pass "$2" || fail "$2" "present: $1"; }
count_marker(){ python3 -c "
import json,sys
d=json.load(open(sys.argv[1])); m=sys.argv[2]
print(sum(1 for bl in d.get('hooks',{}).values() if isinstance(bl,list) for b in bl if isinstance(b,dict) for h in b.get('hooks',[]) if m in (h.get('command','') or '')))" "$1" "$2" 2>/dev/null || echo ERR; }

# ---------- delegation: the plain install lands the full layer (no tiers) ----------
# --from-here: this suite runs $INSTALL from whatever checkout it lives in,
# which is routinely a linked worktree in this fleet — bypass the SABLE-s6qk
# canonical-checkout guard here since these tests exercise install.sh's
# delegation wiring, not the guard itself (see test-install-guard.sh for that).
TS="$(mktemp -d)"
HOME="$TS" bash "$INSTALL" --from-here --merge-settings >/tmp/ti-orch.log 2>&1
SS="$TS/.claude/settings.json"
CS="$TS/.codex/hooks.json"
present "$TS/.claude/hooks/multi-manager/mode-interlock.sh" "orchestration: delegate installed multi-manager hooks"
present "$TS/.claude/sable/agents.yaml"                     "orchestration: registry installed"
present "$TS/.claude/skills/sable-plan/SKILL.md"            "orchestration: /sable-plan skill installed"
present "$TS/.claude/sable/roles/optimus.md"                "orchestration: pane roles installed"
absent  "$TS/.claude/agents-teams"                          "orchestration: no agents-teams defs (tmux-only)"
[ "$(count_marker "$SS" mode-interlock.sh)" = "2" ] && pass "orchestration: interlock merged on both legs" || fail "orchestration: interlock merged on both legs" "count=$(count_marker "$SS" mode-interlock.sh)"
[ "$(count_marker "$SS" pre-push-rebase-test)" -ge 1 ] && pass "orchestration: governance hooks present in settings" || fail "orchestration: governance hooks present" "count=$(count_marker "$SS" pre-push-rebase-test)"
present "$CS" "Codex: hooks.json created"
for _base_hook in tdd-evidence.sh tdd-gate.sh bead-description-gate.sh tdd-remind.sh agent-tdd-enforce.sh bead-quality.sh; do
  [ "$(count_marker "$CS" "$_base_hook")" = "1" ] &&
    pass "Codex: canonical base hook merged exactly once ($_base_hook)" ||
    fail "Codex: canonical base hook merged exactly once ($_base_hook)" "count=$(count_marker "$CS" "$_base_hook")"
done
grep -q 'sable-tmux' /tmp/ti-orch.log && pass "orchestration: output points at the sable-tmux bring-up" || fail "orchestration: output points at the sable-tmux bring-up"
! grep -q 'CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS' /tmp/ti-orch.log && pass "orchestration: no experimental-teams-flag instruction printed" || fail "orchestration: no experimental-teams-flag instruction printed"

# installed hook copy is runnable (no-op on benign input → exit 0)
printf '%s' '{"tool_name":"Bash","tool_input":{"command":"echo hi"}}' | bash "$TS/.claude/hooks/multi-manager/mode-interlock.sh" >/dev/null 2>&1
[ "$?" = "0" ] && pass "orchestration: installed interlock copy runs" || fail "orchestration: installed interlock copy runs"

# the plain install also lands the base agent defs (producers)
present "$TS/.claude/agents/sherlock.md" "plain install lands base agent defs"

# idempotent re-run: interlock count stable
HOME="$TS" bash "$INSTALL" --from-here --merge-settings >/dev/null 2>&1
[ "$(count_marker "$SS" mode-interlock.sh)" = "2" ] && pass "re-run idempotent (interlock still 2)" || fail "re-run idempotent" "count=$(count_marker "$SS" mode-interlock.sh)"
[ "$(count_marker "$CS" tdd-gate.sh)" = "1" ] && pass "re-run idempotent (Codex base gate still 1)" || fail "re-run idempotent (Codex base gate)" "count=$(count_marker "$CS" tdd-gate.sh)"

# non-clobber: a pre-existing user hook survives the merge
TN="$(mktemp -d)"; mkdir -p "$TN/.claude" "$TN/.codex"
printf '%s\n' '{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"bash /tmp/user-own.sh"}]}]}}' > "$TN/.claude/settings.json"
printf '%s\n' '{"hooks":{"PreToolUse":[{"matcher":"CustomTool","hooks":[{"type":"command","command":"bash /tmp/codex-user-own.sh"}]}]}}' > "$TN/.codex/hooks.json"
HOME="$TN" bash "$INSTALL" --from-here --merge-settings >/dev/null 2>&1
grep -q 'user-own.sh' "$TN/.claude/settings.json" && pass "non-clobber: pre-existing user hook survives" || fail "non-clobber: pre-existing user hook survives"
grep -q 'codex-user-own.sh' "$TN/.codex/hooks.json" && pass "non-clobber: pre-existing Codex hook survives" || fail "non-clobber: pre-existing Codex hook survives"

# default is genuinely print-only, including when the proposal has changes.
TP="$(mktemp -d)"; mkdir -p "$TP/.claude" "$TP/.codex"
printf '%s\n' '{"permissions":{"allow":["Read"]}}' > "$TP/.claude/settings.json"
printf '%s\n' '{"hooks":{"CustomEvent":[]}}' > "$TP/.codex/hooks.json"
cp "$TP/.claude/settings.json" "$TP/settings.before"
cp "$TP/.codex/hooks.json" "$TP/codex-hooks.before"
HOME="$TP" bash "$INSTALL" --from-here >/tmp/ti-print-only.log 2>&1
cmp -s "$TP/settings.before" "$TP/.claude/settings.json" &&
  pass "unflagged install leaves settings.json byte-identical" ||
  fail "unflagged install leaves settings.json byte-identical"
cmp -s "$TP/codex-hooks.before" "$TP/.codex/hooks.json" &&
  pass "unflagged install leaves Codex hooks.json byte-identical" ||
  fail "unflagged install leaves Codex hooks.json byte-identical"
grep -q 'tdd-gate.sh' /tmp/ti-print-only.log &&
  pass "unflagged install proposal names the Codex base gate" ||
  fail "unflagged install proposal names the Codex base gate"
grep -q 'NOT APPLIED' /tmp/ti-print-only.log &&
  pass "unflagged install names the unapplied settings proposal" ||
  fail "unflagged install names the unapplied settings proposal"
grep -q 'whole install is print-only for settings unless --merge-settings was' "$INSTALL" &&
  pass "source contract and unflagged no-write behavior agree" ||
  fail "source contract and unflagged no-write behavior agree"
present "$TP/.claude/hooks/multi-manager/mode-interlock.sh" \
  "print-only settings still stages the orchestration artifacts"

# ---------- retired flags are rejected (one topology, one tier) ----------
for _flag in --teams --subagent --nested --orchestration --foundation; do
  TR="$(mktemp -d)"
  if HOME="$TR" bash "$INSTALL" "$_flag" >/dev/null 2>&1; then
    fail "retired flag $_flag is rejected" "install.sh exited 0"
  else
    pass "retired flag $_flag is rejected"
  fi
  absent "$TR/.claude/hooks/multi-manager" "retired flag $_flag installs nothing"
  rm -rf "$TR"
done

# ===================================================================
# --project scope (SABLE-59t6.3): parameterize-once destinations, HOME
# untouched, prime block project-side, double-fire refusal + --force.
# ===================================================================

# claude_manifest DIR HOMEDIR REPODIR — content-hash manifest of every file
# under DIR. HOMEDIR is normalized throughout; REPODIR is normalized only in
# the two generated timer artifacts that intentionally embed it. This keeps
# hashes deterministic across scratch HOMEs and checkout/worktree paths without
# hiding an accidental source-path leak elsewhere. Excludes .bak / *install-bak*.
# .sable-install-provenance (SABLE-78kxu) additionally gets its commit=/branch=/
# timestamp= fields normalized: those are stamped from the CURRENT repo HEAD and
# wall-clock time, so they vary every run/commit/worktree and would make the
# golden baseline re-drift immediately after any regen (SABLE-z31s1).
# MUST stay byte-identical to fixtures/regen: golden was captured with this exact fn.
claude_manifest() {
  local dir="$1" home_dir="$2" repo_dir="$3"
  [ -d "$dir" ] || { printf '(no dir: %s)\n' "$dir"; return 0; }
  ( cd "$dir" && find . -type f ! -name '*.bak' ! -path '*install-bak*' | LC_ALL=C sort | while IFS= read -r f; do
      if [ "$f" = "./.sable-install-provenance" ]; then
        h="$(sed -e "s@${home_dir}@__HOME__@g" \
                  -e 's/^commit=.*/commit=__COMMIT__/' \
                  -e 's/^branch=.*/branch=__BRANCH__/' \
                  -e 's/^timestamp=.*/timestamp=__TIMESTAMP__/' "$f" | sha256sum | cut -d' ' -f1)"
      elif [ "$f" = "./sable/reconcile-timer/sable-reconcile-timer.service" ] ||
           [ "$f" = "./sable/reconcile-timer/sable-reconcile-timer.cron" ]; then
        # These two generated artifacts intentionally bind to the source
        # checkout. Normalize only this known volatile field: a repo path
        # appearing in any other installed file remains visible as drift.
        h="$(sed -e "s@${repo_dir}@__REPO__@g" \
                  -e "s@${home_dir}@__HOME__@g" "$f" | sha256sum | cut -d' ' -f1)"
      else
        h="$(sed -e "s@${home_dir}@__HOME__@g" "$f" | sha256sum | cut -d' ' -f1)"
      fi
      printf '%s  %s\n' "$h" "$f"
    done )
}
mkrepo(){ git init "$1" >/dev/null 2>&1; }  # throwaway git repo (git-common-dir resolvable)

# A golden regenerated in a clone/worktree must be valid in the canonical
# checkout too. The raw artifacts deliberately differ (positive control);
# only the normalized manifests may agree (SABLE-clla4).
TM1="$(mktemp -d)"; TM2="$(mktemp -d)"
TM1_TIMER="$TM1/install/sable/reconcile-timer/sable-reconcile-timer.service"
TM2_TIMER="$TM2/install/sable/reconcile-timer/sable-reconcile-timer.service"
mkdir -p "$(dirname "$TM1_TIMER")" "$(dirname "$TM2_TIMER")"
printf 'ExecStart=%s/bin/sable-reconcile-handoffs --once\n' "$REPO" > "$TM1_TIMER"
printf 'ExecStart=%s/bin/sable-reconcile-handoffs --once\n' "$TM2/source-repo" > "$TM2_TIMER"
! cmp -s "$TM1_TIMER" "$TM2_TIMER" &&
  pass "golden normalization control: source-path-bearing artifacts genuinely differ" ||
  fail "golden normalization control: source-path-bearing artifacts genuinely differ"
TM1_MANIFEST="$(claude_manifest "$TM1/install" "$TM1" "$REPO")"
TM2_MANIFEST="$(claude_manifest "$TM2/install" "$TM2" "$TM2/source-repo")"
[ "$TM1_MANIFEST" = "$TM2_MANIFEST" ] &&
  pass "golden manifest normalizes the source checkout path" ||
  fail "golden manifest normalizes the source checkout path"
printf 'Environment=PLANTED_CONTENT_CHANGE=1\n' >> "$TM2_TIMER"
[ "$TM1_MANIFEST" != "$(claude_manifest "$TM2/install" "$TM2" "$TM2/source-repo")" ] &&
  pass "golden manifest still detects a real installed-content change" ||
  fail "golden manifest still detects a real installed-content change"
rm -rf "$TM1" "$TM2"

# ---------- CRITICAL: --project leaves ~/.claude byte-identical ----------
# Snapshot HOME/.claude ONLY (the ~/.local/bin CLI symlinks are by-design global,
# hybrid contract). Pre-seed a benign global state (no SABLE hooks → guard passes).
TH1="$(mktemp -d)"; mkdir -p "$TH1/.claude/agents"
printf '%s\n' '{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"bash /tmp/my-own.sh"}]}]}}' > "$TH1/.claude/settings.json"
printf 'pre-existing user notes\n' > "$TH1/.claude/CLAUDE.md"
printf 'keep me\n' > "$TH1/.claude/agents/keep.md"
PROJ1="$(mktemp -d)"; mkrepo "$PROJ1"
SNAP1="$(claude_manifest "$TH1/.claude" "$TH1" "$REPO")"
HOME="$TH1" bash "$INSTALL" --project="$PROJ1" --from-here --merge-settings >/tmp/ti-proj1.log 2>&1; rc1=$?
SNAP2="$(claude_manifest "$TH1/.claude" "$TH1" "$REPO")"
[ "$rc1" = "0" ] && pass "project install runs (rc=0)" || fail "project install runs" "rc=$rc1 (see /tmp/ti-proj1.log)"
present "$PROJ1/.claude/settings.json" "project install actually populated the project (control)"
[ "$SNAP1" = "$SNAP2" ] && pass "test_project_install_leaves_home_claude_snapshot_byte_identical" || { fail "test_project_install_leaves_home_claude_snapshot_byte_identical" "HOME/.claude changed under --project"; diff <(printf '%s\n' "$SNAP1") <(printf '%s\n' "$SNAP2") | head -20; }

# ---------- CRITICAL: unflagged (global) install matches golden baseline ----------
GOLDEN="$REPO/hooks/test/fixtures/install-golden-manifest.txt"
TG2="$(mktemp -d)"
HOME="$TG2" bash "$INSTALL" --from-here >/tmp/ti-golden.log 2>&1
ACT="$(claude_manifest "$TG2/.claude" "$TG2" "$REPO")"
if [ "${GOLDEN_REGEN:-0}" = "1" ]; then
  mkdir -p "$(dirname "$GOLDEN")"; printf '%s\n' "$ACT" > "$GOLDEN"
  pass "REGEN wrote golden ($(printf '%s\n' "$ACT" | grep -c .) files) — rerun without GOLDEN_REGEN to assert"
elif [ ! -f "$GOLDEN" ]; then
  fail "test_unflagged_install_file_set_and_destinations_match_golden_baseline_byte_identical" "golden fixture missing: $GOLDEN (regen: GOLDEN_REGEN=1 bash $0)"
elif [ "$ACT" = "$(cat "$GOLDEN")" ]; then
  pass "test_unflagged_install_file_set_and_destinations_match_golden_baseline_byte_identical"
else
  fail "test_unflagged_install_file_set_and_destinations_match_golden_baseline_byte_identical" "unflagged install drifted from golden (regen intentionally: GOLDEN_REGEN=1 bash $0)"
  diff <(cat "$GOLDEN") <(printf '%s\n' "$ACT") | head -30
fi

# ---------- --project populates the FULL project .claude layer ----------
TH3="$(mktemp -d)"; PROJ3="$(mktemp -d)"; mkrepo "$PROJ3"
printf '# My Project\n\nExisting project instructions.\n' > "$PROJ3/CLAUDE.md"
HOME="$TH3" bash "$INSTALL" --project="$PROJ3" --from-here --merge-settings >/tmp/ti-proj3.log 2>&1
present "$PROJ3/.claude/hooks/multi-manager/mode-interlock.sh" "test_project_install_populates_project_claude_full_layer: orchestration hooks"
present "$PROJ3/.claude/sable/agents.yaml"                     "test_project_install_populates_project_claude_full_layer: registry agents.yaml"
present "$PROJ3/.claude/sable/roles/lincoln.md"               "test_project_install_populates_project_claude_full_layer: pane roles"
present "$PROJ3/.claude/skills/sable-plan/SKILL.md"           "test_project_install_populates_project_claude_full_layer: SABLE skills"
present "$PROJ3/.claude/settings.json"                        "test_project_install_populates_project_claude_full_layer: committed settings.json"
present "$PROJ3/.claude/hooks/tdd-gate.sh"                    "test_project_install_populates_project_claude_full_layer: base beads/tdd hooks"
present "$PROJ3/.claude/agents/sherlock.md"                   "test_project_install_populates_project_claude_full_layer: producer agent defs"
grep -q 'CLAUDE_PROJECT_DIR' "$PROJ3/.claude/settings.json" && pass "test_project_install_populates_project_claude_full_layer: settings.json uses \${CLAUDE_PROJECT_DIR} (portable)" || fail "full layer: settings.json portable placeholder" "no CLAUDE_PROJECT_DIR in $PROJ3/.claude/settings.json"

# ---------- prime block lands on <project>/CLAUDE.md, NOT the global one ----------
grep -q 'Prime Directive' "$PROJ3/CLAUDE.md" && pass "test_project_install_prepends_prime_block_to_project_claude_md_not_global: project CLAUDE.md gets prime block" || fail "prime block on project CLAUDE.md" "no Prime Directive in $PROJ3/CLAUDE.md"
grep -q 'My Project' "$PROJ3/CLAUDE.md" && pass "prime block prepends (preserves existing project CLAUDE.md body)" || fail "project CLAUDE.md body preserved"
if [ -f "$TH3/.claude/CLAUDE.md" ]; then
  grep -q 'Prime Directive' "$TH3/.claude/CLAUDE.md" && fail "prime block did NOT touch global CLAUDE.md" "global ~/.claude/CLAUDE.md got the prime block" || pass "test_project_install_prepends_prime_block...not_global: global CLAUDE.md untouched"
else
  pass "test_project_install_prepends_prime_block...not_global: no global CLAUDE.md written"
fi

# ---------- double-fire refusal: global already carries SABLE hooks ----------
TH5="$(mktemp -d)"; mkdir -p "$TH5/.claude"
printf '%s\n' '{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"bash ~/.claude/hooks/multi-manager/mode-interlock.sh"}]}]}}' > "$TH5/.claude/settings.json"
PROJ5="$(mktemp -d)"; mkrepo "$PROJ5"
HOME="$TH5" bash "$INSTALL" --project="$PROJ5" --from-here >/tmp/ti-refuse.log 2>&1; rc5=$?
[ "$rc5" != "0" ] && pass "test_install_project_refuses_when_global_settings_already_carries_sable_hooks: exits non-zero" || fail "double-fire refusal exits non-zero" "rc=$rc5 (see /tmp/ti-refuse.log)"
grep -qi 'twice\|double' /tmp/ti-refuse.log && pass "test_install_project_refuses...names_remedy: warns hooks would fire twice" || fail "refusal warns about double-fire"
grep -q -- '--force' /tmp/ti-refuse.log && pass "test_install_project_refuses...names_remedy: names --force remedy" || fail "refusal names --force remedy"
grep -qi 'uninstall' /tmp/ti-refuse.log && pass "test_install_project_refuses...names_remedy: names remove-one-scope remedy" || fail "refusal names uninstall remedy"
absent "$PROJ5/.claude" "test_install_project_refuses...: nothing written to the project on refusal"

# ---------- --force proceeds past the double-fire guard ----------
TH6="$(mktemp -d)"; mkdir -p "$TH6/.claude"
printf '%s\n' '{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"bash ~/.claude/hooks/multi-manager/mode-interlock.sh"}]}]}}' > "$TH6/.claude/settings.json"
PROJ6="$(mktemp -d)"; mkrepo "$PROJ6"
HOME="$TH6" bash "$INSTALL" --project="$PROJ6" --force --from-here --merge-settings >/tmp/ti-force.log 2>&1; rc6=$?
[ "$rc6" = "0" ] && pass "test_install_project_force_flag_proceeds: exits 0 with --force" || fail "--force proceeds (exit 0)" "rc=$rc6 (see /tmp/ti-force.log)"
present "$PROJ6/.claude/settings.json" "test_install_project_force_flag_proceeds: project layer installed under --force"
grep -q 'mode-interlock' "$TH6/.claude/settings.json" && pass "test_install_project_force_flag_proceeds: global settings left intact" || fail "--force left global settings intact"

# ---------- derive-once: no per-step scope fork (static analysis of install.sh) ----------
n_claudedir="$(grep -cE '^[[:space:]]*CLAUDE_DIR=' "$INSTALL")"
[ "$n_claudedir" = "2" ] && pass "test_install_project_derives_once_no_perstep_fork: one CLAUDE_DIR assignment per scope (2 total)" || fail "derive-once: CLAUDE_DIR assignment count" "count=$n_claudedir (expected 2 — one per scope)"
post_step_projmode="$(awk '/Step 1\/8/{f=1} f&&/PROJECT_MODE/{c++} END{print c+0}' "$INSTALL")"
[ "$post_step_projmode" = "0" ] && pass "test_install_project_derives_once_no_perstep_fork: steps never re-test the raw --project flag" || fail "derive-once: no per-step PROJECT_MODE fork" "PROJECT_MODE referenced $post_step_projmode time(s) after Step 1"

# ---------- --dry-run under --project reports project destinations, writes nothing ----------
# Bare --project (no path) must default to the CURRENT repo root via git-common-dir.
TH8="$(mktemp -d)"; PROJ8="$(mktemp -d)"; mkrepo "$PROJ8"
( cd "$PROJ8" && HOME="$TH8" bash "$INSTALL" --project --dry-run --from-here ) >/tmp/ti-dry.log 2>&1; rc8=$?
[ "$rc8" = "0" ] && pass "test_install_project_dry_run: exits 0" || fail "--project --dry-run exit 0" "rc=$rc8 (see /tmp/ti-dry.log)"
grep -q "$PROJ8/.claude" /tmp/ti-dry.log && pass "test_install_project_dry_run_reports_project_destinations: shows project .claude target" || fail "dry-run reports project destinations" "no $PROJ8/.claude in output"
grep -q 'would delegate: sable-orchestration-install --project' /tmp/ti-dry.log && pass "test_install_project_dry_run: orchestration delegated with --project scope" || fail "dry-run delegates orchestration --project"
absent "$PROJ8/.claude" "test_install_project_dry_run: writes nothing (bare --project defaults to cwd repo root)"

rm -rf "$TH1" "$PROJ1" "$TG2" "$TH3" "$PROJ3" "$TH5" "$PROJ5" "$TH6" "$PROJ6" "$TH8" "$PROJ8"

rm -rf "$TS" "$TN" "$TP"
echo
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
if [ "$FAIL" -gt 0 ]; then echo -e "Failed:$NAMES"; exit 1; fi
exit 0
