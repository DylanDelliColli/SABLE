#!/usr/bin/env bash
# test-spawn-manager-codex-boot.sh — Codex manager epoch bootstrap integration.
#
# Codex accepts hooks.json but does not dispatch SessionStart command hooks, so
# sable-spawn-manager must run the shared anchor from inside the new manager
# pane before it can deliver a kick under a stable agent-written epoch.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SPAWN="$REPO/bin/sable-spawn-manager"
MODE="$REPO/bin/sable-mode"
ANCHOR="$REPO/hooks/multi-manager/session-role-anchor.sh"
TMP="$(mktemp -d)"
SOCK="sable-codex-boot-${BASHPID}"
SESSION="codex-boot"
PASS=0
FAIL=0

cleanup() {
  tmux -L "$SOCK" kill-server >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT

pass() { printf 'PASS: %s\n' "$1"; PASS=$((PASS+1)); }
fail() { printf 'FAIL: %s%s\n' "$1" "${2:+ — $2}"; FAIL=$((FAIL+1)); }

mkdir -p "$TMP/home/.codex" "$TMP/home/.claude/sable/roles"
printf '%s\n' '{"hooks":{"SessionStart":[{"matcher":"","hooks":[{"type":"command","command":"true"}]}]}}' \
  > "$TMP/home/.codex/hooks.json"
printf '%s\n' '# Optimus test role' > "$TMP/home/.claude/sable/roles/optimus.md"
printf '%s\n' 'agents:' '  optimus:' '    type: epic_manager' > "$TMP/agents.yaml"

export HOME="$TMP/home"
export CODEX_HOME="$TMP/home/.codex"
export SABLE_AGENTS_YAML="$TMP/agents.yaml"
export SABLE_MODE_STATE="$TMP/mode-state.json"
if ! "$MODE" set execution --break-glass \
    --reason "synthetic authority for Codex boot integration" \
    --providers optimus=codex,tarzan=claude,chuck=claude,worker=codex \
    >/dev/null 2>&1; then
  fail "fixture enters execution mode"
  printf 'Summary: %d passed, %d failed\n' "$PASS" "$FAIL"
  exit 1
fi

tmux -L "$SOCK" new-session -d -s "$SESSION" -x 180 -y 50 -n lincoln \
  -e SABLE_WORKER_PANE= bash
lincoln="$(tmux -L "$SOCK" list-panes -t "$SESSION" -F '#{pane_id}')"
tmux -L "$SOCK" set-option -p -t "$lincoln" @sable_role lincoln

FAKE_TUI="bash --noprofile --norc -c 'while true; do printf \"› \"; IFS= read -r line || break; printf \"%s\\n\" \"\$line\"; done'"
run_spawn() {
  env SABLE_TMUX_SOCKET="$SOCK" \
      SABLE_TMUX_SESSION="$SESSION" \
      SABLE_TMUX_PANE_CMD="$FAKE_TUI" \
      SABLE_CODEX_BOOT_ANCHOR="$1" \
      SABLE_DISPATCH_READY_TIMEOUT=3 \
      SABLE_DISPATCH_SUBMIT_TRIES=1 \
      SABLE_DISPATCH_POLL_INTERVAL=0.1 \
      SABLE_MANAGER_REKICK_TRIES=1 \
      python3 "$SPAWN" optimus
}

positive="$(run_spawn "$ANCHOR" 2>&1)"; positive_rc=$?
pane="$(tmux -L "$SOCK" list-panes -a -F '#{pane_id}|#{@sable_role}' | awk -F '|' '$2 == "optimus" {print $1; exit}')"
epoch=""
agent=""
kicked=""
if [ -n "$pane" ]; then
  epoch="$(tmux -L "$SOCK" show-options -p -v -t "$pane" @sable_boot_epoch 2>/dev/null || true)"
  agent="$(tmux -L "$SOCK" show-options -p -v -t "$pane" @sable_boot_agent 2>/dev/null || true)"
  kicked="$(tmux -L "$SOCK" show-options -p -v -t "$pane" @sable_kicked_epoch 2>/dev/null || true)"
fi

if [ "$positive_rc" -eq 0 ] && [ -n "$epoch" ] && [ "$agent" = optimus ] \
    && [ "$kicked" = "$epoch" ]; then
  pass "Codex manager boot wrapper publishes an agent/epoch pair before kick"
else
  fail "Codex manager boot wrapper publishes an agent/epoch pair before kick" \
    "rc=$positive_rc pane=$pane agent=$agent epoch=$epoch kicked=$kicked output=$positive"
fi

if [ -n "$pane" ]; then
  tmux -L "$SOCK" kill-window -t "$pane" >/dev/null 2>&1 || true
fi

negative="$(run_spawn disabled 2>&1)"; negative_rc=$?
if [ "$negative_rc" -ne 0 ] \
    && printf '%s' "$negative" | grep -q 'missing-agent-boot-epoch'; then
  pass "disabled Codex boot mechanism fails closed with missing-agent-boot-epoch"
else
  fail "disabled Codex boot mechanism fails closed with missing-agent-boot-epoch" \
    "rc=$negative_rc output=$negative"
fi

remaining="$(tmux -L "$SOCK" list-panes -a -F '#{@sable_role}' | grep -c '^optimus$' || true)"
if [ "$remaining" -eq 0 ]; then
  pass "failed Codex bootstrap rolls its invocation-owned pane back"
else
  fail "failed Codex bootstrap rolls its invocation-owned pane back" \
    "remaining optimus panes=$remaining"
fi

printf 'Summary: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
