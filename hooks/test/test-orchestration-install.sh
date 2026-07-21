#!/usr/bin/env bash
# test-cockpit-install.sh — tests bin/sable-orchestration-install (SABLE-cav.7).
#
# Verifies: project-default scope (./.claude) + --user scope (~/.claude),
# idempotent + non-clobbering settings merge, --uninstall, and the
# missing-multi-manager-base warning. Uses SABLE_PROJECT_DIR / CLAUDE_USER_DIR
# to redirect both scopes at temp dirs so nothing touches the real config.
#
# Run with:
#   bash hooks/test/test-cockpit-install.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
INSTALLER="$REPO/bin/sable-orchestration-install"

PASS=0; FAIL=0; FAIL_NAMES=""
pass(){ PASS=$((PASS+1)); echo "PASS: $1"; }
fail(){ FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }
exists(){ if [ -e "$1" ]; then pass "$2"; else fail "$2" "missing: $1"; fi; }

if [ ! -x "$INSTALLER" ]; then echo "FAIL: installer not executable at $INSTALLER"; exit 2; fi

count_interlock(){ python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
print(sum(1 for b in d.get('hooks',{}).get('PreToolUse',[]) if isinstance(b,dict)
          for h in b.get('hooks',[]) if 'mode-interlock.sh' in h.get('command','')))" "$1" 2>/dev/null || echo ERR; }
count_in_event(){ python3 -c "
import json,sys
d=json.load(open(sys.argv[1])); ev=sys.argv[2]; m=sys.argv[3]
print(sum(1 for b in d.get('hooks',{}).get(ev,[]) if isinstance(b,dict) for h in b.get('hooks',[]) if m in h.get('command','')))" "$1" "$2" "$3" 2>/dev/null || echo ERR; }
count_marker(){ python3 -c "
import json,sys
d=json.load(open(sys.argv[1])); m=sys.argv[2]
print(sum(1 for blocks in d.get('hooks',{}).values() if isinstance(blocks,list) for b in blocks if isinstance(b,dict) for h in b.get('hooks',[]) if m in h.get('command','')))" "$1" "$2" 2>/dev/null || echo ERR; }
valid_json(){ python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$1" 2>/dev/null; }

# ---------- project scope (explicit) ----------
P="$(mktemp -d)"
out1="$(SABLE_PROJECT_DIR="$P" bash "$INSTALLER" --project 2>&1)"
exists "$P/.claude/skills/sable-plan/SKILL.md"    "project: /plan skill installed"
exists "$P/.claude/skills/sable-execute/SKILL.md" "project: /execute skill installed"
exists "$P/.claude/sable/roles/lincoln.md"  "project: lincoln role installed"
exists "$P/.claude/sable/roles/optimus.md"  "project: optimus pane role installed (tmux-native)"
exists "$P/.claude/sable/roles/tarzan.md"   "project: tarzan pane role installed (tmux-native)"
exists "$P/.claude/sable/roles/chuck.md"    "project: chuck pane role installed (tmux-native)"
if [ ! -e "$P/.claude/agents-teams" ]; then pass "project: agents-teams defs NOT installed (tmux-only)"; else fail "project: agents-teams defs NOT installed (tmux-only)" "unexpected $P/.claude/agents-teams/"; fi
if printf '%s' "$out1" | grep -q "sable-tmux"; then pass "project: install output points at the sable-tmux bring-up"; else fail "project: install output points at the sable-tmux bring-up" "no sable-tmux mention"; fi
if [ -x "$P/.claude/hooks/multi-manager/mode-interlock.sh" ]; then pass "project: interlock hook installed+exec"; else fail "project: interlock hook installed+exec"; fi
SET="$P/.claude/settings.json"
exists "$SET" "project: COMMITTED settings.json created"
if [ ! -e "$P/.claude/settings.local.json" ]; then pass "project: settings.local.json ABSENT (gitignored-wiring pitfall guard)"; else fail "project: settings.local.json ABSENT" "unexpected $P/.claude/settings.local.json"; fi
if valid_json "$SET"; then pass "project: settings is valid JSON"; else fail "project: settings is valid JSON"; fi
if [ "$(count_interlock "$SET")" = "2" ]; then pass "project: interlock registered on both legs (Bash+Agent)"; else fail "project: interlock registered on both legs (Bash+Agent)" "count=$(count_interlock "$SET")"; fi
if grep -qF '${CLAUDE_PROJECT_DIR}/.claude/hooks/' "$SET"; then pass "project: hook commands rooted at \${CLAUDE_PROJECT_DIR} placeholder"; else fail "project: hook commands rooted at \${CLAUDE_PROJECT_DIR} placeholder"; fi
if grep -q "$P/.claude/hooks/" "$SET"; then fail "project: no absolute machine path in hook commands" "found absolute path in $SET"; else pass "project: no absolute machine path in hook commands"; fi
exists "$P/.claude/sable/agents.yaml" "project: registry (agents.yaml) installed"
if [ -x "$P/.claude/hooks/multi-manager/session-role-anchor.sh" ]; then pass "project: identity hook installed+exec"; else fail "project: identity hook installed+exec"; fi
if [ "$(count_in_event "$SET" SessionStart session-role-anchor.sh)" = "1" ]; then pass "project: identity hook registered SessionStart"; else fail "project: identity hook registered SessionStart" "count=$(count_in_event "$SET" SessionStart session-role-anchor.sh)"; fi
if [ "$(count_in_event "$SET" PreCompact session-role-anchor.sh)" = "1" ]; then pass "project: identity hook registered PreCompact"; else fail "project: identity hook registered PreCompact"; fi

# ---------- SABLE-jfg6.5: reconcile-timer artifacts staged, never activated ----------
exists "$P/.claude/sable/reconcile-timer/sable-reconcile-timer.service" "project: reconcile-timer systemd .service staged"
exists "$P/.claude/sable/reconcile-timer/sable-reconcile-timer.timer"   "project: reconcile-timer systemd .timer staged"
exists "$P/.claude/sable/reconcile-timer/sable-reconcile-timer.cron"    "project: reconcile-timer cron fallback line staged"
if grep -q -- "--once --repo $REPO" "$P/.claude/sable/reconcile-timer/sable-reconcile-timer.service"; then
  pass "project: .service ExecStart carries --once and the target repo"
else
  fail "project: .service ExecStart carries --once and the target repo"
fi
if grep -q "OnUnitActiveSec=15min" "$P/.claude/sable/reconcile-timer/sable-reconcile-timer.timer"; then
  pass "project: .timer defaults to the 15min cadence"
else
  fail "project: .timer defaults to the 15min cadence"
fi
if grep -q "^\*/15 " "$P/.claude/sable/reconcile-timer/sable-reconcile-timer.cron"; then
  pass "project: .cron line defaults to the 15min cadence"
else
  fail "project: .cron line defaults to the 15min cadence"
fi
if printf '%s' "$out1" | grep -qi "NOT activated"; then
  pass "project: install output says the timer is staged, not activated"
else
  fail "project: install output says the timer is staged, not activated"
fi
# SABLE-5xz68 DEFECT ONE: the old output printed a multi-command recipe that
# nobody ever ran, so the units sat staged and NOTHING was scheduled. The
# activation instruction must now be ONE copy-pasteable command that verifies
# itself, plus a standalone check the operator can re-run any time.
if printf '%s' "$out1" | grep -q -- "sable-reconcile-timer --install-schedule"; then
  pass "project: install output prints the ONE self-verifying activation command"
else
  fail "project: install output prints the ONE self-verifying activation command" "out=$out1"
fi
if printf '%s' "$out1" | grep -q -- "--check-schedule"; then
  pass "project: install output points at the post-install schedule verification"
else
  fail "project: install output points at the post-install schedule verification"
fi
# the install itself must never actually touch a live systemd/cron surface.
# SABLE-i8kv: run this against a SANDBOXED HOME, not the developer's real one —
# on a host where the real D5 reconcile-timer is legitimately installed (e.g.
# o9ru), checking the unsandboxed $HOME conflates "install wrote here" with
# "this developer happens to run the timer" and fails for the right answer.
# SABLE-f00o: shared predicate so the guard below exercises the SAME detection
# code path as the real assertion, instead of tautologically re-checking the
# file it just touched itself.
home_has_timer_unit(){ [ -e "$1/.config/systemd/user/sable-reconcile-timer.timer" ]; }

HS="$(mktemp -d)"; mkdir -p "$HS/.config/systemd/user"
HP="$(mktemp -d)"
HOME="$HS" SABLE_PROJECT_DIR="$HP" bash "$INSTALLER" --project >/dev/null 2>&1
if home_has_timer_unit "$HS"; then
  fail "project: install does not copy the unit into the real ~/.config/systemd/user" "found $HS/.config/systemd/user/sable-reconcile-timer.timer"
else
  pass "project: install does not copy the unit into the real ~/.config/systemd/user"
fi

# guard: plant a unit inside the SANDBOXED HOME and re-invoke home_has_timer_unit
# (the SAME predicate the assertion above calls) — proving detection actually
# works. If home_has_timer_unit is ever neutered (e.g. hardcoded to report
# "not found"), this guard must go red; that is the acceptance invariant.
touch "$HS/.config/systemd/user/sable-reconcile-timer.timer"
if home_has_timer_unit "$HS"; then
  pass "project: assertion still bites when a unit IS present under sandboxed HOME (guard)"
else
  fail "project: assertion still bites when a unit IS present under sandboxed HOME (guard)" "home_has_timer_unit reported absent after planting $HS/.config/systemd/user/sable-reconcile-timer.timer"
fi
rm -rf "$HS" "$HP"

# ---------- SABLE-7oj5: generated units carry a resolved bd env, not bare PATH lookup ----------
# Hermetic against the host: do NOT rely on ambient PATH having (dev host, via
# nvm) or lacking (CI clean room) a real bd. Force both cases explicitly so
# this suite proves the contract on every host, not just this one.

# POSITIVE CASE: a resolvable bd on PATH must be baked in verbatim.
FAKEBIN="$(mktemp -d)"
cat > "$FAKEBIN/bd" <<'FAKEBD'
#!/usr/bin/env bash
exit 0
FAKEBD
chmod +x "$FAKEBIN/bd"
P_BD="$(mktemp -d)"
SABLE_PROJECT_DIR="$P_BD" PATH="$FAKEBIN:$PATH" bash "$INSTALLER" --project >/dev/null 2>&1
SVC_BD="$P_BD/.claude/sable/reconcile-timer/sable-reconcile-timer.service"
CRON_BD="$P_BD/.claude/sable/reconcile-timer/sable-reconcile-timer.cron"
bd_env_val="$(grep -o 'Environment=SABLE_RC_BD=.*' "$SVC_BD" | cut -d= -f3-)"
if [ "$bd_env_val" = "$FAKEBIN/bd" ]; then
  pass "project: .service carries Environment=SABLE_RC_BD=<path> (hermetic bd present)"
else
  fail "project: .service carries Environment=SABLE_RC_BD=<path> (hermetic bd present)" "expected $FAKEBIN/bd got '$bd_env_val'"
fi
if [ -n "$bd_env_val" ] && [ -x "$bd_env_val" ]; then
  pass "project: .service's SABLE_RC_BD points at a real executable at generation time"
else
  fail "project: .service's SABLE_RC_BD points at a real executable at generation time" "path=$bd_env_val"
fi
if grep -q "SABLE_RC_BD=\"$bd_env_val\"" "$CRON_BD"; then
  pass "project: .cron line sets SABLE_RC_BD consistently with the .service"
else
  fail "project: .cron line sets SABLE_RC_BD consistently with the .service"
fi

# NEGATIVE CASE: no bd anywhere on PATH — the installer must not fabricate a
# path, must warn, and must still emit the fallback Environment=PATH= line.
P_NOBD="$(mktemp -d)"
nobd_err="$(SABLE_PROJECT_DIR="$P_NOBD" PATH="/usr/bin:/bin" bash "$INSTALLER" --project 2>&1 >/dev/null)"
SVC_NOBD="$P_NOBD/.claude/sable/reconcile-timer/sable-reconcile-timer.service"
if ! grep -q '^Environment=SABLE_RC_BD=' "$SVC_NOBD"; then
  pass "project: .service omits Environment=SABLE_RC_BD when bd is absent from PATH"
else
  fail "project: .service omits Environment=SABLE_RC_BD when bd is absent from PATH" "found: $(grep '^Environment=SABLE_RC_BD=' "$SVC_NOBD")"
fi
if printf '%s' "$nobd_err" | grep -qi "WARNING.*bd.*not found"; then
  pass "project: install warns on stderr when bd is absent from PATH"
else
  fail "project: install warns on stderr when bd is absent from PATH" "stderr=$nobd_err"
fi
if grep -q '^Environment=PATH=' "$SVC_NOBD"; then
  pass "project: .service carries a fallback Environment=PATH= line even when bd is absent"
else
  fail "project: .service carries a fallback Environment=PATH= line even when bd is absent"
fi

# ---------- SABLE-5xz68 DEFECT TWO: the swept repo is a PARAMETER ----------
# The shipped units used to hardcode the SABLE tooling repo, so an operator who
# followed the printed recipe exactly got a timer that swept the WRONG repo and
# never looked at the fleet it was meant to protect. These assert the generated
# unit and cron lines carry the repo actually PASSED IN — and that a host
# running several fleets gets every one of them swept, since a unit covering
# one repo looks protected while leaving the others stranded.
P_REPO="$(mktemp -d)"
SABLE_PROJECT_DIR="$P_REPO" SABLE_RECONCILE_TARGET_REPO=/srv/fleet-alpha \
  bash "$INSTALLER" --project >/dev/null 2>&1
SVC_REPO="$P_REPO/.claude/sable/reconcile-timer/sable-reconcile-timer.service"
CRON_REPO="$P_REPO/.claude/sable/reconcile-timer/sable-reconcile-timer.cron"
if grep -q -- "--repo /srv/fleet-alpha" "$SVC_REPO"; then
  pass "project: .service ExecStart carries the repo PASSED IN, not a hardcoded path"
else
  fail "project: .service ExecStart carries the repo PASSED IN, not a hardcoded path" "$(grep ExecStart "$SVC_REPO")"
fi
if grep -q -- "--repo /srv/fleet-alpha" "$CRON_REPO"; then
  pass "project: .cron line carries the repo PASSED IN, not a hardcoded path"
else
  fail "project: .cron line carries the repo PASSED IN, not a hardcoded path" "$(cat "$CRON_REPO")"
fi
if grep -q -- "--repo $REPO" "$SVC_REPO"; then
  fail "project: the passed-in repo REPLACES the default, it does not sweep both" "$(grep ExecStart "$SVC_REPO")"
else
  pass "project: the passed-in repo REPLACES the default, it does not sweep both"
fi
rm -rf "$P_REPO"

P_MULTI="$(mktemp -d)"
SABLE_PROJECT_DIR="$P_MULTI" SABLE_RECONCILE_TARGET_REPO=/srv/fleet-alpha:/srv/fleet-beta \
  bash "$INSTALLER" --project >/dev/null 2>&1
SVC_MULTI="$P_MULTI/.claude/sable/reconcile-timer/sable-reconcile-timer.service"
CRON_MULTI="$P_MULTI/.claude/sable/reconcile-timer/sable-reconcile-timer.cron"
if grep -q -- "--repo /srv/fleet-alpha --repo /srv/fleet-beta" "$SVC_MULTI"; then
  pass "project: .service sweeps EVERY repo on a multi-fleet host"
else
  fail "project: .service sweeps EVERY repo on a multi-fleet host" "$(grep ExecStart "$SVC_MULTI")"
fi
if grep -q -- "--repo /srv/fleet-alpha --repo /srv/fleet-beta" "$CRON_MULTI"; then
  pass "project: .cron line sweeps EVERY repo on a multi-fleet host"
else
  fail "project: .cron line sweeps EVERY repo on a multi-fleet host" "$(cat "$CRON_MULTI")"
fi
rm -rf "$P_MULTI"

# env override of the cadence
P_CADENCE="$(mktemp -d)"
SABLE_PROJECT_DIR="$P_CADENCE" SABLE_RECONCILE_INTERVAL_MIN=5 bash "$INSTALLER" --project >/dev/null 2>&1
if grep -q "OnUnitActiveSec=5min" "$P_CADENCE/.claude/sable/reconcile-timer/sable-reconcile-timer.timer"; then
  pass "project: SABLE_RECONCILE_INTERVAL_MIN overrides the staged cadence"
else
  fail "project: SABLE_RECONCILE_INTERVAL_MIN overrides the staged cadence"
fi
rm -rf "$P_CADENCE"

# idempotent re-run
SABLE_PROJECT_DIR="$P" bash "$INSTALLER" --project >/dev/null 2>&1
if [ "$(count_interlock "$SET")" = "2" ]; then pass "project: re-run stays idempotent"; else fail "project: re-run idempotent" "count=$(count_interlock "$SET")"; fi
if valid_json "$SET"; then pass "project: settings valid after re-run"; else fail "project: settings valid after re-run"; fi

# preserves an existing unrelated hook
python3 - "$SET" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
d['hooks']['PreToolUse'].append({'matcher':'Bash','hooks':[{'type':'command','command':'bash /tmp/other-hook.sh','timeout':1000}]})
open(sys.argv[1],'w').write(json.dumps(d,indent=2))
PY
SABLE_PROJECT_DIR="$P" bash "$INSTALLER" --project >/dev/null 2>&1
if grep -q 'other-hook.sh' "$SET"; then pass "project: preserves existing hooks"; else fail "project: preserves existing hooks"; fi

# ---------- default scope is project; there is NO topology choice ----------
P2="$(mktemp -d)"
SABLE_PROJECT_DIR="$P2" bash "$INSTALLER" >/dev/null 2>&1
exists "$P2/.claude/skills/sable-plan/SKILL.md" "default (no flag) installs into project ./.claude"
if [ ! -e "$P2/.claude/agents-teams" ]; then pass "default install: no agents-teams defs (tmux-only)"; else fail "default install: no agents-teams defs (tmux-only)" "unexpected agents-teams/"; fi
SET2="$P2/.claude/settings.json"
if [ "$(count_marker "$SET2" pre-push-rebase-test)" -ge 1 ]; then pass "default install: governance hooks in settings"; else fail "default install: governance hooks in settings" "count=$(count_marker "$SET2" pre-push-rebase-test)"; fi

# retired topology flags are rejected with a clear error (tmux is the only topology)
for _flag in --teams --subagent --nested; do
  PN="$(mktemp -d)"
  if SABLE_PROJECT_DIR="$PN" bash "$INSTALLER" "$_flag" >/dev/null 2>&1; then
    fail "retired topology flag $_flag is rejected" "installer exited 0"
  else
    pass "retired topology flag $_flag is rejected"
  fi
  rm -rf "$PN"
done
rm -rf "$P2"

# ---------- user scope ----------
U="$(mktemp -d)"
CLAUDE_USER_DIR="$U/.claude" bash "$INSTALLER" --user >/dev/null 2>&1
exists "$U/.claude/skills/sable-execute/SKILL.md" "user: skill installed under ~/.claude"
exists "$U/.claude/settings.json" "user: settings.json created"
if [ "$(count_interlock "$U/.claude/settings.json")" = "2" ]; then pass "user: interlock registered on both legs"; else fail "user: interlock registered on both legs" "count=$(count_interlock "$U/.claude/settings.json")"; fi

# ---------- uninstall (project) ----------
# seed a legacy agents-teams dir from a pre-tmux-only install: uninstall must still clean it
mkdir -p "$P/.claude/agents-teams"; touch "$P/.claude/agents-teams/chuck.md"
SABLE_PROJECT_DIR="$P" bash "$INSTALLER" --project --uninstall >/dev/null 2>&1
if [ ! -e "$P/.claude/skills/sable-plan/SKILL.md" ]; then pass "uninstall removes skills"; else fail "uninstall removes skills"; fi
if [ ! -e "$P/.claude/sable/agents.yaml" ]; then pass "uninstall removes registry"; else fail "uninstall removes registry"; fi
if [ ! -e "$P/.claude/sable/roles/optimus.md" ] && [ ! -e "$P/.claude/sable/roles/chuck.md" ]; then pass "uninstall removes tmux-native pane roles"; else fail "uninstall removes tmux-native pane roles"; fi
if [ ! -e "$P/.claude/agents-teams" ]; then pass "uninstall cleans up legacy agents-teams defs"; else fail "uninstall cleans up legacy agents-teams defs"; fi
if [ ! -e "$P/.claude/sable/reconcile-timer" ]; then pass "uninstall removes staged reconcile-timer artifacts"; else fail "uninstall removes staged reconcile-timer artifacts"; fi
if [ "$(count_interlock "$SET")" = "0" ]; then pass "uninstall de-registers interlock"; else fail "uninstall de-registers interlock" "count=$(count_interlock "$SET")"; fi
if [ "$(count_marker "$SET" session-role-anchor.sh)" = "0" ]; then pass "uninstall de-registers identity hook"; else fail "uninstall de-registers identity hook" "count=$(count_marker "$SET" session-role-anchor.sh)"; fi
if grep -q 'other-hook.sh' "$SET"; then pass "uninstall keeps unrelated hooks"; else fail "uninstall keeps unrelated hooks"; fi

# ---------- SABLE-md7: idempotent across sibling same-matcher blocks ----------
# Repro: an event already has TWO matcher='' blocks — one for `bd prime`, one for
# session-role-anchor. The installer must detect the existing registration in the
# sibling block and NOT add a second one. (Buggy add_hooks only checked the first
# same-matcher block, so it double-registered session-role-anchor.)

seed_sibling_blocks(){ python3 - "$1" <<'PY'
import json, sys
seed = {
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash", "hooks": [
        {"type": "command", "command": "bash ~/.claude/hooks/tdd-gate.sh", "timeout": 5000}]}
    ],
    "SessionStart": [
      {"matcher": "", "hooks": [
        {"type": "command", "command": "bd prime 2>/dev/null || true"}]},
      {"matcher": "", "hooks": [
        {"type": "command", "command": "bash ~/.claude/hooks/multi-manager/session-role-anchor.sh", "timeout": 3000}]}
    ],
    "PreCompact": [
      {"matcher": "", "hooks": [
        {"type": "command", "command": "bd prime 2>/dev/null || true"}]},
      {"matcher": "", "hooks": [
        {"type": "command", "command": "bash ~/.claude/hooks/multi-manager/session-role-anchor.sh", "timeout": 3000}]}
    ]
  }
}
open(sys.argv[1], 'w').write(json.dumps(seed, indent=2))
PY
}

# Unit: add_hooks stays idempotent across sibling same-matcher blocks (SessionStart only,
# run the real installer twice — count must remain 1).
PU="$(mktemp -d)"; mkdir -p "$PU/.claude"
seed_sibling_blocks "$PU/.claude/settings.json"
CLAUDE_USER_DIR="$PU/.claude" bash "$INSTALLER" --user >/dev/null 2>&1
CLAUDE_USER_DIR="$PU/.claude" bash "$INSTALLER" --user >/dev/null 2>&1
PUSET="$PU/.claude/settings.json"
if [ "$(count_in_event "$PUSET" SessionStart session-role-anchor.sh)" = "1" ]; then pass "md7: sibling-block dedup keeps identity hook once (SessionStart)"; else fail "md7: sibling-block dedup keeps identity hook once (SessionStart)" "count=$(count_in_event "$PUSET" SessionStart session-role-anchor.sh)"; fi
if valid_json "$PUSET"; then pass "md7: settings valid after sibling-block dedup"; else fail "md7: settings valid after sibling-block dedup"; fi

# Integration: real `sable-orchestration-install --user` over a realistic pre-seeded
# multi-manager user scope leaves exactly one identity registration per event and
# one interlock, and preserves the bd prime entries.
M="$(mktemp -d)"; mkdir -p "$M/.claude"
seed_sibling_blocks "$M/.claude/settings.json"
CLAUDE_USER_DIR="$M/.claude" bash "$INSTALLER" --user >/dev/null 2>&1
MSET="$M/.claude/settings.json"
if [ "$(count_in_event "$MSET" SessionStart session-role-anchor.sh)" = "1" ]; then pass "md7: multi-manager re-install — identity once (SessionStart)"; else fail "md7: multi-manager re-install — identity once (SessionStart)" "count=$(count_in_event "$MSET" SessionStart session-role-anchor.sh)"; fi
if [ "$(count_in_event "$MSET" PreCompact session-role-anchor.sh)" = "1" ]; then pass "md7: multi-manager re-install — identity once (PreCompact)"; else fail "md7: multi-manager re-install — identity once (PreCompact)" "count=$(count_in_event "$MSET" PreCompact session-role-anchor.sh)"; fi
if [ "$(count_interlock "$MSET")" = "2" ]; then pass "md7: multi-manager re-install — interlock on both legs"; else fail "md7: multi-manager re-install — interlock on both legs" "count=$(count_interlock "$MSET")"; fi
if [ "$(count_marker "$MSET" 'bd prime')" = "2" ]; then pass "md7: multi-manager re-install — bd prime preserved"; else fail "md7: multi-manager re-install — bd prime preserved" "count=$(count_marker "$MSET" 'bd prime')"; fi
if valid_json "$MSET"; then pass "md7: settings valid after multi-manager re-install"; else fail "md7: settings valid after multi-manager re-install"; fi

# ---------- SABLE-qa4d.6: poll-based inbox hooks are gone ----------
PIH="$(mktemp -d)"
SABLE_PROJECT_DIR="$PIH" bash "$INSTALLER" --project >/dev/null 2>&1
PIHSET="$PIH/.claude/settings.json"
if [ "$(count_marker "$PIHSET" inbox-injection)" = "0" ]; then pass "settings register no inbox-injection hooks (sable-msg replaces the poll)"; else fail "settings register no inbox-injection hooks" "count=$(count_marker "$PIHSET" inbox-injection)"; fi
if [ ! -e "$PIH/.claude/hooks/multi-manager/inbox-injection.sh" ] && [ ! -e "$PIH/.claude/hooks/multi-manager/inbox-injection-precompact.sh" ]; then pass "no inbox-injection hook files installed"; else fail "no inbox-injection hook files installed"; fi
if [ -e "$PIH/.claude/hooks/multi-manager/read-guard.sh" ]; then pass "read-guard survives (durable-inbox guard stays)"; else fail "read-guard survives (durable-inbox guard stays)"; fi
rm -rf "$PIH"

# ---------- SABLE-qa4d.2: no teams residue in a fresh install ----------
TMONLY="$(mktemp -d)"
TM_OUT="$(SABLE_PROJECT_DIR="$TMONLY" bash "$INSTALLER" --project 2>&1)"
if printf '%s' "$TM_OUT" | grep -q "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"; then fail "install output has no experimental-teams-flag instruction" "flag instruction still printed"; else pass "install output has no experimental-teams-flag instruction"; fi
if printf '%s' "$TM_OUT" | grep -qi "topology"; then fail "install output does not speak of topologies" "topology wording still printed"; else pass "install output does not speak of topologies"; fi
rm -rf "$TMONLY"

# ---------- SABLE-gsqj: upgrade over a stale scope retires dead machinery ----------
# Seed a scope as if from the multi-topology era: retired inbox-poll hook files +
# their settings rows, a legacy agents-teams/ dir, and retired manager agent defs
# in agents/ — then run a PLAIN (non --uninstall) install and assert the retired
# stuff is gone while unrelated settings rows and genuinely custom agent files survive.
seed_retired_settings(){ python3 - "$1" <<'PY'
import json, sys
seed = {
  "hooks": {
    "SessionStart": [
      {"matcher": "", "hooks": [
        {"type": "command", "command": "bash ~/.claude/hooks/multi-manager/inbox-injection.sh", "timeout": 3000},
        {"type": "command", "command": "bd prime"}
      ]}
    ],
    "PreCompact": [
      {"matcher": "", "hooks": [
        {"type": "command", "command": "bash ~/.claude/hooks/multi-manager/inbox-injection-precompact.sh", "timeout": 3000}
      ]}
    ],
    "PreToolUse": [
      {"matcher": "Bash", "hooks": [
        {"type": "command", "command": "bash /tmp/other-hook.sh", "timeout": 1000}
      ]}
    ]
  }
}
open(sys.argv[1], 'w').write(json.dumps(seed, indent=2))
PY
}

RA="$(mktemp -d)"
mkdir -p "$RA/.claude/hooks/multi-manager" "$RA/.claude/agents-teams" "$RA/.claude/agents"
touch "$RA/.claude/agents-teams/chuck.md"
touch "$RA/.claude/agents/optimus.md" "$RA/.claude/agents/tarzan.md" "$RA/.claude/agents/chuck.md"
touch "$RA/.claude/agents/my-custom-agent.md"
printf '#!/usr/bin/env bash\necho retired\n' > "$RA/.claude/hooks/multi-manager/inbox-injection.sh"
printf '#!/usr/bin/env bash\necho retired\n' > "$RA/.claude/hooks/multi-manager/inbox-injection-precompact.sh"
seed_retired_settings "$RA/.claude/settings.json"

RA_OUT="$(SABLE_PROJECT_DIR="$RA" bash "$INSTALLER" --project 2>&1)"
RASET="$RA/.claude/settings.json"

if [ ! -e "$RA/.claude/hooks/multi-manager/inbox-injection.sh" ]; then pass "gsqj: retired inbox-injection.sh removed on plain (upgrade) install"; else fail "gsqj: retired inbox-injection.sh removed on plain install"; fi
if [ ! -e "$RA/.claude/hooks/multi-manager/inbox-injection-precompact.sh" ]; then pass "gsqj: retired inbox-injection-precompact.sh removed"; else fail "gsqj: retired inbox-injection-precompact.sh removed"; fi
if [ "$(count_marker "$RASET" inbox-injection)" = "0" ]; then pass "gsqj: retired settings rows removed"; else fail "gsqj: retired settings rows removed" "count=$(count_marker "$RASET" inbox-injection)"; fi
if [ ! -e "$RA/.claude/agents-teams" ]; then pass "gsqj: retired agents-teams dir removed on plain install (not just --uninstall)"; else fail "gsqj: retired agents-teams dir removed on plain install"; fi
if [ ! -e "$RA/.claude/agents/optimus.md" ] && [ ! -e "$RA/.claude/agents/tarzan.md" ] && [ ! -e "$RA/.claude/agents/chuck.md" ]; then pass "gsqj: retired manager agent defs removed from scope's agents/ dir"; else fail "gsqj: retired manager agent defs removed from scope's agents/ dir"; fi
if [ -e "$RA/.claude/agents/my-custom-agent.md" ]; then pass "gsqj: genuinely custom agent def survives"; else fail "gsqj: genuinely custom agent def survives"; fi
if grep -q 'other-hook.sh' "$RASET"; then pass "gsqj: unrelated settings row survives"; else fail "gsqj: unrelated settings row survives"; fi
if grep -q 'bd prime' "$RASET"; then pass "gsqj: unrelated bd prime row survives"; else fail "gsqj: unrelated bd prime row survives"; fi
if valid_json "$RASET"; then pass "gsqj: settings valid JSON after retired-artifact cleanup"; else fail "gsqj: settings valid JSON after retired-artifact cleanup"; fi
if printf '%s' "$RA_OUT" | grep -qi "retired artifacts cleaned"; then pass "gsqj: install output reports what was cleaned"; else fail "gsqj: install output reports what was cleaned"; fi

# a second (already-clean) run is silent about retired artifacts and stays idempotent
RA_OUT2="$(SABLE_PROJECT_DIR="$RA" bash "$INSTALLER" --project 2>&1)"
if printf '%s' "$RA_OUT2" | grep -qi "retired artifacts cleaned"; then fail "gsqj: re-run on clean scope reports nothing to clean" "still printed cleanup banner"; else pass "gsqj: re-run on clean scope reports nothing to clean"; fi
if valid_json "$RASET"; then pass "gsqj: settings still valid JSON after second run"; else fail "gsqj: settings still valid JSON after second run"; fi
rm -rf "$RA"

# ---------- SABLE-6lfz: per-file change manifest + snapshot ----------
# Point the installer at a throwaway source tree (SABLE_REPO_DIR) so "modify one
# source file" never touches this repo's own tracked files; it's a copy of the
# real hooks/templates/skills subset the installer actually reads from.
RS="$(mktemp -d)"
mkdir -p "$RS/hooks/multi-manager" "$RS/templates/multi-manager/roles" "$RS/skills/sample-skill"
cp "$REPO"/hooks/multi-manager/*.sh "$RS/hooks/multi-manager/"
cp "$REPO/templates/multi-manager/agents.yaml" "$RS/templates/multi-manager/agents.yaml"
cp "$REPO/templates/multi-manager/settings-snippet.json" "$RS/templates/multi-manager/settings-snippet.json"
cp "$REPO"/templates/multi-manager/roles/*.md "$RS/templates/multi-manager/roles/"
printf -- '---\nname: sample-skill\n---\nplaceholder\n' > "$RS/skills/sample-skill/SKILL.md"

MF="$(mktemp -d)"
out_first="$(SABLE_REPO_DIR="$RS" SABLE_PROJECT_DIR="$MF" bash "$INSTALLER" --project 2>&1)"
if printf '%s' "$out_first" | grep -q "Change manifest"; then pass "manifest: first install prints a change manifest"; else fail "manifest: first install prints a change manifest"; fi
if printf '%s' "$out_first" | grep -q "NEW "; then pass "manifest: first install reports NEW entries"; else fail "manifest: first install reports NEW entries"; fi

out_second="$(SABLE_REPO_DIR="$RS" SABLE_PROJECT_DIR="$MF" bash "$INSTALLER" --project 2>&1)"
if printf '%s' "$out_second" | grep -q "all files identical"; then pass "manifest: second run reports all-identical"; else fail "manifest: second run reports all-identical" "$out_second"; fi
if printf '%s' "$out_second" | grep -q "CHANGED"; then fail "manifest: second run has no CHANGED entries"; else pass "manifest: second run has no CHANGED entries"; fi

# modify one source file (in the throwaway RS tree, never the real repo)
echo "# test-touch" >> "$RS/hooks/multi-manager/mode-interlock.sh"
out_third="$(SABLE_REPO_DIR="$RS" SABLE_PROJECT_DIR="$MF" bash "$INSTALLER" --project 2>&1)"
if printf '%s' "$out_third" | grep -q "CHANGED.*mode-interlock.sh"; then pass "manifest: third run reports the modified file as CHANGED"; else fail "manifest: third run reports the modified file as CHANGED" "$out_third"; fi
changed_count="$(printf '%s' "$out_third" | grep -c '^  CHANGED')"
if [ "$changed_count" = "1" ]; then pass "manifest: third run reports exactly one changed file"; else fail "manifest: third run reports exactly one changed file" "count=$changed_count"; fi

bak_dir="$(find "$MF/.claude" -maxdepth 1 -name '.install-bak-*' | sort | tail -1)"
if [ -n "$bak_dir" ] && [ -f "$bak_dir/hooks/multi-manager/mode-interlock.sh" ]; then pass "manifest: snapshot dir captures the prior copy"; else fail "manifest: snapshot dir captures the prior copy" "bak_dir=$bak_dir"; fi
if [ -n "$bak_dir" ] && ! grep -q "test-touch" "$bak_dir/hooks/multi-manager/mode-interlock.sh"; then pass "manifest: snapshot holds the pre-change content"; else fail "manifest: snapshot holds the pre-change content"; fi
if grep -q "test-touch" "$MF/.claude/hooks/multi-manager/mode-interlock.sh"; then pass "manifest: installed copy now matches the new source"; else fail "manifest: installed copy now matches the new source"; fi

# SABLE-0pn: the summary line must interpolate the REAL (non-empty, existing)
# snapshot dir, not print 'snapshotted to )' with BAK_DIR unset.
summary_line="$(printf '%s\n' "$out_third" | grep 'file(s) changed')"
if printf '%s' "$summary_line" | grep -qE 'snapshotted to \)$|snapshotted to $'; then
    fail "0pn: summary line does not print an empty snapshot path" "$summary_line"
else
    pass "0pn: summary line does not print an empty snapshot path"
fi
summary_dir="$(printf '%s' "$summary_line" | sed -n 's/.*snapshotted to \(.*\))$/\1/p')"
if [ -n "$summary_dir" ] && [ -d "$summary_dir" ]; then pass "0pn: summary line's snapshot dir exists on disk"; else fail "0pn: summary line's snapshot dir exists on disk" "summary_dir=$summary_dir"; fi
if [ -n "$bak_dir" ] && [ "$summary_dir" = "$bak_dir" ]; then pass "0pn: summary line's dir matches the actual .install-bak-* dir"; else fail "0pn: summary line's dir matches the actual .install-bak-* dir" "summary_dir=$summary_dir bak_dir=$bak_dir"; fi
rm -rf "$RS" "$MF"

rm -rf "$P" "$P2" "$U" "$PU" "$M"
echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then echo -e "Failed tests:$FAIL_NAMES"; exit 1; fi
exit 0
