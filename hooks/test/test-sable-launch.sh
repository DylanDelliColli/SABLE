#!/usr/bin/env bash
# test-sable-launch.sh — SABLE-njiv
# Unit + integration tests for bin/sable-launch: the one-command operator entry
# point that sets agent identity/role and verifies (warn-only) the teams flag,
# then exec's claude. A stub `claude` on PATH echoes the env it received so we
# can assert identity is set correctly and that exec actually happened.
#
# Run with:
#   bash hooks/test/test-sable-launch.sh

set -uo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
LAUNCH="$REPO/bin/sable-launch"

PASS=0
FAIL=0
FAIL_NAMES=""
pass() { PASS=$((PASS+1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL+1)); FAIL_NAMES="$FAIL_NAMES\n  $1"; echo "FAIL: $1"; [ -n "${2:-}" ] && echo "  $2"; }

[ -x "$LAUNCH" ] || { fail "sable-launch is executable"; echo "Tests: 1 | Passed: 0 | Failed: 1"; exit 1; }
pass "sable-launch is executable"

# ---- Stub `claude` on PATH: echoes identity env + args, then exits 0 ----
STUB_BIN=$(mktemp -d)
cat > "$STUB_BIN/claude" <<'STUB'
#!/usr/bin/env bash
echo "STUB_EXEC=1"
echo "NAME=${CLAUDE_AGENT_NAME:-}"
echo "ROLE=${CLAUDE_AGENT_ROLE:-}"
echo "ARGS=$*"
STUB
chmod +x "$STUB_BIN/claude"

# HOME with the teams flag present in settings.json
HOME_FLAG=$(mktemp -d)
mkdir -p "$HOME_FLAG/.claude"
cat > "$HOME_FLAG/.claude/settings.json" <<'JSON'
{ "env": { "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1" } }
JSON

# HOME without the flag (empty settings.json)
HOME_NOFLAG=$(mktemp -d)
mkdir -p "$HOME_NOFLAG/.claude"
cat > "$HOME_NOFLAG/.claude/settings.json" <<'JSON'
{ "env": {} }
JSON

# Non-git scratch CWD for run_launch invocations (SABLE-jd5fj.14): the suite's
# own CWD is inside this repo checkout, which may carry an untracked project
# registry (.claude/sable/agents.yaml, gitignored local install state) that
# resolves via git common-dir from ANY worktree of this repo. Running the
# session-form subtests from the suite's inherited CWD therefore exercises
# whatever scope the checkout happens to carry instead of the default resolver
# path on a known-clean tree. Every run_launch invocation cd's into this fixed,
# non-git scratch dir so the default (no-override) resolution path is always
# exercised against a clean tree, regardless of where the suite itself runs
# from. Tests that deliberately need a poisoned/git CWD (session name
# derivation, the v1 fleet-boundary fixtures) bypass run_launch and cd their
# own fixture repos directly.
RUN_LAUNCH_CWD="$(mktemp -d)"

# run <label> <home> <env_prefix> <args...> -> sets OUT/ERR/CODE globals
run_launch() {
  local home="$1" env_prefix="$2"; shift 2
  local tmpout tmperr
  tmpout=$(mktemp); tmperr=$(mktemp)
  ( cd "$RUN_LAUNCH_CWD" && env -i PATH="$STUB_BIN:$PATH" HOME="$home" $env_prefix bash "$LAUNCH" "$@" ) >"$tmpout" 2>"$tmperr"
  CODE=$?
  OUT=$(cat "$tmpout"); ERR=$(cat "$tmperr")
  rm -f "$tmpout" "$tmperr"
}

# ---- UNIT: session form (no-arg = bring up + attach; SABLE-ssws.2) ----
# Stub tmux records argv; has-session behavior driven by STUB_HAS_SESSION.
cat > "$STUB_BIN/tmux" <<'STUB'
#!/usr/bin/env bash
[ -n "${TMUX_LOG:-}" ] && echo "tmux $*" >> "$TMUX_LOG"
case "$1" in
  has-session)      exit "${STUB_HAS_SESSION:-1}" ;;
  attach|attach-session) echo "STUB_ATTACH=1"; exit 0 ;;
  display-message)  echo "not-sable"; exit 0 ;;
esac
exit 0
STUB
chmod +x "$STUB_BIN/tmux"
# Recording sable-tmux stand-in (selected via SABLE_TMUX_BIN)
cat > "$STUB_BIN/sable-tmux-rec" <<'STUB'
#!/usr/bin/env bash
[ -n "${ST_LOG:-}" ] && echo "sable-tmux $*" >> "$ST_LOG"
exit 0
STUB
chmod +x "$STUB_BIN/sable-tmux-rec"

WORK="$(mktemp -d)"
# 1. no session yet -> creates a LINCOLN-ONLY session (mode-neutral: no manager
#    panes, no autostart kicks — SABLE-dqhn.1), then attaches
run_launch "$HOME_FLAG" "TMUX_LOG=$WORK/t1.log ST_LOG=$WORK/s1.log STUB_HAS_SESSION=1 SABLE_TMUX_BIN=$STUB_BIN/sable-tmux-rec"
if [ "$CODE" -eq 0 ] && printf '%s' "$OUT" | grep -q "STUB_ATTACH=1" \
   && grep -q -- "--roles lincoln" "$WORK/s1.log" 2>/dev/null \
   && ! grep -q -- "--autostart" "$WORK/s1.log" 2>/dev/null; then
  pass "no-arg + no session -> lincoln-only layout (no --autostart) then attach"
else
  fail "no-arg + no session -> lincoln-only layout (no --autostart) then attach" "out=[$OUT] err=[$ERR] code=$CODE s1=[$(cat "$WORK/s1.log" 2>/dev/null)]"
fi

# 2. session already exists -> reuses (no sable-tmux), attaches
run_launch "$HOME_FLAG" "TMUX_LOG=$WORK/t2.log ST_LOG=$WORK/s2.log STUB_HAS_SESSION=0 SABLE_TMUX_BIN=$STUB_BIN/sable-tmux-rec"
if [ "$CODE" -eq 0 ] && printf '%s' "$OUT" | grep -q "STUB_ATTACH=1" && [ ! -s "$WORK/s2.log" ]; then
  pass "no-arg + existing session -> reuse (no re-create) then attach"
else
  fail "no-arg + existing session -> reuse then attach" "out=[$OUT] err=[$ERR] code=$CODE s2=[$(cat "$WORK/s2.log" 2>/dev/null)]"
fi

# 3. --no-attach -> brings up only, never attaches
run_launch "$HOME_FLAG" "TMUX_LOG=$WORK/t3.log ST_LOG=$WORK/s3.log STUB_HAS_SESSION=1 SABLE_TMUX_BIN=$STUB_BIN/sable-tmux-rec" --no-attach
if [ "$CODE" -eq 0 ] && ! printf '%s' "$OUT" | grep -q "STUB_ATTACH=1" && grep -q -- "--roles lincoln" "$WORK/s3.log" 2>/dev/null; then
  pass "--no-attach brings up the session without attaching"
else
  fail "--no-attach brings up the session without attaching" "out=[$OUT] err=[$ERR] code=$CODE"
fi

# 3c. planted-poison control (SABLE-jd5fj.14): the session-form subtests above
#     must stay green even when the SUITE's own CWD sits inside a repo that
#     carries its own untracked project registry (mirrors the live merge-seat
#     checkout, which is exactly this state) and HOME has no global install.
#     Without run_launch's scratch-CWD hermeticity this reproduces the exact
#     seat failure: FLEET_PROJECT_ONLY_REMEDY (exit 3) instead of PASS.
POISON_TOP="$(mktemp -d)"; POISON_REPO="$POISON_TOP/poison"
mkdir -p "$POISON_REPO/.claude/sable"
git init -q "$POISON_REPO" 2>/dev/null
cat > "$POISON_REPO/.claude/sable/agents.yaml" <<'YAML'
agents:
  optimus:
    type: epic_manager
YAML
POISON_HOME="$(mktemp -d)"   # isolated HOME — no global install (worst case)

cd "$POISON_REPO" || exit 1
run_launch "$POISON_HOME" "TMUX_LOG=$WORK/tp.log ST_LOG=$WORK/sp.log STUB_HAS_SESSION=1 SABLE_TMUX_BIN=$STUB_BIN/sable-tmux-rec"
cd "$REPO" || exit 1
if [ "$CODE" -eq 0 ] && printf '%s' "$OUT" | grep -q "STUB_ATTACH=1" \
   && grep -q -- "--roles lincoln" "$WORK/sp.log" 2>/dev/null; then
  pass "session-form stays green when the suite's CWD sits inside a poisoned project-registry repo"
else
  fail "session-form stays green when the suite's CWD sits inside a poisoned project-registry repo" "out=[$OUT] err=[$ERR] code=$CODE sp=[$(cat "$WORK/sp.log" 2>/dev/null)]"
fi
rm -rf "$POISON_TOP" "$POISON_HOME"

# 3b. per-repo derivation (SABLE-e1e3.2): from a repo named 'alpha' with no
#     SABLE_TMUX_SESSION, the derived session sable-alpha reaches sable-tmux
TREPO="$(mktemp -d)/alpha"
mkdir -p "$TREPO"
git init -q "$TREPO" 2>/dev/null
( cd "$TREPO" && env -i PATH="$STUB_BIN:$PATH" HOME="$HOME_FLAG" \
    TMUX_LOG="$WORK/t3b.log" ST_LOG="$WORK/s3b.log" STUB_HAS_SESSION=1 \
    SABLE_TMUX_BIN="$STUB_BIN/sable-tmux-rec" bash "$LAUNCH" --no-attach ) >/dev/null 2>&1
if grep -q -- "--session sable-alpha" "$WORK/s3b.log" 2>/dev/null; then
  pass "no SABLE_TMUX_SESSION -> session name derives from the repo (sable-alpha)"
else
  fail "no SABLE_TMUX_SESSION -> session name derives from the repo (sable-alpha)" "s3b=[$(cat "$WORK/s3b.log" 2>/dev/null)] t3b=[$(cat "$WORK/t3b.log" 2>/dev/null)]"
fi
rm -rf "${TREPO%/alpha}"

# 4. tmux missing -> clear error naming tmux, non-zero exit
EMPTY_BIN="$(mktemp -d)"
ln -s "$(command -v bash)" "$EMPTY_BIN/bash"
OUT4="$(env -i PATH="$EMPTY_BIN" HOME="$HOME_FLAG" bash "$LAUNCH" 2>&1)"; CODE4=$?
if [ "$CODE4" -ne 0 ] && printf '%s' "$OUT4" | grep -qi "tmux"; then
  pass "missing tmux -> non-zero exit with a message naming tmux"
else
  fail "missing tmux -> non-zero exit with a message naming tmux" "out=[$OUT4] code=$CODE4"
fi
rm -rf "$EMPTY_BIN"

# ---- UNIT: role form (advanced single-pane door — unchanged) ----

run_launch "$HOME_FLAG" "" lincoln
if printf '%s' "$OUT" | grep -q "NAME=lincoln" && printf '%s' "$OUT" | grep -q "ROLE=manager"; then
  pass "explicit role lincoln -> CLAUDE_AGENT_NAME=lincoln, ROLE=manager, exec'd claude"
else
  fail "explicit role lincoln -> lincoln/manager" "out=[$OUT] err=[$ERR] code=$CODE"
fi

run_launch "$HOME_FLAG" "" chuck
if printf '%s' "$OUT" | grep -q "NAME=chuck" && printf '%s' "$OUT" | grep -q "ROLE=manager"; then
  pass "explicit role chuck -> CLAUDE_AGENT_NAME=chuck"
else
  fail "explicit role chuck" "out=[$OUT] err=[$ERR] code=$CODE"
fi

run_launch "$HOME_FLAG" "" bogus
if [ "$CODE" -ne 0 ] && ! printf '%s' "$OUT" | grep -q "STUB_EXEC=1" && printf '%s' "$ERR" | grep -qi "role"; then
  pass "unknown role -> non-zero exit, usage on stderr, claude NOT exec'd"
else
  fail "unknown role rejected" "out=[$OUT] err=[$ERR] code=$CODE"
fi

run_launch "$HOME_FLAG" "" --help
if [ "$CODE" -eq 0 ] && printf '%s' "$OUT$ERR" | grep -qi "usage" && ! printf '%s' "$OUT" | grep -q "STUB_EXEC=1"; then
  pass "--help -> exit 0, prints usage, claude NOT exec'd"
else
  fail "--help prints usage" "out=[$OUT] err=[$ERR] code=$CODE"
fi

# ---- UNIT: args passthrough ----

run_launch "$HOME_FLAG" "" lincoln --resume sess-123
if printf '%s' "$OUT" | grep -q "NAME=lincoln" && printf '%s' "$OUT" | grep -q -- "--resume sess-123"; then
  pass "args after role are passed through to claude"
else
  fail "args passthrough after role" "out=[$OUT] err=[$ERR] code=$CODE"
fi

run_launch "$HOME_FLAG" "" --dangerously-skip-permissions
if printf '%s' "$OUT" | grep -q "NAME=lincoln" && printf '%s' "$OUT" | grep -q -- "--dangerously-skip-permissions"; then
  pass "leading claude flag -> default role lincoln, flag passed through"
else
  fail "leading flag passthrough with default role" "out=[$OUT] err=[$ERR] code=$CODE"
fi

# ---- SABLE-59t6.4: v1 fleet boundary under a PROJECT-only install ----
# When the registry resolves to a PROJECT registry and no global install exists,
# the FLEET session door refuses with the exact remedy; the SOLO role door does
# not (Lincoln + producer subagents + planning never depend on the global fleet
# machinery). Fixture: a git repo shipping its own .claude/sable/agents.yaml,
# entered as CWD, with an isolated HOME that has NO ~/.claude/sable/agents.yaml
# and no SABLE_AGENTS_YAML override (env -i drops it) — exactly project-only.
FB_REMEDY="fleet requires the global install in v1, or export SABLE_AGENTS_YAML and SABLE_DISPATCH_DIR in the shell that creates the tmux session"
FB_HOME="$(mktemp -d)"                       # isolated HOME — no global install
FB_TOP="$(mktemp -d)"; FB_REPO="$FB_TOP/proj"
mkdir -p "$FB_REPO/.claude/sable"
git init -q "$FB_REPO" 2>/dev/null
cat > "$FB_REPO/.claude/sable/agents.yaml" <<'YAML'
agents:
  optimus:
    type: epic_manager
YAML

# test_fleet_launch_refuses_project_only_install_with_exact_remedy_text
FB_OUT="$(mktemp)"; FB_ERR="$(mktemp)"
( cd "$FB_REPO" && env -i PATH="$STUB_BIN:$PATH" HOME="$FB_HOME" bash "$LAUNCH" --no-attach ) >"$FB_OUT" 2>"$FB_ERR"
FB_CODE=$?
FB_ERRTXT="$(cat "$FB_ERR")"
if [ "$FB_CODE" -ne 0 ] && printf '%s' "$FB_ERRTXT" | grep -qF "$FB_REMEDY"; then
  pass "fleet launch refuses project-only install with exact remedy text"
else
  fail "fleet launch refuses project-only install with exact remedy text" "code=$FB_CODE err=[$FB_ERRTXT]"
fi
rm -f "$FB_OUT" "$FB_ERR"

# test_solo_lincoln_plus_producers_loop_not_refused_under_project_scope
# The SAME project-only scope: the solo role door must exec claude, never refuse.
FB_OUT="$(mktemp)"; FB_ERR="$(mktemp)"
( cd "$FB_REPO" && env -i PATH="$STUB_BIN:$PATH" HOME="$FB_HOME" bash "$LAUNCH" lincoln ) >"$FB_OUT" 2>"$FB_ERR"
SOLO_CODE=$?
SOLO_OUT="$(cat "$FB_OUT")"; SOLO_ERR="$(cat "$FB_ERR")"
if [ "$SOLO_CODE" -eq 0 ] \
   && printf '%s' "$SOLO_OUT" | grep -q "STUB_EXEC=1" \
   && printf '%s' "$SOLO_OUT" | grep -q "NAME=lincoln" \
   && ! printf '%s' "$SOLO_ERR" | grep -qF "$FB_REMEDY"; then
  pass "solo lincoln + producers loop not refused under project scope"
else
  fail "solo lincoln + producers loop not refused under project scope" "code=$SOLO_CODE out=[$SOLO_OUT] err=[$SOLO_ERR]"
fi
rm -f "$FB_OUT" "$FB_ERR"
rm -rf "$FB_HOME" "$FB_TOP"

# ---- INTEGRATION: no teams-flag machinery remains (tmux-only, SABLE-qa4d) ----

run_launch "$HOME_NOFLAG" "" lincoln
if ! printf '%s' "$ERR" | grep -q "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" && printf '%s' "$OUT" | grep -q "NAME=lincoln"; then
  pass "no teams-flag warning is ever printed (tmux-only)"
else
  fail "no teams-flag warning is ever printed (tmux-only)" "out=[$OUT] err=[$ERR] code=$CODE"
fi

# It must not have touched settings.json
if grep -q "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" "$HOME_NOFLAG/.claude/settings.json"; then
  fail "never writes settings.json" "settings.json was modified to add the flag"
else
  pass "never writes settings.json"
fi

# Static: the launcher itself carries no teams-flag machinery
if ! grep -q "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" "$LAUNCH"; then
  pass "sable-launch carries no teams-flag machinery"
else
  fail "sable-launch carries no teams-flag machinery" "flag still referenced in $LAUNCH"
fi

# ---- INTEGRATION: real tmux on an isolated socket (SABLE-ssws.2) ----
if command -v tmux >/dev/null 2>&1; then
  SOCK="sable-launch-test-$$"
  ISESS="slaunch"
  int_env=(SABLE_TMUX_SOCKET="$SOCK" SABLE_TMUX_SESSION="$ISESS" SABLE_TMUX_PANE_CMD="bash")
  if env "${int_env[@]}" bash "$LAUNCH" --no-attach >/dev/null 2>&1; then
    pass "integration: sable-launch --no-attach exits 0"
  else
    fail "integration: sable-launch --no-attach exits 0"
  fi
  if tmux -L "$SOCK" has-session -t "$ISESS" 2>/dev/null; then
    pass "integration: session created"
  else
    fail "integration: session created"
  fi
  ROLES_LIST="$(tmux -L "$SOCK" list-panes -s -t "$ISESS" -F '#{@sable_role}' 2>/dev/null | grep .)"
  if [ "$ROLES_LIST" = "lincoln" ]; then
    pass "integration: exactly one pane, role lincoln (mode-neutral launch)"
  else
    fail "integration: exactly one pane, role lincoln (mode-neutral launch)" "got [$ROLES_LIST]"
  fi
  if env "${int_env[@]}" bash "$LAUNCH" --no-attach >/dev/null 2>&1; then
    pass "integration: second run reuses the existing session (exit 0)"
  else
    fail "integration: second run reuses the existing session (exit 0)"
  fi
  tmux -L "$SOCK" kill-server 2>/dev/null || true
else
  echo "SKIP: tmux not available — integration cases skipped"
fi

# Cleanup
rm -rf "$STUB_BIN" "$HOME_FLAG" "$HOME_NOFLAG" "$RUN_LAUNCH_CWD"

echo
echo "=========================================="
echo "Tests: $((PASS+FAIL)) | Passed: $PASS | Failed: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then printf "Failed tests:%b\n" "$FAIL_NAMES"; exit 1; fi
exit 0
